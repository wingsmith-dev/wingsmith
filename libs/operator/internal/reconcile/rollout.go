package reconcile

import (
	"context"
	"fmt"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// graphDeploymentLabel is how the Dynamo operator marks the pods it created for one graph.
//
// Its label, not ours. The labels this operator renders sit on the graph deployment, and Dynamo
// does not copy them onto the pod templates it derives — so the only reliable link from a running
// pod back to the deployment that asked for it is the one Dynamo itself writes, keyed by the
// graph's name. That name is `render.WorkloadName(dep)`, which is the deployment's own name.
const graphDeploymentLabel = "nvidia.com/dynamo-graph-deployment-name"

// rollout is what is actually running for one deployment, as opposed to what was applied.
//
// The distinction is the entire point. `reconcileDeployment` concludes "applied" and that is a
// statement about a write to the API server; it says nothing about whether a single worker ever
// started. Until this existed the plane had no other source, so a deployment that was serving
// happily read `placed · 0/0 ready` forever — which is indistinguishable, to somebody looking at
// the console, from one that never came up at all.
type rollout struct {
	desired     int
	ready       int
	updated     int
	unavailable int
	replicas    []wire.ReplicaObserved
	allocations []wire.AllocationObserved

	// trouble is why the shortfall exists, when anything in the cluster said. Nil is not
	// health: it is a shortfall nothing has explained yet, which for the first minute of a
	// rollout is the normal reading. See diagnose.go.
	trouble *trouble

	// frontendUnready says this deployment's own frontend is not serving, on a cluster that
	// renders one per model. Kept apart from the counts rather than folded into them, because a
	// frontend is not a replica of the model and counting it as one is the defect
	// `observeRollout` selects on the component to avoid. It exists so that a complete set of
	// ready workers cannot report `running` while nothing can reach them.
	frontendUnready bool

	// roleShort says at least one role of a disaggregated deployment has fewer ready workers
	// than it asked for, even where the totals happen to add up.
	//
	// Separate from the counts because the totals cannot express it. Two ready decode workers
	// and no prefill in a 1P:2D group is two of three — a shortfall that reads as degraded and
	// would be expected to resolve itself, where in fact nothing is serving at all and the
	// arithmetic is what hides it. Always false for an aggregated deployment, which has one
	// role and for which `ready < desired` already says everything.
	roleShort bool

	// roles is the per-role split of the counts above, in the order the renderer declares the
	// roles, and nil for an aggregated deployment.
	//
	// Nil rather than a single entry, because the counts are already the whole answer where
	// there is one role and a degenerate `roles` would make every deployment look disaggregated
	// to everything downstream. It carries what `roleShort` decided on rather than only the
	// verdict, so the plane can say which role is short instead of only that one is.
	roles []wire.RoleObserved
}

// countReady counts the pods that are ready and not on their way out.
//
// Terminating pods are skipped for the same reason `rolloutFrom` skips them: counting one as
// unavailable makes a completed rollout look degraded for as long as the old pod takes to go.
func countReady(pods []corev1.Pod) int {
	n := 0
	for i := range pods {
		if pods[i].DeletionTimestamp.IsZero() && podReady(&pods[i]) {
			n++
		}
	}
	return n
}

// phase is the observed phase these counts amount to.
//
// `placed` rather than `pending` when nothing is ready yet: the work has been handed to the
// cluster and accepted, and pending is reserved for the orderings that stop it being handed over
// at all. `degraded` is any shortfall while something is still serving, which is the state a
// rolling update passes through and the one a crash-looping replica sits in.
func (r rollout) phase() string {
	switch {
	case r.desired == 0:
		return phasePlaced
	// A diagnosis that will not resolve on its own is a failure, whatever the counts say.
	// Without this a workload the API server had permanently refused read `placed` for as long
	// as it existed, which is the same word a healthy deployment uses on its way up.
	case r.trouble != nil && r.trouble.fatal:
		return phaseFailed
	case r.ready == 0:
		return phasePlaced
	// A model whose every worker is ready and whose frontend is not is not running, whatever the
	// replica counts say — under one frontend per model the frontend is the only way in, so
	// `running` here would have the plane admit it and the gateway prefer it.
	case r.ready < r.desired, r.frontendUnready, r.roleShort:
		return phaseDegraded
	default:
		return phaseRunning
	}
}

// reason is the closed-set name for what these counts amount to (MESSAGES.md).
//
// Derived here rather than carried over from the apply, because "applied" answers a different
// question and answering it again once replicas are up is how the plane came to describe a serving
// deployment as one still waiting for replicas.
func (r rollout) reason() string {
	// A diagnosis outranks the counts. `Applied` and `Progressing` are both true of a
	// deployment whose pods will never start, and neither is the thing anybody needs told.
	if r.trouble != nil {
		return r.trouble.reason
	}
	switch r.phase() {
	case phaseRunning:
		return "Serving"
	case phaseDegraded:
		return "Progressing"
	default:
		return "Applied"
	}
}

// message keeps the counts and adds the diagnosis to them, rather than replacing one with the
// other. The counts are what a reader looks for first and the diagnosis is what they came for.
func (r rollout) message() string {
	counts := fmt.Sprintf("%d of %d replicas ready", r.ready, r.desired)
	if r.trouble == nil {
		return counts
	}
	return counts + "; " + r.trouble.message
}

// observeRollout reads what is running for one deployment.
//
// Pods rather than the intermediate Deployment object, because a pod is the thing that is either
// serving or not and is also the unit the plane models as a replica — reading the Deployment
// would give counts with no way to say which replica is on which node, and the allocation view
// needs the latter.
//
// Selected on the component as well as the graph, and that second label is what keeps the counts
// honest once a graph holds more than the engine. Dynamo stamps the graph label on every pod it
// derives, so a graph carrying a frontend beside its worker would have the frontend counted as a
// replica of the model — and `desired` is the worker count, so two frontend pods becoming ready
// in seconds would report a deployment as `running` while its weights are still loading. The
// plane would then mark it ready and the gateway would prefer it, which is live traffic at a
// deployment that has no worker.
//
// A disaggregated deployment is counted per role and the roles are summed, which is the change
// this comment used to anticipate rather than describe. It used to say that a hand-written
// topology's "prefill pods are not counted here", defending the shortfall as safe because "a
// shortfall reads as degraded and still serves, where a surplus reads as ready and does not".
// That defence held while nobody had declared the topology. It stops holding the moment the plane
// renders the prefill component itself: an uncounted role is a role that can be entirely absent
// while the deployment reports every replica ready.
//
// Per role and not in aggregate, and the difference is the whole point. A 1P:2D group with two
// ready decode workers and no prefill has two of three workers ready — a shortfall, which reads
// as degraded and would resolve on its own if it were one. It is not one: nothing is serving,
// because a decode worker with no prefill peer has nothing to decode from. So `rolesShort` is
// what decides, and it asks each role separately.
func (r *SyncedResourceReconciler) observeRollout(
	ctx context.Context,
	dep wire.DeploymentDesired,
) (rollout, error) {
	name := render.WorkloadName(dep)
	roles, err := render.WorkerRoles(dep)
	if err != nil {
		return rollout{}, fmt.Errorf("roles for %s: %w", dep.DeploymentID, err)
	}

	var all []corev1.Pod
	var split []wire.RoleObserved
	total := 0
	short := false
	for _, role := range roles {
		var pods corev1.PodList
		err := r.Client.List(ctx, &pods,
			client.InNamespace(r.Render.Namespace),
			client.MatchingLabels{
				graphDeploymentLabel: name,
				componentNameLabel:   role.Component,
			},
		)
		if err != nil {
			return rollout{}, fmt.Errorf("list %s pods for %s: %w", role.Component, dep.DeploymentID, err)
		}
		ready := countReady(pods.Items)
		total += role.Replicas
		if ready < role.Replicas {
			short = true
		}
		split = append(split, wire.RoleObserved{Role: role.Role, Desired: role.Replicas, Ready: ready})
		all = append(all, pods.Items...)
	}

	out := r.rolloutFrom(all, dep, total, dep.Serving.Engine, dep.Serving.RuntimeVersion())
	out.roleShort = short && render.Disaggregated(dep)
	// The split is reported only where it says something the totals do not. An aggregated
	// deployment has one role, so its `roles` would restate `ready of desired` under a second
	// name and invite a reader to believe a ratio had been declared for it.
	if render.Disaggregated(dep) {
		out.roles = split
	}
	out.trouble = r.diagnose(ctx, name, all, out)
	serving, found := r.frontendState(ctx, name, dep)
	out.frontendUnready = !serving
	if out.trouble == nil {
		out.trouble = found
	}
	return out, nil
}

// frontendTrouble is why this deployment's own frontend is not serving.
//
// It exists because nothing else in this operator would ever say so. `diagnose` is called only on
// a shortfall of ready workers, and the frontend is deliberately not one of those — so a cluster
// rendering one frontend per model can have every worker ready, every count complete, and a
// frontend that never started, and the plane is told `running · Serving` for a deployment the
// agent cannot route to. That is the same silence the renderer's overlay refusal exists to stop,
// one layer further on: there the frontend is deleted before it is applied, here it is applied and
// never comes up, and both leave a healthy-looking deployment reachable by nothing.
//
// The case it was built for is the one the whole change was built for. A frontend image is not
// always NVIDIA's — the Kimi K3 fix is a patched build in a private repository — and a private
// pull with no credential in the workload namespace is an ImagePullBackOff for ever, on a pod
// nothing here was reading.
//
// Silent unless a pod says something, and that restraint is deliberate in both directions. No
// frontend pods at all is Dynamo not having derived them yet, and a pod merely starting is a pod
// starting; reporting either would make the first pass of every rollout a failure. What is
// reported is a pod the cluster has already given up on, through the same reading of waiting
// reasons and scheduling conditions the worker path uses, so the vocabulary stays the closed set
// in MESSAGES.md and no new reason has to reach the plane for this to be legible.
func (r *SyncedResourceReconciler) frontendState(
	ctx context.Context,
	name string,
	dep wire.DeploymentDesired,
) (bool, *trouble) {
	if !r.Render.FrontendEnabled {
		return true, nil
	}

	var pods corev1.PodList
	err := r.Client.List(ctx, &pods,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{
			graphDeploymentLabel: name,
			componentNameLabel:   render.FrontendComponent,
		},
	)
	if err != nil {
		// Not a reconcile failure, for the same reason `rejectedBy` treats its own list that
		// way: a cluster this cannot read is one this operator still reconciles, less well.
		// Serving is the answer on a read that failed, because a transient API error must not
		// take a serving deployment out of the routing table.
		r.Log.Debug("could not read the frontend pods", "graph", name, "error", err)
		return true, nil
	}

	anyReady := false
	for i := range pods.Items {
		pod := &pods.Items[i]
		if !podReady(pod) {
			continue
		}
		anyReady = true
		serves, known := r.frontendServes(ctx, pod, strings.TrimSpace(dep.ServedModelName))
		// An unanswerable question is not a negative answer. A frontend this loop cannot
		// reach is judged on its pod alone, exactly as it was before anything asked.
		if !known || serves {
			return true, nil
		}
	}

	// A ready frontend that does not list this model is the failure this check exists for, and
	// it is the one shape nothing else in the cluster reports. Every count is complete, the pod
	// passes its probe -- the probe answers for an HTTP server that is listening, not for a
	// model that registered -- and the deployment reads `running` while the frontend answers
	// 404 to every request for it. It happens whenever the model card cannot be resolved by the
	// frontend that reads it: a chat template the tokenizer config never carried, or a card
	// naming a path on a runtime that does not serve its own metadata over HTTP.
	if anyReady {
		return false, &trouble{
			reason:  reasonModelNotRegistered,
			message: "the frontend is ready but does not serve " + strings.TrimSpace(dep.ServedModelName),
		}
	}

	// Not serving, whether or not anything can say why, and those are two answers rather than
	// one. `degraded` needs only the first: no ready frontend means nothing can reach this
	// model, and a rollout that reported `running` through the window before its frontend comes
	// up would have the plane admit it and the gateway prefer it — the 503 the agent then
	// answers is the same silence one layer on. The second answer is rarer and stays as
	// restrained as the worker path: no pods at all is Dynamo not having derived them yet, and a
	// pod merely starting is a pod starting, so neither becomes a reason the plane is told.
	found := troubleFromPods(pods.Items)
	if found != nil {
		found.message = "no frontend is serving this deployment: " + found.message
	}
	return false, found
}

// observeAdoptedRollout reads what is running for a deployment this operator did not render.
//
// Same fact as `observeRollout` and a different way of finding it, because the link this operator
// relies on for its own objects does not exist here. `observeRollout` selects on the graph
// deployment name Dynamo stamps on the pods it derives, which only holds for a workload this
// operator rendered. An adopted object is whatever was already running — a plain Deployment or
// StatefulSet, put there by something that never heard of this operator — so the only selector
// guaranteed to find its pods is the one the object carries itself.
//
// Reading it is what makes an adopted deployment visible at all: nothing else on the adopted path
// reports upward, so without this the plane holds a deployment it knows is enrolled, with no
// replicas under it and no counts against it (`0/1` in the console for a workload that is serving).
//
// Engine and engine version are left empty rather than guessed. They come off the serving block,
// and an adopted workload has none — the plane took it as it found it.
func (r *SyncedResourceReconciler) observeAdoptedRollout(
	ctx context.Context,
	adopt *v1alpha1.AdoptRef,
	dep wire.DeploymentDesired,
) (rollout, error) {
	// A bare adopted pod is its own rollout of one. It has no selector to list by, so it is
	// fetched directly rather than being forced through the label path.
	if strings.ToLower(adopt.Kind) == "pod" {
		var pod corev1.Pod
		key := types.NamespacedName{Namespace: adopt.Namespace, Name: adopt.Name}
		if err := r.Client.Get(ctx, key, &pod); err != nil {
			return rollout{}, fmt.Errorf("get adopted pod %s/%s: %w", adopt.Namespace, adopt.Name, err)
		}
		return r.rolloutFrom([]corev1.Pod{pod}, dep, 1, "", ""), nil
	}

	selector, desired, err := r.adoptedSelector(ctx, adopt)
	if err != nil {
		return rollout{}, err
	}

	var pods corev1.PodList
	if err := r.Client.List(ctx, &pods,
		client.InNamespace(adopt.Namespace),
		client.MatchingLabelsSelector{Selector: selector},
	); err != nil {
		return rollout{}, fmt.Errorf("list pods for adopted %s/%s: %w", adopt.Namespace, adopt.Name, err)
	}
	out := r.rolloutFrom(pods.Items, dep, desired, "", "")
	// Pod evidence only. The other half of a diagnosis reads the objects this operator
	// rendered, and an adopted deployment was rendered by somebody else.
	if out.ready < out.desired {
		out.trouble = troubleFromPods(pods.Items)
	}
	return out, nil
}

// adoptedSelector is the live object's own pod selector, and how many replicas it asks for.
//
// Pods are handled by the caller and never reach here: one has no selector and is its own rollout.
//
// The replica count comes from the live object rather than from `dep`, because on the adopted path
// the plane's copy is a record of what was found and the object is the thing that decides. A nil
// `Spec.Replicas` is one, which is the Kubernetes default and not a missing value.
func (r *SyncedResourceReconciler) adoptedSelector(
	ctx context.Context,
	adopt *v1alpha1.AdoptRef,
) (labels.Selector, int, error) {
	key := types.NamespacedName{Namespace: adopt.Namespace, Name: adopt.Name}
	switch strings.ToLower(adopt.Kind) {
	case "deployment":
		var d appsv1.Deployment
		if err := r.Client.Get(ctx, key, &d); err != nil {
			return nil, 0, err
		}
		selector, err := metav1.LabelSelectorAsSelector(d.Spec.Selector)
		return selector, replicaCount(d.Spec.Replicas), err
	case "statefulset":
		var s appsv1.StatefulSet
		if err := r.Client.Get(ctx, key, &s); err != nil {
			return nil, 0, err
		}
		selector, err := metav1.LabelSelectorAsSelector(s.Spec.Selector)
		return selector, replicaCount(s.Spec.Replicas), err
	default:
		return nil, 0, fmt.Errorf("cannot read a rollout for an adopted %s", adopt.Kind)
	}
}

// allocate records what one replica is holding, if the binding is known.
//
// A pod with no binding contributes no allocation rather than an empty one. The two are not the
// same claim: an empty allocation says this replica holds no accelerators, which for an inference
// worker is always false and would hand every card it is actually using back to the placement
// engine as free.
//
// `exclusive` is true because the cards come from the device plugin under `nvidia.com/gpu`, which
// hands out whole devices — a MIG partition is advertised as a device in its own right, so a
// shared physical card is still an exclusive hold on the partition that was granted.
func (out *rollout) allocate(
	r *SyncedResourceReconciler,
	dep wire.DeploymentDesired,
	pod *corev1.Pod,
	replicaID string,
	now string,
) {
	if r.Devices == nil {
		return
	}
	uuids := r.Devices.DevicesForPod(pod.Namespace, pod.Name)
	if len(uuids) == 0 {
		return
	}
	ids := make([]string, 0, len(uuids))
	for _, uuid := range uuids {
		ids = append(ids, observe.DeviceID(r.ClusterID, uuid))
	}

	granted := now
	if at := startedAt(pod); at != nil {
		granted = *at
	}
	out.allocations = append(out.allocations, wire.AllocationObserved{
		AllocationID: observe.AllocationID(r.ClusterID, pod.Namespace, pod.Name),
		ClusterID:    r.ClusterID,
		ReplicaID:    replicaID,
		DeploymentID: dep.DeploymentID,
		ProjectID:    dep.ProjectID,
		NodeID:       nodeIDFor(r.ClusterID, pod.Spec.NodeName),
		DeviceIDs:    ids,
		Exclusive:    true,
		GrantedAt:    granted,
		ObservedAt:   now,
	})
}

func replicaCount(replicas *int32) int {
	if replicas == nil {
		return 1
	}
	return int(*replicas)
}

// rolloutFrom turns a set of pods into the counts and replica frames the plane is sent.
//
// Shared by the rendered and the adopted paths, which disagree only on how the pods were found and
// on whether there is a serving block behind them. Everything after that — what counts as ready, what a
// terminating pod does to the totals, how a pod maps onto the replica enum — is one rule, and
// keeping it one function is what stops the adopted path from drifting into a second answer.
func (r *SyncedResourceReconciler) rolloutFrom(
	pods []corev1.Pod,
	dep wire.DeploymentDesired,
	desired int,
	engine string,
	runtimeVersion string,
) rollout {
	out := rollout{desired: desired}
	now := time.Now().UTC().Format(time.RFC3339Nano)

	for i := range pods {
		pod := &pods[i]
		if !pod.DeletionTimestamp.IsZero() {
			// Terminating. Counting it as unavailable would make a completed rollout look
			// degraded for as long as the old pod takes to go away.
			continue
		}

		ready := podReady(pod)
		if ready {
			out.ready++
		} else {
			out.unavailable++
		}
		if pod.Labels["pod-template-hash"] != "" {
			out.updated++
		}

		replicaID := observe.ReplicaID(r.ClusterID, pod.Namespace, pod.Name)
		out.allocate(r, dep, pod, replicaID, now)

		out.replicas = append(out.replicas, wire.ReplicaObserved{
			ReplicaID:    replicaID,
			Name:         pod.Name,
			DeploymentID: dep.DeploymentID,
			ClusterID:    r.ClusterID,
			NodeID:       nodeIDFor(r.ClusterID, pod.Spec.NodeName),
			// Empty, never nil. The field is required on the wire and a nil slice marshals to
			// `null`, which the plane refuses — so every replica frame this loop sent was
			// rejected as invalid and the replica rollup stayed at zero for deployments that
			// were plainly serving. Empty is also the truthful value: which devices a replica
			// holds is an Allocation, reported by the observe loop, and a pod spec carries a
			// count rather than the ids.
			DeviceIDs: []string{},
			// Read off the label the renderer stamped rather than inferred from the pod's
			// name. The name is Dynamo's convention and could change under us; the label is
			// something this operator wrote and is the one string both sides agree on.
			// Empty for an aggregated deployment, which has one worker and nothing to say.
			Role:                 pod.Labels[render.RoleLabel],
			Phase:                replicaPhase(pod, ready),
			Ready:                ready,
			Engine:               engine,
			EngineRuntimeVersion: runtimeVersion,
			// The same value under the name a plane that predates the rename requires. Sent as
			// well as, never instead of; drop it once no such plane is running.
			LegacyEngineVersion: runtimeVersion,
			ServingDigest:       dep.Serving.Digest,
			RestartCount:        restarts(pod),
			ObservedGeneration:  dep.Generation,
			StartedAt:           startedAt(pod),
			ObservedAt:          now,
		})
	}
	return out
}

// nodeIDFor is the observed id of the node a replica landed on, or empty while it has none.
//
// An unscheduled pod has no node name, and minting an id from the empty string would produce a
// stable, meaningless id that the plane would then try to resolve against its inventory.
func nodeIDFor(clusterID, nodeName string) string {
	if nodeName == "" {
		return ""
	}
	return observe.NodeID(clusterID, nodeName)
}

func podReady(pod *corev1.Pod) bool {
	if pod.Status.Phase != corev1.PodRunning {
		return false
	}
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}

// replicaPhase maps a pod onto the phase enum the contract defines for a replica.
//
// A pod that is Running but not Ready is `starting`, not `ready`. For an inference worker that
// distinction is most of the wait: the container is up for as long as it takes to load tens of
// gigabytes of weights into VRAM, and reporting it as ready would tell the plane a replica is
// serving when nothing can reach it yet.
//
// The enum is the contract's — `pending | starting | ready | draining | terminated | failed`
// (MESSAGES.md) — and not Kubernetes'. They overlap enough to be mistaken for each other and
// disagree on the two that matter: a pod's `Running` is the contract's `ready`, and a pod's
// `Succeeded` is the contract's `terminated`. Emitting the Kubernetes spelling had the plane
// reject every replica frame from this loop as an invalid enum value.
func replicaPhase(pod *corev1.Pod, ready bool) string {
	switch pod.Status.Phase {
	case corev1.PodPending:
		return "pending"
	case corev1.PodSucceeded:
		return "terminated"
	case corev1.PodFailed:
		return "failed"
	case corev1.PodRunning:
		if ready {
			return "ready"
		}
		return "starting"
	default:
		return "pending"
	}
}

func restarts(pod *corev1.Pod) int {
	total := 0
	for _, status := range pod.Status.ContainerStatuses {
		total += int(status.RestartCount)
	}
	return total
}

func startedAt(pod *corev1.Pod) *string {
	if pod.Status.StartTime == nil {
		return nil
	}
	at := pod.Status.StartTime.UTC().Format(time.RFC3339Nano)
	return &at
}
