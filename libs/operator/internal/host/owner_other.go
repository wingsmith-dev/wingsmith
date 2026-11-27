//go:build !unix

package host

func chownToService(string) error { return nil }
