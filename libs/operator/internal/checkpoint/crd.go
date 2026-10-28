package checkpoint

import (
	"context"
	"fmt"
	stdsync "sync"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	checkpointv1alpha1 "github.com/wingsmith-dev/wingsmith/libs/operator/api/checkpoint/v1alpha1"
)

// DefaultName is the name of the singleton StreamCheckpoint object. There is exactly one stream
// per operator, so there is exactly one cursor.
const DefaultName = "stream"

// CRDStore persists the resume cursor as a StreamCheckpoint custom resource in the operator's own
// namespace.
//
// A CRD was chosen over a ConfigMap for three reasons. It lives beside the CRDs as PROTOCOL.md §4
// requires, so a single RBAC rule and a single backup covers desired state and the cursor that
// indexes it. It carries an OpenAPI schema, so a malformed or hand-edited cursor is rejected by
// the API server, not decoded into a plausible-looking wrong number. And it is a resource
// of its own, so the operator can be granted write access to its cursor without being granted
// write access to ConfigMaps: a permission that in most clusters is a lateral move.
//
// The client must be uncached. The cursor is read during startup before the manager's cache has
// synced, and a cursor read that blocks on cache warm-up would make the resume point depend on
// the very connectivity the durable cursor exists to survive.
type CRDStore struct {
	Client    client.Client
	Namespace string
	Name      string

	mu     stdsync.Mutex
	loaded bool
	last   State
}

// +kubebuilder:rbac:groups=wingsmith.dev,resources=streamcheckpoints,verbs=get;list;watch;create;update;patch

var _ Store = (*CRDStore)(nil)

// NewCRDStore builds a CRDStore for the singleton cursor in ns.
func NewCRDStore(c client.Client, ns string) *CRDStore {
	return &CRDStore{Client: c, Namespace: ns, Name: DefaultName}
}

func (s *CRDStore) key() types.NamespacedName {
	name := s.Name
	if name == "" {
		name = DefaultName
	}
	return types.NamespacedName{Namespace: s.Namespace, Name: name}
}

// Load reads the persisted cursor. A missing object is a genuine first boot and yields the zero
// State; any other error is returned, never flattened into a zero cursor.
func (s *CRDStore) Load(ctx context.Context) (State, error) {
	var obj checkpointv1alpha1.StreamCheckpoint
	err := s.Client.Get(ctx, s.key(), &obj)
	switch {
	case apierrors.IsNotFound(err):
		s.mu.Lock()
		s.loaded, s.last = true, State{}
		s.mu.Unlock()
		return State{}, nil
	case err != nil:
		return State{}, fmt.Errorf("read checkpoint %s: %w", s.key(), err)
	}
	st := fromSpec(obj.Spec)
	s.mu.Lock()
	s.loaded, s.last = true, st
	s.mu.Unlock()
	return st, nil
}

// Save validates the advance and writes it. The write is a full read-modify-write against the
// live object so that a second writer (a stray operator replica) collides on resourceVersion
// instead of silently overwriting a further-ahead cursor.
func (s *CRDStore) Save(ctx context.Context, next State) error {
	s.mu.Lock()
	if !s.loaded {
		s.mu.Unlock()
		return fmt.Errorf("checkpoint: Save before Load")
	}
	if err := validateAdvance(s.last, next); err != nil {
		s.mu.Unlock()
		return err
	}
	merged := merge(s.last, next)
	s.mu.Unlock()

	if err := s.write(ctx, merged); err != nil {
		return err
	}
	s.mu.Lock()
	s.last = merged
	s.mu.Unlock()
	return nil
}

// SetLossy records or clears the resume-invalid marker without touching the sequence cursor.
func (s *CRDStore) SetLossy(ctx context.Context, lossy bool) error {
	s.mu.Lock()
	if !s.loaded {
		s.mu.Unlock()
		return fmt.Errorf("checkpoint: SetLossy before Load")
	}
	if s.last.Lossy == lossy {
		s.mu.Unlock()
		return nil
	}
	next := s.last
	next.Lossy = lossy
	s.mu.Unlock()

	if next.SessionID == "" {
		return nil
	}
	if err := s.write(ctx, next); err != nil {
		return err
	}
	s.mu.Lock()
	s.last = next
	s.mu.Unlock()
	return nil
}

func (s *CRDStore) write(ctx context.Context, st State) error {
	key := s.key()
	var obj checkpointv1alpha1.StreamCheckpoint
	err := s.Client.Get(ctx, key, &obj)
	switch {
	case apierrors.IsNotFound(err):
		obj = checkpointv1alpha1.StreamCheckpoint{
			ObjectMeta: metav1.ObjectMeta{Namespace: key.Namespace, Name: key.Name},
			Spec:       toSpec(st),
		}
		return s.Client.Create(ctx, &obj)
	case err != nil:
		return fmt.Errorf("read checkpoint %s: %w", key, err)
	}
	obj.Spec = toSpec(st)
	return s.Client.Update(ctx, &obj)
}

func toSpec(s State) checkpointv1alpha1.StreamCheckpointSpec {
	now := metav1.Now()
	return checkpointv1alpha1.StreamCheckpointSpec{
		SessionID:  s.SessionID,
		AppliedSeq: int64(s.AppliedSeq),
		Lossy:      s.Lossy,
		UpdatedAt:  &now,
	}
}

func fromSpec(s checkpointv1alpha1.StreamCheckpointSpec) State {
	seq := s.AppliedSeq
	if seq < 0 {
		seq = 0
	}
	return State{SessionID: s.SessionID, AppliedSeq: uint64(seq), Lossy: s.Lossy}
}
