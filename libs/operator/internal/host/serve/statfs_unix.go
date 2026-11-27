//go:build unix

package serve

import "syscall"

// statfsUsage is the bytes in use and the size of the filesystem holding path. Used counts the
// root-reserved blocks, since the cache cannot write into them.
func statfsUsage(path string) (used, total uint64, err error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return 0, 0, err
	}
	bs := uint64(st.Bsize) //nolint:gosec,unconvert // Bsize is signed on linux, unsigned on darwin
	total = st.Blocks * bs
	return total - st.Bavail*bs, total, nil
}
