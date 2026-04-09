package render

import (
	"fmt"
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
// The result comes back through pod logs rather than a shared volume or a sidecar that posts it.
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
}

// aiperfScript runs the harness and then hands its export back through the log.
//
// Written as a shell wrapper rather than as the container's argv because three things have to
// happen in one container and only the first is the harness: run it, find the export it wrote
// under a directory whose name it chooses, and frame that file so the operator can lift it out of
// the log. `set -o pipefail` is deliberately absent — sh has no such option, and the exit status
// that matters is aiperf's, which is captured before anything else runs.
//
// The export is emitted even when the harness exits non-zero. A run that hit its deadline after
// most of its requests still measured those requests, and throwing that away because the process
// failed would discard the only evidence about what happened.
const aiperfScript = `set -e
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

// artifactDir is where the harness is told to write, inside the pod.
const artifactDir = "/benchmark-artifacts"

// BenchmarkJob renders one benchmark run as a one-shot job.
//
// It runs on the cache pool rather than the accelerator pool. A load generator is CPU and network
// work, and holding a GPU idle to drive requests at other GPUs would take capacity away from the
// thing being measured — which would also change the measurement. The cost is that the load
// originates a node hop away from the engine, so the reported TTFT carries that hop; that is both
// unavoidable without burning an accelerator and closer to what a real client sees than
// co-scheduling would be.
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
	image := parameters["image"]
	if image == "" {
		return nil, fmt.Errorf("benchmark %s names no harness image", benchmarkID)
	}
	if bench.Endpoint == "" {
		return nil, fmt.Errorf("this cluster has no inference endpoint configured to benchmark against")
	}
	model := parameters["model"]
	if model == "" {
		return nil, fmt.Errorf("benchmark %s names no model to ask for", benchmarkID)
	}

	harness := parameters["harness"]
	if harness != "aiperf" {
		return nil, fmt.Errorf("this operator cannot render the %q harness", harness)
	}

	script := fmt.Sprintf(
		aiperfScript,
		strings.Join(aiperfArgs(model, bench.Endpoint, parameters), " "),
		VersionLine, artifactDir, SummaryBegin, SummaryEnd,
	)

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
					RestartPolicy: corev1.RestartPolicyNever,
					NodeSelector:  map[string]string{opts.PoolLabelKey: opts.CachePoolValue},
					Tolerations: []corev1.Toleration{{
						Key:      opts.CacheTaintKey,
						Operator: corev1.TolerationOpEqual,
						Value:    "true",
						Effect:   corev1.TaintEffectNoSchedule,
					}},
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
					},
					Containers: []corev1.Container{{
						Name:    "benchmark",
						Image:   image,
						Command: []string{"/bin/sh", "-c"},
						Args:    []string{script},
						Env: []corev1.EnvVar{
							{Name: "HF_HOME", Value: "/models/hf"},
							{Name: "HF_HUB_DISABLE_PROGRESS_BARS", Value: "1"},
							{Name: "HANGAR_BENCHMARK_ID", Value: benchmarkID},
							{Name: "HANGAR_ARTIFACT_BASE", Value: bench.ArtifactBase},
						},
						EnvFrom: []corev1.EnvFromSource{{
							SecretRef: &corev1.SecretEnvSource{
								LocalObjectReference: corev1.LocalObjectReference{Name: "hf-token-secret"},
								Optional:             ptr(true),
							},
						}},
						VolumeMounts: []corev1.VolumeMount{
							{Name: "model-cache", MountPath: "/models", ReadOnly: true},
							{Name: "artifacts", MountPath: artifactDir},
						},
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{
								corev1.ResourceCPU:    resource.MustParse("4"),
								corev1.ResourceMemory: resource.MustParse("8Gi"),
							},
							Limits: corev1.ResourceList{
								corev1.ResourceMemory: resource.MustParse("16Gi"),
							},
						},
					}},
				},
			},
		},
	}, nil
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
// stddev knobs — asking for zero spread is a real request — so they are always written, while
// warmup is omitted at zero because AIPerf's own default there is what "no warmup" means.
//
// `extra` goes on last and unquoted, which makes it both the escape hatch for the long tail of
// flags and the caller's problem if what they put in it is wrong. The alternative is enumerating
// every flag AIPerf has, in two languages, forever.
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
	if warmup := numberParam(parameters, "warmup_request_count", 0); warmup > 0 {
		args = append(args, "--warmup-request-count", strconv.Itoa(warmup))
	}
	if flagParam(parameters, "streaming", true) {
		args = append(args, "--streaming")
	}
	if extra := strings.TrimSpace(parameters["extra"]); extra != "" {
		args = append(args, extra)
	}
	return args
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

func flagParam(parameters map[string]string, key string, def bool) bool {
	if raw, ok := parameters[key]; ok {
		if v, err := strconv.ParseBool(raw); err == nil {
			return v
		}
	}
	return def
}
