package lab

import (
	"os"
	"strings"
	"testing"
	"time"
)

// fakeProcesses drives the sampler without real processes.
type fakeProcesses struct {
	now time.Time
	cpu map[int]time.Duration
}

func (f *fakeProcesses) read(pid int) (time.Duration, float64, error) {
	return f.cpu[pid], 100, nil
}

func TestSamplerReportsTheShareOfACoreUsedBetweenPolls(t *testing.T) {
	procs := &fakeProcesses{now: time.Unix(0, 0), cpu: map[int]time.Duration{10: 5 * time.Hour}}
	pids := map[string]int{"gateway": 10}
	s := newSampler(func() map[string]int { return pids })
	s.read = procs.read
	s.now = func() time.Time { return procs.now }

	// A process that has burned five hours of CPU before the run starts must
	// not show up as busy: only the delta counts.
	s.poll()
	procs.now = procs.now.Add(time.Second)
	procs.cpu[10] += 500 * time.Millisecond
	s.poll()
	procs.now = procs.now.Add(time.Second)
	procs.cpu[10] += time.Second

	usage := s.drain()
	if len(usage) != 1 || usage[0].Name != "gateway" {
		t.Fatalf("usage = %+v", usage)
	}
	if usage[0].CPUPeak != 100 || usage[0].CPUMean != 75 || usage[0].RSSPeakMB != 100 {
		t.Fatalf("usage = %+v, want peak 100%% mean 75%% rss 100MB", usage[0])
	}

	// The next stage starts from the reading drain took, not from nothing.
	procs.now = procs.now.Add(2 * time.Second)
	procs.cpu[10] += 500 * time.Millisecond
	usage = s.drain()
	if len(usage) != 1 || usage[0].CPUMean != 25 {
		t.Fatalf("second stage usage = %+v, want 25%%", usage)
	}
}

func TestSamplerTakesARestartedProcessAsANewBaseline(t *testing.T) {
	procs := &fakeProcesses{now: time.Unix(0, 0), cpu: map[int]time.Duration{10: time.Minute, 11: 0}}
	pids := map[string]int{"agent-0": 10}
	s := newSampler(func() map[string]int { return pids })
	s.read = procs.read
	s.now = func() time.Time { return procs.now }

	s.poll()
	pids["agent-0"] = 11
	procs.now = procs.now.Add(time.Second)
	s.poll()
	if usage := s.drain(); len(usage) != 0 {
		t.Fatalf("usage = %+v, want nothing across a restart", usage)
	}
}

func TestParseCPUTime(t *testing.T) {
	cases := map[string]time.Duration{
		"0:00.01":     10 * time.Millisecond,
		"444:37.22":   444*time.Minute + 37*time.Second + 220*time.Millisecond,
		"00:01:02":    62 * time.Second,
		"1-02:03:04":  26*time.Hour + 3*time.Minute + 4*time.Second,
		"12:00:00.50": 12*time.Hour + 500*time.Millisecond,
	}
	for text, want := range cases {
		got, err := parseCPUTime(text)
		if err != nil || got.Round(time.Millisecond) != want {
			t.Errorf("parseCPUTime(%q) = %s, %v; want %s", text, got, err, want)
		}
	}
	for _, bad := range []string{"", "12", "a:b", "1:2:3:4", "x-1:00"} {
		if _, err := parseCPUTime(bad); err == nil {
			t.Errorf("parseCPUTime(%q) should fail", bad)
		}
	}
}

func TestParseProcStat(t *testing.T) {
	// The command name holds a space and a parenthesis to exercise the parser.
	stat := "4242 (gate way) S) R 1 4242 4242 0 -1 4194560 100 0 0 0 250 50 0 0 20 0 8 0 1000 0 0"
	got, err := parseProcStat(stat)
	if err != nil || got != 3*time.Second {
		t.Fatalf("parseProcStat = %s, %v; want 3s (300 ticks)", got, err)
	}
	if _, err := parseProcStat("no command"); err == nil {
		t.Fatal("want an error without a command field")
	}
}

func TestProcessUsageReadsThisProcess(t *testing.T) {
	cpu, rss, err := processUsage(os.Getpid())
	if err != nil {
		t.Skipf("neither /proc nor ps is available: %v", err)
	}
	if cpu < 0 || rss <= 0 {
		t.Fatalf("cpu %s rss %.1fMB", cpu, rss)
	}
}

func TestSumGauge(t *testing.T) {
	exposition := strings.Join([]string{
		"# HELP hangar_gateway_tunnels Live tunnels.",
		"# TYPE hangar_gateway_tunnels gauge",
		`hangar_gateway_tunnels{cluster_id="clu_a"} 2`,
		`hangar_gateway_tunnels{cluster_id="clu_b"} 3`,
		`hangar_gateway_tunnels_total 99`,
		`hangar_gateway_tunnel_coverage{cluster_id="clu_a",cluster="a"} 1`,
	}, "\n")
	total, ok := sumGauge(strings.NewReader(exposition), "hangar_gateway_tunnels")
	if !ok || total != 5 {
		t.Fatalf("sumGauge = %v, %v; want 5", total, ok)
	}
	if _, ok := sumGauge(strings.NewReader("# TYPE hangar_gateway_tunnels gauge\n"), "hangar_gateway_tunnels"); ok {
		t.Fatal("no samples should report false")
	}
}
