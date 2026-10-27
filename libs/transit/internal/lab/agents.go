package lab

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"time"
)

// agentSpec is everything an agent process needs to be started again.
//
// Again is the operative word: the churn mode kills one and expects it back, and
// a spec that could only produce the first generation would turn a reconnect
// test into a capacity test with one fewer agent.
type agentSpec struct {
	binary     string
	gateway    string
	frontend   string
	conns      int
	maxStreams int
	verbose    bool
}

// agentSet runs the cluster's agents and can replace one while load is running.
type agentSet struct {
	ctx  context.Context
	spec agentSpec

	mu       sync.Mutex
	running  map[int]*exec.Cmd
	restarts int
}

func newAgentSet(ctx context.Context, spec agentSpec) *agentSet {
	return &agentSet{ctx: ctx, spec: spec, running: map[int]*exec.Cmd{}}
}

// start brings up agent i, replacing whatever was there.
func (a *agentSet) start(i int) error {
	health, err := free()
	if err != nil {
		return err
	}
	cmd := exec.CommandContext(a.ctx, a.spec.binary)
	cmd.Env = append(os.Environ(),
		"HANGAR_CLUSTER_ID="+clusterName,
		"HANGAR_TUNNEL_GATEWAY="+a.spec.gateway,
		"HANGAR_ENROLMENT_TOKEN="+tunnelToken,
		"HANGAR_TUNNEL_TLS=false",
		"HANGAR_INFERENCE_ENDPOINT="+a.spec.frontend,
		"HANGAR_HEALTH_ADDR=127.0.0.1:"+strconv.Itoa(health),
		"HANGAR_TUNNEL_CONNS_PER_GATEWAY="+strconv.Itoa(a.spec.conns),
		"HANGAR_TUNNEL_MAX_CONCURRENT_STREAMS="+strconv.Itoa(a.spec.maxStreams),
		"HANGAR_LOG_FORMAT=text",
		"HANGAR_DRAIN_TIMEOUT=10s",
	)
	pipe(cmd, a.spec.verbose, "agent-"+strconv.Itoa(i))
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start agent %d: %w", i, err)
	}

	a.mu.Lock()
	a.running[i] = cmd
	a.mu.Unlock()
	return nil
}

// churn kills one agent on an interval and starts it again, so a run measures
// what a caller sees while a tunnel dies underneath it.
//
// SIGKILL and not SIGTERM. A drained agent sends GOAWAY and the
// gateway retries what was never processed, which is the case the tunnel package
// already has tests for; the case with no coverage is the one where a node
// vanishes and the socket resets with no GOAWAY on it.
func (a *agentSet) churn(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	victim := 0
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			a.mu.Lock()
			cmd := a.running[victim]
			count := len(a.running)
			a.restarts++
			a.mu.Unlock()
			if cmd != nil && cmd.Process != nil {
				_ = cmd.Process.Kill()
				_ = cmd.Wait()
			}
			if err := a.start(victim); err != nil {
				return
			}
			if count > 0 {
				victim = (victim + 1) % count
			}
		}
	}
}

// configured is how many tunnels the running agents were asked to hold. It is
// what was asked for, not what is up; the gateway's gauge says that.
func (a *agentSet) configured() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.running) * a.spec.conns
}

func (a *agentSet) pids() map[string]int {
	a.mu.Lock()
	defer a.mu.Unlock()
	pids := make(map[string]int, len(a.running))
	for i, cmd := range a.running {
		if cmd.Process != nil {
			pids["agent-"+strconv.Itoa(i)] = cmd.Process.Pid
		}
	}
	return pids
}

func (a *agentSet) stopAll() {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, cmd := range a.running {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	}
}
