// Command hgrtransit measures hangar's inference request path.
//
// One tool, three environments. `synthetic` builds the path on this host — the
// real gateway and the real agent over the real tunnel, with a fake control
// plane beside it and a scheduled frontend at the end of it — and is what a
// laptop and CI run. `live` points the same generator and the same report at a
// gateway somebody has deployed. `upstream` and `plane` expose the two fakes on
// their own, for a caller assembling something this tool does not.
//
// The report is the same shape in every mode on purpose: a number from a laptop
// and a number from production are only worth having together if they are the
// same measurement.
package main

import (
	"context"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/fakeplane"
	"github.com/hangar-dev/hangar/libs/transit/internal/lab"
	"github.com/hangar-dev/hangar/libs/transit/internal/load"
	"github.com/hangar-dev/hangar/libs/transit/internal/upstream"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var err error
	switch os.Args[1] {
	case "synthetic":
		err = synthetic(ctx, os.Args[2:])
	case "live":
		err = live(ctx, os.Args[2:])
	case "upstream":
		err = frontend(ctx, os.Args[2:])
	case "plane":
		err = plane(ctx, os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "hgrtransit:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `hgrtransit — measure the gateway → tunnel → agent → frontend path

  synthetic   build the path on this host and ramp through it
  live        ramp against a deployed gateway
  upstream    serve the fake OpenAI-compatible frontend on its own
  plane       serve the fake control plane on its own

Each subcommand takes -h.
`)
}

func synthetic(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("synthetic", flag.ExitOnError)
	repo := fs.String("repo", "", "the checkout to build the gateway and agent from; empty finds it above the working directory")
	agents := fs.Int("agents", 1, "how many agent processes serve the cluster")
	conns := fs.Int("conns", 2, "tunnels each agent holds to each gateway replica")
	streams := fs.Int("streams", 0, "per-tunnel stream limit the agent advertises, 0 for the tunnel default of 256")
	ramp := fs.String("ramp", "8,32,128", "concurrency stages")
	hold := fs.Duration("hold", 15*time.Second, "how long each stage starts requests")
	drain := fs.Duration("drain", load.DefaultDrain, "how long a stage's in-flight requests get to finish before they are cut off and reported as censored")
	ttft := fs.String("ttft", "200ms", "the frontend's time to first token")
	itl := fs.String("itl", "10ms", "the frontend's inter-token delay")
	tokens := fs.String("tokens", "64", "how many tokens the frontend emits")
	prompt := fs.Int("prompt-bytes", 512, "request body padding")
	stream := fs.Bool("stream", true, "ask for SSE")
	baseline := fs.Bool("baseline", true, "measure the frontend directly first, so the path's cost is a subtraction")
	killEvery := fs.Duration("kill-agent-every", 0, "SIGKILL one agent this often, to measure a tunnel dying mid-stream")
	successRate := fs.Float64("slo-success", 0.999, "the success rate the capacity number is reported under")
	sloTTFT := fs.Duration("slo-ttft-p99", 2*time.Second, "the TTFT p99 the capacity number is reported under")
	asJSON := fs.Bool("json", false, "write the report as JSON")
	verbose := fs.Bool("v", false, "show the gateway's and agents' logs")
	if err := fs.Parse(args); err != nil {
		return err
	}

	profile, err := upstream.ParseProfile(*ttft, *itl, *tokens)
	if err != nil {
		return err
	}
	stages, err := load.ParseStages(*ramp, *hold)
	if err != nil {
		return err
	}
	for i := range stages {
		stages[i].Drain = *drain
	}
	root := *repo
	if root == "" {
		cwd, err := os.Getwd()
		if err != nil {
			return err
		}
		if root, err = lab.FindRepo(cwd); err != nil {
			return err
		}
	}

	report, err := lab.RunSynthetic(ctx, lab.Options{
		Repo:            root,
		Agents:          *agents,
		ConnsPerGateway: *conns,
		MaxStreams:      *streams,
		Upstream:        profile,
		Stages:          stages,
		Stream:          *stream,
		PromptBytes:     *prompt,
		Baseline:        *baseline,
		KillAgentEvery:  *killEvery,
		Verbose:         *verbose,
		SLO:             lab.SLO{SuccessRate: *successRate, TTFTP99: *sloTTFT},
	})
	if err != nil {
		return err
	}
	return emit(report, *asJSON)
}

// live ramps against a deployed gateway.
//
// The concurrency guard is the one piece of policy in this tool. A ramp that is
// harmless on a loopback is a shared production path here, and the default is
// deliberately small enough to be a probe rather than a load test; going past it
// is a decision somebody makes on the command line with the word out loud.
func live(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("live", flag.ExitOnError)
	url := fs.String("url", "", "the gateway's base URL")
	model := fs.String("model", "", "a model the target actually serves")
	ramp := fs.String("ramp", "1,2,4", "concurrency stages")
	hold := fs.Duration("hold", 20*time.Second, "how long each stage starts requests")
	drain := fs.Duration("drain", load.DefaultDrain, "how long a stage's in-flight requests get to finish before they are cut off and reported as censored")
	prompt := fs.Int("prompt-bytes", 512, "request body padding")
	stream := fs.Bool("stream", true, "ask for SSE")
	successRate := fs.Float64("slo-success", 0.999, "the success rate the capacity number is reported under")
	sloTTFT := fs.Duration("slo-ttft-p99", 10*time.Second, "the TTFT p99 the capacity number is reported under")
	ceiling := fs.Int("max-concurrency", 8, "refuse any stage above this, because the target is shared")
	asJSON := fs.Bool("json", false, "write the report as JSON")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *url == "" || *model == "" {
		return fmt.Errorf("live mode needs -url and -model")
	}

	stages, err := load.ParseStages(*ramp, *hold)
	if err != nil {
		return err
	}
	for i := range stages {
		stages[i].Drain = *drain
	}
	for _, stage := range stages {
		if stage.Concurrency > *ceiling {
			return fmt.Errorf("stage of %d is above -max-concurrency %d; raise it deliberately if this target can take it",
				stage.Concurrency, *ceiling)
		}
	}

	token := os.Getenv("HANGAR_GATEWAY_TOKEN")
	if token == "" {
		fmt.Fprintln(os.Stderr, "hgrtransit: HANGAR_GATEWAY_TOKEN is unset, sending no credential")
	}

	report, err := lab.RunLive(ctx, lab.LiveOptions{
		URL:         *url,
		Model:       *model,
		Token:       token,
		Stages:      stages,
		Stream:      *stream,
		PromptBytes: *prompt,
		SLO:         lab.SLO{SuccessRate: *successRate, TTFTP99: *sloTTFT},
	})
	if err != nil {
		return err
	}
	return emit(report, *asJSON)
}

func frontend(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("upstream", flag.ExitOnError)
	addr := fs.String("listen", "127.0.0.1:9900", "listen address")
	ttft := fs.String("ttft", "200ms", "time to first token")
	itl := fs.String("itl", "10ms", "inter-token delay")
	tokens := fs.String("tokens", "64", "tokens per answer")
	if err := fs.Parse(args); err != nil {
		return err
	}
	profile, err := upstream.ParseProfile(*ttft, *itl, *tokens)
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "hgrtransit: fake frontend on %s\n", *addr)
	return listen(ctx, *addr, upstream.New(profile).Handler())
}

func plane(ctx context.Context, args []string) error {
	fs := flag.NewFlagSet("plane", flag.ExitOnError)
	addr := fs.String("listen", "127.0.0.1:9901", "listen address")
	cluster := fs.String("cluster", "cluster-transit", "the cluster id every token resolves to")
	token := fs.String("token", "transit-enrolment-token", "the enrolment token accepted")
	service := fs.String("service-token", "transit-gateway-service-token", "the bearer the gateway must present (its HANGAR_GATEWAY_SERVICE_TOKEN)")
	model := fs.String("model", "transit-model", "the endpoint name served")
	if err := fs.Parse(args); err != nil {
		return err
	}
	server := fakeplane.New(fakeplane.Config{
		ServiceToken: *service,
		Clusters:     map[string]fakeplane.Cluster{*token: {ID: *cluster, Name: strings.TrimPrefix(*cluster, "cluster-")}},
		Table: fakeplane.Table{
			Revision: "transit-1",
			Endpoints: []fakeplane.Endpoint{{
				ID:              "endpoint-transit",
				Name:            *model,
				RoutingStrategy: "weighted",
				Upstreams: []fakeplane.Upstream{{
					DeploymentID:    "deployment-transit",
					AdmissionPhase:  "running",
					ClusterID:       *cluster,
					ClusterName:     strings.TrimPrefix(*cluster, "cluster-"),
					ServedModelName: *model,
					Weight:          100,
				}},
			}},
		},
	})
	fmt.Fprintf(os.Stderr, "hgrtransit: fake plane on %s\n", *addr)
	return listen(ctx, *addr, server.Handler())
}

func listen(ctx context.Context, addr string, handler http.Handler) error {
	srv := &http.Server{Addr: addr, Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		<-ctx.Done()
		_ = srv.Close()
	}()
	if err := srv.ListenAndServe(); err != nil && ctx.Err() == nil {
		return err
	}
	return nil
}

// emit writes the report and then reports whether the run passed, so a CI job
// gets the whole report and a non-zero exit when no stage held the objective.
func emit(report *lab.Report, asJSON bool) error {
	if asJSON {
		if err := report.WriteJSON(os.Stdout); err != nil {
			return err
		}
	} else {
		report.WriteText(os.Stdout)
	}
	return report.Err()
}
