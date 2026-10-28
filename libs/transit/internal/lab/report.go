package lab

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/transit/internal/load"
)

// SLO is the condition a capacity number is only true under.
//
// A capacity figure with no condition on it is not a figure. "Sixty concurrent"
// means nothing until it says at what success rate and inside what time to first
// token, because every system serves any concurrency if it is allowed to be slow
// enough and fail often enough.
type SLO struct {
	SuccessRate float64
	TTFTP99     time.Duration
}

// DefaultSLO is what a stage has to meet to count as carried.
func DefaultSLO() SLO {
	return SLO{SuccessRate: 0.999, TTFTP99: 2 * time.Second}
}

// Met reports whether a stage held the objective.
func (s SLO) Met(sample load.Sample) bool {
	if sample.Requests-sample.Censored == 0 {
		return false
	}
	if sample.SuccessRate < s.SuccessRate {
		return false
	}
	return s.TTFTP99 == 0 || sample.TTFTP99 <= s.TTFTP99
}

// Usage is one process's cost over a stage.
type Usage struct {
	Name      string  `json:"name"`
	CPUPeak   float64 `json:"cpu_peak_percent"`
	CPUMean   float64 `json:"cpu_mean_percent"`
	RSSPeakMB float64 `json:"rss_peak_mb"`
}

// Stage is one step of a run, with everything measured about it.
type Stage struct {
	Sample load.Sample `json:"sample"`
	Usage  []Usage     `json:"usage,omitempty"`
	// UpstreamPeak is the most requests the far end had in flight at once.
	// Compared with Concurrency it says where requests were waiting: equal means
	// the path passed everything through, lower means something between the
	// caller and the frontend was holding them.
	UpstreamPeak int64 `json:"upstream_peak_in_flight"`
	// Tunnels is how many tunnels the gateway reported holding at the end of
	// the stage, read from its own wingsmith_gateway_tunnels gauge; nil when it
	// could not be read, which is every live run. ConfiguredTunnels is how many
	// the agents were asked to open; a gap between the two is tunnels that
	// never came up or were lost.
	Tunnels           *int `json:"tunnels,omitempty"`
	ConfiguredTunnels int  `json:"configured_tunnels,omitempty"`
	// Verified is how many token verifications the fake plane had answered by
	// the end of the stage. A count that climbs during a stage is a reconnect
	// storm.
	Verified     int  `json:"plane_verifications,omitempty"`
	MetObjective bool `json:"met_objective"`
}

// Report is one whole run.
type Report struct {
	Mode        string        `json:"mode"`
	Target      string        `json:"target"`
	Topology    string        `json:"topology"`
	Started     time.Time     `json:"started"`
	SLO         string        `json:"slo"`
	Stages      []Stage       `json:"stages"`
	Capacity    int           `json:"capacity_concurrent"`
	CapacityTPM float64       `json:"capacity_tokens_per_minute"`
	Bottleneck  string        `json:"bottleneck"`
	Baseline    *load.Sample  `json:"baseline,omitempty"`
	Overhead    time.Duration `json:"path_overhead_p50,omitempty"`
}

// Verdict fills in the capacity, the objective and the named bottleneck.
//
// The bottleneck is named from evidence and not from a guess: whichever process
// was closest to a core, whether the frontend ever saw the concurrency that was
// offered, and which direction of the round trip grew. Where the evidence does
// not separate two candidates it says so instead of choosing.
func (r *Report) Verdict(slo SLO) {
	r.SLO = fmt.Sprintf("success ≥ %.3f%% and TTFT p99 ≤ %s", slo.SuccessRate*100, slo.TTFTP99)
	var carried *Stage
	var failed *Stage
	for i := range r.Stages {
		stage := &r.Stages[i]
		stage.MetObjective = slo.Met(stage.Sample)
		if stage.MetObjective && (carried == nil || stage.Sample.Concurrency > carried.Sample.Concurrency) {
			carried = stage
		}
		if !stage.MetObjective && failed == nil {
			failed = stage
		}
	}
	if carried != nil {
		r.Capacity = carried.Sample.Concurrency
		r.CapacityTPM = carried.Sample.TokensMin
	}
	r.Bottleneck = bottleneck(failed, carried)
}

func bottleneck(failed, carried *Stage) string {
	stage := failed
	if stage == nil {
		stage = carried
	}
	if stage == nil {
		return "nothing was measured"
	}
	var reasons []string

	hottest := ""
	peak := 0.0
	for _, usage := range stage.Usage {
		if usage.CPUPeak > peak {
			hottest, peak = usage.Name, usage.CPUPeak
		}
	}
	if peak >= 80 {
		reasons = append(reasons, fmt.Sprintf("%s CPU at %.0f%% of a core", hottest, peak))
	}
	if stage.UpstreamPeak > 0 && int(stage.UpstreamPeak) < stage.Sample.Concurrency {
		reasons = append(reasons, fmt.Sprintf(
			"the frontend never saw more than %d of %d offered, so requests were held before it (stream limits or flow control)",
			stage.UpstreamPeak, stage.Sample.Concurrency))
	}
	// Only when both legs were measured: a leg that was not stamped reads as
	// zero, and anything is more than twice zero.
	if in, back := stage.Sample.InboundP99, stage.Sample.ReturnP99; in > 0 && back > 0 {
		if in > back*2 {
			reasons = append(reasons, "the inbound leg grew faster than the return leg, so the cost is on the way in")
		}
		if back > in*2 {
			reasons = append(reasons, "the return leg grew faster than the inbound leg, so the cost is on the way back")
		}
	}
	for fault, count := range stage.Sample.Faults {
		reasons = append(reasons, fmt.Sprintf("%d requests ended as %s", count, fault))
	}
	if stage.Sample.Censored > 0 {
		reasons = append(reasons, fmt.Sprintf("%d requests were still running when the drain ended", stage.Sample.Censored))
	}
	if len(reasons) == 0 {
		if failed == nil {
			return "not reached: every stage held the objective, so the number below is a floor and not a limit"
		}
		return "unattributed: the objective was missed with no process near a core and no request held before the frontend"
	}
	sort.Strings(reasons)
	return strings.Join(reasons, "; ")
}

// ErrObjectiveMissed is what Err returns when no stage held the objective.
var ErrObjectiveMissed = errors.New("no stage held the objective")

// Err reports whether the run passed: nil when at least one stage held the
// objective, ErrObjectiveMissed otherwise, including a run that measured
// nothing. Call it after Verdict.
func (r *Report) Err() error {
	for _, stage := range r.Stages {
		if stage.MetObjective {
			return nil
		}
	}
	return fmt.Errorf("%w (%s)", ErrObjectiveMissed, r.SLO)
}

// WriteText renders the report for a terminal.
func (r *Report) WriteText(w io.Writer) {
	fmt.Fprintf(w, "\nwingstransit: %s mode, %s\n", r.Mode, r.Topology)
	fmt.Fprintf(w, "target %s, started %s\n\n", r.Target, r.Started.Format(time.RFC3339))

	fmt.Fprintf(w, "%5s %7s %6s %9s %9s %9s %9s %9s %8s %8s %9s\n",
		"conc", "reqs", "ok%", "ttft p50", "ttft p90", "ttft p99", "gap p99", "jitter", "req/s", "tok/s", "in|out p50")
	for _, stage := range r.Stages {
		s := stage.Sample
		legs := "-"
		if s.InboundP50 > 0 || s.ReturnP50 > 0 {
			legs = fmt.Sprintf("%s|%s", round(s.InboundP50), round(s.ReturnP50))
		}
		mark := " "
		if !stage.MetObjective {
			mark = "!"
		}
		fmt.Fprintf(w, "%4d%s %7d %5.1f%% %9s %9s %9s %9s %8s %8.1f %8.0f %9s\n",
			s.Concurrency, mark, s.Requests, s.SuccessRate*100,
			round(s.TTFTP50), round(s.TTFTP90), round(s.TTFTP99),
			round(s.GapP99), round(s.GapJitter), s.Throughput, s.TokenRate, legs)
		if len(s.Faults) > 0 {
			fmt.Fprintf(w, "      faults: %s\n", faultLine(s.Faults))
		}
		if s.Censored > 0 {
			fmt.Fprintf(w, "      cut:    %d still running when the drain ended, counted in TTFT at the time they were cut\n", s.Censored)
		}
		if len(stage.Usage) > 0 {
			fmt.Fprintf(w, "      cost:   %s\n", usageLine(stage.Usage))
		}
		if stage.UpstreamPeak > 0 {
			held := "an unknown number of"
			if stage.Tunnels != nil {
				held = strconv.Itoa(*stage.Tunnels)
			}
			fmt.Fprintf(w, "      path:   frontend saw %d in flight at peak, gateway held %s tunnels of %d configured\n",
				stage.UpstreamPeak, held, stage.ConfiguredTunnels)
		}
	}

	if r.Baseline != nil {
		fmt.Fprintf(w, "\nbaseline (caller straight to the frontend, no gateway and no tunnel): ttft p50 %s, p99 %s\n",
			round(r.Baseline.TTFTP50), round(r.Baseline.TTFTP99))
		fmt.Fprintf(w, "path overhead at p50: %s\n", round(r.Overhead))
	}

	fmt.Fprintf(w, "\nobjective:  %s\n", r.SLO)
	if r.Capacity > 0 {
		fmt.Fprintf(w, "capacity:   %d concurrent streams, %.0f tokens/minute, holding the objective\n",
			r.Capacity, r.CapacityTPM)
	} else {
		fmt.Fprintf(w, "capacity:   none of the stages held the objective\n")
	}
	fmt.Fprintf(w, "bottleneck: %s\n\n", r.Bottleneck)
}

// WriteJSON renders the report for CI.
func (r *Report) WriteJSON(w io.Writer) error {
	encoder := json.NewEncoder(w)
	encoder.SetIndent("", "  ")
	return encoder.Encode(r)
}

func faultLine(faults map[string]int) string {
	names := make([]string, 0, len(faults))
	for name := range faults {
		names = append(names, name)
	}
	sort.Strings(names)
	parts := make([]string, 0, len(names))
	for _, name := range names {
		parts = append(parts, fmt.Sprintf("%s=%d", name, faults[name]))
	}
	return strings.Join(parts, " ")
}

func usageLine(usage []Usage) string {
	parts := make([]string, 0, len(usage))
	for _, u := range usage {
		parts = append(parts, fmt.Sprintf("%s cpu %.0f%%/%.0f%% rss %.0fMB", u.Name, u.CPUMean, u.CPUPeak, u.RSSPeakMB))
	}
	return strings.Join(parts, "  ")
}

func round(d time.Duration) string {
	switch {
	case d == 0:
		return "-"
	case d < time.Millisecond:
		return d.Round(time.Microsecond).String()
	case d < time.Second:
		return d.Round(100 * time.Microsecond).String()
	default:
		return d.Round(time.Millisecond).String()
	}
}

// sampler polls the cost of a set of processes.
//
// CPU is taken from each process's accumulated CPU time, differenced between
// two polls and divided by the wall time between them, so a figure is the
// share of a core the process used in that interval. ps's own %cpu is not
// that: on Linux it is CPU time over the process's whole lifetime, and on
// macOS a decaying average, and either would smear one stage's cost into the
// next. A one-second poll can still miss a spike inside an interval, which is
// why the peak is reported beside the mean.
type sampler struct {
	pids func() map[string]int
	read func(pid int) (cpu time.Duration, rssMB float64, err error)
	now  func() time.Time

	mu      sync.Mutex
	last    map[string]reading
	samples map[string][]sample
}

// reading is one process's accumulated CPU time at one instant.
type reading struct {
	pid int
	cpu time.Duration
	at  time.Time
}

type sample struct {
	cpu float64
	rss float64
}

func newSampler(pids func() map[string]int) *sampler {
	return &sampler{
		pids:    pids,
		read:    processUsage,
		now:     time.Now,
		last:    map[string]reading{},
		samples: map[string][]sample{},
	}
}

func (s *sampler) run(ctx context.Context, every time.Duration) {
	s.poll()
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.poll()
		}
	}
}

// poll reads every process once. The first reading of a process, including a
// restarted agent under a new pid, is only a baseline: a share needs two.
func (s *sampler) poll() {
	for name, pid := range s.pids() {
		cpu, rss, err := s.read(pid)
		if err != nil {
			continue
		}
		at := s.now()
		s.mu.Lock()
		prev, ok := s.last[name]
		s.last[name] = reading{pid: pid, cpu: cpu, at: at}
		if ok && prev.pid == pid && at.After(prev.at) && cpu >= prev.cpu {
			share := float64(cpu-prev.cpu) / float64(at.Sub(prev.at)) * 100
			s.samples[name] = append(s.samples[name], sample{cpu: share, rss: rss})
		}
		s.mu.Unlock()
	}
}

// drain closes the current interval, returns what was sampled and starts
// again, so each stage reports its own cost instead of the run's.
func (s *sampler) drain() []Usage {
	s.poll()
	s.mu.Lock()
	collected := s.samples
	s.samples = map[string][]sample{}
	s.mu.Unlock()

	usage := make([]Usage, 0, len(collected))
	for name, samples := range collected {
		if len(samples) == 0 {
			continue
		}
		var sum, peak, rssPeak float64
		for _, sample := range samples {
			sum += sample.cpu
			if sample.cpu > peak {
				peak = sample.cpu
			}
			if sample.rss > rssPeak {
				rssPeak = sample.rss
			}
		}
		usage = append(usage, Usage{
			Name:      name,
			CPUPeak:   peak,
			CPUMean:   sum / float64(len(samples)),
			RSSPeakMB: rssPeak,
		})
	}
	sort.Slice(usage, func(i, j int) bool { return usage[i].Name < usage[j].Name })
	return usage
}

// processUsage reads a process's accumulated user+system CPU time and its
// resident set. /proc where there is one, ps otherwise: Linux's ps reports
// CPU time in whole seconds, too coarse for a one-second poll.
func processUsage(pid int) (time.Duration, float64, error) {
	if cpu, rss, err := procUsage(pid); err == nil {
		return cpu, rss, nil
	}
	out, err := exec.Command("ps", "-o", "time=,rss=", "-p", strconv.Itoa(pid)).Output()
	if err != nil {
		return 0, 0, err
	}
	fields := strings.Fields(string(out))
	if len(fields) < 2 {
		return 0, 0, fmt.Errorf("ps: unexpected output %q", out)
	}
	cpu, err := parseCPUTime(fields[0])
	if err != nil {
		return 0, 0, err
	}
	rss, err := strconv.ParseFloat(fields[1], 64)
	if err != nil {
		return 0, 0, err
	}
	return cpu, rss / 1024, nil
}

// clockTicks is USER_HZ, the unit of utime and stime in /proc/<pid>/stat. It
// is 100 on every Linux architecture Go supports.
const clockTicks = 100

func procUsage(pid int) (time.Duration, float64, error) {
	stat, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/stat")
	if err != nil {
		return 0, 0, err
	}
	cpu, err := parseProcStat(string(stat))
	if err != nil {
		return 0, 0, err
	}
	statm, err := os.ReadFile("/proc/" + strconv.Itoa(pid) + "/statm")
	if err != nil {
		return 0, 0, err
	}
	fields := strings.Fields(string(statm))
	if len(fields) < 2 {
		return 0, 0, fmt.Errorf("statm: unexpected %q", statm)
	}
	pages, err := strconv.ParseFloat(fields[1], 64)
	if err != nil {
		return 0, 0, err
	}
	return cpu, pages * float64(os.Getpagesize()) / (1 << 20), nil
}

// parseProcStat reads utime+stime from a /proc/<pid>/stat line. The command
// name in parentheses may hold spaces and parentheses itself, so the fields
// are counted from the last ")".
func parseProcStat(stat string) (time.Duration, error) {
	end := strings.LastIndexByte(stat, ')')
	if end < 0 {
		return 0, fmt.Errorf("stat: no command field in %q", stat)
	}
	// After ")" the fields start at the third, state; utime and stime are the
	// 14th and 15th.
	fields := strings.Fields(stat[end+1:])
	if len(fields) < 13 {
		return 0, fmt.Errorf("stat: too few fields in %q", stat)
	}
	utime, err1 := strconv.ParseInt(fields[11], 10, 64)
	stime, err2 := strconv.ParseInt(fields[12], 10, 64)
	if err1 != nil || err2 != nil {
		return 0, fmt.Errorf("stat: utime %q stime %q", fields[11], fields[12])
	}
	return time.Duration(utime+stime) * time.Second / clockTicks, nil
}

// parseCPUTime reads ps's cumulative time column: [dd-][hh:]mm:ss[.cc]. macOS
// prints minutes past sixty, not hours, which this reads the same way.
func parseCPUTime(text string) (time.Duration, error) {
	var days int64
	if day, rest, ok := strings.Cut(text, "-"); ok {
		d, err := strconv.ParseInt(day, 10, 64)
		if err != nil {
			return 0, fmt.Errorf("cpu time %q: %w", text, err)
		}
		days, text = d, rest
	}
	parts := strings.Split(text, ":")
	if len(parts) < 2 || len(parts) > 3 {
		return 0, fmt.Errorf("cpu time %q: want mm:ss or hh:mm:ss", text)
	}
	seconds, err := strconv.ParseFloat(parts[len(parts)-1], 64)
	if err != nil {
		return 0, fmt.Errorf("cpu time %q: %w", text, err)
	}
	total := seconds + float64(days)*86400
	scale := 60.0
	for i := len(parts) - 2; i >= 0; i-- {
		n, err := strconv.ParseInt(parts[i], 10, 64)
		if err != nil {
			return 0, fmt.Errorf("cpu time %q: %w", text, err)
		}
		total += float64(n) * scale
		scale *= 60
	}
	return time.Duration(total * float64(time.Second)), nil
}
