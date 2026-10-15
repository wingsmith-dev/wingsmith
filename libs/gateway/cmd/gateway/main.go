// Command gateway is hangar's data plane: it accepts OpenAI-compatible requests
// and forwards each to a cluster currently serving the model it names.
package main

import (
	"context"
	"crypto/tls"
	"errors"
	"flag"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/gateway/internal/serving"
	"github.com/hangar-dev/hangar/libs/gateway/internal/tenancy"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

func main() {
	listenAddr := flag.String("listen", ":8080", "HTTP listen address")
	planeURL := flag.String("plane-url", "", "hangar plane base URL (HANGAR_PLANE_URL env fallback)")
	resync := flag.Duration("resync", 30*time.Second, "how often to re-read the routing table")
	credentialsPath := flag.String("credentials", "", "path to the mounted credential file (HANGAR_GATEWAY_CREDENTIALS env fallback)")
	// Both of the settings below default to something that works and are read
	// from the environment as well as the command line, and neither is ever
	// fatal by its absence. That is not politeness, it is the deploy path:
	// `just deploy plane` runs `kubectl apply -k infra/`, and infra/ carries
	// this component's manifest — so a manifest that names a flag the running
	// image does not know reaches the old binary first and flag.Parse exits 2,
	// which is a crashloop of the data plane caused by deploying the control
	// plane. An unknown environment variable is inert; an unknown flag is not.
	metricsAddr := flag.String("metrics-listen", "", "address for the Prometheus /metrics listener, empty for the default (HANGAR_GATEWAY_METRICS_LISTEN env fallback, \"off\" to disable)")
	probeToken := flag.String("probe-token", "", "token the conformance runner presents to mark its traffic as probe rather than live; accepts a comma-separated pair while rotating (HANGAR_GATEWAY_PROBE_TOKEN env fallback)")
	tunnelAddr := flag.String("tunnel-listen", "", "address the cluster agents dial, empty for the default (HANGAR_GATEWAY_TUNNEL_LISTEN env fallback, \"off\" to disable)")
	tunnelCert := flag.String("tunnel-cert", "", "PEM certificate for the tunnel listener (HANGAR_GATEWAY_TUNNEL_TLS_CERT env fallback); without it the listener is plaintext and TLS must be terminated in front of it")
	tunnelKey := flag.String("tunnel-key", "", "PEM private key for the tunnel listener (HANGAR_GATEWAY_TUNNEL_TLS_KEY env fallback)")
	gatewayID := flag.String("gateway-id", "", "this replica's stable identity, reported to dialling agents; empty takes the hostname (HANGAR_GATEWAY_ID env fallback)")
	headerTimeout := flag.Duration("response-header-timeout", 0, "how long a cluster may hold a request without sending a response header, zero for the built-in default (HANGAR_RESPONSE_HEADER_TIMEOUT env fallback)")
	replicas := flag.Int("gateways", 0, "how many gateway replicas are running, reported to dialling agents so they know how many tunnels to hold; must match the manifest's replica count (HANGAR_GATEWAY_REPLICAS env fallback)")
	logUpstreamBodies := flag.Bool("log-upstream-bodies", false, "write discarded upstream error bodies to the log whole; they can quote the caller's request, so this is for debugging a cluster carrying no customer traffic and is off in production (HANGAR_GATEWAY_LOG_UPSTREAM_BODIES=true env fallback)")
	flag.Parse()

	if *planeURL == "" {
		*planeURL = os.Getenv("HANGAR_PLANE_URL")
	}
	if *credentialsPath == "" {
		*credentialsPath = os.Getenv("HANGAR_GATEWAY_CREDENTIALS")
	}
	if *metricsAddr == "" {
		*metricsAddr = os.Getenv("HANGAR_GATEWAY_METRICS_LISTEN")
	}
	if *metricsAddr == "" {
		*metricsAddr = defaultMetricsAddr
	}
	if *probeToken == "" {
		*probeToken = os.Getenv("HANGAR_GATEWAY_PROBE_TOKEN")
	}
	if *tunnelAddr == "" {
		*tunnelAddr = os.Getenv("HANGAR_GATEWAY_TUNNEL_LISTEN")
	}
	if *tunnelAddr == "" {
		*tunnelAddr = defaultTunnelAddr
	}
	if *tunnelCert == "" {
		*tunnelCert = os.Getenv("HANGAR_GATEWAY_TUNNEL_TLS_CERT")
	}
	if *tunnelKey == "" {
		*tunnelKey = os.Getenv("HANGAR_GATEWAY_TUNNEL_TLS_KEY")
	}
	if *gatewayID == "" {
		*gatewayID = os.Getenv("HANGAR_GATEWAY_ID")
	}
	if *gatewayID == "" {
		// The pod name, which in a StatefulSet is the replica's identity and is
		// stable across restarts. A host that will not name itself leaves this
		// empty, and an agent then treats the gateway as a single replica — a
		// degradation and not a failure, which is the right trade for a name
		// whose only job is to tell replicas apart.
		if name, err := os.Hostname(); err == nil {
			*gatewayID = name
		}
	}
	if *replicas == 0 {
		*replicas = envInt("HANGAR_GATEWAY_REPLICAS")
	}
	if !*logUpstreamBodies {
		*logUpstreamBodies = envBool("HANGAR_GATEWAY_LOG_UPSTREAM_BODIES")
	}
	if *logUpstreamBodies {
		slog.Warn("gateway: logging upstream error bodies whole; they can carry request content, never run this with customer traffic")
	}
	// The same variable name the agent reads, and it has to stay that way. Both
	// ends of the tunnel arm a response-header timer from this number and only
	// the smaller of the two is ever observed, so raising it here alone buys the
	// caller nothing — see tunnel.InnerHopTimeout. Setting it in one place and
	// not the other is the one way left to get this wrong, which is why the
	// value is logged at startup on both ends.
	if *headerTimeout == 0 {
		*headerTimeout = envDuration("HANGAR_RESPONSE_HEADER_TIMEOUT")
	}
	if *headerTimeout <= 0 {
		*headerTimeout = tunnel.DefaultResponseHeaderTimeout
	}
	slog.Info("gateway: response header timeout", "timeout", *headerTimeout,
		"agent_must_be_at_least", tunnel.InnerHopTimeout(*headerTimeout))
	// Comma-separated names this deployment's infrastructure goes by, stripped from any engine
	// message quoted back to a caller on top of the built-in stack names. Environment only, for the
	// reason the comment above the flags gives.
	if names := os.Getenv("HANGAR_GATEWAY_REDACT_NAMES"); names != "" {
		serving.RedactNames(strings.Split(names, ","))
	}
	// The gateway's own service principal. The plane accepts it on the three
	// routes this process reads and nowhere else, so it carries no admin or
	// operator rights on the plane API.
	planeToken := os.Getenv("HANGAR_GATEWAY_SERVICE_TOKEN")
	// HANGAR_GATEWAY_PUBLIC_LISTEN is deliberately read only from the
	// environment, never a flag — the reason is the same one metricsAddr and
	// tunnelAddr are read from both: a manifest naming a flag this binary
	// does not know reaches flag.Parse before main runs and exits 2, which is
	// a crashloop of the data plane. An unset variable is inert, which is
	// what "off" needs to mean for a listener defaulting closed. Empty means
	// off; an operator that accepts direct tenants turns it on.
	publicAddr := os.Getenv("HANGAR_GATEWAY_PUBLIC_LISTEN")

	if *planeURL == "" || planeToken == "" {
		if planeToken == "" && os.Getenv("HANGAR_OPERATOR_TOKEN") != "" {
			slog.Error("gateway: HANGAR_OPERATOR_TOKEN is no longer read; set HANGAR_GATEWAY_SERVICE_TOKEN to the gateway's service token")
		}
		slog.Error("gateway: -plane-url and HANGAR_GATEWAY_SERVICE_TOKEN are both required")
		os.Exit(1)
	}

	credentials, err := serving.LoadCredentials(*credentialsPath)
	if err != nil {
		slog.Error("gateway: could not read credentials", "err", err)
		os.Exit(1)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	recorder := metrics.New()
	planeClient := plane.New(*planeURL, planeToken, 5*time.Second)

	// The tunnel pool and its listener, or neither. Assigned through an
	// interface variable rather than passed directly, because a (*tunnel.Pool)(nil)
	// stored in an interface is not a nil interface — a gateway with tunnels
	// disabled would then ask a nil pool for coverage on every request.
	var tunnels serving.Tunnels
	var reach routing.Reachability
	pool, listener, err := newTunnels(*tunnelAddr, *gatewayID, *replicas, *headerTimeout, planeClient, recorder)
	if err != nil {
		slog.Error("gateway: could not set up the tunnel listener", "err", err)
		os.Exit(1)
	}
	if pool != nil {
		tunnels, reach = pool, pool
	}

	routerOpts := []routing.Option{routing.WithObserver(recorder)}
	if reach != nil {
		routerOpts = append(routerOpts, routing.WithReachability(reach))
	}
	router := routing.New(
		planeClient,
		func(target routing.Target) http.Handler {
			return serving.NewClusterHandler(serving.ClusterConfig{
				ClusterID:             target.ClusterID,
				ClusterName:           target.ClusterName,
				Headers:               credentials.Headers(target.CredentialID),
				URL:                   target.URL,
				Tunnels:               tunnels,
				ResponseHeaderTimeout: *headerTimeout,
			})
		},
		routerOpts...,
	)

	// The first load must succeed. Coming up with an empty table would answer
	// every request with "model not found", which reads as an outage of the
	// models rather than as the misconfiguration it is.
	if err := router.Load(ctx); err != nil {
		slog.Error("gateway: could not load the routing table", "err", err)
		os.Exit(1)
	}
	go router.RunResync(ctx, *resync)

	// Same failure posture as the routing table, and the same reason: coming
	// up with nothing loaded would mean the tenant stages have never seen a
	// tenant at all, and the internal listener refuses an endpoint whose tenant
	// it cannot find enabled.
	tenants := tenancy.New(planeClient)
	if err := tenants.Load(ctx); err != nil {
		slog.Error("gateway: could not load the tenant table", "err", err)
		os.Exit(1)
	}
	go tenants.RunResync(ctx, *resync)

	// A second listener, on its own port. The request path answers callers and
	// nothing else: a scrape endpoint on the same mux is one loose NetworkPolicy
	// away from being reachable by anyone who can reach the API, and it is the
	// one surface that has to stay answerable when the token guard, the plane
	// and the table are all unavailable.
	metricsSrv := serveMetrics(*metricsAddr, recorder)

	// A third listener, and the one that must not be gated on readiness: a
	// replica is ready because it holds a tunnel, so an agent that could only
	// dial a ready replica could never make one ready. It is on its own port,
	// started before the request path, and nothing in it consults readiness — the
	// Service in front of it is a headless one publishing not-ready addresses.
	if listener != nil {
		serveTunnels(ctx, listener, *tunnelAddr, *tunnelCert, *tunnelKey, recorder, stop)
	}

	readiness := serving.NewReadiness(router, recorder)
	go readiness.RunCoverage(ctx, 15*time.Second)

	limiter := serving.NewLimiter(serving.TenantLimits{
		MaxInFlight: envIntDefault("HANGAR_GATEWAY_TENANT_MAX_IN_FLIGHT", serving.DefaultTenantMaxInFlight),
		RPS:         envFloat("HANGAR_GATEWAY_TENANT_RPS"),
		Burst:       envInt("HANGAR_GATEWAY_TENANT_BURST"),
	})
	allowNoAuth := envBool("HANGAR_GATEWAY_PUBLIC_ALLOW_NO_AUTH")

	srv := &http.Server{
		Addr: *listenAddr,
		Handler: serving.New(serving.Config{
			Router:  router,
			Metrics: recorder,
			Probes:  serving.NewProbeMarker(*probeToken),
			Ready:   readiness,
			Tenants: tenants,
			Limiter: limiter,
			// Off unless asked for: zero retention of request content.
			LogUpstreamBodies: *logUpstreamBodies,
			// The ClusterIP port with no ingress (docs/decisions/0016). The
			// operator's own edge dials this one and is trusted by having
			// reached it, which is only true behind a NetworkPolicy that
			// admits the edge alone (libs/gateway/deploy/networkpolicy.yaml).
			Zone: serving.ZoneInternal,
		}),
		// No WriteTimeout: it is absolute rather than idle-based, so any value
		// would sever long generations and SSE streams mid-token.
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		slog.Info("gateway: listening", "addr", *listenAddr, "endpoints", len(router.Names()), "revision", router.Revision())
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("gateway: serve failed", "err", err)
			stop()
		}
	}()

	// The public listener, off unless HANGAR_GATEWAY_PUBLIC_LISTEN names an
	// address. A caller here identifies itself by the `Host` it dials rather
	// than by which port it reached — the internal listener's own proof — so
	// every request run through it takes the tenant and key stages the
	// internal listener's Zone skips (docs/decisions/0016).
	var publicSrv *http.Server
	if publicAddr != "" {
		publicSrv = &http.Server{
			Addr: publicAddr,
			Handler: serving.New(serving.Config{
				Router:  router,
				Metrics: recorder,
				Probes:  serving.NewProbeMarker(*probeToken),
				Ready:   readiness,
				Tenants: tenants,
				Limiter: limiter,
				Zone:    serving.ZonePublic,
				// A tenant declaring `auth: none` is refused here unless the
				// operator opts in.
				AllowUnauthenticatedPublic: allowNoAuth,
			}),
			ReadHeaderTimeout: 10 * time.Second,
			IdleTimeout:       120 * time.Second,
		}
		go func() {
			slog.Info("gateway: public listening", "addr", publicAddr)
			if err := publicSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				slog.Error("gateway: public serve failed", "err", err)
				stop()
			}
		}()
	} else {
		slog.Info("gateway: public listener disabled, set HANGAR_GATEWAY_PUBLIC_LISTEN to enable it")
	}

	<-ctx.Done()
	slog.Info("gateway: shutting down")

	// Not-ready first, and before anything is closed. Endpoint removal takes a
	// few hundred milliseconds to reach every kube-proxy, and the requests that
	// arrive inside those milliseconds are precisely the ones a rollout severs.
	readiness.Drain()

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("gateway: shutdown failed", "err", err)
	}
	if publicSrv != nil {
		if err := publicSrv.Shutdown(shutdownCtx); err != nil {
			slog.Error("gateway: public shutdown failed", "err", err)
		}
	}
	// After the request path and not before it: the in-flight requests being
	// drained above are being carried by these tunnels, and a GOAWAY sent first
	// would cut the generations that Shutdown is waiting for.
	if listener != nil {
		if err := listener.Shutdown(shutdownCtx); err != nil {
			slog.Error("gateway: tunnel drain failed", "err", err)
		}
	}
	if metricsSrv != nil {
		if err := metricsSrv.Shutdown(shutdownCtx); err != nil {
			slog.Error("gateway: metrics shutdown failed", "err", err)
		}
	}
}

// defaultTunnelAddr is where cluster agents dial when nothing says otherwise.
const defaultTunnelAddr = ":8443"

// tunnelCertReloadEvery is how often the tunnel certificate is re-read from disk.
//
// Ten minutes against a renewal window of 30 days. There is no benefit to being
// quicker — the old certificate stays valid for the whole window — and a slow
// poll keeps a mid-swap read rare rather than merely survivable.
const tunnelCertReloadEvery = 10 * time.Minute

// newTunnels builds the pool and the listener, or nothing when tunnels are
// disabled.
//
// The verifier is the plane client, and it is asked once per connection. That is
// the property to keep: a plane that is down means existing tunnels go on
// serving, and only a new dial fails — and even a new dial succeeds while the
// listener's cache still holds an answer for its token.
// The replica count reported to agents is a number this process is told, not one
// it discovers. Discovering it would mean a Kubernetes client, and the gateway
// not having one is a property worth more than the convenience — so scaling the
// gateway is two numbers in one file, `replicas` and HANGAR_GATEWAY_REPLICAS in
// infra/80-gateway.yaml, and changing either restarts the pods, which is what
// makes every agent redial and read the new number.
//
// Setting it too low is the failure that matters: agents stop dialling before
// they have covered every replica, and the replicas they missed answer nothing
// for those clusters while looking healthy. Too high only costs the agents some
// redials that find no home. It is logged at startup for exactly that reason.
func newTunnels(addr, id string, replicas int, headerTimeout time.Duration, verifier tunnel.Verifier, recorder *metrics.Recorder) (*tunnel.Pool, *tunnel.Listener, error) {
	if addr == "" || addr == "off" {
		slog.Warn("gateway: tunnel listener disabled, only clusters with a published serving address are reachable")
		return nil, nil, nil
	}
	pool, err := tunnel.NewPool(tunnel.PoolConfig{Observer: recorder, ResponseHeaderTimeout: headerTimeout})
	if err != nil {
		return nil, nil, err
	}
	listener, err := tunnel.NewListener(tunnel.ListenerConfig{
		Pool:      pool,
		GatewayID: id,
		Gateways:  replicas,
		Verifier:  verifier,
		Dials:     recorder,
	})
	if err != nil {
		return nil, nil, err
	}
	if replicas < 1 {
		slog.Warn("gateway: HANGAR_GATEWAY_REPLICAS is unset, telling agents there is one replica; "+
			"if there is more than one, the others hold no tunnels and serve nothing", "gateway_id", id)
	} else {
		slog.Info("gateway: tunnel identity", "gateway_id", id, "replicas", replicas)
	}
	return pool, listener, nil
}

// envInt reads a positive integer from the environment, treating anything
// unparseable as absent. Nothing here is worth exiting over: the caller's
// default is safe and a gateway that refused to start over a malformed number
// would be an outage of the data plane caused by a typo in a ConfigMap.
func envInt(key string) int {
	raw := os.Getenv(key)
	if raw == "" {
		return 0
	}
	n, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil || n < 0 {
		slog.Warn("gateway: ignoring unreadable environment value", "key", key, "value", raw)
		return 0
	}
	return n
}

// envIntDefault reads a non-negative integer from the environment, returning
// def when the variable is unset or unreadable. Unlike envInt, an explicit 0 is
// kept, so a default can be switched off.
func envIntDefault(key string, def int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		slog.Warn("gateway: ignoring unreadable environment value", "key", key, "value", raw)
		return def
	}
	return n
}

// envFloat reads a non-negative number from the environment, treating anything
// unparseable as absent.
func envFloat(key string) float64 {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return 0
	}
	f, err := strconv.ParseFloat(raw, 64)
	if err != nil || f < 0 {
		slog.Warn("gateway: ignoring unreadable environment value", "key", key, "value", raw)
		return 0
	}
	return f
}

// envDuration reads a duration from the environment, treating anything
// unparseable as absent, for the same reason envInt does: a malformed value in a
// ConfigMap must not be an outage of the data plane.
func envDuration(key string) time.Duration {
	raw := os.Getenv(key)
	if raw == "" {
		return 0
	}
	d, err := time.ParseDuration(strings.TrimSpace(raw))
	if err != nil || d < 0 {
		slog.Warn("gateway: ignoring unreadable environment value", "key", key, "value", raw)
		return 0
	}
	return d
}

// envBool reads a boolean from the environment. Anything strconv.ParseBool
// cannot read is false, which for the one switch read this way is the safe side.
func envBool(key string) bool {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return false
	}
	on, err := strconv.ParseBool(raw)
	if err != nil {
		slog.Warn("gateway: ignoring unreadable environment value", "key", key, "value", raw)
		return false
	}
	return on
}

// serveTunnels binds the tunnel port and accepts dials until shutdown.
//
// Failing to bind is fatal by way of stop: a gateway that cannot be dialled by
// any agent is a gateway that can serve nothing, and carrying on would mean
// reporting unready forever while looking healthy in the logs.
func serveTunnels(ctx context.Context, listener *tunnel.Listener, addr, certPath, keyPath string, recorder *metrics.Recorder, stop func()) {
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		slog.Error("gateway: could not bind the tunnel listener", "addr", addr, "err", err)
		stop()
		return
	}

	switch {
	case (certPath == "") != (keyPath == ""):
		// Half a TLS configuration is a misconfiguration, and the failure it
		// produces if it is allowed to fall through to plaintext is an agent
		// presenting its enrolment token over a cleartext connection.
		slog.Error("gateway: the tunnel listener needs both a certificate and a key, or neither")
		_ = ln.Close()
		stop()
		return
	case certPath != "":
		keypair, err := serving.NewKeypair(certPath, keyPath)
		if err != nil {
			slog.Error("gateway: could not load the tunnel certificate", "err", err)
			_ = ln.Close()
			stop()
			return
		}
		slog.Info("gateway: tunnel certificate loaded", "expires", keypair.NotAfter())
		recorder.TunnelCertificateExpiry(keypair.NotAfter())
		// cert-manager renews 30 days out and the kubelet lands the new bytes
		// within about a minute, so the poll only has to be far shorter than
		// the window, not prompt.
		go keypair.Watch(ctx, tunnelCertReloadEvery, recorder.TunnelCertificateExpiry)
		// NextProtos is deliberately absent. The agent negotiates nothing:
		// the handshake on this connection is hangar's own and HTTP/2 starts
		// after it, so advertising h2 here would invite a TLS stack to hand the
		// socket to an HTTP/2 client that expects to be the one dialling.
		ln = tls.NewListener(ln, &tls.Config{GetCertificate: keypair.GetCertificate, MinVersion: tls.VersionTLS12})
	default:
		slog.Warn("gateway: tunnel listener is plaintext, TLS must be terminated in front of it", "addr", addr)
	}

	go func() {
		slog.Info("gateway: tunnel listening", "addr", addr)
		if err := listener.Serve(ctx, ln); err != nil {
			slog.Error("gateway: tunnel listener stopped", "err", err)
			stop()
		}
	}()
}

// defaultMetricsAddr is where the scrape looks when nothing says otherwise, so
// that a gateway rolled out with no manifest change is already exporting.
const defaultMetricsAddr = ":9090"

// serveMetrics starts the exposition listener.
//
// Every failure in here is logged and survived. Losing the ability to describe
// the data plane is a monitoring incident; refusing to run the data plane
// because it cannot be described would be an outage, and the numbers this
// listener exists to publish are the ones that would have to report it.
func serveMetrics(addr string, recorder *metrics.Recorder) *http.Server {
	if addr == "" || addr == "off" {
		slog.Warn("gateway: metrics listener disabled, this gateway reports no failure rate")
		return nil
	}

	mux := http.NewServeMux()
	mux.Handle("GET /metrics", recorder.Handler())
	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		slog.Info("gateway: metrics listening", "addr", addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			slog.Error("gateway: metrics listener stopped, serving traffic without it", "err", err)
		}
	}()
	return srv
}
