package checkpoint

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	stdsync "sync"
	"time"
)

// FileStore persists the resume cursor as a JSON file, for hosts that have no API server to hold
// a StreamCheckpoint. It enforces the same advance rules as CRDStore.
//
// Every write goes to a temp file in the same directory, is fsynced, renamed over the target, and
// the directory is fsynced, so a crash leaves either the old cursor or the new one, never a torn
// file. A file that exists but does not decode is an error, not a first boot.
type FileStore struct {
	Path string

	mu     stdsync.Mutex
	loaded bool
	last   State
}

var _ Store = (*FileStore)(nil)

// NewFileStore builds a FileStore at path. The parent directory must exist.
func NewFileStore(path string) *FileStore { return &FileStore{Path: path} }

type fileState struct {
	SessionID  string    `json:"sessionId"`
	AppliedSeq uint64    `json:"appliedSeq"`
	Lossy      bool      `json:"lossy"`
	UpdatedAt  time.Time `json:"updatedAt"`
}

// Load reads the persisted cursor. A missing file yields the zero State.
func (s *FileStore) Load(context.Context) (State, error) {
	raw, err := os.ReadFile(s.Path)
	var st State
	switch {
	case errors.Is(err, fs.ErrNotExist):
	case err != nil:
		return State{}, fmt.Errorf("read checkpoint %s: %w", s.Path, err)
	default:
		var f fileState
		if err := json.Unmarshal(raw, &f); err != nil {
			return State{}, fmt.Errorf("decode checkpoint %s: %w", s.Path, err)
		}
		st = State{SessionID: f.SessionID, AppliedSeq: f.AppliedSeq, Lossy: f.Lossy}
	}
	s.mu.Lock()
	s.loaded, s.last = true, st
	s.mu.Unlock()
	return st, nil
}

// Save validates the advance and writes it atomically.
func (s *FileStore) Save(_ context.Context, next State) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.loaded {
		return fmt.Errorf("checkpoint: Save before Load")
	}
	if err := validateAdvance(s.last, next); err != nil {
		return err
	}
	merged := merge(s.last, next)
	if err := s.write(merged); err != nil {
		return err
	}
	s.last = merged
	return nil
}

// SetLossy records or clears the resume-invalid marker without touching the sequence cursor.
func (s *FileStore) SetLossy(_ context.Context, lossy bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.loaded {
		return fmt.Errorf("checkpoint: SetLossy before Load")
	}
	if s.last.Lossy == lossy {
		return nil
	}
	next := s.last
	next.Lossy = lossy
	if next.SessionID == "" {
		return nil
	}
	if err := s.write(next); err != nil {
		return err
	}
	s.last = next
	return nil
}

func (s *FileStore) write(st State) error {
	raw, err := json.Marshal(fileState{
		SessionID: st.SessionID, AppliedSeq: st.AppliedSeq, Lossy: st.Lossy, UpdatedAt: time.Now().UTC(),
	})
	if err != nil {
		return err
	}
	dir := filepath.Dir(s.Path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(s.Path)+".*")
	if err != nil {
		return fmt.Errorf("write checkpoint %s: %w", s.Path, err)
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write checkpoint %s: %w", s.Path, err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync checkpoint %s: %w", s.Path, err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("write checkpoint %s: %w", s.Path, err)
	}
	if err := os.Rename(tmp.Name(), s.Path); err != nil {
		return fmt.Errorf("replace checkpoint %s: %w", s.Path, err)
	}
	d, err := os.Open(dir)
	if err != nil {
		return fmt.Errorf("sync checkpoint dir %s: %w", dir, err)
	}
	defer func() { _ = d.Close() }()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("sync checkpoint dir %s: %w", dir, err)
	}
	return nil
}
