// Package lab stands the inference request path up on one host and ramps load
// through it.
//
// The path it builds is the real one: the gateway binary from libs/gateway, the
// agent binary from libs/agent, the tunnel between them from
// libs/packages/tunnel, and a reverse proxy at each end exactly as production
// wires them. Only two things are fakes, and both are outside the path — the
// control plane, which the gateway asks two questions and never asks again, and
// the serving frontend, which is replaced by a schedule so that the measurement
// is of the transport rather than of a model.
//
// What that buys is the answer to "where does it break" without a GPU. What it
// does not buy is a production number: a loopback interface is not a WAN, and
// the section on it in the README says so at more length than this comment can.
package lab

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/transit/internal/fakeplane"
	"github.com/hangar-dev/hangar/libs/transit/internal/load"
	"github.com/hangar-dev/hangar/libs/transit/internal/upstream"
)

// Options configures a synthetic run.
type Options struct {
	// Repo is the checkout the gateway and agent are built from.
	Repo string
	// Agents is how many agent processes serve the one cluster. More than one is
	// the real shape — the agent is a Deployment — and it is the only way to ask
	// whether a request lands on a tunnel that has room.
	Agents int
	// ConnsPerGateway is how many tunnels each agent holds to each replica.
	ConnsPerGateway int
	// MaxStreams is what each agent advertises as its per-tunnel stream limit.
	// Zero takes the tunnel package's own default of 256.
	MaxStreams int
	// Upstream is the schedule the fake frontend answers on.
	Upstream upstream.Profile
	// Stages is the ramp.
	Stages []load.Stage
	// Stream asks for SSE.
	Stream bool
	// PromptBytes pads the request body.
	PromptBytes int
	// Baseline runs one stage straight at the frontend first, so the path's own
	// cost is a subtraction rather than an assertion.
	Baseline bool
	// KillAgentEvery, when set, kills an agent process on that interval for the
	// whole run. It is how the harness asks what a caller sees when a tunnel
	// dies mid-stream, which is the failure this transport exists to survive.
	KillAgentEvery time.Duration
	// Verbose sends the children's logs to this process's stderr.
	Verbose bool
	// SLO is the condition the capacity number is reported under.
	SLO SLO
}

const (
	clusterID    = "cluster-transit"
	clusterName  = "transit"
	planeToken   = "transit-operator-token"
	tunnelToken  = "transit-enrolment-token"
	modelName    = "transit-model"
	deploymentID = "deployment-transit"
)

// RunSynthetic builds the path, ramps through it, and reports.
func RunSynthetic(ctx context.Context, opts Options) (*Report, error) {
	if opts.Agents < 1 {
		opts.Agents = 1
	}
	if opts.ConnsPerGateway < 1 {
		opts.ConnsPerGateway = 2
	}

	workdir, err := os.MkdirTemp("", "hgrtransit-")
	if err != nil {
		return nil, err
	}
	defer func() { _ = os.RemoveAll(workdir) }()

	gatewayBin, err := build(ctx, opts.Repo, "libs/gateway", "./cmd/gateway", filepath.Join(workdir, "gateway"))
	if err != nil {
		return nil, err
	}
	agentBin, err := build(ctx, opts.Repo, "libs/agent", "./cmd/agent", filepath.Join(workdir, "agent"))
	if err != nil {
		return nil, err
	}

	frontend := upstream.New(opts.Upstream)
	frontendSrv, frontendAddr, err := serve(frontend.Handler())
	if err != nil {
		return nil, err
	}
	defer func() { _ = frontendSrv.Close() }()

	plane := fakeplane.New(fakeplane.Config{
		OperatorToken: planeToken,
		Clusters:      map[string]fakeplane.Cluster{tunnelToken: {ID: clusterID, Name: clusterName}},
		Table: fakeplane.Table{
			Revision: "transit-1",
			Endpoints: []fakeplane.Endpoint{{
				ID:              "endpoint-transit",
				Name:            modelName,
				Visibility:      "public",
				RoutingStrategy: "weighted",
				Upstreams: []fakeplane.Upstream{{
					DeploymentID:    deploymentID,
					DeploymentName:  "transit",
					AdmissionPhase:  "running",
					ClusterID:       clusterID,
					ClusterName:     clusterName,
					ServedModelName: modelName,
					Weight:          100,
					Role:            "primary",
				}},
			}},
		},
	})
	planeSrv, planeAddr, err := serve(plane.Handler())
	if err != nil {
		return nil, err
	}
	defer func() { _ = planeSrv.Close() }()

	servingPort, err := free()
	if err != nil {
		return nil, err
	}
	tunnelPort, err := free()
	if err != nil {
		return nil, err
	}

	runCtx, stop := context.WithCancel(ctx)
	defer stop()

	gateway := exec.CommandContext(runCtx, gatewayBin,
		"-listen", "127.0.0.1:"+strconv.Itoa(servingPort),
		"-plane-url", "http://"+planeAddr,
		"-tunnel-listen", "127.0.0.1:"+strconv.Itoa(tunnelPort),
		"-metrics-listen", "off",
		"-gateway-id", "gateway-0",
		"-gateways", "1",
		"-resync", "60s",
	)
	gateway.Env = append(os.Environ(), "HANGAR_OPERATOR_TOKEN="+planeToken)
	pipe(gateway, opts.Verbose, "gateway")
	if err := gateway.Start(); err != nil {
		return nil, fmt.Errorf("start the gateway: %w", err)
	}
	defer func() { _ = gateway.Process.Kill() }()

	gatewayURL := "http://127.0.0.1:" + strconv.Itoa(servingPort)
	if err := waitFor(runCtx, gatewayURL+"/healthz", 30*time.Second); err != nil {
		return nil, fmt.Errorf("the gateway never became live: %w", err)
	}

	agents := newAgentSet(runCtx, agentSpec{
		binary:     agentBin,
		gateway:    "127.0.0.1:" + strconv.Itoa(tunnelPort),
		frontend:   "http://" + frontendAddr,
		conns:      opts.ConnsPerGateway,
		maxStreams: opts.MaxStreams,
		verbose:    opts.Verbose,
	})
	defer agents.stopAll()
	for i := 0; i < opts.Agents; i++ {
		if err := agents.start(i); err != nil {
			return nil, err
		}
	}

	if err := waitFor(runCtx, gatewayURL+"/readyz", 60*time.Second); err != nil {
		return nil, fmt.Errorf("the gateway never held a tunnel: %w", err)
	}

	report := &Report{
		Mode:    "synthetic",
		Target:  gatewayURL,
		Started: time.Now(),
		Topology: fmt.Sprintf("1 gateway, %d agents × %d tunnels, streams/tunnel %s, frontend ttft %s itl %s tokens %d",
			opts.Agents, opts.ConnsPerGateway, streamLimit(opts.MaxStreams),
			opts.Upstream.TTFT, opts.Upstream.ITL, opts.Upstream.Tokens),
	}

	if opts.Baseline && len(opts.Stages) > 0 {
		stage := opts.Stages[0]
		baseline := load.Summarise(stage, load.Run(runCtx, load.NewClient(stage.Concurrency), load.Request{
			URL:         "http://" + frontendAddr + "/v1/chat/completions",
			Model:       modelName,
			Stream:      opts.Stream,
			PromptBytes: opts.PromptBytes,
			Schedule:    opts.Upstream,
		}, stage))
		report.Baseline = &baseline
	}

	if opts.KillAgentEvery > 0 {
		go agents.churn(runCtx, opts.KillAgentEvery)
	}

	monitor := newSampler(func() map[string]int {
		pids := map[string]int{"gateway": gateway.Process.Pid}
		for name, pid := range agents.pids() {
			pids[name] = pid
		}
		return pids
	})
	go monitor.run(runCtx, time.Second)

	for _, stage := range opts.Stages {
		client := load.NewClient(stage.Concurrency)
		outcomes := load.Run(runCtx, client, load.Request{
			URL:         gatewayURL + "/v1/chat/completions",
			Model:       modelName,
			Stream:      opts.Stream,
			PromptBytes: opts.PromptBytes,
			Schedule:    opts.Upstream,
		}, stage)
		client.CloseIdleConnections()

		frontendStats := frontend.Stats()
		_, verified, _ := plane.Counts()
		report.Stages = append(report.Stages, Stage{
			Sample:       load.Summarise(stage, outcomes),
			Usage:        monitor.drain(),
			UpstreamPeak: frontendStats.PeakInFlight,
			Tunnels:      agents.live(),
			Verified:     verified,
		})
	}

	report.Verdict(opts.SLO)
	if report.Baseline != nil && len(report.Stages) > 0 {
		report.Overhead = report.Stages[0].Sample.TTFTP50 - report.Baseline.TTFTP50
	}
	return report, nil
}

// LiveOptions configures a run against a gateway somebody else is operating.
type LiveOptions struct {
	// URL is the gateway's base address.
	URL string
	// Model is the public name to ask for. It must be one the target serves.
	Model string
	// Token is the caller credential.
	Token string
	// Stages is the ramp. Every stage is offered load against a shared system,
	// so the caller is the one deciding what is safe.
	Stages      []load.Stage
	Stream      bool
	PromptBytes int
	SLO         SLO
}

// RunLive ramps against a deployed gateway.
//
// The same generator, the same percentiles and the same report shape as the
// synthetic run, which is the whole point: a laptop, CI and production produce
// three reports that can be put beside each other. What it cannot produce is the
// per-leg split or the per-process cost — the frontend is a real engine and does
// not stamp its clock, and the gateway is not this process's child — so those
// columns are empty rather than estimated, and the bottleneck it names is
// correspondingly coarser.
func RunLive(ctx context.Context, opts LiveOptions) (*Report, error) {
	if opts.Model == "" {
		return nil, errors.New("live mode needs a model the target actually serves")
	}
	base := strings.TrimRight(opts.URL, "/")
	report := &Report{
		Mode:     "live",
		Target:   base,
		Started:  time.Now(),
		Topology: "the deployed path: whatever is in front of the gateway, the gateway, the tunnel, the agent and a real engine",
	}

	for _, stage := range opts.Stages {
		client := load.NewClient(stage.Concurrency)
		outcomes := load.Run(ctx, client, load.Request{
			URL:         base + "/v1/chat/completions",
			Model:       opts.Model,
			Token:       opts.Token,
			Stream:      opts.Stream,
			PromptBytes: opts.PromptBytes,
		}, stage)
		client.CloseIdleConnections()
		report.Stages = append(report.Stages, Stage{Sample: load.Summarise(stage, outcomes)})
		if ctx.Err() != nil {
			break
		}
	}
	report.Verdict(opts.SLO)
	return report, nil
}

func streamLimit(n int) string {
	if n <= 0 {
		return "256 (tunnel default)"
	}
	return strconv.Itoa(n)
}

// build compiles one of the repository's binaries.
func build(ctx context.Context, repo, module, pkg, out string) (string, error) {
	cmd := exec.CommandContext(ctx, "go", "build", "-o", out, pkg)
	cmd.Dir = filepath.Join(repo, module)
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("build %s: %w", pkg, err)
	}
	return out, nil
}

// serve starts an in-process HTTP server on a loopback port of the kernel's
// choosing and reports where it landed.
func serve(handler http.Handler) (*http.Server, string, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, "", err
	}
	srv := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go func() { _ = srv.Serve(ln) }()
	return srv, ln.Addr().String(), nil
}

// free picks a port nothing is listening on.
//
// It closes the listener before handing the number over, which is a race with
// anything else on the host that binds in the same instant. That is accepted:
// the alternative is passing a file descriptor into a child process that expects
// to bind for itself, and the failure mode here is a loud bind error at startup
// rather than a wrong measurement.
func free() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	return port, nil
}

// waitFor polls a probe until it answers 200.
func waitFor(ctx context.Context, url string, limit time.Duration) error {
	deadline := time.Now().Add(limit)
	client := &http.Client{Timeout: time.Second}
	var last string
	for time.Now().Before(deadline) {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		resp, err := client.Get(url)
		if err == nil {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			last = strconv.Itoa(resp.StatusCode) + " " + string(body)
		} else {
			last = err.Error()
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("%s never answered: %s", url, last)
}

func pipe(cmd *exec.Cmd, verbose bool, name string) {
	if !verbose {
		cmd.Stdout = io.Discard
		cmd.Stderr = io.Discard
		return
	}
	cmd.Stdout = prefixed(os.Stderr, name)
	cmd.Stderr = prefixed(os.Stderr, name)
}

func prefixed(w io.Writer, name string) io.Writer {
	return &prefixWriter{w: w, prefix: []byte("[" + name + "] ")}
}

type prefixWriter struct {
	w      io.Writer
	prefix []byte
}

func (p *prefixWriter) Write(b []byte) (int, error) {
	out := make([]byte, 0, len(b)+len(p.prefix))
	for _, line := range strings.Split(strings.TrimRight(string(b), "\n"), "\n") {
		out = append(out, p.prefix...)
		out = append(out, line...)
		out = append(out, '\n')
	}
	if _, err := p.w.Write(out); err != nil {
		return 0, err
	}
	return len(b), nil
}
