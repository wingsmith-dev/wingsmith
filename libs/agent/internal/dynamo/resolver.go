// Package dynamo is the agent's local half: it finds this cluster's
// OpenAI-compatible Dynamo frontend Services and forwards tunnelled requests to
// them.
//
// It is the cluster-local end of the data path. A request arrives over a connection
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

// DeploymentIDLabel is what tells one deployment's frontend from another's, and
// it is ours rather than Dynamo's.
//
// The operator stamps it on a component's pod template, and Dynamo copies a
// component's pod-template labels onto the Service it derives from that
// component — verified on cluster-site-c, where the worker Service
// carries the deployment id and the project the operator set on the worker's
// pods. That copy is the whole mechanism: without it there is nothing
// on a Service to key on, and this hop would be back to asking each frontend
// what models it serves.
//
// A frontend Service with no such label is the cluster's shared frontend — the
// one `cluster-setup` installs, which belongs to the cluster and not to any
// deployment — and it is the fallback for everything this map cannot answer.
const DeploymentIDLabel = "hangar.dev/deployment-id"

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

// ErrNoDeploymentFrontend means this cluster is serving, but not this
// deployment: frontends were discovered, none of them is the one the request
// names, and there is no shared frontend to fall back to.
//
// Separate from ErrNoFrontend because the two facts are different and a reader
// has to tell them apart. "This cluster serves nothing" is a cluster that has
// not come up, and every model on it is affected. "This model's frontend has not
// come up" is one deployment mid-rollout while everything beside it answers
// normally — the ordinary shape of the seconds after a deployment is created,
// and the one case where retrying the same request shortly is exactly right.
var ErrNoDeploymentFrontend = errors.New("no inference frontend for this deployment in this cluster")

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

// Resolver yields the current frontend addresses.
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
//
// It holds a set rather than one address. A cluster used to run exactly one
// frontend and the binary behind it had to be able to tokenize every model on
// the cluster, which made a frontier model's runtime a fleet-wide lowest common
// denominator; the answer is a frontend per model, and from here that is a set
// of Services keyed by the deployment each one belongs to. A cluster running
// only the shared frontend has a set of one and behaves exactly as it did.
type Resolver struct {
	services  Services
	namespace string
	interval  time.Duration

	// fixed is the configured override. Non-nil means URL never blocks on
	// anything and Run has nothing to do.
	fixed *url.URL

	mu      sync.RWMutex
	current frontends
}

// frontends is one discovery pass: the cluster's shared frontend, and the
// frontend each deployment renders for itself.
//
// Kept as one value and replaced wholesale, so a reader never sees half of a
// refresh — the shared frontend from the new pass beside a deployment map from
// the old one is a state the cluster was never in.
type frontends struct {
	// shared is the frontend no deployment owns, or nil when this cluster
	// renders none. It is the fallback and, on a cluster that has not adopted
	// per-model frontends, it is the whole set.
	shared *url.URL
	// byDeployment is one frontend per deployment that renders its own.
	byDeployment map[string]*url.URL
}

func (f frontends) empty() bool { return f.shared == nil && len(f.byDeployment) == 0 }

// describe renders the set for a log line, sorted so that two identical sets
// never read as a change.
func (f frontends) describe() string {
	parts := make([]string, 0, len(f.byDeployment)+1)
	if f.shared != nil {
		parts = append(parts, "shared="+f.shared.String())
	}
	for deployment, address := range f.byDeployment {
		parts = append(parts, deployment+"="+address.String())
	}
	sort.Strings(parts)
	return strings.Join(parts, " ")
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

// URL returns the address for a request that names no deployment, or
// ErrNoFrontend when there is none.
//
// That is the cluster's shared frontend and never a per-deployment one. Picking
// one of those would be guessing which model a request is for, and the frontend
// is the last hop that could notice it had guessed wrong — it would answer with
// a model-not-found for a model that is running perfectly well two Services
// away.
func (r *Resolver) URL() (*url.URL, error) {
	if r.fixed != nil {
		return r.fixed, nil
	}

	r.mu.RLock()
	defer r.mu.RUnlock()
	if r.current.shared == nil {
		return nil, ErrNoFrontend
	}
	return r.current.shared, nil
}

// URLFor returns the frontend serving one deployment.
//
// The fallback to the shared frontend is not a courtesy, it is the migration:
// every cluster runs one shared frontend today and none of them renders a
// per-deployment one yet, so every request falls through this path and reaches
// exactly the address it reached before. A cluster that has adopted per-model
// frontends answers from the map instead, and one that is halfway through does
// both, per deployment, with no flag anywhere saying which.
//
// An empty id is the same question as URL: the gateway had no answer, so
// neither does this.
func (r *Resolver) URLFor(deploymentID string) (*url.URL, error) {
	if r.fixed != nil {
		return r.fixed, nil
	}
	if deploymentID == "" {
		return r.URL()
	}

	r.mu.RLock()
	defer r.mu.RUnlock()
	if address, ok := r.current.byDeployment[deploymentID]; ok {
		return address, nil
	}
	if r.current.shared != nil {
		return r.current.shared, nil
	}
	if r.current.empty() {
		return nil, ErrNoFrontend
	}
	return nil, fmt.Errorf("%w: %s", ErrNoDeploymentFrontend, deploymentID)
}

// Refresh runs discovery once.
//
// A failed lookup leaves the set that is already loaded in place. An API
// server that is unreachable, or a graph that is mid-redeploy, must not stop
// traffic reaching frontends that are answering perfectly well.
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
	if before, after := r.current.describe(), found.describe(); before != after {
		slog.Info("frontend: forwarding to", "frontends", after)
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
			slog.Warn("frontend: discovery failed, keeping the last known addresses", "err", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// discover finds every Service selecting Dynamo's frontend component and keys
// them by the deployment each one belongs to.
//
// Two frontends used to be an error and not a guess, and the refusal was right
// while nothing on a Service said which model it fronted. What changed is that
// something does: the operator renders a frontend into the same graph as its
// worker and the ownership labels ride onto the Service, so plurality is now
// answerable rather than ambiguous. What is still an error is two Services
// claiming the same key — two shared frontends, or two frontends for one
// deployment — because that is the original ambiguity with none of the new
// information, and picking one would send traffic somewhere the caller did not
// ask for.
//
// Finding nothing is still an error rather than an empty set, so that a cluster
// whose API server has gone quiet keeps forwarding to the frontends it last
// saw. Refresh is what holds onto them; returning an empty set here would
// discard them instead.
func (r *Resolver) discover(ctx context.Context) (frontends, error) {
	services, err := r.services.List(ctx, metav1.ListOptions{})
	if err != nil {
		return frontends{}, fmt.Errorf("look for the inference frontend in %s: %w", r.namespace, err)
	}

	found := map[string][]corev1.Service{}
	for _, service := range services.Items {
		if r.namespace != "" && service.Namespace != r.namespace {
			continue
		}
		if service.Spec.Selector[ComponentLabel] != componentFrontend {
			continue
		}
		key := deploymentOf(service)
		found[key] = append(found[key], service)
	}

	if len(found) == 0 {
		return frontends{}, fmt.Errorf(
			"%w: no service in %s selects %s=%s; set the endpoint override if this cluster "+
				"fronts its models with something else",
			ErrNoFrontend, r.namespace, ComponentLabel, componentFrontend)
	}

	discovered := frontends{byDeployment: map[string]*url.URL{}}
	for key, services := range found {
		if len(services) > 1 {
			return frontends{}, ambiguous(r.namespace, key, services)
		}
		address, err := serviceURL(services[0])
		if err != nil {
			return frontends{}, err
		}
		if key == "" {
			discovered.shared = address
			continue
		}
		discovered.byDeployment[key] = address
	}
	return discovered, nil
}

// deploymentOf reads the routing key off a frontend Service, or empty for the
// cluster's shared frontend.
//
// Both the Service's own labels and its selector are consulted, and they are
// not two guesses: the labels are where Dynamo was observed to copy a
// component's pod-template labels, and the selector is where the same copy
// would put them if it were derived from the pod template's selector instead.
// Either placement means the same thing — this Service fronts that deployment's
// pods and nothing else's — and reading only one of them and being wrong would
// send every request to the shared frontend with nothing anywhere reporting a
// failure. A Service can carry no other deployment's id in either place, so
// there is no false answer to reach.
func deploymentOf(service corev1.Service) string {
	if id := service.Labels[DeploymentIDLabel]; id != "" {
		return id
	}
	return service.Spec.Selector[DeploymentIDLabel]
}

// ambiguous is the refusal for two Services claiming one key. It names them,
// because the names are the only part a person can act on.
func ambiguous(namespace, deployment string, services []corev1.Service) error {
	names := make([]string, 0, len(services))
	for _, service := range services {
		names = append(names, service.Name)
	}
	sort.Strings(names)

	if deployment == "" {
		return fmt.Errorf(
			"%s holds %d shared inference frontends (%s) and nothing here can tell which one a "+
				"request belongs to; a frontend with no %s label is this cluster's fallback and "+
				"there can be only one, so label the others or set the endpoint override",
			namespace, len(services), strings.Join(names, ", "), DeploymentIDLabel)
	}
	return fmt.Errorf(
		"%s holds %d inference frontends (%s) labelled %s=%s and nothing here can tell which one "+
			"a request belongs to; set the endpoint override",
		namespace, len(services), strings.Join(names, ", "), DeploymentIDLabel, deployment)
}

// serviceURL is the in-cluster address of one frontend Service.
func serviceURL(service corev1.Service) (*url.URL, error) {
	return url.Parse(fmt.Sprintf("http://%s.%s.svc.cluster.local:%d",
		service.Name, service.Namespace, servicePort(service)))
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
