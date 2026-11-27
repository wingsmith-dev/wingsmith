// Package hostlink is the agent's data path for a bare Linux host (P2.4): the same outbound
// tunnel, supervisor and forwarding proxy the in-cluster agent runs, aimed at replica containers
// published on 127.0.0.1 instead of Kubernetes Services.
//
// It is the one package of this module meant to be imported from outside it, by wingsmith-host,
// which runs it as the second of its two connections. It must not link Kubernetes; the host
// binary's dependency test checks that.
package hostlink

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strconv"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/agent/internal/agent"
	"github.com/wingsmith-dev/wingsmith/libs/agent/internal/forward"
	"github.com/wingsmith-dev/wingsmith/libs/packages/tunnel"
)

// Routes maps a deployment id to the loopback port its replica listens on. The serving backend
// writes it after every observation; the proxy reads it on every request.
//
// There is no shared frontend on a host: a request naming no deployment, or one with no ready
// replica here, is refused as not serving, never sent to some other model.
type Routes struct {
	mu    sync.RWMutex
	ports map[string][]int
	next  map[string]int
}

// NewRoutes returns an empty table.
func NewRoutes() *Routes { return &Routes{ports: map[string][]int{}, next: map[string]int{}} }

// Set replaces the table. Each deployment lists the loopback ports of its ready replicas.
func (r *Routes) Set(ports map[string][]int) {
	copied := make(map[string][]int, len(ports))
	for id, p := range ports {
		if len(p) > 0 {
			copied[id] = append([]int(nil), p...)
		}
	}
	r.mu.Lock()
	r.ports = copied
	r.mu.Unlock()
}

// URL implements forward.Target. A host has no shared frontend.
func (r *Routes) URL() (*url.URL, error) { return nil, forward.ErrNoFrontend }

// URLFor implements forward.DeploymentTarget, spreading requests over ready replicas in turn.
func (r *Routes) URLFor(deploymentID string) (*url.URL, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	ports := r.ports[deploymentID]
	if len(ports) == 0 {
		if len(r.ports) == 0 {
			return nil, forward.ErrNoFrontend
		}
		return nil, fmt.Errorf("%w: %s", forward.ErrNoDeploymentFrontend, deploymentID)
	}
	i := r.next[deploymentID] % len(ports)
	r.next[deploymentID] = i + 1
	return &url.URL{Scheme: "http", Host: "127.0.0.1:" + strconv.Itoa(ports[i])}, nil
}

var _ forward.DeploymentTarget = (*Routes)(nil)

// Config is what the host's data path needs; the rest takes the in-cluster agent's defaults.
type Config struct {
	ClusterID string
	// Gateway is the tunnel address, host:port.
	Gateway string
	Token   string
	// CAFile verifies the gateway instead of the system roots.
	CAFile string
	// Plaintext dials without TLS. Local development only.
	Plaintext bool
	Log       *slog.Logger
}

// drainTimeout is how long in-flight streams get after shutdown starts. systemd's default
// TimeoutStopSec is 90s.
const drainTimeout = 80 * time.Second

// Run holds tunnels to every gateway replica and forwards what arrives to routes until ctx ends,
// then drains.
func Run(ctx context.Context, cfg Config, routes *Routes) error {
	if cfg.Gateway == "" {
		return errors.New("hostlink: no gateway address")
	}
	log := cfg.Log
	if log == nil {
		log = slog.Default()
	}
	acfg := &agent.Config{
		ClusterID:        cfg.ClusterID,
		Gateway:          cfg.Gateway,
		EnrolmentToken:   cfg.Token,
		TLSEnabled:       !cfg.Plaintext,
		TLSCAFile:        cfg.CAFile,
		DialTimeout:      10 * time.Second,
		HandshakeTimeout: 10 * time.Second,
	}
	server, err := tunnel.NewConnector(tunnel.ConnectorConfig{
		Handler:     forward.NewHandler(routes, tunnel.InnerHopTimeout(tunnel.DefaultResponseHeaderTimeout)),
		BaseContext: context.Background(),
	})
	if err != nil {
		return err
	}
	dialler, err := agent.NewDialler(acfg)
	if err != nil {
		return err
	}
	supervisor, err := agent.NewSupervisor(agent.SupervisorConfig{
		Gateway:         cfg.Gateway,
		ConnsPerGateway: 2,
		Dial:            dialler.Dial,
		Server:          server,
		BackoffMin:      time.Second,
		BackoffMax:      30 * time.Second,
		Log:             log,
	})
	if err != nil {
		return err
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		supervisor.Run(ctx)
	}()
	log.Info("hostlink: dialling out", "gateway", cfg.Gateway)
	<-ctx.Done()
	drain, cancel := context.WithTimeout(context.WithoutCancel(ctx), drainTimeout)
	defer cancel()
	if err := server.Shutdown(drain); err != nil {
		_ = server.Close()
	}
	<-done
	return nil
}
