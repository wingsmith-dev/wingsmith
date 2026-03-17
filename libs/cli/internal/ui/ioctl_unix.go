//go:build unix

package ui

import (
	"os"
	"syscall"
	"unsafe"
)

type winsize struct{ row, col, xpixel, ypixel uint16 }

func ioctlWinsize(f *os.File) (winsize, bool) {
	var ws winsize
	_, _, err := syscall.Syscall6(syscall.SYS_IOCTL, f.Fd(),
		uintptr(syscall.TIOCGWINSZ), uintptr(unsafe.Pointer(&ws)), 0, 0, 0)
	return ws, err == 0
}

func ioctlWidth(f *os.File) int {
	ws, ok := ioctlWinsize(f)
	if !ok {
		return 0
	}
	return int(ws.col)
}

// isTerminal asks the kernel for the window size. Only a real terminal answers;
// /dev/null and every other character device fail with ENOTTY, which the
// os.ModeCharDevice heuristic cannot distinguish.
func isTerminal(f *os.File) bool {
	_, ok := ioctlWinsize(f)
	return ok
}
