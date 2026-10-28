package tunnel

import (
	"net/http"
	"testing"
)

// TestHeaderDeploymentIDIsTheAgreedName pins the literal, because the value is
// the contract. Both ends compile against this constant, but a real gateway and
// a real agent are two binaries on two release cadences: a rename here is a
// silent routing change for every pair that is not upgraded together, and it
// would look like a cluster that had stopped choosing frontends, not like
// a header nobody was sending.
func TestHeaderDeploymentIDIsTheAgreedName(t *testing.T) {
	if HeaderDeploymentID != "X-Wingsmith-Deployment-Id" {
		t.Fatalf("HeaderDeploymentID = %q, want the name both ends of the wire agree on", HeaderDeploymentID)
	}
}

// TestHeaderDeploymentIDIsAlreadyCanonical is not pedantry. Header.Get
// canonicalises the key it looks up, while a map assignment, which is how the
// gateway stamps its per-credential headers onto an outbound request, does
// not. A constant that was not already in canonical form would therefore be
// written under one key and read under another, and the read would return empty
// with nothing anywhere reporting a failure.
func TestHeaderDeploymentIDIsAlreadyCanonical(t *testing.T) {
	if got := http.CanonicalHeaderKey(HeaderDeploymentID); got != HeaderDeploymentID {
		t.Fatalf("canonical form of %q is %q; a header written by map assignment would be unreadable by Get",
			HeaderDeploymentID, got)
	}

	header := http.Header{}
	header[HeaderDeploymentID] = []string{"dep_01"}
	if got := header.Get(HeaderDeploymentID); got != "dep_01" {
		t.Fatalf("Get after a map write = %q, want the value that was written", got)
	}
}
