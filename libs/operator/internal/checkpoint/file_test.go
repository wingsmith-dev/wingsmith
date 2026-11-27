package checkpoint

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

func TestFileStoreFirstBootAndRoundTrip(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "cursor.json")
	s := NewFileStore(path)
	st, err := s.Load(ctx)
	if err != nil || !st.Zero() {
		t.Fatalf("first boot: %+v %v", st, err)
	}
	for _, seq := range []uint64{0, 1, 2} {
		if err := s.Save(ctx, State{SessionID: "s1", AppliedSeq: seq}); err != nil {
			t.Fatalf("save %d: %v", seq, err)
		}
	}
	if err := s.SetLossy(ctx, true); err != nil {
		t.Fatal(err)
	}
	_ = s.Close()
	got, err := NewFileStore(path).Load(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if want := (State{SessionID: "s1", AppliedSeq: 2, Lossy: true}); got != want {
		t.Fatalf("reload: got %+v want %+v", got, want)
	}
	entries, _ := os.ReadDir(filepath.Dir(path))
	if len(entries) != 2 { // the cursor and its .lock sidecar
		t.Fatalf("temp files left behind: %v", entries)
	}
}

func TestFileStoreRefusesGapRewindAndUnloaded(t *testing.T) {
	ctx := context.Background()
	s := NewFileStore(filepath.Join(t.TempDir(), "cursor.json"))
	if err := s.Save(ctx, State{SessionID: "s1"}); err == nil {
		t.Fatal("save before load accepted")
	}
	if err := s.SetLossy(ctx, true); err == nil {
		t.Fatal("setlossy before load accepted")
	}
	if _, err := s.Load(ctx); err != nil {
		t.Fatal(err)
	}
	if err := s.Save(ctx, State{SessionID: "s1", AppliedSeq: 1}); err != nil {
		t.Fatal(err)
	}
	if err := s.Save(ctx, State{SessionID: "s1", AppliedSeq: 3}); !errors.Is(err, ErrGap) {
		t.Fatalf("gap: %v", err)
	}
	if err := s.Save(ctx, State{SessionID: "s1", AppliedSeq: 0}); !errors.Is(err, ErrRewind) {
		t.Fatalf("rewind: %v", err)
	}
	if err := s.Save(ctx, State{AppliedSeq: 2}); !errors.Is(err, ErrNoSession) {
		t.Fatalf("no session: %v", err)
	}
}

func TestFileStoreLossyClearsAcrossSession(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "cursor.json")
	s := NewFileStore(path)
	_, _ = s.Load(ctx)
	// Lossy before any session is not persisted: there is nothing to resume from.
	if err := s.SetLossy(ctx, true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("lossy without session wrote a file: %v", err)
	}
	_ = s.Save(ctx, State{SessionID: "s1", AppliedSeq: 1, Lossy: true})
	if err := s.Save(ctx, State{SessionID: "s2", AppliedSeq: 1}); err != nil {
		t.Fatal(err)
	}
	_ = s.Close()
	got, _ := NewFileStore(path).Load(ctx)
	if got.Lossy || got.SessionID != "s2" {
		t.Fatalf("got %+v", got)
	}
}

func TestFileStoreCorruptFileIsAnError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cursor.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := NewFileStore(path).Load(context.Background()); err == nil {
		t.Fatal("corrupt cursor decoded as first boot")
	}
}

func TestFileStoreAdaptsToCheckpointer(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "cursor.json")
	fs := NewFileStore(path)
	a := Adapt(fs)
	if _, err := a.Load(ctx); err != nil {
		t.Fatal(err)
	}
	if err := a.Save(ctx, syncpkg.Cursor{SessionID: "s1", Seq: 1}); err != nil {
		t.Fatal(err)
	}
	if err := a.MarkLossy(ctx); err != nil {
		t.Fatal(err)
	}
	_ = fs.Close()
	got, _ := NewFileStore(path).Load(ctx)
	if want := (State{SessionID: "s1", AppliedSeq: 1, Lossy: true}); got != want {
		t.Fatalf("got %+v", got)
	}
}

func TestFileStoreLockExcludesSecondHolder(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "cursor.json")
	a := NewFileStore(path)
	if _, err := a.Load(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Load(ctx); err != nil {
		t.Fatalf("reload by the holder: %v", err)
	}
	b := NewFileStore(path)
	if _, err := b.Load(ctx); !errors.Is(err, ErrLocked) {
		t.Fatalf("second holder: got %v, want ErrLocked", err)
	}
	if err := a.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := b.Load(ctx); err != nil {
		t.Fatalf("after release: %v", err)
	}
	_ = b.Close()
}
