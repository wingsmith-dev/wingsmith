//go:build !unix

package serve

import "errors"

func readToken(string) (string, error) {
	return "", errors.New("token files are not supported on this platform")
}
