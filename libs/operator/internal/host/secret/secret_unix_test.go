//go:build unix

package secret

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, mode os.FileMode, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(p, []byte(body), mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	return p
}

func asUID(t *testing.T, uid uint32) {
	old := processUID
	processUID = func() uint32 { return uid }
	t.Cleanup(func() { processUID = old })
}

func TestReadOwnedBySelf(t *testing.T) {
	asUID(t, uint32(os.Geteuid()))
	tok, err := Read(write(t, 0o600, " wso1.x\n"))
	if err != nil || tok != "wso1.x" {
		t.Fatalf("%q %v", tok, err)
	}
}

func TestReadRefusesOtherOwner(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root owns the file; the rule always admits root")
	}
	asUID(t, uint32(os.Geteuid())+1)
	if _, err := Read(write(t, 0o600, "wso1.x")); err == nil || !strings.Contains(err.Error(), "owned by root or") {
		t.Fatalf("want owner refusal, got %v", err)
	}
}

func TestReadRefusesLooseMode(t *testing.T) {
	asUID(t, uint32(os.Geteuid()))
	for _, m := range []os.FileMode{0o640, 0o644, 0o400} {
		if _, err := Read(write(t, m, "wso1.x")); err == nil {
			t.Fatalf("mode %o was read", m)
		}
	}
}

func TestReadRefusesSymlinkAndEmpty(t *testing.T) {
	asUID(t, uint32(os.Geteuid()))
	target := write(t, 0o600, "wso1.x")
	link := filepath.Join(t.TempDir(), "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	if _, err := Read(link); err == nil {
		t.Fatal("a symlink was read")
	}
	if _, err := Read(write(t, 0o600, "  \n")); err == nil {
		t.Fatal("an empty token was read")
	}
}

func TestOwnerAllowed(t *testing.T) {
	asUID(t, 997)
	if !OwnerAllowed(0) || !OwnerAllowed(997) || OwnerAllowed(1000) {
		t.Fatal("owner rule is root or the process uid")
	}
}
