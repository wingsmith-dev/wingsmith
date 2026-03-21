package reconcile

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	stdsync "sync"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/checkpoint"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// desiredState is one declared object lifted out of its `<noun>.desired` frame: the identity the
// local copy is keyed by, and the payload verbatim. The payload is stored as it arrived rather
// than re-encoded, because the CRD is a durable copy of what the hub said, not this operator's
// reading of it.
type desiredState struct {
	Kind     string
	Name     string
	Revision int64
	Deleted  bool
	Object   json.RawMessage
	Adopt    *v1alpha1.AdoptRef
}

// resyncEpoch accumulates a full-resync snapshot as it streams in.
//
// It exists only in memory and only between Resync and the matching stream.end. That is
// deliberate: the epoch is the authority for pruning, so losing it — to a crash, a disconnect, or
// a truncated stream — can only ever result in no prune, never in a prune against a partial view.
type resyncEpoch struct {
	present  map[string]struct{}
	received int
	poisoned bool
}

// Store is the seam between the two loops: it takes envelopes off the sync stream and writes them
// into CRDs, which are the durable local copy of desired state. It never creates a Kubernetes
// workload object; that is the reconciler's job, driven off the CRs this writes.
type Store struct {
	Client    client.Client
	Namespace string
	Log       *slog.Logger

	// Checkpoint marks the local view as an invalid resume base for the duration of a resync
	// epoch. Without it, a crash part-way through a snapshot would leave a half-pruned local
	// view that the next handshake would offer up as a resumable cursor.
	Checkpoint checkpoint.Store

	mu    stdsync.Mutex
	epoch *resyncEpoch
}

var _ syncpkg.Handler = (*Store)(nil)

// Handle implements sync.Handler. Returning an error withholds the acknowledgement, so the hub
// replays the envelope on the next resume rather than the operator losing it.
func (s *Store) Handle(ctx context.Context, e *syncpkg.Envelope) error {
	switch {
	case isDesired(e.Type):
		return s.handleDesired(ctx, e)
	case e.Type == syncpkg.TypeStreamEnd:
		return s.handleStreamEnd(ctx, e)
	case e.Type == syncpkg.TypeResyncBegin:
		return s.Resync(ctx)
	default:
		s.Log.Debug("ignoring envelope", "type", e.Type, "seq", e.Seq)
		return nil
	}
}

func (s *Store) handleDesired(ctx context.Context, e *syncpkg.Envelope) error {
	d, err := decodeDesired(e)
	if err != nil {
		s.poison()
		return err
	}
	if err := s.apply(ctx, d); err != nil {
		s.poison()
		return err
	}
	s.record(d)
	return nil
}

func (s *Store) apply(ctx context.Context, d desiredState) error {
	key := types.NamespacedName{Namespace: s.Namespace, Name: d.Name}
	if d.Deleted {
		obj := &v1alpha1.SyncedResource{ObjectMeta: metav1.ObjectMeta{Namespace: key.Namespace, Name: key.Name}}
		if err := s.Client.Delete(ctx, obj); err != nil && !apierrors.IsNotFound(err) {
			return err
		}
		return nil
	}

	var existing v1alpha1.SyncedResource
	err := s.Client.Get(ctx, key, &existing)
	switch {
	case apierrors.IsNotFound(err):
		obj := &v1alpha1.SyncedResource{
			ObjectMeta: metav1.ObjectMeta{Namespace: key.Namespace, Name: key.Name},
			Spec: v1alpha1.SyncedResourceSpec{
				Kind:     d.Kind,
				Revision: d.Revision,
				Payload:  rawOrNil(d.Object),
				Adopt:    d.Adopt,
			},
		}
		return s.Client.Create(ctx, obj)
	case err != nil:
		return err
	}

	if existing.Spec.Revision > d.Revision {
		s.Log.Debug("ignoring stale desired state", "name", d.Name,
			"have", existing.Spec.Revision, "got", d.Revision)
		return nil
	}
	patch := client.MergeFrom(existing.DeepCopy())
	existing.Spec.Kind = d.Kind
	existing.Spec.Revision = d.Revision
	existing.Spec.Payload = rawOrNil(d.Object)
	existing.Spec.Adopt = d.Adopt
	return s.Client.Patch(ctx, &existing, patch)
}

// Resync implements sync.Handler. The hub could not honour the resume point, so the local copy is
// no longer known to match the plane and a full snapshot is about to stream in.
//
// This opens a resync epoch. Nothing is pruned here: pruning happens only once the snapshot has
// arrived whole, because a delete driven by an absence that was merely a truncation is exactly the
// mass-delete this guard exists to prevent.
func (s *Store) Resync(ctx context.Context) error {
	s.mu.Lock()
	s.epoch = &resyncEpoch{present: map[string]struct{}{}}
	s.mu.Unlock()
	s.Log.Info("resync epoch opened; awaiting full desired-state snapshot")
	return s.setLossy(ctx, true)
}

// handleStreamEnd closes a resync epoch and, only if the snapshot is provably whole, prunes local
// state that the snapshot did not mention.
func (s *Store) handleStreamEnd(ctx context.Context, e *syncpkg.Envelope) error {
	s.mu.Lock()
	epoch := s.epoch
	s.epoch = nil
	s.mu.Unlock()

	if epoch == nil {
		s.Log.Debug("stream.end outside a resync epoch", "seq", e.Seq)
		return nil
	}

	var end wire.StreamEnd
	if len(e.Payload) > 0 {
		if err := json.Unmarshal(e.Payload, &end); err != nil {
			return fmt.Errorf("decode stream end: %w", err)
		}
	}

	switch {
	case epoch.poisoned:
		s.Log.Warn("resync snapshot had a failed apply; leaving local state untouched")
		return nil
	case !end.Complete:
		s.Log.Warn("resync snapshot did not complete; leaving local state untouched")
		return nil
	case end.Count != epoch.received:
		s.Log.Warn("resync snapshot truncated; leaving local state untouched",
			"declared", end.Count, "received", epoch.received)
		return nil
	}

	pruned, err := s.prune(ctx, epoch.present)
	if err != nil {
		return err
	}
	s.Log.Info("resync complete", "received", epoch.received,
		"present", len(epoch.present), "pruned", pruned)
	return s.setLossy(ctx, false)
}

// prune deletes every local SyncedResource absent from the snapshot.
func (s *Store) prune(ctx context.Context, present map[string]struct{}) (int, error) {
	var list v1alpha1.SyncedResourceList
	if err := s.Client.List(ctx, &list, client.InNamespace(s.Namespace)); err != nil {
		return 0, fmt.Errorf("list local desired state: %w", err)
	}
	n := 0
	for i := range list.Items {
		item := &list.Items[i]
		if _, ok := present[item.Name]; ok {
			continue
		}
		if err := s.Client.Delete(ctx, item); err != nil && !apierrors.IsNotFound(err) {
			return n, fmt.Errorf("prune %s: %w", item.Name, err)
		}
		n++
	}
	return n, nil
}

func (s *Store) record(d desiredState) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.epoch == nil {
		return
	}
	s.epoch.received++
	if !d.Deleted {
		s.epoch.present[d.Name] = struct{}{}
	}
}

// poison marks the open epoch unusable for pruning. An apply that failed means the local view is
// missing something the snapshot contained, so absence from the local view is no longer evidence
// of anything.
func (s *Store) poison() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.epoch != nil {
		s.epoch.poisoned = true
	}
}

// InResync reports whether a resync epoch is open.
func (s *Store) InResync() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.epoch != nil
}

func (s *Store) setLossy(ctx context.Context, lossy bool) error {
	if s.Checkpoint == nil {
		return nil
	}
	return s.Checkpoint.SetLossy(ctx, lossy)
}

func rawOrNil(b json.RawMessage) *apiextensionsv1.JSON {
	if len(b) == 0 {
		return nil
	}
	return &apiextensionsv1.JSON{Raw: b}
}
