package checkpoint

import (
	"context"
	"errors"
	"testing"

	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	checkpointv1alpha1 "github.com/hangar-dev/hangar/libs/operator/api/checkpoint/v1alpha1"
)

func newFakeClient(t *testing.T, objs ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := checkpointv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatalf("scheme: %v", err)
	}
	return fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
}

func loaded(t *testing.T, c client.Client) *CRDStore {
	t.Helper()
	s := NewCRDStore(c, "hangar-system")
	if _, err := s.Load(context.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	return s
}

// TestRestartDuringPartitionDoesNotRewind is the property the whole architecture exists to
// provide: the cursor is read back out of the cluster by a process that has never spoken to the
// hub, so a restart while the plane is unreachable resumes where it left off.
func TestRestartDuringPartitionDoesNotRewind(t *testing.T) {
	ctx := context.Background()
	c := newFakeClient(t)

	first := loaded(t, c)
	for seq := uint64(1); seq <= 7; seq++ {
		if err := first.Save(ctx, State{SessionID: "sess-a", AppliedSeq: seq}); err != nil {
			t.Fatalf("save %d: %v", seq, err)
		}
	}

	restarted := NewCRDStore(c, "hangar-system")
	got, err := restarted.Load(ctx)
	if err != nil {
		t.Fatalf("load after restart: %v", err)
	}
	if got.SessionID != "sess-a" || got.AppliedSeq != 7 {
		t.Fatalf("cursor rewound: got %+v, want session sess-a seq 7", got)
	}
	if got.Lossy {
		t.Fatalf("cursor unexpectedly lossy")
	}

	if err := restarted.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 6}); !errors.Is(err, ErrRewind) {
		t.Fatalf("rewind accepted: %v", err)
	}
}

// TestCrashBetweenApplyAndCheckpointFailsBackwards proves the crash-safety direction: the cursor
// records only what was applied *before* the write, so a crash in between leaves the cursor behind
// reality and the hub replays. It is never ahead, which would skip a frame silently.
func TestCrashBetweenApplyAndCheckpointFailsBackwards(t *testing.T) {
	ctx := context.Background()
	c := newFakeClient(t)

	s := loaded(t, c)
	for seq := uint64(1); seq <= 3; seq++ {
		if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: seq}); err != nil {
			t.Fatalf("save %d: %v", seq, err)
		}
	}

	applied := uint64(4)

	after := NewCRDStore(c, "hangar-system")
	got, err := after.Load(ctx)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AppliedSeq > applied {
		t.Fatalf("cursor ahead of applied state: cursor %d, applied %d", got.AppliedSeq, applied)
	}
	if got.AppliedSeq != 3 {
		t.Fatalf("cursor = %d, want 3 (the last recorded, not the last applied)", got.AppliedSeq)
	}

	if err := after.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 4}); err != nil {
		t.Fatalf("replayed frame rejected: %v", err)
	}
	if err := after.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 4}); err != nil {
		t.Fatalf("duplicate replay rejected: %v", err)
	}
}

func TestGapRejection(t *testing.T) {
	ctx := context.Background()

	stores := map[string]Store{
		"memory": &Memory{},
		"crd":    loaded(t, newFakeClient(t)),
	}
	for name, s := range stores {
		t.Run(name, func(t *testing.T) {
			if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 1}); err != nil {
				t.Fatalf("save 1: %v", err)
			}
			if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 2}); err != nil {
				t.Fatalf("save 2: %v", err)
			}
			if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 5}); !errors.Is(err, ErrGap) {
				t.Fatalf("gap accepted: %v", err)
			}
			got, err := s.Load(ctx)
			if err != nil {
				t.Fatalf("load: %v", err)
			}
			if got.AppliedSeq != 2 {
				t.Fatalf("cursor moved past gap: %d", got.AppliedSeq)
			}
			if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 3}); err != nil {
				t.Fatalf("contiguous save after refused gap: %v", err)
			}
			if err := s.Save(ctx, State{AppliedSeq: 4}); !errors.Is(err, ErrNoSession) {
				t.Fatalf("sessionless save accepted: %v", err)
			}
		})
	}
}

// TestNewSessionCannotClaimHistory covers the session-change branch of the no-gap rule: a fresh
// session numbers from 1, so a cursor arriving above 1 is asserting frames that were never sent.
func TestNewSessionCannotClaimHistory(t *testing.T) {
	ctx := context.Background()
	s := loaded(t, newFakeClient(t))
	if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 1}); err != nil {
		t.Fatalf("save: %v", err)
	}
	if err := s.Save(ctx, State{SessionID: "sess-b", AppliedSeq: 9}); !errors.Is(err, ErrGap) {
		t.Fatalf("new session claiming history accepted: %v", err)
	}
	if err := s.Save(ctx, State{SessionID: "sess-b", AppliedSeq: 0}); err != nil {
		t.Fatalf("new session reset rejected: %v", err)
	}
}

// TestLossySurvivesRestartAndSuppressesResume proves a lossy marker cannot be laundered away by a
// crash: it is durable, and while set the adapter refuses to offer the cursor for resume.
func TestLossySurvivesRestartAndSuppressesResume(t *testing.T) {
	ctx := context.Background()
	c := newFakeClient(t)

	s := loaded(t, c)
	if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 1}); err != nil {
		t.Fatalf("save: %v", err)
	}
	if err := s.SetLossy(ctx, true); err != nil {
		t.Fatalf("set lossy: %v", err)
	}
	if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 2}); err != nil {
		t.Fatalf("save after lossy: %v", err)
	}

	restarted := NewCRDStore(c, "hangar-system")
	got, err := restarted.Load(ctx)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if !got.Lossy {
		t.Fatalf("lossy marker lost across restart: %+v", got)
	}

	session, seq, err := Adapt(restarted).Load(ctx)
	if err != nil {
		t.Fatalf("adapter load: %v", err)
	}
	if session != "" || seq != 0 {
		t.Fatalf("lossy cursor offered for resume: %q/%d", session, seq)
	}

	if err := restarted.Save(ctx, State{SessionID: "sess-b", AppliedSeq: 0}); err != nil {
		t.Fatalf("fresh session save: %v", err)
	}
	after, err := restarted.Load(ctx)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if after.Lossy {
		t.Fatalf("lossy marker not cleared by a fresh session: %+v", after)
	}
}

// TestLoadFailureIsNotAZeroCursor: an unreadable cursor must surface as an error. Flattening it to
// zero would rewind the stream on every transient API-server hiccup.
func TestLoadFailureIsNotAZeroCursor(t *testing.T) {
	s := NewCRDStore(errClient{}, "hangar-system")
	if _, err := s.Load(context.Background()); err == nil {
		t.Fatal("unreadable cursor reported as zero state")
	}
	if err := s.Save(context.Background(), State{SessionID: "sess-a", AppliedSeq: 1}); err == nil {
		t.Fatal("save accepted before a successful load")
	}
}

func TestCursorIsStoredBesideTheCRDs(t *testing.T) {
	ctx := context.Background()
	c := newFakeClient(t)
	s := loaded(t, c)
	if err := s.Save(ctx, State{SessionID: "sess-a", AppliedSeq: 1}); err != nil {
		t.Fatalf("save: %v", err)
	}
	var obj checkpointv1alpha1.StreamCheckpoint
	key := types.NamespacedName{Namespace: "hangar-system", Name: DefaultName}
	if err := c.Get(ctx, key, &obj); err != nil {
		t.Fatalf("checkpoint object not found at %s: %v", key, err)
	}
	if obj.Spec.SessionID != "sess-a" || obj.Spec.AppliedSeq != 1 {
		t.Fatalf("unexpected persisted spec: %+v", obj.Spec)
	}
}

type errClient struct{ client.Client }

func (errClient) Get(context.Context, client.ObjectKey, client.Object, ...client.GetOption) error {
	return errors.New("apiserver unreachable")
}
