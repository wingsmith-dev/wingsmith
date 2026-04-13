package reconcile

import (
	"context"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// TypeDeploymentObserved is the frame this reconciler reports its own progress on.
const TypeDeploymentObserved = "deployment.observed"

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
	phasePending = "pending"
	phasePlaced  = "placed"
	phaseFailed  = "failed"
)

// reportKey is the tuple a repeat is judged against.
type reportKey struct {
	phase   string
	reason  string
	message string
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

	key := reportKey{phase: phase, reason: reason, message: message}
	if r.lastReported(id) == key {
		return
	}

	payload := wire.DeploymentObserved{
		DeploymentID: id,
		ClusterID:    r.ClusterID,
		Generation:   generation,
		Phase:        phase,
		Reason:       reason,
		Message:      message,
		ObservedAt:   time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := reporter.Send(TypeDeploymentObserved, id, payload); err != nil {
		r.Log.Warn("could not report deployment state", "deployment", id, "reason", reason, "error", err)
		return
	}
	r.markReported(id, key)
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
