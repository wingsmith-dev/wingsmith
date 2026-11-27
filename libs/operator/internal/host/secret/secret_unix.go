//go:build unix

// Package secret reads the host's credential files (the enrolment token and the per-project
// Hugging Face tokens) with one ownership and mode rule, so every reader agrees on it.
package secret

import (
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
)

// processUID is the agent's own uid; a var so tests can stand in for the service user.
var processUID = func() uint32 { return uint32(os.Geteuid()) }

// OwnerAllowed reports whether a credential file owned by uid may be read: root's, or the
// account the agent runs as (the wingsmith service user, or root on an older install).
func OwnerAllowed(uid uint32) bool {
	return uid == 0 || uid == processUID()
}

// Read opens path once without following a symlink and checks the open handle, so the file
// checked is the file read. It must be a regular, non-empty file, mode 0600, owned per
// OwnerAllowed.
func Read(path string) (string, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if err != nil {
		if err == syscall.ELOOP {
			return "", fmt.Errorf("%s is a symlink; put the token in a regular file", path)
		}
		return "", &os.PathError{Op: "open", Path: path, Err: err}
	}
	f := os.NewFile(uintptr(fd), path)
	defer func() { _ = f.Close() }()
	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	if err := Check(path, info); err != nil {
		return "", err
	}
	raw, err := io.ReadAll(io.LimitReader(f, 64<<10))
	if err != nil {
		return "", err
	}
	tok := strings.TrimSpace(string(raw))
	if tok == "" {
		return "", fmt.Errorf("%s is empty", path)
	}
	return tok, nil
}

// Check applies the rule to an already-stat'd file.
func Check(path string, info os.FileInfo) error {
	if !info.Mode().IsRegular() {
		return fmt.Errorf("%s is not a regular file", path)
	}
	if info.Mode().Perm() != 0o600 {
		return fmt.Errorf("%s has mode %o; chmod 600 it", path, info.Mode().Perm())
	}
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !OwnerAllowed(st.Uid) {
		return fmt.Errorf("%s must be owned by root or the account wingsmith-host runs as (uid %d); chown it", path, processUID())
	}
	return nil
}
