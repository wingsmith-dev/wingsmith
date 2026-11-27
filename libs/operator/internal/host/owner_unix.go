//go:build unix

package host

import (
	"errors"
	"os"
	"os/user"
	"strconv"
)

// lookupService finds the service user; a var so tests need not create one.
var lookupService = func() (*user.User, error) { return user.Lookup(ServiceUser) }

// chownToService gives path to the service user when running as root and the user exists. A
// host without the user (a bare binary install running as root) keeps root ownership, which
// the credential check also accepts.
func chownToService(path string) error {
	if os.Geteuid() != 0 {
		return nil
	}
	u, err := lookupService()
	var unknown user.UnknownUserError
	if errors.As(err, &unknown) {
		return nil
	}
	if err != nil {
		return err
	}
	uid, err := strconv.Atoi(u.Uid)
	if err != nil {
		return err
	}
	gid, err := strconv.Atoi(u.Gid)
	if err != nil {
		return err
	}
	return os.Chown(path, uid, gid)
}
