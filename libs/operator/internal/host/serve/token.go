package serve

import "github.com/wingsmith-dev/wingsmith/libs/operator/internal/host/secret"

// readToken reads a credentials-dir token under the shared credential-file rule.
func readToken(path string) (string, error) { return secret.Read(path) }
