//go:build !unix

// Package secret reads the host's credential files; only unix hosts are supported.
package secret

import "errors"

// Read is unsupported off unix.
func Read(string) (string, error) {
	return "", errors.New("token files are not supported on this platform")
}
