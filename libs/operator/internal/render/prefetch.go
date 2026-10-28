package render

import (
	"fmt"
	"strconv"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// prefetchNamePrefix distinguishes the download job from anything else rendered for an artifact.
const prefetchNamePrefix = "prefetch-"

// The labels and annotation that say which weights a download job is pulling.
//
// AttemptAnnotation counts how many times this operator has recreated an artifact's download after
// the previous one failed outright. It lives on the object, not in the process, because the
// operator restarts and an in-memory counter would reset the bound on every restart, which is not
// a bound at all, and turns a permanently unfetchable artifact into an unbounded retry against
// somebody else's registry.
//
// ArtifactLabel is the key the whole operator finds a prefetch job by, and it is the only one that
// carries meaning to code: the object name is a rendering for people and has changed shape once
// already. ModelLabel and ModelSourceAnnotation are for the reader: `kubectl get jobs -L
// wingsmith.dev/model` answers "who is downloading" even for an artifact whose slug was truncated out
// of the name, and the annotation holds the full source URI, which no object name could.
const (
	ArtifactLabel         = "wingsmith.dev/artifact"
	ManagedByLabel        = "wingsmith.dev/managed-by"
	ModelLabel            = "wingsmith.dev/model"
	ModelSourceAnnotation = "wingsmith.dev/model-source"
	AttemptAnnotation     = "wingsmith.dev/prefetch-attempt"
)

// What one download asks the scheduler for, when the cluster does not say.
//
// Small by design. Pulling weights is bound by the link and by the write to the shared volume,
// not by cores, and the earlier figures (four CPUs and eight gigabytes) were large enough that
// the job could not be placed on an ordinary node at all. A cluster with a dedicated cache pool
// can raise them; a cluster running this beside its system workloads should not have to.
// The ceiling is not derived from the request. What a download needs reserved and
// what it may peak at are different facts: it is placed on how little it needs to start, and it
// peaks on dirty page cache, because pages written to the shared NFS volume are charged to this
// cgroup until writeback flushes them to the network volume, which can be slower than the pull
// filling it. Deriving one from the other made the ceiling follow the request down and the job was OOM
// killed part-way through a large model.
const (
	defaultPrefetchCPU         = "500m"
	defaultPrefetchMemory      = "2Gi"
	defaultPrefetchMemoryLimit = "6Gi"
)

// maxObjectName is the DNS-1123 label limit the API server enforces on an object name, and
// maxLabelValue the same limit on a label value.
const (
	maxObjectName = 63
	maxLabelValue = 63
)

// minModelSegment is the shortest model fragment worth putting in front of an artifact id.
//
// Below it the leading segment has stopped being a name and become noise: `k-` in front of an id
// tells a reader less than the bare `prefetch-` shape does, because it looks like information.
const minModelSegment = 4

// progressScript prints one line of download progress on a timer, and is prepended to every fetch
// strategy so that a running job says something before it is finished.
//
// A line instead of a bar. HF_HUB_DISABLE_PROGRESS_BARS stays set for the reason it was set (a
// carriage-returned bar renders in `kubectl logs` as thousands of lines that bury the one worth
// reading), but the job that resulted said nothing at all for the forty minutes of a large pull,
// and a silent job is indistinguishable from a stuck one. That question, not the byte count, is
// what somebody tailing the log is actually asking.
//
// Bytes are measured on disk under the directory the fetch writes into, not taken from a
// hook inside the client library: it is the same number the volume will hold at the end, one
// implementation serves both strategies, and it costs a directory walk every interval instead of a
// dependency on huggingface_hub's internals. lstat, not stat, because a HuggingFace snapshot
// is symlinks into a blob store under the same root, and following them would count every file
// twice.
//
// Thirty seconds is slow enough that an hour-long pull is sixty lines of log, and fast enough that
// a reader waiting on a stalled download sees the number stop moving instead of wondering whether
// anything is due. The thread is a daemon so it cannot hold the job open past the fetch.
const progressScript = `import os, threading, time
_start = time.monotonic()
def _size(path):
    total = 0
    for root, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total
def _report(path):
    while True:
        time.sleep(30)
        print("prefetch progress bytes=%%d elapsed_seconds=%%d path=%%s" %% (
            _size(path), int(time.monotonic() - _start), path), flush=True)
def _watch(path):
    threading.Thread(target=_report, args=(path,), daemon=True).start()
`

// hfScript downloads a HuggingFace repo into HF_HOME, held as a constant so the rendered job is a
// pure function of its inputs and can be applied repeatedly without churning the object.
//
// max_workers=4 is enough parallelism to saturate the link without tripping HuggingFace rate
// limiting on an unauthenticated pull. It was 8, which also multiplied by four the dirty page
// cache waiting on writeback to the shared NFS volume: memory this job is charged for.
//
// Progress is watched on the repo's own directory under the hub cache and not on HF_HOME, because
// HF_HOME is the shared volume every artifact lands in: two warm-set entries fetch at once, and a
// count over the root would report each job the other's bytes. The path is reconstructed, not
// asked for, since snapshot_download only returns it once there is nothing left to report.
const hfScript = progressScript + `from huggingface_hub import snapshot_download
try:
    from huggingface_hub.constants import HF_HUB_CACHE as hub
except ImportError:
    hub = os.path.join(os.environ.get("HF_HOME", "/models/hf"), "hub")
repo, revision = %q, %q or None
_watch(os.path.join(hub, "models--" + repo.replace("/", "--")))
p = snapshot_download(repo, revision=revision, max_workers=4)
print("cached at", p, flush=True)
`

// fetchScript pulls custom weights into the cache at the path modelRef falls back to.
//
// Written inline in python3 instead of shelling out to a cloud CLI, because the only thing the
// engine runtime image is guaranteed to carry is a python with huggingface_hub in it; an image
// without the aws CLI would fail at runtime with a message about a missing binary, not
// about the weights.
//
// TODO: credentials. ArtifactSource.CredentialRef names the secret this should read, and nothing
// mounts or reads it yet, so only anonymous sources fetch today.
// TODO: verify ArtifactDesired.Digest once the digest algorithm over a multi-file artifact is
// pinned down; checking the wrong thing is worse than checking nothing.
const fetchScript = progressScript + `import sys, urllib.parse, urllib.request
uri, dest = %q, %q
os.makedirs(dest, exist_ok=True)
_watch(dest)
u = urllib.parse.urlparse(uri)
if u.scheme in ("http", "https"):
    out = os.path.join(dest, os.path.basename(u.path) or "weights")
    urllib.request.urlretrieve(uri, out)
elif u.scheme == "s3":
    import boto3
    boto3.client("s3").download_file(u.netloc, u.path.lstrip("/"), os.path.join(dest, os.path.basename(u.path)))
else:
    sys.exit("unsupported source scheme: " + u.scheme)
print("cached at", dest, flush=True)
`

// PrefetchJob renders the one-shot weight download for an artifact.
//
// This is a correctness gate, not a warm-up optimisation. Engine workers start simultaneously and
// all resolve the same HuggingFace cache on the shared ReadWriteMany volume; huggingface_hub guards
// concurrent downloads with .lock files and that guard does not hold over NFS, so the second worker
// dies on lock acquisition and crash-loops the whole graph deployment. Running the download to
// completion in one job first means the workers only ever read.
//
// It runs on the cache pool instead of the accelerator pool: this is network and disk work, and
// there is no reason to hold a GPU idle for the length of a multi-tens-of-gigabytes pull.
//
// The job is keyed on the artifact and takes no deployment: residency is declared by the
// cluster's warm-set, so an artifact is fetched because it should be present here, whether or
// not anything serves it yet. A second deployment of the same weights then costs nothing and reads
// residency as a precondition instead of doing the work again. The image is passed in, not
// derived, because it must be the same runtime image the workers run: a different huggingface_hub
// version lays the cache out differently and the workers would silently re-download what this job
// just fetched. That is the caller's fact to know, not this function's.
func PrefetchJob(art wire.ArtifactDesired, image string, opts Options) (*batchv1.Job, error) {
	if image == "" {
		return nil, fmt.Errorf("artifact %s has no image to fetch with", art.ArtifactID)
	}
	if !imageAllowed(image, opts.ImagePrefixes) {
		return nil, fmt.Errorf("artifact %s: image %s is not under an allowed prefix (WINGSMITH_ALLOWED_IMAGE_PREFIXES: %s)",
			art.ArtifactID, image, strings.Join(opts.ImagePrefixes, ", "))
	}

	command, err := prefetchCommand(art)
	if err != nil {
		return nil, err
	}

	resources, err := prefetchResources(opts)
	if err != nil {
		return nil, err
	}

	return &batchv1.Job{
		TypeMeta: metav1.TypeMeta{APIVersion: "batch/v1", Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        PrefetchJobName(art),
			Namespace:   opts.Namespace,
			Labels:      prefetchLabels(art),
			Annotations: prefetchAnnotations(art),
		},
		Spec: batchv1.JobSpec{
			// A failed pull is nearly always transient (a rate limit or a dropped
			// connection), so it retries instead of waiting for a human.
			BackoffLimit: ptr(int32(3)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      prefetchLabels(art),
					Annotations: prefetchAnnotations(art),
				},
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyOnFailure,
					// The same credential the workers pull with. This job runs the
					// engine image by design (it must lay the cache out the way the
					// workers read it), so a private engine image is private here too,
					// and without this the download fails on the pull with no clue
					// that the credential exists one namespace over.
					ImagePullSecrets:             imagePullSecrets(opts.EnginePullSecrets),
					NodeSelector:                 map[string]string{opts.PoolLabelKey: opts.CachePoolValue},
					Tolerations:                  cacheTolerations(opts),
					AutomountServiceAccountToken: ptr(false),
					SecurityContext:              prefetchPodSecurityContext(),
					Volumes: []corev1.Volume{{
						Name: "model-cache",
						VolumeSource: corev1.VolumeSource{
							PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: opts.ModelCachePVC},
						},
					}},
					Containers: []corev1.Container{{
						Name:            "fetch",
						Image:           image,
						Command:         []string{"python3", "-c"},
						Args:            []string{command},
						SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: ptr(false)},
						Env: []corev1.EnvVar{
							{Name: "HF_HOME", Value: HFHome},
							// The image's own home may belong to another uid; this one is
							// writable by anyone.
							{Name: "HOME", Value: "/tmp"},
							// Progress bars in a job log are noise that hides the
							// lines worth reading. progressScript is what says how
							// far the download has got instead.
							{Name: "HF_HUB_DISABLE_PROGRESS_BARS", Value: "1"},
						},
						EnvFrom: []corev1.EnvFromSource{{
							SecretRef: &corev1.SecretEnvSource{
								LocalObjectReference: corev1.LocalObjectReference{Name: "hf-token-secret"},
								Optional:             ptr(true),
							},
						}},
						VolumeMounts: []corev1.VolumeMount{{Name: "model-cache", MountPath: "/models"}},
						Resources:    resources,
					}},
				},
			},
		},
	}, nil
}

// cacheTolerations lets the prefetch job onto the cache pool's taint, whatever value the cluster
// gave it. No key configured means no toleration: an empty key with Exists would tolerate every
// taint on every node.
func cacheTolerations(opts Options) []corev1.Toleration {
	key := strings.TrimSpace(opts.CacheTaintKey)
	if key == "" {
		return nil
	}
	return []corev1.Toleration{taintToleration(key, opts.CacheTaintValue)}
}

// taintToleration tolerates a NoSchedule taint on key: any value when value is empty, or exactly
// value when one is configured.
func taintToleration(key, value string) corev1.Toleration {
	if value = strings.TrimSpace(value); value != "" {
		return corev1.Toleration{Key: key, Operator: corev1.TolerationOpEqual, Value: value, Effect: corev1.TaintEffectNoSchedule}
	}
	return corev1.Toleration{Key: key, Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule}
}

// prefetchPodSecurityContext keeps the image's own user and capabilities, and adds the runtime's
// default seccomp profile.
//
// Not jobPodSecurityContext: the prefetch writes into the shared model cache, whose directories
// belong to whichever user wrote them first (root, for every prefetch rendered before this), and
// fsGroup does not re-own an NFS or EFS volume. A fixed non-root uid would fail with EACCES on
// such a cache. The benchmark job only reads that volume, so it can run as jobUID.
func prefetchPodSecurityContext() *corev1.PodSecurityContext {
	return &corev1.PodSecurityContext{
		SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
	}
}

// jobUID is the user the benchmark job runs as. Fixed, not taken from the
// image, so runAsNonRoot holds for images whose USER is a name or root.
const jobUID = 1000

// jobPodSecurityContext is the pod-level context for the benchmark job: non-root,
// with the runtime's default seccomp profile, and a group that owns what they write.
func jobPodSecurityContext() *corev1.PodSecurityContext {
	return &corev1.PodSecurityContext{
		RunAsNonRoot:   ptr(true),
		RunAsUser:      ptr(int64(jobUID)),
		RunAsGroup:     ptr(int64(jobUID)),
		FSGroup:        ptr(int64(jobUID)),
		SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
	}
}

// jobContainerSecurityContext drops every capability and privilege escalation.
func jobContainerSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr(false),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
}

// prefetchResources is what one download asks the scheduler for.
//
// Requests only for CPU, because a throttled download is a slower download and nothing else. The
// memory ceiling is set on its own, not derived from the request, for the reason given on
// defaultPrefetchMemoryLimit.
func prefetchResources(opts Options) (corev1.ResourceRequirements, error) {
	cpuText := opts.PrefetchCPU
	if cpuText == "" {
		cpuText = defaultPrefetchCPU
	}
	memoryText := opts.PrefetchMemory
	if memoryText == "" {
		memoryText = defaultPrefetchMemory
	}

	cpu, err := resource.ParseQuantity(cpuText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("prefetch cpu request %q is not a quantity: %w", cpuText, err)
	}
	memory, err := resource.ParseQuantity(memoryText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("prefetch memory request %q is not a quantity: %w", memoryText, err)
	}

	ceilingText := opts.PrefetchMemoryLimit
	if ceilingText == "" {
		ceilingText = defaultPrefetchMemoryLimit
	}
	ceiling, err := resource.ParseQuantity(ceilingText)
	if err != nil {
		return corev1.ResourceRequirements{}, fmt.Errorf("prefetch memory limit %q is not a quantity: %w", ceilingText, err)
	}
	if ceiling.Cmp(memory) < 0 {
		return corev1.ResourceRequirements{}, fmt.Errorf("prefetch memory limit %s is below the request %s", ceilingText, memoryText)
	}

	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: ceiling},
	}, nil
}

// PrefetchJobName is the object name for an artifact's prefetch job.
//
// Keyed on the artifact id instead of the deployment, because the download is per set of
// weights: two deployments over one artifact want the same job, and the second reconcile must find
// the first one's instead of starting a competing pull.
//
// The model leads the name only so that a `kubectl get jobs` reads as who is downloading and
// not as a list of opaque ids. It is decoration in front of the key, not part of it, which is why
// the truncation cuts the model and never the id when 63 bytes bite, and why an artifact whose
// sources name no model keeps the bare `prefetch-<id>` shape instead of growing a placeholder.
//
// Nothing may find this job by reconstructing this string. The shape has already changed once, so a
// name-based lookup would miss every job created before the change; the artifact label is the key,
// and reconcile.prefetchJobFor is what reads it.
func PrefetchJobName(art wire.ArtifactDesired) string {
	return PrefetchAttemptName(art, 0)
}

// PrefetchAttemptName is the object name for one attempt at an artifact's download.
//
// Attempt zero is the plain name and every restart after a failure carries an `-rN` suffix. The
// suffix exists because a restart has to create the replacement before deleting what it replaces:
// the count of how many times this has happened lives on the Job, and deleting first would leave
// the next pass reading a fresh object with nothing to carry the count forward, so the bound would
// never advance and the retry would be unbounded after all.
//
// Two objects therefore share one artifact for as long as the delete takes, which is only safe
// because the outgoing one has already given up: a Job in JobFailed has no pod still writing into
// the cache, so this is not the concurrent download the whole design is built to prevent. What
// keeps residency from flickering between them meanwhile is reconcile.prefetchOutranks, which
// prefers a job that has not failed.
//
// The suffix is reserved out of the model's room like everything else here, so an id is never cut
// to make space for it.
func PrefetchAttemptName(art wire.ArtifactDesired, attempt int) string {
	suffix := ""
	if attempt > 0 {
		suffix = "-r" + strconv.Itoa(attempt)
	}

	tail := prefetchNamePrefix + Name(art.ArtifactID) + suffix
	if len(tail) > maxObjectName {
		return strings.TrimRight(tail[:maxObjectName], "-")
	}

	model, room := PrefetchModel(art), maxObjectName-len(tail)-1
	if model == "" || room < minModelSegment {
		return tail
	}
	if len(model) > room {
		model = strings.TrimRight(model[:room], "-")
	}
	return model + "-" + tail
}

// prefetchLabels is what a prefetch Job and its pod are marked with.
//
// A fresh map per call, and never one shared between the Job and its pod template: OwnedMeta stamps
// the owner onto the Job's labels after this returns, and a shared map would put the warm-set's
// name into the pod's selector labels too: a difference the API server would then reject on any
// later apply of an immutable template.
//
// The model is a label, not only a name segment, because a label survives what a name cannot:
// it is present in full on an artifact whose slug was truncated out of the name, and `kubectl get
// jobs -L wingsmith.dev/model` reads it back without parsing anything.
func prefetchLabels(art wire.ArtifactDesired) map[string]string {
	labels := map[string]string{
		ArtifactLabel:  art.ArtifactID,
		ManagedByLabel: "wingsmith-operator",
	}
	if model := PrefetchModel(art); model != "" {
		if len(model) > maxLabelValue {
			model = strings.TrimRight(model[:maxLabelValue], "-")
		}
		labels[ModelLabel] = model
	}
	return labels
}

// prefetchAnnotations carries the source URI the job is pulling from.
//
// An annotation and not a label because the URI is neither a valid label value nor something
// anything selects on. It is here for the reader who has found a job and wants the exact repo and
// pin behind a slug that dropped the owner, the case, and the revision.
func prefetchAnnotations(art wire.ArtifactDesired) map[string]string {
	hf, other := prefetchSource(art)
	source := hf
	if source == nil {
		source = other
	}
	if source == nil {
		return nil
	}
	return map[string]string{ModelSourceAnnotation: source.URI}
}

// prefetchCommand chooses the fetch strategy from the artifact's sources.
//
// A HuggingFace source wins over every other kind, whatever its priority, because it is the one
// the engine can resolve by repo id against the shared HF_HOME; anything else has to land at a
// path, and that path is /models/<artifact-id> so it agrees with what modelRef serves. Within a
// kind the lowest Priority wins, since the plane numbers its preferred source 0.
func prefetchCommand(art wire.ArtifactDesired) (string, error) {
	hf, other := prefetchSource(art)

	if hf != nil {
		repo, revision := SplitHFRef(hf.URI)
		return fmt.Sprintf(hfScript, repo, revision), nil
	}
	if other != nil {
		if art.ArtifactID == "" {
			return "", fmt.Errorf("artifact has no id to cache under")
		}
		return fmt.Sprintf(fetchScript, other.URI, "/models/"+art.ArtifactID), nil
	}
	return "", fmt.Errorf("artifact %s declares no source to fetch from", art.ArtifactID)
}

// prefetchSource picks the source a fetch reads from, as the preferred HuggingFace one and the
// preferred source of any other kind.
//
// Extracted so the job's name and the job's command are decided by one rule instead of two. The
// name says which model is being downloaded, and it says so by reading the source: a second copy
// of this preference would eventually name one model while fetching another, which is worse than
// an opaque name because it is confidently wrong.
func prefetchSource(art wire.ArtifactDesired) (hf, other *wire.ArtifactSource) {
	for i := range art.Sources {
		src := &art.Sources[i]
		if src.URI == "" {
			continue
		}
		target := &other
		if strings.HasPrefix(src.URI, "hf://") {
			target = &hf
		}
		if *target == nil || src.Priority < (*target).Priority {
			*target = src
		}
	}
	return hf, other
}

// PrefetchModel is the model a prefetch job is downloading, in a form fit for an object name.
//
// Read off the artifact's own source, because that is the only name of the model the operator has
// here. An artifact carries a digest, a size and a list of URIs; it does not carry the string a
// human calls the model, and the deployments that do have a different lifetime: an artifact in
// the warm-set need not be served by anything on this cluster at all.
//
// Empty when there is nothing to read, and the caller falls back to the artifact id alone. A job
// named after a guess would be worse than one named after an id.
func PrefetchModel(art wire.ArtifactDesired) string {
	hf, other := prefetchSource(art)
	name := ""
	switch {
	case hf != nil:
		repo, _ := SplitHFRef(hf.URI)
		if i := strings.LastIndex(repo, "/"); i >= 0 {
			repo = repo[i+1:]
		}
		name = repo
	case other != nil:
		name = other.URI
		if i := strings.LastIndex(name, "/"); i >= 0 {
			name = name[i+1:]
		}
	}
	return slug(name)
}

// slug reduces a model name to what a DNS-1123 object name and a label value may both contain.
//
// A run of unusable characters collapses to one separator instead of one each, because the input
// is a repo id or a file name and `Qwen3.5-9B.safetensors` should read as a model and
// not as its own punctuation.
func slug(name string) string {
	var b strings.Builder
	separator := false
	for _, r := range strings.ToLower(name) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			if separator && b.Len() > 0 {
				b.WriteByte('-')
			}
			separator = false
			b.WriteRune(r)
		default:
			separator = true
		}
	}
	return b.String()
}
