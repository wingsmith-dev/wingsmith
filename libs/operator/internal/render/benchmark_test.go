package render

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
)

func benchParams(overrides map[string]string) map[string]string {
	params := map[string]string{
		"harness":            "aiperf",
		"image":              "nvcr.io/nvidia/ai-dynamo/aiperf:latest",
		"model":              "Qwen/Qwen3-32B-FP8",
		"benchmark_id":       "bmk_01j9x2mmmmmmmmmmmmmmmmmmmm",
		"deployment_id":      "dep_01j9x2cccccccccccccccccccc",
		"concurrency":        "32",
		"request_count":      "500",
		"input_tokens_mean":  "1024",
		"output_tokens_mean": "128",
		"streaming":          "true",
	}
	for k, v := range overrides {
		params[k] = v
	}
	return params
}

func benchOpts() (Options, BenchmarkOptions) {
	return Options{
			Namespace:      "hangar-system",
			PoolLabelKey:   "hangar.io/pool",
			GPUPoolValue:   "gpu",
			CachePoolValue: "cache",
			CacheTaintKey:  "hangar.io/cache",
			ModelCachePVC:  "model-cache",
		}, BenchmarkOptions{
			Endpoint: "http://dynamo-frontend.dynamo.svc.cluster.local:8000",
		}
}

func script(t *testing.T, params map[string]string) string {
	t.Helper()
	opts, bench := benchOpts()
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	return job.Spec.Template.Spec.Containers[0].Args[0]
}

// The load must be aimed at the cluster's own frontend. A benchmark that dialled anything outside
// would be inference traffic crossing the control plane, which is the one thing rule 4 forbids.
func TestBenchmarkAimsAtTheClusterLocalEndpoint(t *testing.T) {
	body := script(t, benchParams(nil))
	if !strings.Contains(body, "--url 'http://dynamo-frontend.dynamo.svc.cluster.local:8000'") {
		t.Errorf("the job does not point at the configured in-cluster endpoint:\n%s", body)
	}
}

func TestBenchmarkRefusesWithoutAnEndpoint(t *testing.T) {
	opts, _ := benchOpts()
	if _, err := BenchmarkJob("bmk_x", benchParams(nil), opts, BenchmarkOptions{}); err == nil {
		t.Error("a cluster with no configured endpoint has nothing to benchmark against")
	}
}

func TestBenchmarkRefusesAnUnknownHarness(t *testing.T) {
	opts, bench := benchOpts()
	params := benchParams(map[string]string{"harness": "vllm"})
	if _, err := BenchmarkJob("bmk_x", params, opts, bench); err == nil {
		t.Error("a harness this renderer cannot render must be refused, not rendered as aiperf")
	}
}

func TestBenchmarkCarriesTheDeclaredLoad(t *testing.T) {
	body := script(t, benchParams(nil))
	for _, want := range []string{
		"--concurrency 32",
		"--request-count 500",
		"--synthetic-input-tokens-mean 1024",
		"--output-tokens-mean 128",
		"--streaming",
		"--model 'Qwen/Qwen3-32B-FP8'",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q in:\n%s", want, body)
		}
	}
}

// Streaming off means no TTFT and no ITL, so the flag must actually disappear rather than be
// written with a false value AIPerf would read as present.
func TestBenchmarkOmitsStreamingWhenItIsOff(t *testing.T) {
	body := script(t, benchParams(map[string]string{"streaming": "false"}))
	if strings.Contains(body, "--streaming") {
		t.Errorf("streaming was disabled and the flag is still there:\n%s", body)
	}
}

func TestBenchmarkAppendsExtraFlagsVerbatim(t *testing.T) {
	body := script(t, benchParams(map[string]string{"extra": "--request-rate 12 --fixed-schedule"}))
	if !strings.Contains(body, "--request-rate 12 --fixed-schedule") {
		t.Errorf("the escape hatch did not reach the command line:\n%s", body)
	}
}

// A model name is attacker-adjacent input by the time it reaches a shell: it comes from a recipe
// field somebody typed. It goes in quoted.
func TestBenchmarkQuotesTheModelName(t *testing.T) {
	body := script(t, benchParams(map[string]string{"model": "a'; rm -rf /; echo '"}))
	if strings.Contains(body, "rm -rf /; echo") && !strings.Contains(body, `'\''`) {
		t.Errorf("the model name escaped its quoting:\n%s", body)
	}
}

// A benchmark must not be retried behind the plane's back. A second attempt is a second
// measurement under different conditions, reported as if it were the first.
func TestBenchmarkNeverRetries(t *testing.T) {
	opts, bench := benchOpts()
	params := benchParams(nil)
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if job.Spec.BackoffLimit == nil || *job.Spec.BackoffLimit != 0 {
		t.Errorf("BackoffLimit is %v; a benchmark is not idempotent work", job.Spec.BackoffLimit)
	}
}

// The load generator holds no accelerator. One held here is one taken away from what is being
// measured, which changes the answer as well as wasting it.
func TestBenchmarkRunsOffTheAcceleratorPool(t *testing.T) {
	opts, bench := benchOpts()
	params := benchParams(nil)
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	spec := job.Spec.Template.Spec
	if len(spec.NodeSelector) != 0 {
		t.Errorf("pinned to %v; a load generator has no placement requirement of its own", spec.NodeSelector)
	}
	if len(spec.Tolerations) != 0 {
		t.Errorf("tolerates %v; the accelerator taint is what keeps this off a GPU node", spec.Tolerations)
	}
	for _, container := range spec.Containers {
		if q, ok := container.Resources.Limits["nvidia.com/gpu"]; ok && !q.IsZero() {
			t.Error("the load generator asked for a GPU")
		}
	}
}

// An HTTP client's request must fit on an ordinary node. The failure this guards against is not
// subtle — a request larger than a small pool's spare capacity is a run that never schedules.
func TestBenchmarkRequestsFitAModestNode(t *testing.T) {
	opts, bench := benchOpts()
	params := benchParams(nil)
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	container := job.Spec.Template.Spec.Containers[0]

	if cpu := container.Resources.Requests.Cpu(); cpu.MilliValue() > 1000 {
		t.Errorf("requested %s of CPU to drive HTTP requests", cpu)
	}
	if _, capped := container.Resources.Limits[corev1.ResourceCPU]; capped {
		t.Error("capped the CPU of the thing doing the measuring")
	}
}

func TestBenchmarkResourcesAreConfigurable(t *testing.T) {
	opts, bench := benchOpts()
	bench.CPU, bench.Memory = "3", "6Gi"
	params := benchParams(nil)
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	requests := job.Spec.Template.Spec.Containers[0].Resources.Requests

	if cpu := requests.Cpu(); cpu.MilliValue() != 3000 {
		t.Errorf("requested %s of CPU, not the configured 3", cpu)
	}
	if memory := requests.Memory(); memory.String() != "6Gi" {
		t.Errorf("requested %s of memory, not the configured 6Gi", memory)
	}
}

func TestBenchmarkRefusesAnUnreadableResourceRequest(t *testing.T) {
	opts, bench := benchOpts()
	bench.CPU = "loads"
	params := benchParams(nil)
	if _, err := BenchmarkJob(params["benchmark_id"], params, opts, bench); err == nil {
		t.Fatal("rendered a job with an unparseable CPU request")
	}
}

// The weights volume is shared with every running replica. A benchmark reads a tokenizer off it
// and has no business writing to it.
func TestBenchmarkMountsTheModelCacheReadOnly(t *testing.T) {
	opts, bench := benchOpts()
	params := benchParams(nil)
	job, err := BenchmarkJob(params["benchmark_id"], params, opts, bench)
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	for _, mount := range job.Spec.Template.Spec.Containers[0].VolumeMounts {
		if mount.Name == "model-cache" && !mount.ReadOnly {
			t.Error("the model cache is mounted writable")
		}
	}
}

// Two runs against one deployment are two measurements; their jobs must not be the same object.
func TestBenchmarkJobNameIsPerRunAndFitsTheLabelLimit(t *testing.T) {
	first := BenchmarkJobName("bmk_01j9x2mmmmmmmmmmmmmmmmmmmm")
	second := BenchmarkJobName("bmk_01j9x2nnnnnnnnnnnnnnnnnnnn")
	if first == second {
		t.Error("two runs rendered to one job name")
	}
	if len(first) > maxObjectName {
		t.Errorf("name is %d characters, over the DNS-1123 limit", len(first))
	}
	if strings.Contains(first, "_") {
		t.Errorf("%q is not a valid object name", first)
	}
}

// The export has to be framed, or the operator has to guess which of the harness's several
// outputs is the machine-readable one.
func TestBenchmarkFramesItsExport(t *testing.T) {
	body := script(t, benchParams(nil))
	for _, marker := range []string{SummaryBegin, SummaryEnd, VersionLine} {
		if !strings.Contains(body, marker) {
			t.Errorf("the script does not emit %q", marker)
		}
	}
}
