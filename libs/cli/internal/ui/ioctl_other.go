//go:build !unix

package ui

import "os"

func ioctlWidth(f *os.File) int { return 0 }

func isTerminal(f *os.File) bool {
	fi, err := f.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}
