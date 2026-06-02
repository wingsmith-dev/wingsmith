package reconcile

import (
	"context"
	"fmt"
	"time"

	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// demandScope is what this stream claims to be a complete statement about: every replica and every
// allocation this cluster holds. The supply half of the same protocol claims the hardware.
const demandScope = "demand"

// StreamingReporter is a Reporter that can also open a correlated stream.
//
// Separate from Reporter rather than folded into it because only one caller needs it, and widening
// the interface every reporting path takes would make a test double implement a resync to report a
// single conclusion. The sync client satisfies both; anything that does not simply never resyncs.
type StreamingReporter interface {
	Reporter
	SendCorrelated(t syncpkg.MessageType, subject string, payload any, replyTo string) (string, error)
}

// ResyncDemand streams the whole of this cluster's demand-side observed state as one correlated
// snapshot, so the plane can delete what the cluster no longer has.
//
// The gap this closes, and why it has to be a snapshot rather than more frames. Replicas and
// allocations were only ever reported per deployment, as changes: a replica set on the
// deployment's own frame, and an allocation released by diffing against a map held in this
// process's memory. Both are transitions, and a transition is exactly what nobody sees. Tear a
// deployment down and its frame stops arriving, so the last one stays true for ever — a paused
// workload kept `ready` replicas and `held` cards that nothing would ever collect. Restart this
// operator and the map is empty, so a pod that went away while it was down is never reported as
// departed at all.
//
// A snapshot has no such failure. It says what is here now, and absence from it is evidence
// because the stream declares its own length and completeness — the plane prunes only when the two
// agree. That is the same contract the supply side has had all along, and the same guard.
//
// Completeness is the one thing this must never overstate. Any deployment that could not be read
// makes the whole snapshot incomplete, because the frames it would have contributed are exactly
// the ones whose absence would otherwise be read as "gone". An incomplete snapshot costs a stale
// row until the next resync; a wrongly complete one deletes a running replica's allocation and
// hands its accelerators to somebody else.
func (r *SyncedResourceReconciler) ResyncDemand(ctx context.Context) error {
	reporter, ok := r.reporter().(StreamingReporter)
	if !ok {
		return nil
	}

	replicas, allocations, complete := r.demandSnapshot(ctx)

	requestID, err := reporter.SendCorrelated(syncpkg.TypeResyncBegin, "", wire.ResyncBegin{Scope: demandScope}, "")
	if err != nil {
		return fmt.Errorf("open demand resync: %w", err)
	}

	count := 0
	for _, replica := range replicas {
		if _, err := reporter.SendCorrelated(TypeReplicaObserved, replica.ReplicaID, replica, requestID); err != nil {
			return fmt.Errorf("stream replica %s: %w", replica.ReplicaID, err)
		}
		count++
	}
	for _, allocation := range allocations {
		_, err := reporter.SendCorrelated(TypeAllocationObserved, allocation.AllocationID, allocation, requestID)
		if err != nil {
			return fmt.Errorf("stream allocation %s: %w", allocation.AllocationID, err)
		}
		count++
	}

	end := wire.StreamEnd{Count: count, Complete: complete}
	if _, err := reporter.SendCorrelated(syncpkg.TypeStreamEnd, "", end, requestID); err != nil {
		return fmt.Errorf("close demand resync: %w", err)
	}
	r.Log.Info("streamed observed demand", "replicas", len(replicas),
		"allocations", len(allocations), "complete", complete)
	return nil
}

// RunDemandResync streams the snapshot on an interval, for as long as the operator is running.
//
// On a timer rather than only on a fresh session, because a session is the wrong trigger and the
// only one this had at first. The hub resumes a stream whenever it can — `full_resync: false` is
// the normal reconnect — so hanging the repair off a fresh session means it runs almost never, and
// never on any schedule. Drift that nothing is looking for is drift that stays.
//
// The same interval the observer restates the supply side on, for the same reason: this is the
// mechanism that converges the two pictures, and how stale the plane may be is one property of the
// cluster rather than two.
//
// A failed snapshot is logged and waited out rather than returned. It costs a stale row until the
// next tick, which is the cheap half of the trade this whole file is built around — and killing the
// operator over a report it could not send would stop it reconciling anything.
func (r *SyncedResourceReconciler) RunDemandResync(ctx context.Context, every time.Duration) error {
	if every <= 0 {
		return nil
	}
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := r.ResyncDemand(ctx); err != nil {
				r.Log.Warn("periodic demand resync failed", "error", err)
			}
		}
	}
}

// demandSnapshot reads every deployment this cluster holds and returns what is running under them.
//
// The declared copies are the enumeration rather than the pods, because a pod is only evidence
// about a deployment the plane knows: reading pods directly would have to guess which of them the
// plane models, and the ones it guessed wrong about would be streamed as replicas of nothing.
//
// One deployment failing does not abandon the others. Their frames are still worth sending — the
// upserts they carry are useful on their own — and it is only the permission to delete that is
// withdrawn, which is what the false return is.
func (r *SyncedResourceReconciler) demandSnapshot(
	ctx context.Context,
) ([]wire.ReplicaObserved, []wire.AllocationObserved, bool) {
	var list v1alpha1.SyncedResourceList
	if err := r.Client.List(ctx, &list, client.InNamespace(r.Namespace)); err != nil {
		r.Log.Error("could not list declared deployments for a demand resync", "error", err)
		return nil, nil, false
	}

	var (
		replicas    []wire.ReplicaObserved
		allocations []wire.AllocationObserved
		complete    = true
	)
	for i := range list.Items {
		res := &list.Items[i]
		if res.Spec.Kind != "deployment" || !res.DeletionTimestamp.IsZero() {
			continue
		}
		out, ok := r.observeDeclared(ctx, res)
		if !ok {
			complete = false
			continue
		}
		replicas = append(replicas, out.replicas...)
		allocations = append(allocations, out.allocations...)
	}
	return replicas, allocations, complete
}

// observeDeclared reads what one declared deployment is running, by whichever route it was put
// there — rendered by this operator, or adopted from something that was already running.
//
// A deployment waiting on an artifact that has not arrived is not a failure: it has no pods yet,
// and contributing nothing to the snapshot is the correct and complete answer for it.
func (r *SyncedResourceReconciler) observeDeclared(
	ctx context.Context,
	res *v1alpha1.SyncedResource,
) (rollout, bool) {
	var dep wire.DeploymentDesired
	if err := decodePayload(res, &dep); err != nil {
		r.Log.Warn("could not decode a declared deployment for a demand resync",
			"resource", res.Name, "error", err)
		return rollout{}, false
	}
	if dep.DeploymentID == "" {
		return rollout{}, true
	}

	if adopt := res.Spec.Adopt; adopt != nil {
		out, err := r.observeAdoptedRollout(ctx, adopt, dep)
		if err != nil {
			r.Log.Warn("could not read an adopted deployment for a demand resync",
				"deployment", dep.DeploymentID, "error", err)
			return rollout{}, false
		}
		return out, true
	}

	out, err := r.observeRollout(ctx, dep)
	if err != nil {
		r.Log.Warn("could not read what a deployment is running for a demand resync",
			"deployment", dep.DeploymentID, "error", err)
		return rollout{}, false
	}
	return out, true
}
