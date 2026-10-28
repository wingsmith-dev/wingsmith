//go:build e2e

// The reconciler against a real API server, with a real garbage collector running.
//
// This file exists because of a bug that every other test in this package agreed was not
// happening. The rendered workload was applied with an owner reference to a declared object in
// another namespace; the fake client stores whatever it is given and has no garbage collector,
// so the unit test asserted the reference was present and passed, while a real cluster read the
// same reference as an owner that had been deleted and removed the workload seconds later. The
// operator reported "Applied" and the object was already gone.
//
// The lesson is narrow and worth stating: a fake API server is a fine model of the API and no
// model at all of the controllers behind it. Anything whose correctness depends on what
// Kubernetes *does* to an object after it is written (garbage collection, finalizers, defaulting)
// has to be tested somewhere the controller-manager is running.
//
// Run with `just e2e`, or: go test -tags e2e ./internal/reconcile/ -run E2E -v
// It needs the SyncedResource CRD from deploy/crd/ and deploy/crd/testing_dynamographdeployments.yaml.
// It creates and deletes its own namespaces and briefly patches one node, so it runs only against a
// kind- context, or the context WINGSMITH_E2E_CONTEXT names; anything else is refused.
package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/wingsmith-dev/wingsmith/libs/operator/api/v1alpha1"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// settleFor is how long a rendered object is watched before it is called survived.
//
// The garbage collector acts within a couple of seconds (the reproduction of this bug on a
// local cluster deleted a cross-namespace dependent in under five), so this is generous by
// design. A test that asserted the object existed immediately after the apply would have
// passed against the broken code too, which is the whole failure being guarded against.
const settleFor = 20 * time.Second

// reported is one frame the reconciler sent upward.
type reported struct {
	Type    string
	Subject string
	Phase   string
	Reason  string
	Message string
}

// capture is a Reporter that keeps what it was given, so a test can assert on what the plane
// would have been told, not on what the reconciler wrote locally.
type capture struct {
	frames []reported
}

func (c *capture) Send(t syncpkg.MessageType, subject string, payload any) error {
	f := reported{Type: string(t), Subject: subject}
	switch p := payload.(type) {
	case wire.DeploymentObserved:
		f.Phase, f.Reason, f.Message = p.Phase, p.Reason, p.Message
	case wire.CacheObserved:
		f.Phase = p.State
	}
	c.frames = append(c.frames, f)
	return nil
}

func (c *capture) reasons() []string {
	out := make([]string, 0, len(c.frames))
	for _, f := range c.frames {
		out = append(out, f.Reason)
	}
	return out
}

func (c *capture) sawReason(reason string) bool {
	for _, f := range c.frames {
		if f.Reason == reason {
			return true
		}
	}
	return false
}

// e2eContext is the context this suite may write to: the one WINGSMITH_E2E_CONTEXT names, or else
// the current context when it is a kind cluster. The same rule as dev/guards.sh e2e-context, so a
// direct `go test` is refused where `just e2e` would be.
func e2eContext(current, explicit string) (string, error) {
	if explicit != "" {
		return explicit, nil
	}
	if current == "" {
		return "", fmt.Errorf("no current kube context; `just cluster-up` creates a local one")
	}
	if !strings.HasPrefix(current, "kind-") {
		return "", fmt.Errorf("refusing to run e2e against %q: it is not a kind- context. "+
			"Set WINGSMITH_E2E_CONTEXT=%s to run it there anyway", current, current)
	}
	return current, nil
}

// e2eConfig loads the kubeconfig the usual way and pins the client to the context e2eContext
// allows, so the current context changing mid-run cannot move the suite onto another cluster.
func e2eConfig(t *testing.T) *rest.Config {
	t.Helper()
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	raw, err := rules.Load()
	if err != nil {
		t.Skipf("no kubeconfig available: %v", err)
	}
	explicit := os.Getenv("WINGSMITH_E2E_CONTEXT")
	if raw.CurrentContext == "" && explicit == "" {
		t.Skip("no kube context; `just cluster-up` creates a local one")
	}
	name, err := e2eContext(raw.CurrentContext, explicit)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := raw.Contexts[name]; !ok {
		t.Fatalf("no kube context named %q", name)
	}
	config, err := clientcmd.NewNonInteractiveClientConfig(*raw, name, &clientcmd.ConfigOverrides{}, rules).ClientConfig()
	if err != nil {
		t.Fatalf("kube context %q: %v", name, err)
	}
	return config
}

func TestE2EContextIsKindOrNamed(t *testing.T) {
	for _, tc := range []struct {
		current, explicit, want string
		refused                 bool
	}{
		{current: "kind-wingsmith", want: "kind-wingsmith"},
		{current: "prod-eu", refused: true},
		{current: "", refused: true},
		{current: "prod-eu", explicit: "staging", want: "staging"},
	} {
		got, err := e2eContext(tc.current, tc.explicit)
		if tc.refused != (err != nil) || got != tc.want {
			t.Errorf("e2eContext(%q, %q) = %q, %v", tc.current, tc.explicit, got, err)
		}
	}
}

// live is one test's cluster: two namespaces, a client, and a reconciler pointed at them.
type live struct {
	client     client.Client
	reconciler *SyncedResourceReconciler
	reports    *capture
	declaredNS string
	workloadNS string
}

func newLive(t *testing.T) *live {
	t.Helper()

	config := e2eConfig(t)

	scheme := clientgoscheme.Scheme
	if err := v1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	c, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("client: %v", err)
	}

	// A namespace pair per test, so the tests are independent and so that a rendered object
	// landing in the wrong namespace fails loudly instead of colliding with a sibling's.
	stamp := time.Now().UnixNano()
	l := &live{
		client:     c,
		reports:    &capture{},
		declaredNS: fmt.Sprintf("wings-e2e-sys-%d", stamp),
		workloadNS: fmt.Sprintf("wings-e2e-wl-%d", stamp),
	}
	for _, name := range []string{l.declaredNS, l.workloadNS} {
		ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
		if err := c.Create(context.Background(), ns); err != nil {
			t.Fatalf("create namespace %s: %v", name, err)
		}
		t.Cleanup(func() { l.dropNamespace(name) })
	}

	options := testRender
	options.Namespace = l.workloadNS
	l.reconciler = &SyncedResourceReconciler{
		Client:    c,
		Scheme:    scheme,
		Log:       slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn})),
		Namespace: l.declaredNS,
		Render:    options,
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
		Report:    l.reports,
	}
	return l
}

// dropNamespace deletes a test namespace, releasing anything holding it open first.
//
// The finalizer is why this cannot be a plain delete. A namespace will not finish terminating
// while a resource in it still carries one, and the only thing that removes `wingsmith.dev/rendered`
// is a running reconciler, which a finished test no longer has. Deleting the namespace and
// walking away leaves it Terminating forever, and the next run adds another.
//
// The same shape is true of a real install, and is worth knowing: uninstalling the operator from a
// cluster without first letting it drain its own finalizers leaves every declared deployment
// holding its namespace open.
func (l *live) dropNamespace(name string) {
	ctx := context.Background()

	var declared v1alpha1.SyncedResourceList
	if err := l.client.List(ctx, &declared, client.InNamespace(name)); err == nil {
		for i := range declared.Items {
			item := &declared.Items[i]
			if len(item.Finalizers) == 0 {
				continue
			}
			patch := client.MergeFrom(item.DeepCopy())
			item.Finalizers = nil
			_ = l.client.Patch(ctx, item, patch)
		}
	}

	_ = l.client.Delete(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}})
}

// declare writes one declared object into the cluster, in this test's namespace.
func (l *live) declare(t *testing.T, kind, id string, payload any) *v1alpha1.SyncedResource {
	t.Helper()
	res := declared(t, kind, id, payload)
	res.Namespace = l.declaredNS
	if err := l.client.Create(context.Background(), res); err != nil {
		t.Fatalf("declare %s: %v", kind, err)
	}
	return res
}

// reconcile runs one pass over a declared object and returns it as the cluster now holds it.
func (l *live) reconcile(t *testing.T, name string) (*v1alpha1.SyncedResource, ctrl.Result) {
	t.Helper()
	req := ctrl.Request{NamespacedName: types.NamespacedName{Namespace: l.declaredNS, Name: name}}
	result, err := l.reconciler.Reconcile(context.Background(), req)
	if err != nil {
		t.Fatalf("reconcile %s: %v", name, err)
	}
	var out v1alpha1.SyncedResource
	if err := l.client.Get(context.Background(), req.NamespacedName, &out); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, result
		}
		t.Fatalf("read back %s: %v", name, err)
	}
	return &out, result
}

func (l *live) graph() *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetGroupVersionKind(render.GVK)
	return obj
}

func (l *live) getGraph(ctx context.Context) (*unstructured.Unstructured, error) {
	obj := l.graph()
	key := types.NamespacedName{Namespace: l.workloadNS, Name: render.Name(testDeploymentID)}
	return obj, l.client.Get(ctx, key, obj)
}

// prefetchDone writes a completed prefetch Job, which is how residency is reported.
func (l *live) prefetchDone(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      render.PrefetchJobName(testArtifactDesired()),
			Namespace: l.workloadNS,
			// Found by this label, not by name, which is how the reconciler looks for it.
			Labels: map[string]string{render.ArtifactLabel: testArtifactID},
		},
		Spec: batchv1.JobSpec{
			Template: corev1.PodTemplateSpec{
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyNever,
					Containers:    []corev1.Container{{Name: "fetch", Image: "busybox"}},
				},
			},
		},
	}
	if err := l.client.Create(ctx, job); err != nil {
		t.Fatalf("create prefetch job: %v", err)
	}
	job.Status.Succeeded = 1
	if err := l.client.Status().Update(ctx, job); err != nil {
		t.Fatalf("mark prefetch complete: %v", err)
	}
}

// withCapacity makes at least one node in the cluster look like it can run the replica.
//
// The local cluster has no accelerators, so its node is labelled and its allocatable is patched.
// Restored on cleanup so the cluster is left as it was found.
func (l *live) withCapacity(t *testing.T, devices int64) {
	t.Helper()
	ctx := context.Background()

	var nodes corev1.NodeList
	if err := l.client.List(ctx, &nodes); err != nil {
		t.Fatalf("list nodes: %v", err)
	}
	if len(nodes.Items) == 0 {
		t.Skip("no nodes in this cluster to advertise capacity on")
	}
	node := &nodes.Items[0]
	original := node.DeepCopy()

	patch := client.MergeFrom(node.DeepCopy())
	if node.Labels == nil {
		node.Labels = map[string]string{}
	}
	node.Labels[testRender.PoolLabelKey] = testRender.GPUPoolValue
	if err := l.client.Patch(ctx, node, patch); err != nil {
		t.Fatalf("label node: %v", err)
	}

	statusPatch := client.MergeFrom(node.DeepCopy())
	node.Status.Allocatable[acceleratorResource] = *resource.NewQuantity(devices, resource.DecimalSI)
	if err := l.client.Status().Patch(ctx, node, statusPatch); err != nil {
		t.Fatalf("advertise accelerators: %v", err)
	}

	t.Cleanup(func() {
		var current corev1.Node
		if err := l.client.Get(ctx, types.NamespacedName{Name: original.Name}, &current); err != nil {
			return
		}
		p := client.MergeFrom(current.DeepCopy())
		delete(current.Labels, testRender.PoolLabelKey)
		_ = l.client.Patch(ctx, &current, p)

		sp := client.MergeFrom(current.DeepCopy())
		delete(current.Status.Allocatable, acceleratorResource)
		_ = l.client.Status().Patch(ctx, &current, sp)
	})
}

// ready declares the artifact, the resident weights and the capacity a deployment needs, so that
// a test about anything else starts from the case that should work.
func (l *live) ready(t *testing.T) *v1alpha1.SyncedResource {
	t.Helper()
	dep := l.declare(t, "deployment", testDeploymentID, testDeploymentDesired())
	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.prefetchDone(t)
	l.withCapacity(t, 8)
	return dep
}

// ---------------------------------------------------------------- the regression

// The bug, stated as a test. Applying is not the thing that was broken; surviving is.
func TestE2EARenderedDeploymentSurvivesTheGarbageCollector(t *testing.T) {
	l := newLive(t)
	dep := l.ready(t)

	out, result := l.reconcile(t, dep.Name)
	if got := readyCondition(t, out).Reason; got != "Applied" {
		t.Fatalf("reason = %q (%s), want Applied", got, readyCondition(t, out).Message)
	}
	// Nothing is serving in this cluster (there is no Dynamo operator to turn the graph into
	// pods), so a rollout that has not filled in is watched at the rollout's pace, not the
	// drift floor's.
	if result.RequeueAfter != waitForDependency {
		t.Errorf("RequeueAfter = %s, want %s while nothing is ready", result.RequeueAfter, waitForDependency)
	}

	ctx := context.Background()
	if _, err := l.getGraph(ctx); err != nil {
		t.Fatalf("nothing was applied: %v", err)
	}

	deadline := time.Now().Add(settleFor)
	for time.Now().Before(deadline) {
		time.Sleep(2 * time.Second)
		if _, err := l.getGraph(ctx); err != nil {
			t.Fatalf("the workload was deleted %s after it was applied: %v",
				settleFor-time.Until(deadline), err)
		}
	}

	obj, err := l.getGraph(ctx)
	if err != nil {
		t.Fatalf("workload gone: %v", err)
	}
	if owners := obj.GetOwnerReferences(); len(owners) != 0 {
		t.Errorf("owner references = %v, want none across a namespace boundary", owners)
	}
	if got := obj.GetLabels()[render.OwnerNamespaceLabel]; got != l.declaredNS {
		t.Errorf("owner-namespace label = %q, want %q", got, l.declaredNS)
	}
}

// ---------------------------------------------------------------- what it reports

// The second half of "did the operator do its job": it has to say so, in order.
func TestE2EProgressIsReportedAsItHappens(t *testing.T) {
	l := newLive(t)
	dep := l.declare(t, "deployment", testDeploymentID, testDeploymentDesired())

	l.reconcile(t, dep.Name)
	if !l.reports.sawReason("ArtifactNotDeclared") {
		t.Fatalf("reasons = %v, want ArtifactNotDeclared first", l.reports.reasons())
	}

	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.reconcile(t, dep.Name)
	if !l.reports.sawReason("WeightsNotResident") {
		t.Fatalf("reasons = %v, want WeightsNotResident", l.reports.reasons())
	}

	l.prefetchDone(t)
	l.withCapacity(t, 8)
	l.reconcile(t, dep.Name)
	if !l.reports.sawReason("Applied") {
		t.Fatalf("reasons = %v, want Applied", l.reports.reasons())
	}

	// Every frame is about this deployment, and each says something the one before it did not.
	for i, f := range l.reports.frames {
		if f.Type != TypeDeploymentObserved || f.Subject != testDeploymentID {
			t.Errorf("frame %d = %+v, want a deployment.observed about %s", i, f, testDeploymentID)
		}
		if i > 0 && f == l.reports.frames[i-1] {
			t.Errorf("frame %d is identical to the one before it: %+v", i, f)
		}
	}
}

// A reconcile that concludes the same thing twice reports once. The stream is not free.
func TestE2EAnUnchangedConclusionIsNotReportedTwice(t *testing.T) {
	l := newLive(t)
	dep := l.ready(t)

	l.reconcile(t, dep.Name)
	first := len(l.reports.frames)
	l.reconcile(t, dep.Name)

	if len(l.reports.frames) != first {
		t.Errorf("frames = %d after a second identical pass, want %d: %v",
			len(l.reports.frames), first, l.reports.reasons())
	}
}

// ---------------------------------------------------------------- the awkward cases

// The admin asks for a deployment on a cluster with no accelerators at all. The most common
// cause is not "no GPUs" but "the device plugin is not installed", which looks identical.
func TestE2ENoDevicesAtAllIsStatedRatherThanLeftPending(t *testing.T) {
	l := newLive(t)
	dep := l.declare(t, "deployment", testDeploymentID, testDeploymentDesired())
	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.prefetchDone(t)

	out, result := l.reconcile(t, dep.Name)

	condition := readyCondition(t, out)
	if condition.Reason != "NoSchedulableCapacity" {
		t.Fatalf("reason = %q, want NoSchedulableCapacity", condition.Reason)
	}
	if !strings.Contains(condition.Message, "device plugin") {
		t.Errorf("message = %q, want it to raise the device plugin", condition.Message)
	}
	if result.RequeueAfter != waitForDependency {
		t.Errorf("RequeueAfter = %s, want %s: capacity can arrive", result.RequeueAfter, waitForDependency)
	}
	if _, err := l.getGraph(context.Background()); !apierrors.IsNotFound(err) {
		t.Error("a workload was applied against a cluster that cannot run it")
	}
}

// Devices exist, but not enough of them on any one node. A tensor-parallel replica cannot be
// split across machines, so cluster-wide capacity is the wrong number to compare against.
func TestE2ECapacitySpreadAcrossNodesDoesNotSatisfyOneReplica(t *testing.T) {
	l := newLive(t)
	dep := l.declare(t, "deployment", testDeploymentID, testDeploymentDesired())
	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.prefetchDone(t)
	l.withCapacity(t, 1) // the deployment asks for 2 per replica

	out, _ := l.reconcile(t, dep.Name)

	condition := readyCondition(t, out)
	if condition.Reason != "NoSchedulableCapacity" {
		t.Fatalf("reason = %q, want NoSchedulableCapacity", condition.Reason)
	}
	if !strings.Contains(condition.Message, "has 1 free") {
		t.Errorf("message = %q, want it to name what the largest node actually has", condition.Message)
	}
}

// The cluster is there and refusing work: every node cordoned, which is what a drain looks
// like. The accelerators are still allocatable and still reported.
func TestE2EACordonedClusterIsNotCapacity(t *testing.T) {
	l := newLive(t)
	dep := l.declare(t, "deployment", testDeploymentID, testDeploymentDesired())
	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.prefetchDone(t)
	l.withCapacity(t, 8)
	l.cordonAll(t)

	out, _ := l.reconcile(t, dep.Name)

	if got := readyCondition(t, out).Reason; got != "NoSchedulableCapacity" {
		t.Errorf("reason = %q, want NoSchedulableCapacity while draining", got)
	}
}

// A payload that cannot be decoded is not fixed by being retried, and must not spin.
func TestE2EAnUndecodablePayloadFailsWithoutRetrying(t *testing.T) {
	l := newLive(t)
	dep := l.declare(t, "deployment", testDeploymentID, map[string]any{"min_replicas": "two"})

	out, result := l.reconcile(t, dep.Name)

	if out.Status.Phase != "Failed" {
		t.Errorf("phase = %q, want Failed", out.Status.Phase)
	}
	if result != (ctrl.Result{}) {
		t.Error("a payload that does not decode was queued for another attempt")
	}
	if !l.reports.sawReason("PayloadUndecodable") {
		t.Errorf("reasons = %v, want the plane told why", l.reports.reasons())
	}
}

// Authority is the gate. An observed deployment is one the plane has promised not to write.
func TestE2EAnObservedDeploymentIsNeverApplied(t *testing.T) {
	l := newLive(t)
	res := declared(t, "deployment", testDeploymentID, testDeploymentDesired())
	res.Namespace = l.declaredNS
	res.Spec.Authority = ""
	if err := l.client.Create(context.Background(), res); err != nil {
		t.Fatalf("declare: %v", err)
	}
	l.declare(t, "artifact", testArtifactID, testArtifactDesired())
	l.prefetchDone(t)
	l.withCapacity(t, 8)

	l.reconcile(t, res.Name)

	if _, err := l.getGraph(context.Background()); !apierrors.IsNotFound(err) {
		t.Error("an observed deployment was written to the cluster")
	}
}

// ---------------------------------------------------------------- teardown

// Deleting the declared copy has to remove the workload. This never once worked: it was the
// garbage collector's job through the reference that could not cross a namespace.
func TestE2EDeletingTheDeclaredCopyRemovesTheWorkload(t *testing.T) {
	l := newLive(t)
	dep := l.ready(t)
	ctx := context.Background()

	out, _ := l.reconcile(t, dep.Name)
	if _, err := l.getGraph(ctx); err != nil {
		t.Fatalf("nothing to tear down: %v", err)
	}

	if err := l.client.Delete(ctx, out); err != nil {
		t.Fatalf("delete declared copy: %v", err)
	}

	// The finalizer holds it: it is still readable, and marked for deletion.
	var held v1alpha1.SyncedResource
	key := types.NamespacedName{Namespace: l.declaredNS, Name: dep.Name}
	if err := l.client.Get(ctx, key, &held); err != nil {
		t.Fatalf("the finalizer did not hold the object open: %v", err)
	}
	if held.DeletionTimestamp.IsZero() {
		t.Error("no deletion timestamp on a deleted object")
	}

	// The teardown pass: this is the code path the owner reference used to stand in for.
	l.reconcile(t, dep.Name)

	if _, err := l.getGraph(ctx); !apierrors.IsNotFound(err) {
		t.Errorf("the workload outlived its declared copy: %v", err)
	}
	if err := l.client.Get(ctx, key, &held); !apierrors.IsNotFound(err) {
		t.Errorf("the declared copy is still held: %v (%v)", err, held.Finalizers)
	}
}

// ---------------------------------------------------------------- drift

// A workload deleted out of band comes back. Nothing rewrites desired state to make it happen.
func TestE2EAWorkloadDeletedOutOfBandIsRestored(t *testing.T) {
	l := newLive(t)
	dep := l.ready(t)
	ctx := context.Background()

	l.reconcile(t, dep.Name)
	obj, err := l.getGraph(ctx)
	if err != nil {
		t.Fatalf("nothing was applied: %v", err)
	}
	if err := l.client.Delete(ctx, obj); err != nil {
		t.Fatalf("delete workload: %v", err)
	}
	if _, err := l.getGraph(ctx); !apierrors.IsNotFound(err) {
		t.Fatalf("workload not actually deleted: %v", err)
	}

	l.reconcile(t, dep.Name)

	if _, err := l.getGraph(ctx); err != nil {
		t.Errorf("the workload was not restored: %v", err)
	}
}

// cordonAll marks every node unschedulable, and puts them back afterwards.
func (l *live) cordonAll(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	var nodes corev1.NodeList
	if err := l.client.List(ctx, &nodes); err != nil {
		t.Fatalf("list nodes: %v", err)
	}
	for i := range nodes.Items {
		node := &nodes.Items[i]
		if node.Spec.Unschedulable {
			continue
		}
		patch := client.MergeFrom(node.DeepCopy())
		node.Spec.Unschedulable = true
		if err := l.client.Patch(ctx, node, patch); err != nil {
			t.Fatalf("cordon %s: %v", node.Name, err)
		}
		name := node.Name
		t.Cleanup(func() {
			var current corev1.Node
			if err := l.client.Get(ctx, types.NamespacedName{Name: name}, &current); err != nil {
				return
			}
			p := client.MergeFrom(current.DeepCopy())
			current.Spec.Unschedulable = false
			_ = l.client.Patch(ctx, &current, p)
		})
	}
}
