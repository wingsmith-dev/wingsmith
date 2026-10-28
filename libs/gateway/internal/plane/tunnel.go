package plane

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/wingsmith-dev/wingsmith/libs/packages/tunnel"
)

// tunnelAuthPath resolves an enrolment token to the cluster that holds it.
//
// The plane serves it to the gateway's service principal, the same credential
// the routing table is read with: body {"token": "<enrolment token>",
// "source": "<agent address>"}, answering 200 {"cluster_id", "cluster_name"}
// for a token matching a cluster's token_hash, 401 for one that does not or for
// a cluster that is disabled, and 429 while the source's failed-guess budget is
// spent. A 429 whose detail carries the verdict "unknown_token" is still a
// rejection: the plane looked the token up first and found no cluster. The
// token travels in the body because Authorization already carries the gateway's
// own credential.
const tunnelAuthPath = "/v1/clusters/tunnel-auth"

type tunnelAuthRequest struct {
	Token string `json:"token"`
	// Source is the agent's address as the listener keys it. The plane charges
	// a failed verification to it, so one address's bad tokens throttle only
	// that address and not every check this gateway makes.
	Source string `json:"source,omitempty"`
}

type tunnelAuthResponse struct {
	ClusterID   string `json:"cluster_id"`
	ClusterName string `json:"cluster_name"`
}

// VerifyTunnelToken asks the plane which cluster an enrolment token belongs to.
//
// It is called once per tunnel (on the dial, never on a request), which is
// what keeps the control plane off the data path. A plane that is down means
// existing tunnels keep serving and only new dials fail, and even those survive
// while the listener's cache still holds the answer.
//
// Only a 401 about the enrolment token is a rejection. The plane answers 401
// for a bad gateway credential as well, so a 401 is followed by a check of the
// gateway's own credential, and only when that check passes is the 401 the
// agent's. A 403 is never the agent's: the plane uses it for a principal that
// may not call the route at all. Either credential fault is reported as
// ErrCredential, which the listener treats as the plane being unavailable, so
// it keeps the tokens it has already verified instead of forgetting them over
// a gateway misconfiguration.
//
// A 404 is not a rejection either: a plane that predates the route answers 404
// for the path, and reading that as "this cluster's token is wrong" would send
// everyone looking at Secrets during what is really a version skew. Nor is the
// 429 the plane answers while its failed-guess budget is spent, which is a wait
// and not a refusal, unless its body carries the unknown_token verdict: then the
// token itself is wrong and the answer is ErrRejected. Otherwise both are
// reported as unavailable.
//
// Both names are carried back, not just the uid. The plane answers with both
// because they are addressable interchangeably, and the agent's claim is checked
// against either: a cluster's own configuration holds the name, so returning the
// uid alone refused every dial from a chart-installed agent.
func (c *Client) VerifyTunnelToken(ctx context.Context, token string) (tunnel.Identity, error) {
	body, err := json.Marshal(tunnelAuthRequest{Token: token, Source: tunnel.DialSource(ctx)})
	if err != nil {
		return tunnel.Identity{}, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+tunnelAuthPath, bytes.NewReader(body))
	if err != nil {
		return tunnel.Identity{}, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()

	// A throttled answer still carries the plane's verdict. The plane looks the
	// token up before it consults the budget, so "unknown_token" on a 429 means
	// the token names no cluster. Reading it as an outage would let anyone who
	// keeps a source's budget spent keep a rotated token's tunnels open.
	if resp.StatusCode == http.StatusTooManyRequests && verdictOf(resp.Body) == verdictUnknownToken {
		return tunnel.Identity{}, fmt.Errorf("%w: the plane does not know it (answered while throttled)", tunnel.ErrRejected)
	}

	switch {
	case resp.StatusCode == http.StatusOK:
	case resp.StatusCode == http.StatusForbidden:
		return tunnel.Identity{}, credentialFault(fmt.Errorf("plane tunnel-auth: %w (status 403)", ErrCredential))
	case resp.StatusCode == http.StatusUnauthorized:
		if err := c.CheckCredential(ctx); err != nil {
			if errors.Is(err, ErrCredential) {
				return tunnel.Identity{}, credentialFault(err)
			}
			return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: refused, and the gateway credential could not be checked: %w", err)
		}
		return tunnel.Identity{}, fmt.Errorf("%w: the plane refused it", tunnel.ErrRejected)
	case resp.StatusCode == http.StatusTooManyRequests:
		return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: throttled, retry shortly")
	default:
		return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: unexpected status %d", resp.StatusCode)
	}

	var decoded tunnelAuthResponse
	if err := json.NewDecoder(resp.Body).Decode(&decoded); err != nil {
		return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: decode: %w", err)
	}
	if decoded.ClusterID == "" {
		return tunnel.Identity{}, fmt.Errorf("plane tunnel-auth: no cluster in the answer")
	}
	return tunnel.Identity{ClusterID: decoded.ClusterID, ClusterName: decoded.ClusterName}, nil
}

// credentialFault logs a refused service credential where an operator will see
// it: every dial in the meantime fails for a reason no agent can fix.
func credentialFault(err error) error {
	slog.Error("gateway: the plane refused WINGSMITH_GATEWAY_SERVICE_TOKEN; new tunnels cannot be verified until the gateway's service credential is fixed", "err", err)
	return err
}
