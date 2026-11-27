package declare

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// fakePlane answers like the hub would: a manager owns a name per project.
type fakePlane struct {
	mu       sync.Mutex
	offline  bool
	owner    map[string]string // project/name -> namespace that applied it
	applies  []*wire.ManifestApply
	deletes  []*wire.ManifestDelete
	connects uint64
}

func newFakePlane() *fakePlane { return &fakePlane{owner: map[string]string{}} }

func (p *fakePlane) Apply(_ context.Context, m *wire.ManifestApply) (*wire.ManifestResult, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.offline {
		return nil, ErrUnreachable
	}
	p.applies = append(p.applies, m)
	key := m.Project + "/" + m.Name
	g := m.Generation
	if ns, ok := p.owner[key]; ok && ns != m.Namespace {
		return &wire.ManifestResult{Result: "conflict", Generation: &g, Error: "managed by k8s:alpha/" + ns}, nil
	}
	result := "configured"
	if _, ok := p.owner[key]; !ok {
		result = "created"
	}
	p.owner[key] = m.Namespace
	clusters, _, _ := unstructured.NestedStringSlice(m.Body, "spec", "placement", "allowedClusters")
	return &wire.ManifestResult{
		Result: result, Generation: &g, UID: "wkl_1", RV: "42", Manager: "k8s:alpha/" + m.Namespace,
		AllowedClusters: clusters,
	}, nil
}

func (p *fakePlane) Delete(_ context.Context, m *wire.ManifestDelete) (*wire.ManifestResult, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.offline {
		return nil, ErrUnreachable
	}
	p.deletes = append(p.deletes, m)
	key := m.Project + "/" + m.Name
	ns, ok := p.owner[key]
	switch {
	case !ok:
		return &wire.ManifestResult{Result: "absent"}, nil
	case ns != m.Namespace:
		return &wire.ManifestResult{Result: "released"}, nil
	}
	delete(p.owner, key)
	return &wire.ManifestResult{Result: "deleted"}, nil
}

func (p *fakePlane) Connects() uint64 {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.connects
}

func workload(ns, name string, spec map[string]any) *unstructured.Unstructured {
	u := newWorkload()
	u.SetNamespace(ns)
	u.SetName(name)
	u.SetGeneration(1)
	u.SetUID(types.UID(ns + "-" + name))
	u.Object["spec"] = spec
	return u
}

func namespace(name, project string) *corev1.Namespace {
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}
	if project != "" {
		ns.Labels = map[string]string{ProjectLabel: project}
	}
	return ns
}

func setup(t *testing.T, objs ...client.Object) (*Reconciler, client.Client, *fakePlane) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	scheme.AddKnownTypeWithName(WorkloadGVK, &unstructured.Unstructured{})
	scheme.AddKnownTypeWithName(WorkloadGVK.GroupVersion().WithKind("WorkloadList"), &unstructured.UnstructuredList{})
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).WithStatusSubresource(newWorkload()).Build()
	plane := newFakePlane()
	r := &Reconciler{
		Client: c, Plane: plane, Cluster: "alpha", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Retry: time.Second, Resync: time.Minute,
	}
	return r, c, plane
}

func spec() map[string]any {
	return map[string]any{"serving": map[string]any{"artifact": "gpt-oss"}}
}

func reconcileOnce(t *testing.T, r *Reconciler, ns, name string) reconcile.Result {
	t.Helper()
	res, err := r.Reconcile(context.Background(), reconcile.Request{NamespacedName: types.NamespacedName{Namespace: ns, Name: name}})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	return res
}

func get(t *testing.T, c client.Client, ns, name string) *unstructured.Unstructured {
	t.Helper()
	u := newWorkload()
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: ns, Name: name}, u); err != nil {
		t.Fatalf("get: %v", err)
	}
	return u
}

func condition(t *testing.T, u *unstructured.Unstructured, kind string) *metav1.Condition {
	t.Helper()
	raw, _, _ := unstructured.NestedSlice(u.Object, "status", "conditions")
	var conds []metav1.Condition
	b, _ := json.Marshal(raw)
	if err := json.Unmarshal(b, &conds); err != nil {
		t.Fatal(err)
	}
	return meta.FindStatusCondition(conds, kind)
}

func TestApplyDefaultsPlacementOnTheWireAndReportsStatus(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")

	if len(plane.applies) != 1 {
		t.Fatalf("applies = %d", len(plane.applies))
	}
	sent := plane.applies[0]
	if sent.Project != "search" || sent.Namespace != "team-a" || sent.Generation != 1 {
		t.Fatalf("sent %+v", sent)
	}
	got, _, _ := unstructured.NestedStringSlice(sent.Body, "spec", "placement", "allowedClusters")
	if len(got) != 1 || got[0] != "alpha" {
		t.Fatalf("default placement on the wire = %v", got)
	}

	u := get(t, c, "team-a", "chat")
	if _, found, _ := unstructured.NestedFieldNoCopy(u.Object, "spec", "placement"); found {
		t.Fatal("the resource's spec was written")
	}
	if !controllerContains(u.GetFinalizers(), Finalizer) {
		t.Fatal("finalizer missing")
	}
	og, _, _ := unstructured.NestedInt64(u.Object, "status", "observedGeneration")
	uid, _, _ := unstructured.NestedString(u.Object, "status", "planeUID")
	rv, _, _ := unstructured.NestedString(u.Object, "status", "planeResourceVersion")
	placed, _, _ := unstructured.NestedStringSlice(u.Object, "status", "placement", "allowedClusters")
	if og != 1 || uid != "wkl_1" || rv != "42" || len(placed) != 1 {
		t.Fatalf("status = %v", u.Object["status"])
	}
	if s := condition(t, u, ConditionSync); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("Synced = %+v", s)
	}
	if s := condition(t, u, ConditionReady); s == nil || s.Status != metav1.ConditionUnknown || s.Reason != ReasonAwaiting {
		t.Fatalf("Ready = %+v", s)
	}
	if s := condition(t, u, ConditionReconciling); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("Reconciling = %+v", s)
	}
}

func TestExplicitPlacementIsSentAsDeclared(t *testing.T) {
	s := spec()
	s["placement"] = map[string]any{"allowedClusters": []any{"alpha", "beta"}}
	r, _, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", s))
	reconcileOnce(t, r, "team-a", "chat")
	got, _, _ := unstructured.NestedStringSlice(plane.applies[0].Body, "spec", "placement", "allowedClusters")
	if len(got) != 2 {
		t.Fatalf("placement = %v", got)
	}
}

func TestUnlabelledNamespaceIsNotSynced(t *testing.T) {
	r, _, plane := setup(t, namespace("other", ""), workload("other", "chat", spec()))
	reconcileOnce(t, r, "other", "chat")
	if len(plane.applies) != 0 {
		t.Fatal("applied from an unlabelled namespace")
	}
	r.DefaultProject = "search"
	reconcileOnce(t, r, "other", "chat")
	if len(plane.applies) != 1 || plane.applies[0].Project != "search" {
		t.Fatal("default project not used")
	}
}

func TestConflictIsReported(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), namespace("team-b", "search"),
		workload("team-a", "chat", spec()), workload("team-b", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")
	reconcileOnce(t, r, "team-b", "chat")
	if len(plane.applies) != 2 {
		t.Fatal("second apply not sent")
	}
	u := get(t, c, "team-b", "chat")
	s := condition(t, u, ConditionSync)
	if s == nil || s.Status != metav1.ConditionFalse || s.Reason != ReasonConflict {
		t.Fatalf("Synced = %+v", s)
	}
	if st := condition(t, u, ConditionStalled); st == nil || st.Status != metav1.ConditionTrue {
		t.Fatalf("Stalled = %+v", st)
	}
}

func TestOfflineIsUnknownAndReconnectReapplies(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	plane.offline = true
	res := reconcileOnce(t, r, "team-a", "chat")
	if res.RequeueAfter != r.Retry {
		t.Fatalf("requeue = %v", res.RequeueAfter)
	}
	u := get(t, c, "team-a", "chat")
	s := condition(t, u, ConditionSync)
	if s == nil || s.Status != metav1.ConditionUnknown || s.Reason != ReasonUnreachable {
		t.Fatalf("Synced = %+v", s)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	events := make(chan struct{ ns, name string }, 4)
	out := make(chan eventOf, 4)
	go func() { _ = r.watchReconnects(ctx, out) }()
	time.Sleep(100 * time.Millisecond)
	plane.mu.Lock()
	plane.offline = false
	plane.connects++
	plane.mu.Unlock()
	select {
	case e := <-out:
		events <- struct{ ns, name string }{e.Object.GetNamespace(), e.Object.GetName()}
	case <-time.After(5 * time.Second):
		t.Fatal("no relist after reconnect")
	}
	e := <-events
	reconcileOnce(t, r, e.ns, e.name)
	reconcileOnce(t, r, e.ns, e.name)
	if len(plane.applies) != 2 {
		t.Fatalf("applies = %d", len(plane.applies))
	}
	if s := condition(t, get(t, c, "team-a", "chat"), ConditionSync); s.Status != metav1.ConditionTrue {
		t.Fatalf("Synced after reconnect = %+v", s)
	}
}

func TestFinalizerDeletesThePlaneObjectFirst(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")
	if err := c.Delete(context.Background(), get(t, c, "team-a", "chat")); err != nil {
		t.Fatal(err)
	}

	plane.offline = true
	reconcileOnce(t, r, "team-a", "chat")
	u := get(t, c, "team-a", "chat")
	if s := condition(t, u, ConditionSync); s.Status != metav1.ConditionUnknown {
		t.Fatalf("Synced while deleting offline = %+v", s)
	}

	plane.offline = false
	reconcileOnce(t, r, "team-a", "chat")
	if len(plane.deletes) != 1 || plane.deletes[0].Namespace != "team-a" {
		t.Fatalf("deletes = %+v", plane.deletes)
	}
	if _, ok := plane.owner["search/chat"]; ok {
		t.Fatal("plane object not deleted")
	}
	gone := newWorkload()
	err := c.Get(context.Background(), types.NamespacedName{Namespace: "team-a", Name: "chat"}, gone)
	if err == nil {
		t.Fatal("resource still present after the finalizer ran")
	}
}

func TestOrphanPolicyKeepsThePlaneObject(t *testing.T) {
	s := spec()
	s["deletionPolicy"] = DeletionOrphan
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", s))
	reconcileOnce(t, r, "team-a", "chat")
	if _, found := plane.applies[0].Body["spec"].(map[string]any)["deletionPolicy"]; found {
		t.Fatal("deletionPolicy sent to the plane")
	}
	if err := c.Delete(context.Background(), get(t, c, "team-a", "chat")); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "team-a", "chat")
	if len(plane.deletes) != 0 {
		t.Fatal("orphan policy deleted the plane object")
	}
}

func TestFinalizerReleasesAnotherManagersObject(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")
	plane.owner["search/chat"] = "team-b"
	if err := c.Delete(context.Background(), get(t, c, "team-a", "chat")); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "team-a", "chat")
	if _, ok := plane.owner["search/chat"]; !ok {
		t.Fatal("another manager's object was deleted")
	}
}

func TestChannelCorrelatesResults(t *testing.T) {
	ch := NewChannel(time.Second)
	s := &recordingSender{connected: true}
	ch.Bind(s)
	s.channel = ch
	res, err := ch.Apply(context.Background(), &wire.ManifestApply{Kind: "Workload", Name: "chat", Namespace: "team-a"})
	if err != nil || res.Result != "unchanged" {
		t.Fatalf("apply = %+v, %v", res, err)
	}
	s.connected = false
	if _, err := ch.Apply(context.Background(), &wire.ManifestApply{}); err != ErrUnreachable {
		t.Fatalf("offline apply err = %v", err)
	}
	// A result nobody waits on is dropped.
	id := "nobody"
	if err := ch.Deliver(&syncpkg.Envelope{Type: TypeResult, ReplyTo: &id, Payload: []byte(`{"result":"unchanged"}`)}); err != nil {
		t.Fatal(err)
	}
}

type eventOf = event.TypedGenericEvent[*unstructured.Unstructured]

type recordingSender struct {
	channel   *Channel
	connected bool
	n         int
}

func (s *recordingSender) SendCorrelated(_, _ string, _ any, _ string) (string, error) {
	s.n++
	id := "id-" + string(rune('a'+s.n))
	go func() {
		time.Sleep(10 * time.Millisecond)
		_ = s.channel.Deliver(&syncpkg.Envelope{Type: TypeResult, ReplyTo: &id, Payload: []byte(`{"result":"unchanged"}`)})
	}()
	return id, nil
}

func (s *recordingSender) Connected() bool { return s.connected }

func (s *recordingSender) Connects() uint64 { return 1 }

func controllerContains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}

// refusingPlane answers every delete with one result.
type refusingPlane struct {
	*fakePlane
	result string
}

func (p *refusingPlane) Delete(_ context.Context, _ *wire.ManifestDelete) (*wire.ManifestResult, error) {
	return &wire.ManifestResult{Result: p.result, Error: "plane says " + p.result}, nil
}

func TestInvalidDeleteReleases(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")
	r.Plane = &refusingPlane{fakePlane: plane, result: "invalid"}
	if err := c.Delete(context.Background(), get(t, c, "team-a", "chat")); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "team-a", "chat")
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "team-a", Name: "chat"}, newWorkload()); err == nil {
		t.Fatal("invalid delete kept the resource Terminating")
	}
}

func TestFailingDeleteBacksOffThenStallsAndOrphanUnsticks(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	reconcileOnce(t, r, "team-a", "chat")
	r.Plane = &refusingPlane{fakePlane: plane, result: "conflict"}
	if err := c.Delete(context.Background(), get(t, c, "team-a", "chat")); err != nil {
		t.Fatal(err)
	}
	var last time.Duration
	for i := 1; i <= DeleteStallAfter; i++ {
		res := reconcileOnce(t, r, "team-a", "chat")
		if res.RequeueAfter < last || res.RequeueAfter > 32*r.Retry {
			t.Fatalf("attempt %d requeue = %v after %v", i, res.RequeueAfter, last)
		}
		last = res.RequeueAfter
		st := condition(t, get(t, c, "team-a", "chat"), ConditionStalled)
		if i < DeleteStallAfter && st != nil && st.Status == metav1.ConditionTrue {
			t.Fatalf("stalled after %d attempts", i)
		}
		if i == DeleteStallAfter && (st == nil || st.Status != metav1.ConditionTrue || st.Reason != ReasonDeleteStuck) {
			t.Fatalf("Stalled = %+v", st)
		}
	}
	if last != 32*r.Retry {
		t.Fatalf("backoff cap = %v", last)
	}

	u := get(t, c, "team-a", "chat")
	_ = unstructured.SetNestedField(u.Object, DeletionOrphan, "spec", "deletionPolicy")
	if err := c.Update(context.Background(), u); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, "team-a", "chat")
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: "team-a", Name: "chat"}, newWorkload()); err == nil {
		t.Fatal("Orphan did not release the resource")
	}
}

func TestControllerOptionsDefaultsAndOverrides(t *testing.T) {
	r := &Reconciler{}
	if o := r.controllerOptions(); o.MaxConcurrentReconciles != 4 || o.RateLimiter == nil {
		t.Fatalf("defaults = %+v", o)
	}
	r = &Reconciler{QPS: 1, Burst: 1, Concurrency: 2}
	o := r.controllerOptions()
	if o.MaxConcurrentReconciles != 2 {
		t.Fatalf("concurrency = %d", o.MaxConcurrentReconciles)
	}
	// burst 1 at 1 qps: the second distinct item waits for the bucket.
	a := reconcile.Request{NamespacedName: types.NamespacedName{Name: "a"}}
	b := reconcile.Request{NamespacedName: types.NamespacedName{Name: "b"}}
	if d := o.RateLimiter.When(a); d > 50*time.Millisecond {
		t.Fatalf("first item delayed %v", d)
	}
	if d := o.RateLimiter.When(b); d < 500*time.Millisecond {
		t.Fatalf("second item delayed %v, want the bucket to throttle", d)
	}
}

type forbiddingPlane struct{ *fakePlane }

func (p *forbiddingPlane) Apply(_ context.Context, _ *wire.ManifestApply) (*wire.ManifestResult, error) {
	return &wire.ManifestResult{Result: "forbidden", Reason: ReasonNamespaceNotAllowed, Error: "namespace not allowed"}, nil
}

func TestNamespaceNotAllowedStalls(t *testing.T) {
	r, c, plane := setup(t, namespace("team-a", "search"), workload("team-a", "chat", spec()))
	r.Plane = &forbiddingPlane{plane}
	reconcileOnce(t, r, "team-a", "chat")
	u := get(t, c, "team-a", "chat")
	if s := condition(t, u, ConditionSync); s == nil || s.Status != metav1.ConditionFalse || s.Reason != ReasonNamespaceNotAllowed {
		t.Fatalf("Synced = %+v", s)
	}
	if s := condition(t, u, ConditionStalled); s == nil || s.Status != metav1.ConditionTrue {
		t.Fatalf("Stalled = %+v", s)
	}
}
