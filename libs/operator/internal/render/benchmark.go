package render

import (
	"cmp"
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// benchmarkNamePrefix distinguishes a run's job from anything else rendered in the namespace.
const benchmarkNamePrefix = "benchmark-"

// SummaryBegin and SummaryEnd delimit the harness's JSON export in the job's log.
//
// The result comes back through pod logs instead of a shared volume or a sidecar that posts it.
// Logs are the one channel every container already has, need no RBAC beyond what reading a job
// requires, and survive the pod being cleaned up for as long as the operator is still watching.
// The sentinels exist because a load generator prints a human summary too, and picking the JSON
// out of that by shape would be guessing.
const (
	SummaryBegin = "---HANGAR-BENCHMARK-SUMMARY-BEGIN---"
	SummaryEnd   = "---HANGAR-BENCHMARK-SUMMARY-END---"
	VersionLine  = "---HANGAR-BENCHMARK-VERSION---"
)

// BenchmarkOptions are the cluster-local facts a run needs and the plane does not declare.
type BenchmarkOptions struct {
	// Endpoint is the in-cluster OpenAI-compatible frontend the load is aimed at. It is a
	// cluster-local address by construction: the traffic a benchmark generates is inference
	// traffic, and rule 4 says that never leaves the cluster.
	Endpoint string

	// ArtifactBase is a URI prefix for the raw per-request records, or empty to leave them in
	// the pod. Those records are telemetry and do not travel the sync stream either way.
	ArtifactBase string

	// CPU and Memory are what the load generator requests. Empty falls back to a request small
	// enough to land on a modest node, because a benchmark that cannot be scheduled measures
	// nothing at all.
	CPU    string
	Memory string

	// Images are the harness images a run may use, matched exactly. A run naming any other
	// image is refused, and an empty list refuses every run: the plane chooses the image, so
	// this list is what bounds the code a benchmark can execute on the cluster.
	Images []string

	// TokenSecret names a Secret in the workload namespace whose keys become the harness's
	// environment, for a tokenizer in a gated Hugging Face repository. Empty, the default, gives
	// the job no credential: the plane picks what a benchmark runs, so a token in its environment
	// is a token the plane could read back out.
	TokenSecret string
}

// Defaults for a load generator's request. Modest by design: no CPU limit is set anywhere
// here, so this is a scheduling floor a run bursts above, and a floor set higher than a cluster's
// spare capacity is a run that never starts.
const (
	defaultBenchmarkCPU    = "500m"
	defaultBenchmarkMemory = "1Gi"
)

// aiperfScript runs the harness and then hands its export back through the log.
//
// Written as a shell wrapper, not as the container's argv, because three things have to
// happen in one container and only the first is the harness: run it, find the export it wrote
// under a directory whose name it chooses, and frame that file so the operator can lift it out of
// the log. `set -o pipefail` is left out: sh has no such option, and the exit status
// that matters is aiperf's, which is captured before anything else runs.
//
// The export is emitted even when the harness exits non-zero. A run that hit its deadline after
// most of its requests still measured those requests, and throwing that away because the process
// failed would discard the only evidence about what happened.
const aiperfScript = `set -e
%s
aiperf profile %s || rc=$?
echo "%s"
aiperf --version 2>/dev/null || echo "unknown"
export_file=$(find %s -name 'profile_export_aiperf.json' -o -name 'profile_export_genai_perf.json' 2>/dev/null | head -n 1)
echo "%s"
if [ -n "$export_file" ]; then cat "$export_file"; else echo 'null'; fi
echo ""
echo "%s"
exit ${rc:-0}
`

// benchmarkShell is how the wrapper above is started, and it is a bare name instead of a path.
//
// AIPerf ships distroless: there is a `/bin`, but nothing in it, and the only shell is busybox's
// at `/busybox/sh`. `/bin/sh` is the reflex and it fails the pod before the harness runs, with a
// runc error about a missing file instead of anything naming the image. Left unqualified, the
// runtime resolves it against the image's own PATH, which is where an image that moved its shell
// says so. This works for the distroless layout and for an ordinary one without the operator
// having to know which it was handed.
const benchmarkShell = "sh"

// artifactDir is where the harness is told to write, inside the pod.
const artifactDir = "/benchmark-artifacts"

// evalscopeScript runs EvalScope and hands its report back through the log, framed the same way
// aiperf's is. EvalScope prints a human table on stdout and writes one JSON report per dataset
// under its work dir; a short python step merges them into `{"reports": [...]}`.
const evalscopeScript = `set -e
%s
evalscope eval %s 2>&1 | tee %s/stdout.log || rc=$?
echo "%s"
evalscope --version 2>/dev/null || echo "unknown"
echo "%s"
python3 - %s <<'PYEOF'
import glob, json, sys
hits = sorted(glob.glob(sys.argv[1]))
reports = [json.load(open(p)) for p in hits]
print(json.dumps({"reports": reports}) if reports else "null")
PYEOF
echo ""
echo "%s"
exit ${rc:-0}
`

// evalscopeReports is where EvalScope 1.11 writes its per-dataset reports under the work dir.
const evalscopeReports = artifactDir + "/reports/*/*.json"

// runCacheDir is a writable Hugging Face cache private to one run, used only when the shared one
// cannot answer. It is an emptyDir, so it dies with the pod and nothing accumulates.
const runCacheDir = "/benchmark-cache"

// sharedCacheDir is the shared model cache's Hugging Face root, mounted read-only.
const sharedCacheDir = "/models/hf"

// hfCachePreamble decides which Hugging Face cache the harness reads its tokenizer from.
//
// The shared cache is mounted read-only, and huggingface_hub does not treat a cache it cannot
// write as a cache: asked for a repository it does not already hold, it creates the repository
// directory before it checks anything, and the run dies on `[Errno 30] Read-only file system`,
// not on a missing file. That is exactly what happened on the cluster whose cache held
// three other models and not this one.
//
// So the choice is made before the harness starts, on whether the shared cache already holds the
// repository. It does: read it offline, which is also the only path that works on a cluster with
// no egress. It does not: fall back to a writable cache private to this run and let the download
// happen. A tokenizer is a few megabytes, so the fallback costs seconds and the shared cache is
// still never written to by a benchmark.
//
// With no tokenizer resolved at all there is nothing to look for, and the private cache is the
// only safe answer: the harness will default the tokenizer to the served model name and needs
// somewhere writable to fail or succeed on its own terms.
func hfCachePreamble(tokenizer string) string {
	if tokenizer == "" {
		return "export HF_HOME=" + runCacheDir
	}
	dir := sharedCacheDir + "/hub/models--" + strings.ReplaceAll(tokenizer, "/", "--")
	return fmt.Sprintf(
		"if [ -d %s ]; then export HF_HUB_OFFLINE=1; else export HF_HOME=%s; fi",
		shellQuote(dir), runCacheDir,
	)
}

// BenchmarkJob renders one benchmark run as a one-shot job.
//
// It is scheduled unconstrained: no node selector and no toleration. A load generator is CPU and
// network work with no placement requirement of its own: the tokenizer it needs comes off an RWX
// volume that mounts from anywhere, and the endpoint it drives is a Service. What it must not do
// is land on the accelerator pool, where it would hold a GPU idle to drive requests at other GPUs
// and take capacity away from the thing being measured; the taint that pool already carries
// achieves that on its own, without this job having to know how any given cluster names its pools.
//
// An earlier version pinned this to the cache pool, reasoning by analogy with the weight prefetch.
// That was wrong: the cache pool exists to hold the weights volume and is not a general home for
// non-accelerator work, and a cluster is under no obligation to have one: the two that do not
// could not schedule a benchmark at all.
//
// The cost of running off the accelerator pool is that load originates a node hop away from the
// engine, so the reported TTFT carries that hop. That is unavoidable without burning an
// accelerator, and closer to what a real client sees than co-scheduling would be.
//
// BackoffLimit is zero. Every other job this operator renders is idempotent work that should be
// retried, and a benchmark is the opposite: a second attempt is a second measurement under
// different conditions, and silently reporting it as the first would corrupt the record. A failed
// run is reported as failed and re-running it is somebody's decision.
//
// The model cache is mounted read-only. The harness needs the tokenizer to build synthetic
// prompts of a requested length, and resolving it from the volume the weights already sit on
// avoids a second download and works on a cluster with no egress. Read-only because a benchmark
// has no business writing to the weights every replica is reading.
func BenchmarkJob(benchmarkID string, parameters map[string]string, opts Options, bench BenchmarkOptions) (*batchv1.Job, error) {
	image := strings.TrimSpace(parameters["image"])
	if image == "" {
		return nil, fmt.Errorf("benchmark %s names no harness image", benchmarkID)
	}
	if !slices.Contains(bench.Images, image) {
		if len(bench.Images) == 0 {
			return nil, fmt.Errorf("this cluster allows no benchmark images (HANGAR_BENCHMARK_IMAGES is empty), so not %q", image)
		}
		return nil, fmt.Errorf("benchmark image %q is not one this cluster allows (%s)", image, strings.Join(bench.Images, ", "))
	}
	if bench.Endpoint == "" {
		return nil, fmt.Errorf("this cluster has no inference endpoint configured to benchmark against")
	}
	model := parameters["model"]
	if model == "" {
		return nil, fmt.Errorf("benchmark %s names no model to ask for", benchmarkID)
	}
	// The command line is built here from typed parameters, every value quoted. Free-form flags
	// would put the plane's text on a shell inside the cluster.
	if extra := strings.TrimSpace(parameters["extra"]); extra != "" {
		return nil, fmt.Errorf("benchmark %s carries extra flags %q; this operator runs only the flags it builds itself", benchmarkID, extra)
	}

	harness := parameters["harness"]
	preamble := hfCachePreamble(strings.TrimSpace(parameters["tokenizer"]))

	var script string
	switch harness {
	case "aiperf":
		script = fmt.Sprintf(
			aiperfScript,
			preamble,
			strings.Join(aiperfArgs(model, bench.Endpoint, parameters), " "),
			VersionLine, artifactDir, SummaryBegin, SummaryEnd,
		)
	case "evalscope":
		// The request body names the model the frontend answers to, which the plane sends as
		// `endpoint_model` when it differs from the served name.
		bodyModel := cmp.Or(strings.TrimSpace(parameters["endpoint_model"]), model)
		args, err := evalscopeArgs(bodyModel, bench.Endpoint, parameters)
		if err != nil {
			return nil, fmt.Errorf("benchmark %s: %w", benchmarkID, err)
		}
		script = fmt.Sprintf(
			evalscopeScript,
			preamble,
			strings.Join(args, " "),
			artifactDir,
			VersionLine, SummaryBegin,
			shellQuote(evalscopeReports),
			SummaryEnd,
		)
	default:
		return nil, fmt.Errorf("this operator cannot render the %q harness", harness)
	}

	resources, err := benchmarkResources(bench)
	if err != nil {
		return nil, err
	}

	labels := map[string]string{
		"hangar.dev/benchmark":  benchmarkID,
		"hangar.dev/managed-by": "hangar-operator",
	}
	if deployment := parameters["deployment_id"]; deployment != "" {
		labels["hangar.dev/deployment"] = deployment
	}

	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      BenchmarkJobName(benchmarkID),
			Namespace: opts.Namespace,
			Labels:    labels,
		},
		Spec: batchv1.JobSpec{
			BackoffLimit: ptr(int32(0)),
			// Long enough that the operator has read the log back off the pod even after a
			// restart mid-run, short enough that finished runs do not accumulate. The record
			// of the run is in the plane by then; this object is only how it got there.
			TTLSecondsAfterFinished: ptr(int32(3600)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					RestartPolicy:                corev1.RestartPolicyNever,
					AutomountServiceAccountToken: ptr(false),
					SecurityContext:              jobPodSecurityContext(),
					// A harness image may sit in the same private registry as the
					// engine, so it gets the engine's pull credential.
					ImagePullSecrets: imagePullSecrets(opts.EnginePullSecrets),
					Volumes: []corev1.Volume{
						{
							Name: "model-cache",
							VolumeSource: corev1.VolumeSource{
								PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
									ClaimName: opts.ModelCachePVC,
									ReadOnly:  true,
								},
							},
						},
						{
							Name:         "artifacts",
							VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
						},
						{
							Name:         "hf-cache",
							VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
						},
					},
					Containers: []corev1.Container{{
						Name:            "benchmark",
						Image:           image,
						Command:         []string{benchmarkShell, "-c"},
						Args:            []string{script},
						SecurityContext: jobContainerSecurityContext(),
						Env: []corev1.EnvVar{
							{Name: "HF_HOME", Value: sharedCacheDir},
							{Name: "HOME", Value: runCacheDir},
							{Name: "HF_HUB_DISABLE_PROGRESS_BARS", Value: "1"},
							{Name: "HANGAR_BENCHMARK_ID", Value: benchmarkID},
							{Name: "HANGAR_ARTIFACT_BASE", Value: bench.ArtifactBase},
						},
						EnvFrom: benchmarkEnvFrom(bench),
						VolumeMounts: []corev1.VolumeMount{
							{Name: "model-cache", MountPath: "/models", ReadOnly: true},
							{Name: "artifacts", MountPath: artifactDir},
							{Name: "hf-cache", MountPath: runCacheDir},
						},
						Resources: resources,
					}},
				},
			},
		},
	}, nil
}

// benchmarkEnvFrom is the configured token Secret as environment, or nothing.
func benchmarkEnvFrom(bench BenchmarkOptions) []corev1.EnvFromSource {
	name := strings.TrimSpace(bench.TokenSecret)
	if name == "" {
		return nil
	}
	return []corev1.EnvFromSource{{
		SecretRef: &corev1.SecretEnvSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: name},
			Optional:             ptr(true),
		},
	}}
}

// BenchmarkJobName is the object name for a run's job. Keyed on the run and not the deployment:
// two runs against one deployment are two measurements and must not collide into one job.
func BenchmarkJobName(benchmarkID string) string {
	name := benchmarkNamePrefix + Name(benchmarkID)
	if len(name) > maxObjectName {
		name = strings.TrimRight(name[:maxObjectName], "-")
	}
	return name
}

// aiperfArgs turns the plane's parameter map into AIPerf's command line.
//
// Only flags the plane declared are emitted. A zero is not the same as an absent value for the
// stddev knobs (asking for zero spread is a real request), so they are always written, while
// warmup is omitted at zero because AIPerf's own default there is what "no warmup" means.
//
// `--tokenizer` is emitted only when the plane resolved one. AIPerf otherwise defaults it to
// `--model`, which is the name the engine serves under and not a Hugging Face repository, so a
// deployment served as `qwen3.8-27b` fails the tokenizer load before it sends a request.
func aiperfArgs(model, endpoint string, parameters map[string]string) []string {
	args := []string{
		"--model", shellQuote(model),
		"--url", shellQuote(endpoint),
		"--endpoint-type", shellQuote(stringParam(parameters, "endpoint_type", "chat")),
		"--artifact-dir", artifactDir,
		"--concurrency", strconv.Itoa(numberParam(parameters, "concurrency", 10)),
		"--request-count", strconv.Itoa(numberParam(parameters, "request_count", 100)),
		"--synthetic-input-tokens-mean", strconv.Itoa(numberParam(parameters, "input_tokens_mean", 2000)),
		"--synthetic-input-tokens-stddev", strconv.Itoa(numberParam(parameters, "input_tokens_stddev", 0)),
		"--output-tokens-mean", strconv.Itoa(numberParam(parameters, "output_tokens_mean", 256)),
		"--output-tokens-stddev", strconv.Itoa(numberParam(parameters, "output_tokens_stddev", 0)),
	}
	if tokenizer := strings.TrimSpace(parameters["tokenizer"]); tokenizer != "" {
		args = append(args, "--tokenizer", shellQuote(tokenizer))
	}
	if warmup := numberParam(parameters, "warmup_request_count", 0); warmup > 0 {
		args = append(args, "--warmup-request-count", strconv.Itoa(warmup))
	}
	if flagParam(parameters, "streaming", true) {
		args = append(args, "--streaming")
	}
	return args
}

// evalscopeArgs turns the plane's parameter map into `evalscope eval`'s flags, every value quoted.
//
// It mirrors what the plane composes for EvalScope 1.11. The endpoint is the cluster-local one
// with `/v1`, because EvalScope's OpenAI client takes a base URL.
func evalscopeArgs(model, endpoint string, parameters map[string]string) ([]string, error) {
	args := []string{
		"--model", shellQuote(model),
		"--api-url", shellQuote(strings.TrimSuffix(endpoint, "/") + "/v1"),
		"--eval-type", "openai_api",
		"--no-timestamp",
		"--work-dir", artifactDir,
	}

	var datasets []string
	for _, name := range strings.Split(parameters["datasets"], ",") {
		if name = strings.TrimSpace(name); name != "" {
			datasets = append(datasets, name)
		}
	}
	if len(datasets) > 0 {
		args = append(args, "--datasets")
		for _, name := range datasets {
			args = append(args, shellQuote(name))
		}
	}
	if limit := numberParam(parameters, "limit", 0); limit > 0 {
		args = append(args, "--limit", strconv.Itoa(limit))
	}
	args = append(args, "--seed", strconv.Itoa(numberParam(parameters, "seed", 0)))
	if repeats := numberParam(parameters, "repeats", 1); repeats != 1 {
		args = append(args, "--repeats", strconv.Itoa(repeats))
	}

	generation, err := json.Marshal(map[string]any{
		"temperature": floatParam(parameters, "temperature", 0),
		"top_p":       floatParam(parameters, "top_p", 1),
		"max_tokens":  numberParam(parameters, "max_output_tokens", 32768),
	})
	if err != nil {
		return nil, err
	}
	args = append(args, "--generation-config", shellQuote(string(generation)))

	fewShot := numberParam(parameters, "few_shot", 0)
	revision := strings.TrimSpace(parameters["dataset_revision"])
	if (fewShot > 0 || revision != "") && len(datasets) > 0 {
		perDataset := map[string]map[string]any{}
		for _, name := range datasets {
			entry := map[string]any{}
			if fewShot > 0 {
				entry["few_shot_num"] = fewShot
			}
			if revision != "" {
				entry["revision"] = revision
			}
			perDataset[name] = entry
		}
		body, err := json.Marshal(perDataset)
		if err != nil {
			return nil, err
		}
		args = append(args, "--dataset-args", shellQuote(string(body)))
	}

	baseURL := strings.TrimSpace(parameters["judge_base_url"])
	judgeModel := strings.TrimSpace(parameters["judge_model"])
	if baseURL != "" && judgeModel != "" {
		body, err := json.Marshal(map[string]any{
			"base_url":    baseURL,
			"model":       judgeModel,
			"temperature": floatParam(parameters, "judge_temperature", 0),
			"max_tokens":  numberParam(parameters, "judge_max_output_tokens", 4096),
		})
		if err != nil {
			return nil, err
		}
		args = append(args, "--judge", shellQuote(string(body)))
	}
	return args, nil
}

// shellQuote makes a value safe inside the single-quoted context of the rendered script.
func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
}

func stringParam(parameters map[string]string, key, def string) string {
	if v, ok := parameters[key]; ok && v != "" {
		return v
	}
	return def
}

func numberParam(parameters map[string]string, key string, def int) int {
	if raw, ok := parameters[key]; ok {
		if v, err := strconv.Atoi(raw); err == nil && v >= 0 {
			return v
		}
	}
	return def
}

func floatParam(parameters map[string]string, key string, def float64) float64 {
	if raw, ok := parameters[key]; ok {
		if v, err := strconv.ParseFloat(raw, 64); err == nil && v >= 0 && !math.IsInf(v, 0) {
			return v
		}
	}
	return def
}

func flagParam(parameters map[string]string, key string, def bool) bool {
	if raw, ok := parameters[key]; ok {
		if v, err := strconv.ParseBool(raw); err == nil {
			return v
		}
	}
	return def
}

// benchmarkResources is what one load generator asks the scheduler for.
//
// Requests only, and no CPU limit: throttling the thing doing the measuring would put the
// throttle's own latency into the numbers. The memory limit is a backstop against a harness that
// buffers every response instead of a budget, and is derived from the request so that raising
// one raises the other.
func benchmarkResources(bench BenchmarkOptions) (corev1.ResourceRequirements, error) {
	cpuText := bench.CPU
	if cpuText == "" {
		cpuText = defaultBenchmarkCPU
	}
	memoryText := bench.Memory
	if memoryText == "" {
		memoryText = defaultBenchmarkMemory
	}

	cpu, err := resource.ParseQuantity(cpuText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("benchmark cpu request %q is not a quantity: %w", cpuText, err)
	}
	memory, err := resource.ParseQuantity(memoryText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("benchmark memory request %q is not a quantity: %w", memoryText, err)
	}

	ceiling := memory.DeepCopy()
	ceiling.Add(memory)

	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: ceiling},
	}, nil
}
