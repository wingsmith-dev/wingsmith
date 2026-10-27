// Package fakeplane serves what the gateway asks the control plane for, and
// nothing else.
//
// The gateway reads two tables on a timer, the resolved routing table and the
// tenant table, and resolves an enrolment token once per dial. Those three
// routes are the whole of its dependency on the plane, and they are the only
// three the plane lets the gateway's service principal call, so a harness that
// answers them can run the real gateway binary with no database, no migrations
// and no Python, which is what makes this runnable in CI. It is not a
// substitute for the plane and asserts nothing about it: the tables it serves
// are fixtures, and the contract they are fixtures of is libs/packages/contract.
//
// The wire types below mirror libs/gateway/internal/plane field for field. They
// cannot import it, it is internal to another module, so contract_test.go
// reads the gateway's source and fails when a JSON field exists on one side and
// not the other.
package fakeplane

import (
	"encoding/json"
	"net/http"
	"strings"
	"sync"
)

// Upstream is one address behind an endpoint, in the shape the gateway decodes.
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
	OutputModality  string `json:"output_modality,omitempty"`
}

// Rule is one request term an endpoint sells. The gateway's plane.Rule holds
// what each field means.
type Rule struct {
	Route       string   `json:"route,omitempty"`
	Stream      *bool    `json:"stream,omitempty"`
	WhenPresent string   `json:"when_present,omitempty"`
	Key         string   `json:"key,omitempty"`
	Predicate   string   `json:"predicate,omitempty"`
	Enum        []any    `json:"enum,omitempty"`
	Min         *float64 `json:"min,omitempty"`
	Max         *float64 `json:"max,omitempty"`
	Deny        bool     `json:"deny,omitempty"`
	Drop        bool     `json:"drop,omitempty"`
	Default     any      `json:"default,omitempty"`
	ValueFrom   string   `json:"value_from,omitempty"`
}

// Policy is an endpoint's request terms. Nil is no opinion.
type Policy struct {
	Version int    `json:"version"`
	Rules   []Rule `json:"rules"`
}

// Endpoint is one public model name.
type Endpoint struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	Aliases         []string   `json:"aliases"`
	Visibility      string     `json:"visibility"`
	RoutingStrategy string     `json:"routing_strategy"`
	Upstreams       []Upstream `json:"upstreams"`
	FailoverOrder   []string   `json:"failover_order"`
	Policy          *Policy    `json:"policy"`
	TenantID        string     `json:"tenant_id"`
	Revision        string     `json:"revision"`
}

// Table is the whole routing table at one revision.
type Table struct {
	Revision  string     `json:"revision"`
	Endpoints []Endpoint `json:"endpoints"`
}

// GatewayTenant is one tenant as the gateway trusts it: its ingress, its auth
// mode and the sha256 of each live key.
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

// Config is what this plane will answer.
type Config struct {
	// Table is the routing table served at /v1/endpoints/resolved.
	Table Table
	// Tenants is the tenant table served at /v1/gateway/tenants. The zero value
	// is served as an empty table at a fixed revision: the gateway will not
	// start without one, and the load this harness sends goes to the internal
	// listener, which does not consult it.
	Tenants TenantTable
	// Clusters maps an enrolment token to the cluster it belongs to. A token
	// that is not in here is refused, which is what makes the listener's
	// rejection path reachable from a run.
	Clusters map[string]Cluster
	// ServiceToken is the bearer the gateway presents, the value it reads from
	// HANGAR_GATEWAY_SERVICE_TOKEN. Empty accepts anything.
	ServiceToken string
}

// Cluster is a token's identity, under both names.
type Cluster struct {
	ID   string `json:"cluster_id"`
	Name string `json:"cluster_name"`
}

// emptyTenantsRevision is the revision of the table served when Config.Tenants
// is left empty.
const emptyTenantsRevision = "transit-tenants-0"

// Server answers the gateway and counts what it asked.
type Server struct {
	cfg Config

	mu       sync.Mutex
	resolved int
	tenants  int
	verified int
	refused  int
}

// New builds the plane.
func New(cfg Config) *Server {
	if cfg.Tenants.Revision == "" {
		cfg.Tenants.Revision = emptyTenantsRevision
	}
	if cfg.Tenants.Tenants == nil {
		cfg.Tenants.Tenants = []GatewayTenant{}
	}
	return &Server{cfg: cfg}
}

// Counts is how many times the gateway asked, for the report. A verification
// count far above the number of tunnels is a reconnect storm, and it is the one
// place a storm is visible as a number, not as a log.
func (s *Server) Counts() (resolved, verified, refused int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.resolved, s.verified, s.refused
}

// TenantReads is how many times the gateway read the tenant table.
func (s *Server) TenantReads() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.tenants
}

// Handler is the plane's mux: the three routes the gateway's service principal
// is allowed, and nothing else.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/endpoints/resolved", s.resolvedTable)
	mux.HandleFunc("GET /v1/gateway/tenants", s.tenantTable)
	mux.HandleFunc("POST /v1/clusters/tunnel-auth", s.tunnelAuth)
	return mux
}

func (s *Server) resolvedTable(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	s.mu.Lock()
	s.resolved++
	s.mu.Unlock()
	conditional(w, r, s.cfg.Table.Revision, s.cfg.Table)
}

func (s *Server) tenantTable(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	s.mu.Lock()
	s.tenants++
	s.mu.Unlock()
	conditional(w, r, s.cfg.Tenants.Revision, s.cfg.Tenants)
}

// conditional answers a table read the way the plane does: a 304 with no body
// when the caller already holds the revision, the table with an ETag otherwise.
func conditional(w http.ResponseWriter, r *http.Request, revision string, body any) {
	if match := r.Header.Get("If-None-Match"); match != "" && match == revision {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("ETag", revision)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(body)
}

func (s *Server) tunnelAuth(w http.ResponseWriter, r *http.Request) {
	if !s.authorized(r) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	var body struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	cluster, ok := s.cfg.Clusters[body.Token]
	s.mu.Lock()
	if ok {
		s.verified++
	} else {
		s.refused++
	}
	s.mu.Unlock()
	if !ok {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(cluster)
}

func (s *Server) authorized(r *http.Request) bool {
	if s.cfg.ServiceToken == "" {
		return true
	}
	presented, _ := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	return presented == s.cfg.ServiceToken
}
