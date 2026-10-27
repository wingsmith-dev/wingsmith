package serving

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
)

// Credentials maps a credential id the plane handed out to the headers that
// redeem it.
//
// The plane stores a reference and never the secret (that is what
// the reference is for), so the material is mounted next to the gateway and looked
// up here. The file is a JSON object of `{"<credential id>": {"<header>":
// "<value>"}}`, which is intentionally dumb: whatever fronts a cluster's serving
// address (an access service token, a signed header, a shared secret) is
// somebody else's scheme, and hangar does not need to model it to carry it.
//
// mTLS is the one thing this shape cannot express, and when that is needed it
// belongs here as a second kind, not as a header with a certificate in
// it.
type Credentials map[string]http.Header

// LoadCredentials reads the mounted credential file. An empty path is not an
// error: an in-cluster address behind nothing needs no credential at all.
func LoadCredentials(path string) (Credentials, error) {
	if path == "" {
		return Credentials{}, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("credentials: %w", err)
	}

	var parsed map[string]map[string]string
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("credentials: %w", err)
	}

	credentials := make(Credentials, len(parsed))
	for id, headers := range parsed {
		header := http.Header{}
		for name, value := range headers {
			header.Set(name, value)
		}
		credentials[id] = header
	}
	return credentials, nil
}

// Headers returns what to present for a credential reference. An unknown
// reference yields nothing instead of an error: the address is still dialable
// and the upstream is the authority on whether it will answer.
func (c Credentials) Headers(credentialID string) http.Header {
	if credentialID == "" {
		return nil
	}
	return c[credentialID]
}
