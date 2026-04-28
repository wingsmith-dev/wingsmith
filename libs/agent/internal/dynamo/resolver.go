// Package dynamo is the agent's local half: it finds this cluster's
// OpenAI-compatible Dynamo frontend Service and forwards tunnelled requests to
// it.
//
// It is the cluster-local end of rule 4. A request arrives over a connection
// the agent dialled outwards, and from here on it never leaves the cluster:
// the address it is aimed at is a ClusterIP, resolved in the cluster, by asking
// the cluster. Nothing in this package talks to the plane.
package dynamo

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ComponentLabel is how Dynamo's operator marks the pods in front of a graph.
// The Service that selects on it is the graph's HTTP entrypoint.
const ComponentLabel = "nvidia.com/dynamo-component"

// componentFrontend is the value of ComponentLabel on the frontend component.
const componentFrontend = "Frontend"

// defaultPort is Dynamo's frontend port, used when a Service declares several
// ports and names none of them `http`.
const defaultPort = 8000

// defaultInterval is how often discovery re-asks the cluster. Frontends move
// when a graph is redeployed, which is a human-paced event, so this is slow on
// purpose: the address is read on every request and refreshed off the request
// path.
const defaultInterval = 30 * time.Second

// ErrNoFrontend means no address is known yet. It is deliberately distinct from
// a failure to talk to the API server: the cluster answered, and the answer was
// that this cluster is not serving anything.
var ErrNoFrontend = errors.New("no inference frontend found in this cluster")

// Services is the slice of Kubernetes this package needs. It is one method wide
// so that the agent can hand it a typed client and a test can hand it a
// fake without either knowing about the other.
type Services interface {
	List(ctx context.Context, opts metav1.ListOptions) (*corev1.ServiceList, error)
}

// Config holds the resolver's settings.
type Config struct {
	// Endpoint, when set, is the frontend's base URL and discovery never runs.
	// A cluster that fronts its models with something Dynamo did not label has
	// said so, and a lookup is not entitled to overrule it.
	Endpoint string
	// Namespace is where the graphs run. Ignored when Endpoint is set.
	Namespace string
	// Services reads the cluster's Services. Required unless Endpoint is set.
	Services Services
	// Interval is how often discovery re-runs. Zero means defaultInterval.
	Interval time.Duration
}

// Resolver yields the frontend's current base URL.
//
// Configuration used to carry the address outright, with a default of
// `dynamo-frontend`, and that default was wrong on every cluster we run: one
// names its frontend after the graph it serves, another after the shared graph
// fronting three models. A name nobody has is worse than no name, because it
// surfaces as a DNS error deep inside whatever was dialling — which reads like
// a broken cluster rather than a setting nobody set.
//
// So this asks the cluster. The lookup is by Dynamo's own component label
// rather than by name, because the name is the part that varies.
type Resolver struct {
	services  Services
	namespace string
	interval  time.Duration

	// fixed is the configured override. Non-nil means URL never blocks on
	// anything and Run has nothing to do.
	fixed *url.URL

	mu      sync.RWMutex
	current *url.URL
}

// New builds a resolver. An override is parsed here rather than on first use,
// so a typo in it fails at startup instead of on a caller's first request.
func New(cfg Config) (*Resolver, error) {
	resolver := &Resolver{
		services:  cfg.Services,
		namespace: cfg.Namespace,
		interval:  cfg.Interval,
	}
	if resolver.interval <= 0 {
		resolver.interval = defaultInterval
	}

	if cfg.Endpoint != "" {
		parsed, err := parseEndpoint(cfg.Endpoint)
		if err != nil {
			return nil, err
		}
		resolver.fixed = parsed
		return resolver, nil
	}

	if cfg.Services == nil {
		return nil, errors.New("frontend: either an endpoint override or a Kubernetes client is required")
	}
	return resolver, nil
}

// parseEndpoint accepts a bare host:port as well as a URL, because that is what
// a per-cluster setting tends to be written as.
func parseEndpoint(endpoint string) (*url.URL, error) {
	if !strings.Contains(endpoint, "://") {
		endpoint = "http://" + endpoint
	}
	parsed, err := url.Parse(endpoint)
	if err != nil {
		return nil, fmt.Errorf("frontend: endpoint %q is not a URL: %w", endpoint, err)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("frontend: endpoint %q names no host", endpoint)
	}
	return parsed, nil
}

// URL returns the address to forward to, or ErrNoFrontend when none is known.
func (r *Resolver) URL() (*url.URL, error) {
	if r.fixed != nil {
		return r.fixed, nil
	}

	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.current == nil {
		return nil, ErrNoFrontend
	}
	return r.current, nil
}

// Refresh runs discovery once.
//
// A failed lookup leaves the address that is already loaded in place. An API
// server that is unreachable, or a graph that is mid-redeploy, must not stop
// traffic reaching a frontend that is answering perfectly well.
func (r *Resolver) Refresh(ctx context.Context) error {
	if r.fixed != nil {
		return nil
	}

	found, err := r.discover(ctx)
	if err != nil {
		return err
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	if r.current == nil || r.current.String() != found.String() {
		slog.Info("frontend: forwarding to", "url", found.String())
	}
	r.current = found
	return nil
}

// Run refreshes until the context ends. It returns as soon as the address is
// configured rather than discovered, so a caller can start it unconditionally.
func (r *Resolver) Run(ctx context.Context) {
	if r.fixed != nil {
		return
	}

	ticker := time.NewTicker(r.interval)
	defer ticker.Stop()
	for {
		if err := r.Refresh(ctx); err != nil && ctx.Err() == nil {
			slog.Warn("frontend: discovery failed, keeping the last known address", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// discover finds the one Service selecting Dynamo's frontend component.
//
// Two frontends is an error and not a guess: this hop cannot tell which of them
// serves the model a request names, and picking one would send traffic
// somewhere the caller did not ask for.
func (r *Resolver) discover(ctx context.Context) (*url.URL, error) {
	services, err := r.services.List(ctx, metav1.ListOptions{})
	if err != nil {
		return nil, fmt.Errorf("look for the inference frontend in %s: %w", r.namespace, err)
	}

	var found []corev1.Service
	for _, service := range services.Items {
		if r.namespace != "" && service.Namespace != r.namespace {
			continue
		}
		if service.Spec.Selector[ComponentLabel] == componentFrontend {
			found = append(found, service)
		}
	}

	switch len(found) {
	case 0:
		return nil, fmt.Errorf(
			"%w: no service in %s selects %s=%s; set the endpoint override if this cluster "+
				"fronts its models with something else",
			ErrNoFrontend, r.namespace, ComponentLabel, componentFrontend)
	case 1:
		service := found[0]
		address := fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
			service.Name, service.Namespace, servicePort(service))
		return url.Parse(address)
	default:
		names := make([]string, 0, len(found))
		for _, service := range found {
			names = append(names, service.Name)
		}
		sort.Strings(names)
		return nil, fmt.Errorf(
			"%s holds %d inference frontends (%s) and nothing here can tell which one a request "+
				"belongs to; set the endpoint override",
			r.namespace, len(found), strings.Join(names, ", "))
	}
}

// servicePort picks the HTTP port: the one named `http`, else the only one
// declared, else Dynamo's default.
func servicePort(service corev1.Service) int32 {
	for _, declared := range service.Spec.Ports {
		if declared.Name == "http" {
			return declared.Port
		}
	}
	if len(service.Spec.Ports) == 1 {
		return service.Spec.Ports[0].Port
	}
	return defaultPort
}
