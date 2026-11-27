// Command wingsmith-host runs Wingsmith on a bare Linux GPU host with no Kubernetes: the host
// enrols as a cluster of kind host with one node, keeps an outbound sync session to the hub and
// reports its machine and accelerators. With serving on (P2.4) it also runs container deployments
// on the local Docker Engine and holds the data-path tunnel to the gateway: one binary, two
// connections, so the control session dropping never takes inference down with it.
//
// It lives in the operator module because it is the operator's protocol side (sync client,
// resume cursor, observation loop, device table) over a different backend, and those packages
// are internal to this module. It must not link client-go; `go list -deps` in CI checks that.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/agent/hostlink"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/checkpoint"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/host"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/host/serve"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// version is stamped at build time with -ldflags.
var version = "dev"

const usage = `usage:
  wingsmith-host enrol <token> --plane <wss://hub-url> --cluster <name> [--gateway host:port] [--config path] [--token-file path]
  wingsmith-host run [--config path] [--serve]
  wingsmith-host inventory
  wingsmith-host version
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "enrol", "enroll":
		err = enrol(os.Args[2:])
	case "run":
		err = run(os.Args[2:])
	case "inventory":
		err = inventory()
	case "version":
		fmt.Println(version)
	default:
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "wingsmith-host:", err)
		os.Exit(1)
	}
}

func enrol(args []string) error {
	fs := flag.NewFlagSet("enrol", flag.ContinueOnError)
	cfg := host.Config{}
	configPath := fs.String("config", host.DefaultConfigPath, "where to write the host config")
	fs.StringVar(&cfg.Plane, "plane", "", "the hub's wss:// URL from the plane's setup instructions")
	fs.StringVar(&cfg.Cluster, "cluster", "", "the name this host was registered under")
	fs.StringVar(&cfg.TokenPath, "token-file", host.DefaultTokenPath, "where to write the token (0600)")
	fs.StringVar(&cfg.StateDir, "state-dir", host.DefaultStateDir, "where the resume cursor and host state live")
	fs.StringVar(&cfg.CAFile, "ca-file", "", "PEM bundle to verify the hub with, instead of the system roots")
	fs.StringVar(&cfg.Gateway, "gateway", "", "the tunnel gateway host:port, needed to serve")
	insecure := fs.Bool("insecure", false, "allow a ws:// hub URL (local development only)")
	// The token is positional and comes first; flag stops at the first non-flag, so split it off.
	if len(args) == 0 || len(args[0]) == 0 || args[0][0] == '-' {
		return errors.New("enrol needs the enrolment token as its first argument")
	}
	token := args[0]
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	if err := cfg.Validate(*insecure); err != nil {
		return err
	}
	if err := host.Enrol(*configPath, cfg, token); err != nil {
		return err
	}
	fmt.Printf("enrolled as %s; start the service with: systemctl enable --now wingsmith-host\n", cfg.Cluster)
	return nil
}

func deviceSource() host.Source {
	return host.Fallback{&host.NVMLSource{Lib: host.SystemNVML()}, &host.SMISource{}}
}

func inventory() error {
	d := &host.Discoverer{Devices: deviceSource()}
	snap, err := d.Discover(context.Background())
	if err != nil {
		return err
	}
	for _, n := range snap.Nodes {
		fmt.Printf("%s ready=%v driver=%s kernel=%s\n", n.Name, n.Ready, n.DriverVersion, n.KernelVersion)
		for _, dev := range n.Devices {
			fmt.Printf("  %d %s %s %dMiB %s\n", dev.Index, dev.UUID, dev.DeviceClass, dev.MemoryTotalMB, dev.Health)
		}
	}
	return nil
}

func run(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	configPath := fs.String("config", host.DefaultConfigPath, "host config written by enrol")
	interval := fs.Duration("observe-interval", 30*time.Second, "how often to read the inventory")
	serveFlag := fs.Bool("serve", false, "run container deployments (overrides serve in the config)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, token, err := host.LoadConfig(*configPath)
	if err != nil {
		return err
	}
	if *serveFlag {
		cfg.Serve = true
	}
	if err := cfg.ValidateServing(); err != nil {
		return err
	}
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil)).With("cluster_id", cfg.Cluster, "version", version)
	slog.SetDefault(log)

	tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
	if cfg.CAFile != "" {
		pem, err := os.ReadFile(cfg.CAFile)
		if err != nil {
			return fmt.Errorf("read hub CA: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return fmt.Errorf("hub CA file %s contains no certificates", cfg.CAFile)
		}
		tlsCfg.RootCAs = pool
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return fmt.Errorf("state dir: %w", err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	discoverer := &host.Discoverer{Devices: deviceSource(), Labels: cfg.Labels, Pool: cfg.Pool, Log: log}
	if cfg.Serve {
		discoverer.Runtimes = []string{wire.RuntimeContainer}
	}
	observer := observe.New(&observe.Observer{
		ClusterID:       cfg.Cluster,
		OperatorVersion: version,
		// A supply-only host applies nothing. Saying so is what keeps the plane from reading an
		// idle host as one that has not acted yet.
		ReadOnly:   !cfg.Serve,
		Discoverer: discoverer,
		Interval:   *interval,
		Log:        log,
	})
	cursor := checkpoint.NewFileStore(filepath.Join(cfg.StateDir, "cursor.json"))
	var demand syncpkg.Handler = syncpkg.HandlerFunc(ignoreDemand(log))
	var driver *serve.Driver
	if cfg.Serve {
		alloc, err := serve.OpenAllocations(filepath.Join(cfg.StateDir, "allocations.json"))
		if err != nil {
			return err
		}
		defer func() { _ = alloc.Close() }()
		socket := cfg.DockerSocket
		if socket == "" {
			socket = serve.DefaultDockerSocket
		}
		docker := serve.NewDockerClient(socket)
		policy := serve.Policy{ImagePrefixes: cfg.AllowedImagePrefixes, TagsAllowed: cfg.ContainerImageTagsAllowed}
		driver = serve.NewDriver(&serve.Driver{
			Backend: &serve.Backend{
				Docker:      docker,
				Allocations: alloc,
				Policy:      policy,
				ClusterID:   cfg.Cluster,
				ModelRoot:   cfg.ModelRoot,
				GPUs:        healthyGPUs(discoverer),
				Supply:      observer,
				CDI:         cfg.CDI,
				Cache: &serve.Cache{
					Docker: docker, Policy: policy, Root: cfg.ModelRoot, CredentialsDir: cfg.CredentialsDir,
					Image: cfg.PrefetchImage, MaxConcurrent: cfg.MaxPrefetches,
					HighWatermark: cfg.CacheHighWatermark, LowWatermark: cfg.CacheLowWatermark, Log: log,
				},
				Log: log,
			},
			ClusterID: cfg.Cluster,
			StatePath: filepath.Join(cfg.StateDir, "desired.json"),
			Log:       log,
		})
		demand = driver
	}
	client := syncpkg.NewClient(syncpkg.Options{
		HubURL:          cfg.Plane,
		ClusterID:       cfg.Cluster,
		Token:           token,
		OperatorVersion: version,
		AgentKind:       "host",
		TLSConfig:       tlsCfg,
		Handler:         observe.WithObserver(demand, observer),
		Checkpointer:    checkpoint.Adapt(cursor),
		Logger:          log,
	})
	observer.Bind(client)

	tasks := []func(context.Context) error{client.Run, observer.Run}
	if driver != nil {
		driver.Reporter = client
		routes := hostlink.NewRoutes()
		driver.Routes = routes.Set
		link := hostlink.Config{
			ClusterID: cfg.Cluster, Gateway: cfg.Gateway, Token: token,
			CAFile: cfg.GatewayCAFile, Plaintext: cfg.GatewayPlaintext, Log: log,
		}
		tasks = append(tasks, driver.Run, func(ctx context.Context) error { return hostlink.Run(ctx, link, routes) })
	}
	errc := make(chan error, len(tasks))
	for _, task := range tasks {
		go func() { errc <- task(ctx) }()
	}
	log.Info("host started", "hub", cfg.Plane, "serving", cfg.Serve)
	err = <-errc
	stop()
	for range len(tasks) - 1 {
		<-errc
	}
	if errors.Is(err, context.Canceled) {
		err = nil
	}
	log.Info("host stopped")
	return err
}

// healthyGPUs reads the node name and its healthy accelerators for allocation.
func healthyGPUs(d *host.Discoverer) func(context.Context) (string, []string, error) {
	return func(ctx context.Context) (string, []string, error) {
		snap, err := d.Discover(ctx)
		if err != nil {
			return "", nil, err
		}
		if len(snap.Nodes) == 0 {
			return "", nil, errors.New("no node discovered")
		}
		n := snap.Nodes[0]
		var uuids []string
		for _, dev := range n.Devices {
			if dev.Health == observe.HealthHealthy {
				uuids = append(uuids, dev.UUID)
			}
		}
		return n.Name, uuids, nil
	}
}

// ignoreDemand drops whatever the hub declares to a host that has not turned serving on. The
// plane places onto a host only once it reports the container runtime, so anything arriving here
// is a plane older or newer than this binary.
func ignoreDemand(log *slog.Logger) func(context.Context, *syncpkg.Envelope) error {
	return func(_ context.Context, e *syncpkg.Envelope) error {
		log.Warn("host serves nothing yet; ignoring declared state", "type", e.Type)
		return nil
	}
}
