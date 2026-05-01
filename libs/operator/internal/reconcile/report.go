package reconcile

import (
	"context"
	"sort"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// The frames this reconciler reports its own progress on.
const (
	TypeDeploymentObserved = "deployment.observed"
	TypeCacheObserved      = "cache.observed"
	TypeReplicaObserved    = "replica.observed"
	TypeAllocationObserved = "allocation.observed"
)

// Reporter is the upward half of the reconcile loop: what this operator decided, sent to the hub.
//
// The same shape the observe loop's Sender has, and deliberately not the same object. The observer
// reports what the cluster is; this reports what the operator did about it, which is a different
// fact with a different writer even though both travel the same stream.
type Reporter interface {
	Send(t syncpkg.MessageType, subject string, payload any) error
}

// Bind supplies the Reporter after construction, for the same reason the observe loop binds its
// Sender: the sync client takes this operator's other halves as its own options, so one of the two
// has to be built first. A reconciler that is never bound reports nothing and reconciles normally.
func (r *SyncedResourceReconciler) Bind(rep Reporter) {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	r.Report = rep
}

// The phases a reconcile outcome maps onto. The wire enum is wider than this — running, degraded
// and terminating describe replicas actually serving, which is the Dynamo object's status to
// report and not this loop's.
const (
	phasePending  = "pending"
	phasePlaced   = "placed"
	phaseDegraded = "degraded"
	phaseRunning  = "running"
	phaseFailed   = "failed"
)

// stageRank orders the steps a reconcile passes through on its way to applying, as opposed to the
// ones it comes to rest in.
//
// A pass that has already reported a later step does not report an earlier one again for the same
// revision. Without the ordering, `Accepted` would be re-sent on the pass after `Rendering` and the
// two would alternate for the life of the deployment (MESSAGES.md).
var stageRank = map[string]int{
	"Accepted":  1,
	"Rendering": 2,
}

// stageState is how far into `stageRank` this loop has reported for one revision of one deployment.
type stageState struct {
	revision int64
	rank     int
}

// reportKey is the tuple a repeat is judged against.
//
// Replica counts are part of it because a rollout moves them without moving the phase: four of
// four ready and two of four ready are both `degraded`, and suppressing the second as a repeat
// would hide the only thing that changed.
type reportKey struct {
	phase    string
	reason   string
	message  string
	ready    int
	desired  int
	replicas string
	// errorCode is part of the key because a diagnosis can appear without moving anything
	// else: a deployment holding at zero of one gets its first `ClusterRejected` with the same
	// phase, counts and replica set it had a pass earlier, and suppressing that frame would
	// hold back the only thing worth sending.
	errorCode string
}

// report sends one deployment.observed, unless it would say exactly what the last one said.
//
// A waiting deployment is re-examined every fifteen seconds and almost always concludes the same
// thing, so sending each pass would spend the stream on a fact that has not moved. The supply loop
// applies the same rule to nodes for the same reason (PROTOCOL §6): the stream is full-object and
// at-least-once, so a repeat is not wrong, only wasteful.
//
// Only deployments are reported. A recipe or an artifact has no observed counterpart — the cluster
// holds them, it does not do anything with them — and a cache's residency is reported by the cache
// path, from what is on disk rather than from what a reconcile concluded.
//
// A failure to send is swallowed on purpose. The frame is a progress report, and losing one must
// never turn a reconcile that otherwise succeeded into a retry: the next pass restates it, and the
// next resync restates it whether or not anything changed.
func (r *SyncedResourceReconciler) report(res *v1alpha1.SyncedResource, phase, reason, message string) {
	reporter := r.reporter()
	if reporter == nil || res.Spec.Kind != "deployment" {
		return
	}

	var dep wire.DeploymentDesired
	generation := int64(0)
	id := deploymentID(res)
	if err := decodePayload(res, &dep); err == nil {
		generation = dep.Generation
		if dep.DeploymentID != "" {
			id = dep.DeploymentID
		}
	}
	if id == "" {
		return
	}

	// ReplicaIDs is left nil deliberately. This frame is a conclusion about a write and never
	// reads the pods, so it is not entitled to say which replicas exist; an empty set here
	// would tell the plane to delete every replica of a deployment that is merely waiting.
	r.send(reporter, id, wire.DeploymentObserved{
		DeploymentID: id,
		ClusterID:    r.ClusterID,
		Generation:   generation,
		Phase:        phase,
		Reason:       reason,
		Message:      message,
		ObservedAt:   time.Now().UTC().Format(time.RFC3339Nano),
	})
}

// reportRollout tells the plane what is actually running, as opposed to what was applied.
//
// Separate from `report` because it carries a different fact from a different source. `report`
// says what this loop concluded about a write; this says what came of it, read back off the pods.
// They share the frame and the suppression rule, and nothing else.
//
// The reason comes from the counts rather than from the reconcile that applied them. `Applied` is a
// statement about a write and stays true forever once it has happened, so keeping it here made it
// the reason on every frame a deployment ever sent after its first — which is why a serving
// deployment read "Applied to the cluster; waiting for replicas" underneath a strip that already
// said it was serving. `Progressing` and `Serving` are what the pods actually say.
func (r *SyncedResourceReconciler) reportRollout(res *v1alpha1.SyncedResource, dep wire.DeploymentDesired, out rollout) {
	reporter := r.reporter()
	if reporter == nil {
		return
	}
	id := dep.DeploymentID
	if id == "" {
		return
	}

	present := make([]string, 0, len(out.replicas))
	for _, replica := range out.replicas {
		present = append(present, replica.ReplicaID)
	}
	sort.Strings(present)

	observedAt := time.Now().UTC().Format(time.RFC3339Nano)
	r.send(reporter, id, wire.DeploymentObserved{
		DeploymentID:        id,
		ClusterID:           r.ClusterID,
		Generation:          dep.Generation,
		Phase:               out.phase(),
		DesiredReplicas:     out.desired,
		ReadyReplicas:       out.ready,
		UpdatedReplicas:     out.updated,
		UnavailableReplicas: out.unavailable,
		// Empty, never nil: this loop always knows the whole set, so it always makes the
		// claim. See wire.DeploymentObserved.ReplicaIDs for why the distinction matters.
		ReplicaIDs: present,
		Reason:     out.reason(),
		Message:    out.message(),
		// The failure the shortfall is explained by, when the cluster explained it. The field
		// has been on the wire and in the plane's schema since the beginning and nothing ever
		// wrote it, so `last_error_message` was null for every deployment that ever broke.
		LastError:  out.trouble.lastError(observedAt),
		ObservedAt: observedAt,
	})

	// The replicas themselves, each under its own id. Not suppressed against a previous
	// report: a replica frame carries a restart count and a readiness that move on their own,
	// and the set is small — one frame per running pod per reconcile, not per pod per second.
	for _, replica := range out.replicas {
		if err := reporter.Send(TypeReplicaObserved, replica.ReplicaID, replica); err != nil {
			r.Log.Warn("could not report replica", "replica", replica.Name, "error", err)
			return
		}
	}

	r.reportAllocations(reporter, id, out)
}

// reportAllocations says which accelerators this deployment's replicas are holding, and hands back
// the ones they have stopped holding.
//
// The releases are the half that is easy to leave out and cannot be. The plane treats an allocation
// as held for as long as it is reported and expects release to be "the allocation ceasing to be
// reported" — but nothing on the receiving side acts on a frame that does not arrive, and demand
// state is never pruned against a resync the way supply is. So an allocation that simply stopped
// being sent stayed held forever, and a deployment that rolled ten times would have marked ten
// pods' worth of cards permanently committed. Reporting the departure explicitly, as an allocation
// holding nothing, is what actually frees them: the plane releases every device the frame no longer
// names, and a frame naming none releases all of them.
//
// The set is remembered per deployment and only in memory, which bounds what this can repair. A
// pod that goes away while this operator is down is never reported as departed, because nothing
// here knows it existed. Those rows are caught by the other half instead: the replicas they hang
// off are pruned against `deployment.observed.replica_ids`, which is rebuilt from the live pods on
// every reconcile and so survives a restart, and an allocation cascades with its replica.
func (r *SyncedResourceReconciler) reportAllocations(reporter Reporter, id string, out rollout) {
	if r.Devices == nil {
		return
	}

	held := make(map[string]wire.AllocationObserved, len(out.allocations))
	for _, allocation := range out.allocations {
		held[allocation.AllocationID] = allocation
		if err := reporter.Send(TypeAllocationObserved, allocation.AllocationID, allocation); err != nil {
			r.Log.Warn("could not report allocation",
				"deployment", allocation.DeploymentID, "allocation", allocation.AllocationID, "error", err)
			return
		}
	}

	for _, released := range r.departed(id, held) {
		if err := reporter.Send(TypeAllocationObserved, released.AllocationID, released); err != nil {
			r.Log.Warn("could not release allocation",
				"deployment", released.DeploymentID, "allocation", released.AllocationID, "error", err)
			return
		}
	}
	r.markHolding(id, held)
}

// departed is the allocations this deployment held on the last pass and does not hold now, each
// restated as the frame that granted it with its devices taken away.
//
// The whole frame is kept and replayed rather than a bare id being sent, because the receiving side
// resolves a deployment *and* a project before it will apply anything and drops the frame when
// either is missing. A release built from the id alone carries no project, so it would be discarded
// on arrival and the devices it was sent to free would stay held — the exact failure it exists to
// prevent, and a silent one.
func (r *SyncedResourceReconciler) departed(id string, held map[string]wire.AllocationObserved) []wire.AllocationObserved {
	r.reportMu.Lock()
	previous := r.holding[id]
	r.reportMu.Unlock()

	var out []wire.AllocationObserved
	now := time.Now().UTC().Format(time.RFC3339Nano)
	for allocationID, last := range previous {
		if _, still := held[allocationID]; still {
			continue
		}
		released := last
		// Empty, never nil: the field is required and the emptiness is the message.
		released.DeviceIDs = []string{}
		released.ObservedAt = now
		out = append(out, released)
	}
	return out
}

func (r *SyncedResourceReconciler) markHolding(id string, held map[string]wire.AllocationObserved) {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	if r.holding == nil {
		r.holding = make(map[string]map[string]wire.AllocationObserved)
	}
	if len(held) == 0 {
		delete(r.holding, id)
		return
	}
	r.holding[id] = held
}

// send ships one deployment.observed, unless it would say exactly what the last one said.
func (r *SyncedResourceReconciler) send(reporter Reporter, id string, payload wire.DeploymentObserved) {
	key := reportKey{
		phase:   payload.Phase,
		reason:  payload.Reason,
		message: payload.Message,
		ready:   payload.ReadyReplicas,
		desired: payload.DesiredReplicas,
		// Part of the key because the frame carries the replica set, and a rolled pod changes
		// which replicas exist without changing any of the counts. Suppressing that frame
		// would suppress the prune that goes with it.
		replicas: strings.Join(payload.ReplicaIDs, ","),
	}
	if payload.LastError != nil {
		key.errorCode = payload.LastError.Code
	}
	// A frame that makes no claim about replicas inherits the last one's, so it compares on
	// the fields it actually carries. Without this the two senders alternate — `report` with
	// no set, `reportRollout` with one — each looks like news against the other's key, and the
	// pair re-sends on every pass forever, which is the exact loop the key exists to stop.
	if payload.ReplicaIDs == nil {
		key.replicas = r.lastReported(id).replicas
	}
	if r.lastReported(id) == key {
		return
	}
	if err := reporter.Send(TypeDeploymentObserved, id, payload); err != nil {
		r.Log.Warn("could not report deployment state", "deployment", id, "reason", payload.Reason, "error", err)
		return
	}
	r.markReported(id, key)
}

// stage reports a step the reconcile passes through, at most once per revision.
//
// The plain suppression rule is not enough for these. It compares against the last frame sent, and
// a waiting deployment reports `Accepted` then a wait reason then `Accepted` again on the next
// pass — each one news against the one before it. What makes a stage a stage is that it is true
// once per instruction rather than for an interval, so it is remembered per revision instead.
func (r *SyncedResourceReconciler) stage(res *v1alpha1.SyncedResource, dep wire.DeploymentDesired, reason, message string) {
	if dep.DeploymentID == "" || !r.markStaged(dep.DeploymentID, res.Spec.Revision, reason) {
		return
	}
	r.report(res, phasePending, reason, message)
}

// markStaged records that `reason` has been reported for this revision, and answers whether it had
// not been already. A revision it has not seen resets the progress, which is what makes a bumped
// generation report its own arrival.
func (r *SyncedResourceReconciler) markStaged(id string, revision int64, reason string) bool {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	if r.staged == nil {
		r.staged = make(map[string]stageState)
	}
	rank := stageRank[reason]
	if current, ok := r.staged[id]; ok && current.revision == revision && current.rank >= rank {
		return false
	}
	r.staged[id] = stageState{revision: revision, rank: rank}
	return true
}

func (r *SyncedResourceReconciler) reporter() Reporter {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	return r.Report
}

func (r *SyncedResourceReconciler) lastReported(id string) reportKey {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	return r.reported[id]
}

func (r *SyncedResourceReconciler) markReported(id string, key reportKey) {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	if r.reported == nil {
		r.reported = make(map[string]reportKey)
	}
	r.reported[id] = key
}

// forgetReported drops a deployment's last-sent state, so the next outcome is sent whatever it is.
//
// Called when the object goes away. Without it a deployment deleted and recreated at the same id
// would be judged a repeat of the state it was in before it was deleted, and its first report
// would be silently dropped.
func (r *SyncedResourceReconciler) forgetReported(id string) {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	delete(r.reported, id)
	delete(r.staged, id)
	delete(r.holding, id)
}

// deploymentID recovers the plane's id from the object name, for the one case that cannot read it
// out of the payload: a payload that does not decode.
//
// Exact rather than a guess. resourceName swaps the prefix separator for a hyphen and lowercases,
// and a ULID contains neither an underscore nor a hyphen, so the first hyphen is always the one
// that was an underscore.
func deploymentID(res *v1alpha1.SyncedResource) string {
	return nameToID(res.Name)
}

// nameToID is resourceName inverted.
func nameToID(name string) string {
	i := strings.Index(name, "-")
	if i < 0 {
		return ""
	}
	return name[:i] + "_" + name[i+1:]
}

// ForgetAll drops every deployment's last-sent state.
//
// Called when the hub opens a fresh session. The dedupe in report assumes the frame it skipped was
// delivered, and a session the hub could not resume is one where that assumption does not hold:
// the hub prunes what it had and expects the cluster to restate it. Nothing else the operator
// sends has this problem, because the observe loop restates its whole view on Resync — the
// reconciler had no such hook, so its conclusions were the one thing a new session lost for good.
//
// The stages are deliberately not cleared with it. They are transitions rather than state, and the
// state they preceded is restated by the conclusion that follows on the next pass. Replaying
// `Accepted` for a deployment that has been serving for a week would file a step backwards into
// its timeline and describe a reconnect as a redeploy.
func (r *SyncedResourceReconciler) ForgetAll() {
	r.reportMu.Lock()
	defer r.reportMu.Unlock()
	clear(r.reported)
}

// reportingHandler restates the reconciler's conclusions when the hub could not honour a resume.
type reportingHandler struct {
	inner      syncpkg.Handler
	reconciler *SyncedResourceReconciler
}

// WithReporter joins the reconciler to the handler chain, so that a fresh session forgets what it
// last told the hub and says it again.
func WithReporter(inner syncpkg.Handler, r *SyncedResourceReconciler) syncpkg.Handler {
	if r == nil {
		return inner
	}
	return &reportingHandler{inner: inner, reconciler: r}
}

// Handle implements sync.Handler.
func (h *reportingHandler) Handle(ctx context.Context, e *syncpkg.Envelope) error {
	return h.inner.Handle(ctx, e)
}

// Resync implements sync.Handler.
func (h *reportingHandler) Resync(ctx context.Context) error {
	if err := h.inner.Resync(ctx); err != nil {
		return err
	}
	h.reconciler.ForgetAll()
	return nil
}
