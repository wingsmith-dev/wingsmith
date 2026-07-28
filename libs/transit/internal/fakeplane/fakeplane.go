// Package fakeplane serves the two things the gateway asks the control plane
// for, and nothing else.
//
// The gateway reads a resolved routing table on a timer and resolves an
// enrolment token once per dial. Those are the whole of its dependency on the
// plane, so a harness that answers both can run the real gateway binary with no
// database, no migrations and no Python — which is what makes this runnable in
// CI. It is not a substitute for the plane and asserts nothing about it: the
// table it serves is a fixture, and the contract it is a fixture of is
// libs/packages/contract.
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
}

// Endpoint is one public model name.
type Endpoint struct {
	ID              string     `json:"id"`
	Name            string     `json:"name"`
	Aliases         []string   `json:"aliases"`
	Visibility      string     `json:"visibility"`
	RoutingStrategy string     `json:"routing_strategy"`
	Upstreams       []Upstream `json:"upstreams"`
	Revision        string     `json:"revision"`
}

// Table is the whole routing table at one revision.
type Table struct {
	Revision  string     `json:"revision"`
	Endpoints []Endpoint `json:"endpoints"`
}

// Config is what this plane will answer.
type Config struct {
	// Table is the routing table served at /v1/endpoints/resolved.
	Table Table
	// Clusters maps an enrolment token to the cluster it belongs to. A token
	// that is not in here is refused, which is what makes the listener's
	// rejection path reachable from a run.
	Clusters map[string]Cluster
	// OperatorToken is the bearer the gateway presents. Empty accepts anything.
	OperatorToken string
}

// Cluster is a token's identity, under both names.
type Cluster struct {
	ID   string `json:"cluster_id"`
	Name string `json:"cluster_name"`
}

// Server answers the gateway's two questions and counts them.
type Server struct {
	cfg Config

	mu       sync.Mutex
	resolved int
	verified int
	refused  int
}

// New builds the plane.
func New(cfg Config) *Server { return &Server{cfg: cfg} }

// Counts is how many times the gateway asked, for the report. A verification
// count far above the number of tunnels is a reconnect storm, and it is the one
// place a storm is visible as a number rather than as a log.
func (s *Server) Counts() (resolved, verified, refused int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.resolved, s.verified, s.refused
}

// Handler is the plane's mux.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/endpoints/resolved", s.resolvedTable)
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

	if match := r.Header.Get("If-None-Match"); match != "" && match == s.cfg.Table.Revision {
		w.WriteHeader(http.StatusNotModified)
		return
	}
	w.Header().Set("ETag", s.cfg.Table.Revision)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(s.cfg.Table)
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
	if s.cfg.OperatorToken == "" {
		return true
	}
	presented, _ := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	return presented == s.cfg.OperatorToken
}
