// Command wingsmith-host runs Wingsmith on a bare Linux GPU host with no Kubernetes: the host
// enrols as a cluster of kind host with one node, keeps an outbound sync session to the hub and
// reports its machine and accelerators. It serves nothing yet (P2.4).
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

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/checkpoint"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/host"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

// version is stamped at build time with -ldflags.
var version = "dev"

const usage = `usage:
  wingsmith-host enrol <token> --plane <wss://hub-url> --cluster <name> [--config path] [--token-file path]
  wingsmith-host run [--config path]
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
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, token, err := host.LoadConfig(*configPath)
	if err != nil {
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

	observer := observe.New(&observe.Observer{
		ClusterID:       cfg.Cluster,
		OperatorVersion: version,
		// Nothing is applied on a host until P2.4. Saying so is what keeps the plane from
		// reading an idle host as one that has not acted yet.
		ReadOnly:   true,
		Discoverer: &host.Discoverer{Devices: deviceSource(), Labels: cfg.Labels, Pool: cfg.Pool, Log: log},
		Interval:   *interval,
		Log:        log,
	})
	cursor := checkpoint.NewFileStore(filepath.Join(cfg.StateDir, "cursor.json"))
	client := syncpkg.NewClient(syncpkg.Options{
		HubURL:          cfg.Plane,
		ClusterID:       cfg.Cluster,
		Token:           token,
		OperatorVersion: version,
		AgentKind:       "host",
		TLSConfig:       tlsCfg,
		Handler:         observe.WithObserver(syncpkg.HandlerFunc(ignoreDemand(log)), observer),
		Checkpointer:    checkpoint.Adapt(cursor),
		Logger:          log,
	})
	observer.Bind(client)

	errc := make(chan error, 2)
	go func() { errc <- client.Run(ctx) }()
	go func() { errc <- observer.Run(ctx) }()
	log.Info("host started", "hub", cfg.Plane)
	err = <-errc
	stop()
	<-errc
	if errors.Is(err, context.Canceled) {
		err = nil
	}
	log.Info("host stopped")
	return err
}

// ignoreDemand drops whatever the hub declares. The plane refuses to place onto a host until the
// serving backend exists, so anything arriving here is a plane older or newer than this binary.
func ignoreDemand(log *slog.Logger) func(context.Context, *syncpkg.Envelope) error {
	return func(_ context.Context, e *syncpkg.Envelope) error {
		log.Warn("host serves nothing yet; ignoring declared state", "type", e.Type)
		return nil
	}
}
