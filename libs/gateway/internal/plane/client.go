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
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
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
	// OutputModality is what the engine behind this upstream emits: `text`, `image` or `video`.
	// Empty from a plane that predates the field, which reads as text.
	OutputModality string `json:"output_modality,omitempty"`
}

// Rule is one term an endpoint sells, scoped to where it applies.
//
// A rule has a scope, a subject and its terms. The scope decides whether the
// rule is looked at for this request at all; the subject is what it is about;
// the terms are what it says. Every rule whose scope matches applies, and the
// request must satisfy all of them — there is no precedence between rules and
// the order of the array is never read. Two rules that disagree are two terms
// that were both sold, which is an authoring mistake and is refused where the
// policy is written rather than resolved here.
type Rule struct {
	// Route is the request path this rule is about — "/v1/chat/completions".
	// Empty matches every path.
	//
	// It is the whole reason rules are a list rather than a map: a term
	// measured against one protocol is not a term about another, and before
	// this existed a rule written for chat completions was enforced against
	// /v1/embeddings, /v1/messages and /v1/responses as well.
	Route string `json:"route,omitempty"`
	// Stream narrows a rule to streaming or non-streaming requests. Nil is both.
	Stream *bool `json:"stream,omitempty"`
	// WhenPresent narrows a rule to requests that carry another JSON path,
	// dotted like Key. Empty asks nothing of the body.
	//
	// It is a scope and not a term, and it sits here rather than beside Drop for
	// that reason: "does this rule look at this request at all" is one question,
	// whether what narrows it is the path the request came in on, the transport
	// mode, or another field in the body. Putting the condition in the scope
	// block means it composes with every term rather than with one of them — the
	// same field that expresses "drop `reasoning_effort` when `thinking.effort`
	// is set" would express "`temperature` must be 1 while thinking is on",
	// without another field and without a second way to say `when`.
	WhenPresent string `json:"when_present,omitempty"`

	// Key is the JSON path this rule constrains, dotted: "thinking.type".
	// Exclusive with Predicate.
	Key string `json:"key,omitempty"`
	// Predicate names a rule this build implements in Go, resolved against the
	// same registry the old `deny` list named. Exclusive with Key.
	//
	// The two are different namespaces and never fall back to one another: a
	// key is read out of the body, a predicate is looked up in a map. A name
	// that means nothing to this build is ignored rather than enforced, and
	// reported at refresh time.
	Predicate string `json:"predicate,omitempty"`

	Enum []any    `json:"enum,omitempty"`
	Min  *float64 `json:"min,omitempty"`
	Max  *float64 `json:"max,omitempty"`
	// Deny refuses the request: on a key, whenever the key is sent at all; on a
	// predicate, whenever the predicate fires.
	Deny bool `json:"deny,omitempty"`
	// Drop removes the key from the request before forwarding, and answers 200.
	//
	// It is the term for a field the provider accepts and ignores. Denying one
	// of those would 400 a request the vendor serves, and leaving it in place
	// lets an engine honour a field the provider does not — which is a wrong
	// answer rather than an error, and the harder of the two to notice.
	//
	// The parent object is never removed with the last key out of it: an empty
	// `thinking` and no `thinking` at all are different requests.
	Drop bool `json:"drop,omitempty"`
	// Default is the value to write into an absent key before forwarding. It and
	// Drop are the terms that write rather than refuse; neither ever fails a
	// request.
	//
	// It fills the key only when the key's parent container is already in the
	// request, and it never creates an intermediate object. `thinking.type`
	// with a default of "enabled" fills a `thinking` the caller sent without a
	// `type`, and does nothing at all to a request that never mentioned
	// thinking: inventing the parent would change what every other caller gets.
	Default any `json:"default,omitempty"`
	// ValueFrom is another JSON path whose value is written into Key before
	// forwarding, overwriting whatever the caller put there. The third writing
	// term, and the one for a provider that spells a knob differently from the
	// engine behind it.
	//
	// `{key: "reasoning_effort", value_from: "thinking.effort"}` is the whole of
	// K3's effort contract in one rule: the vendor's own field is carried onto the
	// field the engine reads, so the level the caller named in `thinking.effort`
	// is the level that runs, and the `reasoning_effort` they sent beside it is
	// ignored because it has been overwritten. A request with no `thinking.effort`
	// is untouched, so a caller who sent only `reasoning_effort` still gets it.
	//
	// Overwriting is what separates it from Default, which exists to fill in what
	// a caller left out and leaves a value they wrote alone. Here the caller's
	// value is precisely what has to go: a term that deferred to it would be
	// honouring the field the provider ignores.
	//
	// Like Default it never creates an intermediate object, for the same reason. A
	// source path that is not in the request writes nothing at all — which is what
	// makes the rule conditional without needing a WhenPresent beside it.
	ValueFrom string `json:"value_from,omitempty"`
}

// Policy is the request terms an endpoint sells, as the plane resolved them.
// Nil means no opinion, which is every endpoint that has not sold a consistency
// term.
//
// Version is the plane's, not ours. A policy naming a version above the one this
// build understands is still enforced as far as it is understood — dropping
// traffic because the plane learned a new rule first would trade a conformance
// gap for an outage.
type Policy struct {
	Version int    `json:"version"`
	Rules   []Rule `json:"rules"`
}

// Endpoint is one public name and every address currently behind it.
type Endpoint struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	// Aliases are the other public model ids that resolve to this endpoint.
	// The plane lowercases them and holds them in one namespace with every
	// endpoint's name, so an alias is only ever an extra key into this route.
	Aliases         []string   `json:"aliases"`
	Visibility      string     `json:"visibility"`
	RoutingStrategy string     `json:"routing_strategy"`
	Upstreams       []Upstream `json:"upstreams"`
	FailoverOrder   []string   `json:"failover_order"`
	Policy          *Policy    `json:"policy"`
	// TenantID is the endpoint's project's tenant (docs/decisions/0016). There is
	// exactly one per endpoint; this is not a cross-tenant endpoint's table.
	TenantID string `json:"tenant_id"`
	Revision string `json:"revision"`
}

// Table is the whole routing table at one revision.
type Table struct {
	Revision  string     `json:"revision"`
	Endpoints []Endpoint `json:"endpoints"`
}

// GatewayTenant is one tenant, as the gateway needs to trust it (docs/decisions/0016).
//
// `KeyHashes` carries only the live ones — a revoked key is dropped from the
// plane's own feed, and that drop is the whole mechanism a revoke has. Its
// stamp still moved `TenantTable.Revision`, which is what a poller needs and
// nothing more: this hop never sees a plaintext key, only the sha256 it
// checks a presented one against.
type GatewayTenant struct {
	ID        string   `json:"id"`
	Slug      string   `json:"slug"`
	Enabled   bool     `json:"enabled"`
	Ingress   string   `json:"ingress"`
	Auth      string   `json:"auth"`
	Host      string   `json:"host"`
	KeyHashes []string `json:"key_hashes"`
}

// TenantTable is every tenant at one revision.
type TenantTable struct {
	Revision string          `json:"revision"`
	Tenants  []GatewayTenant `json:"tenants"`
}

// ErrNotModified reports that the caller's revision is still current.
//
// A sentinel rather than a nil table, because "nothing changed" and "the table
// is empty" have to stay distinguishable — one is the steady state and the
// other means every model just went away.
var ErrNotModified = fmt.Errorf("routing table not modified")

// ErrCredential reports that the plane refused the gateway's own service
// credential (HANGAR_GATEWAY_SERVICE_TOKEN) with a 401 or a 403.
//
// It is a configuration fault on this side and never a verdict about anything
// the gateway was asking about: a tunnel-auth answer carrying it says nothing
// about the agent's enrolment token.
var ErrCredential = errors.New("the plane refused the gateway service credential")

// Client reads the routing table from one plane, as the gateway's service
// principal. The plane accepts that principal on three routes only:
// GET /v1/endpoints/resolved, GET /v1/gateway/tenants and
// POST /v1/clusters/tunnel-auth.
type Client struct {
	baseURL string
	token   string
	http    *http.Client

	// tenantsRevision is the last tenant-table revision the plane sent, so a
	// credential check can be a conditional read that costs a 304.
	mu              sync.Mutex
	tenantsRevision string
}

// New builds a client against the plane's base URL, authenticating with the
// gateway's service token.
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
	if credentialRefused(resp.StatusCode) {
		return Table{}, fmt.Errorf("plane resolved: %w (status %d)", ErrCredential, resp.StatusCode)
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

// TenantsResolved fetches the gateway's tenant feed, or reports
// ErrNotModified when revision still holds. Same shape as Resolved, against
// the surface ADR 0016 adds specifically for this hop.
func (c *Client) TenantsResolved(ctx context.Context, revision string) (TenantTable, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/v1/gateway/tenants", nil)
	if err != nil {
		return TenantTable{}, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if revision != "" {
		req.Header.Set("If-None-Match", revision)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return TenantTable{}, err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode == http.StatusNotModified {
		return TenantTable{}, ErrNotModified
	}
	if credentialRefused(resp.StatusCode) {
		return TenantTable{}, fmt.Errorf("plane gateway tenants: %w (status %d)", ErrCredential, resp.StatusCode)
	}
	if resp.StatusCode != http.StatusOK {
		return TenantTable{}, fmt.Errorf("plane gateway tenants: unexpected status %d", resp.StatusCode)
	}

	var table TenantTable
	if err := json.NewDecoder(resp.Body).Decode(&table); err != nil {
		return TenantTable{}, fmt.Errorf("plane gateway tenants: decode: %w", err)
	}
	c.mu.Lock()
	c.tenantsRevision = table.Revision
	c.mu.Unlock()
	return table, nil
}

// CheckCredential asks the plane whether it still accepts this client's service
// credential, as a conditional read of the tenant feed so the usual answer is a
// bodiless 304. Nil means accepted; ErrCredential means refused; any other error
// means the plane could not answer.
func (c *Client) CheckCredential(ctx context.Context) error {
	c.mu.Lock()
	revision := c.tenantsRevision
	c.mu.Unlock()
	_, err := c.TenantsResolved(ctx, revision)
	if err == nil || errors.Is(err, ErrNotModified) {
		return nil
	}
	return err
}

func credentialRefused(status int) bool {
	return status == http.StatusUnauthorized || status == http.StatusForbidden
}
