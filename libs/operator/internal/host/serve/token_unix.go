//go:build unix

package serve

import (
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
)

// tokenOwnerUID is the owner a token file must have: root, which is who the agent runs as.
var tokenOwnerUID uint32 = 0

// readToken opens the file once without following a symlink and checks the open handle, so the
// file checked is the file read.
func readToken(path string) (string, error) {
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
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("%s is not a regular file", path)
	}
	if info.Mode().Perm() != 0o600 {
		return "", fmt.Errorf("%s has mode %o; chmod 600 it", path, info.Mode().Perm())
	}
	if st, ok := info.Sys().(*syscall.Stat_t); !ok || st.Uid != tokenOwnerUID {
		return "", fmt.Errorf("%s is not owned by root; chown root it", path)
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
