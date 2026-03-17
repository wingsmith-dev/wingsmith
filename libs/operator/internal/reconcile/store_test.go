package reconcile

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"sort"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/checkpoint"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const testNS = "hangar-system"

func existing(names ...string) []client.Object {
	objs := make([]client.Object, 0, len(names))
	for _, n := range names {
		objs = append(objs, &v1alpha1.SyncedResource{
			ObjectMeta: metav1.ObjectMeta{Namespace: testNS, Name: n},
			Spec:       v1alpha1.SyncedResourceSpec{Kind: "thing", Revision: 1},
		})
	}
	return objs
}

func newStore(t *testing.T, objs []client.Object, funcs *interceptor.Funcs) (*Store, client.Client) {
	t.Helper()
	scheme, err := Scheme()
	if err != nil {
		t.Fatalf("scheme: %v", err)
	}
	b := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...)
	if funcs != nil {
		b = b.WithInterceptorFuncs(*funcs)
	}
	c := b.Build()
	return &Store{
		Client:     c,
		Namespace:  testNS,
		Log:        slog.New(slog.NewTextHandler(io.Discard, nil)),
		Checkpoint: &checkpoint.Memory{},
	}, c
}

func frame(t *testing.T, typ string, seq uint64, payload any) *syncpkg.Envelope {
	t.Helper()
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	s := seq
	return &syncpkg.Envelope{Type: typ, Seq: &s, Payload: b}
}

func desired(t *testing.T, name string, rev int64) *syncpkg.Envelope {
	t.Helper()
	return frame(t, wireDeploymentDesired, uint64(rev),
		wire.DeploymentDesired{DeploymentID: name, ClusterID: "clu_test", Generation: rev})
}

func deleted(t *testing.T, name string, rev int64) *syncpkg.Envelope {
	t.Helper()
	return frame(t, wireDeploymentDeleted, uint64(rev),
		wire.DeploymentDeleted{DeploymentID: name, ClusterID: "clu_test", Generation: rev})
}

func endFrame(t *testing.T, e wire.StreamEnd) *syncpkg.Envelope {
	t.Helper()
	return frame(t, syncpkg.TypeStreamEnd, 999, e)
}

func localNames(t *testing.T, c client.Client) []string {
	t.Helper()
	var list v1alpha1.SyncedResourceList
	if err := c.List(context.Background(), &list, client.InNamespace(testNS)); err != nil {
		t.Fatalf("list: %v", err)
	}
	names := make([]string, 0, len(list.Items))
	for i := range list.Items {
		names = append(names, list.Items[i].Name)
	}
	sort.Strings(names)
	return names
}

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestResyncPrunesWhatTheSnapshotOmits(t *testing.T) {
	ctx := context.Background()
	s, c := newStore(t, existing("keep-a", "keep-b", "stale"), nil)

	if err := s.Resync(ctx); err != nil {
		t.Fatalf("resync: %v", err)
	}
	for i, n := range []string{"keep-a", "keep-b", "fresh"} {
		if err := s.Handle(ctx, desired(t, n, int64(i+2))); err != nil {
			t.Fatalf("handle %s: %v", n, err)
		}
	}
	if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 3})); err != nil {
		t.Fatalf("stream end: %v", err)
	}

	want := []string{"fresh", "keep-a", "keep-b"}
	if got := localNames(t, c); !equal(got, want) {
		t.Fatalf("after prune got %v, want %v", got, want)
	}
	if s.InResync() {
		t.Fatal("resync epoch still open")
	}
	st, _ := s.Checkpoint.Load(ctx)
	if st.Lossy {
		t.Fatal("checkpoint still marked lossy after a completed resync")
	}
}

// TestTruncatedSnapshotNeverPrunes is the guard that matters: a snapshot that stops early looks
// exactly like a snapshot that legitimately omits everything, so absence is only evidence of
// deletion once the stream has proven it arrived whole.
func TestTruncatedSnapshotNeverPrunes(t *testing.T) {
	ctx := context.Background()
	before := []string{"keep-a", "keep-b", "stale"}

	cases := map[string]func(*testing.T, *Store, context.Context){
		"disconnect before stream.end": func(t *testing.T, s *Store, ctx context.Context) {
			if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
				t.Fatal(err)
			}
		},
		"stream.end reports incomplete": func(t *testing.T, s *Store, ctx context.Context) {
			if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
				t.Fatal(err)
			}
			if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: false})); err != nil {
				t.Fatal(err)
			}
		},
		"declared count exceeds received": func(t *testing.T, s *Store, ctx context.Context) {
			if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
				t.Fatal(err)
			}
			if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 9})); err != nil {
				t.Fatal(err)
			}
		},
		"empty stream.end payload": func(t *testing.T, s *Store, ctx context.Context) {
			if err := s.Handle(ctx, &syncpkg.Envelope{Type: syncpkg.TypeStreamEnd}); err != nil {
				t.Fatal(err)
			}
		},
	}

	for name, run := range cases {
		t.Run(name, func(t *testing.T) {
			s, c := newStore(t, existing(before...), nil)
			if err := s.Resync(ctx); err != nil {
				t.Fatalf("resync: %v", err)
			}
			run(t, s, ctx)
			if got := localNames(t, c); !equal(got, before) {
				t.Fatalf("local state changed by a truncated snapshot: got %v, want %v", got, before)
			}
			st, _ := s.Checkpoint.Load(ctx)
			if !st.Lossy {
				t.Fatal("checkpoint offered as a resume base after an incomplete resync")
			}
		})
	}
}

// TestFailedApplyDuringResyncNeverPrunes: an apply that failed means the local view is missing
// something the snapshot contained, so absence from the local view proves nothing.
func TestFailedApplyDuringResyncNeverPrunes(t *testing.T) {
	ctx := context.Background()
	before := []string{"keep-a", "stale"}
	boom := errors.New("apiserver rejected create")
	funcs := interceptor.Funcs{
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			return boom
		},
	}
	s, c := newStore(t, existing(before...), &funcs)

	if err := s.Resync(ctx); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
		t.Fatalf("handle keep-a: %v", err)
	}
	if err := s.Handle(ctx, desired(t, "brand-new", 3)); err == nil {
		t.Fatal("failed apply reported success")
	}
	if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 2})); err != nil {
		t.Fatalf("stream end: %v", err)
	}

	if got := localNames(t, c); !equal(got, before) {
		t.Fatalf("pruned against a poisoned snapshot: got %v, want %v", got, before)
	}
}

// TestMalformedFrameDuringResyncNeverPrunes covers the undecodable-frame path, which is a
// truncation of a different kind: the frame arrived but its contents were never counted.
func TestMalformedFrameDuringResyncNeverPrunes(t *testing.T) {
	ctx := context.Background()
	before := []string{"keep-a", "stale"}
	s, c := newStore(t, existing(before...), nil)

	if err := s.Resync(ctx); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
		t.Fatal(err)
	}
	bad := &syncpkg.Envelope{Type: wireDeploymentDesired, Payload: json.RawMessage(`{"deployment_id":`)}
	if err := s.Handle(ctx, bad); err == nil {
		t.Fatal("malformed frame accepted")
	}
	if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 2})); err != nil {
		t.Fatal(err)
	}
	if got := localNames(t, c); !equal(got, before) {
		t.Fatalf("pruned after a malformed frame: got %v, want %v", got, before)
	}
}

// TestStreamEndOutsideResyncIsInert: a stream.end belonging to some other stream must not be read
// as permission to delete everything.
func TestStreamEndOutsideResyncIsInert(t *testing.T) {
	ctx := context.Background()
	before := []string{"keep-a", "keep-b"}
	s, c := newStore(t, existing(before...), nil)

	if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 0})); err != nil {
		t.Fatalf("stream end: %v", err)
	}
	if got := localNames(t, c); !equal(got, before) {
		t.Fatalf("pruned outside a resync epoch: got %v, want %v", got, before)
	}
}

// TestResyncSnapshotHonoursDeletions: an object the snapshot explicitly deletes is removed, and it
// counts toward the declared frame count even though it is absent from the resulting view.
func TestResyncSnapshotHonoursDeletions(t *testing.T) {
	ctx := context.Background()
	s, c := newStore(t, existing("keep-a", "gone"), nil)

	if err := s.Resync(ctx); err != nil {
		t.Fatalf("resync: %v", err)
	}
	if err := s.Handle(ctx, desired(t, "keep-a", 2)); err != nil {
		t.Fatal(err)
	}
	if err := s.Handle(ctx, deleted(t, "gone", 3)); err != nil {
		t.Fatal(err)
	}
	if err := s.Handle(ctx, endFrame(t, wire.StreamEnd{Complete: true, Count: 2})); err != nil {
		t.Fatal(err)
	}
	if got := localNames(t, c); !equal(got, []string{"keep-a"}) {
		t.Fatalf("got %v, want [keep-a]", got)
	}
}
