package plane

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// tunnelAuthPath resolves an enrolment token to the cluster that holds it.
//
// The plane serves it for the gateway's operator principal only, which is the
// same credential the routing table is read with: body {"token": "<enrolment
// token>"}, answering 200 {"cluster_id", "cluster_name"} for a token matching a
// cluster's token_hash, 401 for one that does not or for a cluster that is
// disabled, and 429 while its failed-guess budget is spent. The token travels in
// the body because Authorization already carries the caller's own credential.
const tunnelAuthPath = "/v1/clusters/tunnel-auth"

type tunnelAuthRequest struct {
	Token string `json:"token"`
}

type tunnelAuthResponse struct {
	ClusterID   string `json:"cluster_id"`
	ClusterName string `json:"cluster_name"`
}

// VerifyTunnelToken asks the plane which cluster an enrolment token belongs to.
//
// It is called once per tunnel — on the dial, never on a request — which is
// what keeps the control plane off the data path. A plane that is down means
// existing tunnels keep serving and only new dials fail, and even those survive
// while the listener's cache still holds the answer.
//
// Only a 401 or a 403 is a rejection. A 404 deliberately is not: a plane that
// predates the route answers 404 for the path, and reading that as "this
// cluster's token is wrong" would send everyone looking at Secrets during what is
// really a version skew. Nor is the 429 the plane answers while its failed-guess
// budget is spent — that is a wait, not a refusal. Both are reported as
// unavailable, which is recoverable, rather than as a refusal, which is not.
func (c *Client) VerifyTunnelToken(ctx context.Context, token string) (string, error) {
	body, err := json.Marshal(tunnelAuthRequest{Token: token})
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+tunnelAuthPath, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return "", fmt.Errorf("plane tunnel-auth: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	switch {
	case resp.StatusCode == http.StatusOK:
	case resp.StatusCode == http.StatusUnauthorized, resp.StatusCode == http.StatusForbidden:
		return "", fmt.Errorf("%w: the plane refused it", tunnel.ErrRejected)
	default:
		return "", fmt.Errorf("plane tunnel-auth: unexpected status %d", resp.StatusCode)
	}

	var decoded tunnelAuthResponse
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return "", fmt.Errorf("plane tunnel-auth: decode: %w", err)
	}
	if decoded.ClusterID == "" {
		return "", fmt.Errorf("plane tunnel-auth: no cluster in the answer")
	}
	return decoded.ClusterID, nil
}
