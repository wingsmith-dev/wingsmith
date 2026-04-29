// Package plane fetches the resolved routing table from the hangar control
// plane.
//
// The plane derives the table on every read and versions it with an ETag
// covering everything it was built from — endpoints, targets, deployments and
// the serving addresses of the clusters behind them. So the gateway polls with
// `If-None-Match` and the steady state costs a 304 with no body.
package plane

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Upstream is one dialable address behind an endpoint.
type Upstream struct {
	DeploymentID    string `json:"deployment_id"`
	DeploymentName  string `json:"deployment_name"`
	AdmissionPhase  string `json:"admission_phase"`
	ClusterID       string `json:"cluster_id"`
	ClusterName     string `json:"cluster_name"`
	URL             string `json:"url"`
	ServedModelName string `json:"served_model_name"`
	CredentialID    string `json:"credential_id"`
	Weight          int    `json:"weight"`
	Role            string `json:"role"`
}

// Endpoint is one public name and every address currently behind it.
type Endpoint struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	Visibility      string     `json:"visibility"`
	RoutingStrategy string     `json:"routing_strategy"`
	Upstreams       []Upstream `json:"upstreams"`
	FailoverOrder   []string   `json:"failover_order"`
	Revision        string     `json:"revision"`
}

// Table is the whole routing table at one revision.
type Table struct {
	Revision  string     `json:"revision"`
	Endpoints []Endpoint `json:"endpoints"`
}

// ErrNotModified reports that the caller's revision is still current.
//
// A sentinel rather than a nil table, because "nothing changed" and "the table
// is empty" have to stay distinguishable — one is the steady state and the
// other means every model just went away.
var ErrNotModified = fmt.Errorf("routing table not modified")

// Client reads the routing table from one plane.
type Client struct {
	baseURL string
	token   string
	http    *http.Client
}

// New builds a client against the plane's base URL.
func New(baseURL, token string, timeout time.Duration) *Client {
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		http:    &http.Client{Timeout: timeout},
	}
}

// Resolved fetches the table, or reports ErrNotModified when revision still holds.
func (c *Client) Resolved(ctx context.Context, revision string) (Table, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/v1/endpoints/resolved", nil)
	if err != nil {
		return Table{}, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if revision != "" {
		req.Header.Set("If-None-Match", revision)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return Table{}, err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == http.StatusNotModified {
		return Table{}, ErrNotModified
	}
	if resp.StatusCode != http.StatusOK {
		return Table{}, fmt.Errorf("plane resolved: unexpected status %d", resp.StatusCode)
	}

	var table Table
	if err := json.NewDecoder(resp.Body).Decode(&table); err != nil {
		return Table{}, fmt.Errorf("plane resolved: decode: %w", err)
	}
	return table, nil
}
