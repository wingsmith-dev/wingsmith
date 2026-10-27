package serving

import (
	"os"
	"path/filepath"
	"testing"
)

func TestCredentialsAreLoadedByReferenceAndMissingOnesAreNotFatal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "credentials.json")
	if err := os.WriteFile(path, []byte(`{"cred-1":{"CF-Access-Client-Id":"abc"}}`), 0o600); err != nil {
		t.Fatalf("write: %v", err)
	}

	credentials, err := LoadCredentials(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if credentials.Headers("cred-1").Get("CF-Access-Client-Id") != "abc" {
		t.Fatal("the mounted header was not loaded")
	}
	if credentials.Headers("cred-2") != nil {
		t.Fatal("an unknown reference must yield nothing, not invent headers")
	}
	if credentials.Headers("") != nil {
		t.Fatal("an address behind nothing needs no headers")
	}
}

func TestNoCredentialFileIsNotAnError(t *testing.T) {
	if _, err := LoadCredentials(""); err != nil {
		t.Fatalf("load: %v", err)
	}
}
