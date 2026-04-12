package render

import (
	"fmt"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// prefetchNamePrefix distinguishes the download job from anything else rendered for an artifact.
const prefetchNamePrefix = "prefetch-"

// What one download asks the scheduler for, when the cluster does not say.
//
// Deliberately small. Pulling weights is bound by the link and by the write to the shared volume,
// not by cores, and the earlier figures — four CPUs and eight gigabytes — were large enough that
// the job could not be placed on an ordinary node at all. A cluster with a dedicated cache pool
// can raise them; a cluster running this beside its system workloads should not have to.
const (
	defaultPrefetchCPU    = "500m"
	defaultPrefetchMemory = "2Gi"
)

// maxObjectName is the DNS-1123 label limit the API server enforces on an object name.
const maxObjectName = 63

// hfScript downloads a HuggingFace repo into HF_HOME, held as a constant so the rendered job is a
// pure function of its inputs and can be applied repeatedly without churning the object.
//
// max_workers=8 is enough parallelism to saturate the link without tripping HuggingFace rate
// limiting on an unauthenticated pull.
const hfScript = `from huggingface_hub import snapshot_download
p = snapshot_download(%q, max_workers=8)
print("cached at", p)
`

// fetchScript pulls custom weights into the cache at the path modelRef falls back to.
//
// Written inline in python3 rather than shelling out to a cloud CLI, because the only thing the
// engine runtime image is guaranteed to carry is a python with huggingface_hub in it; an image
// without the aws CLI would fail at runtime with a message about a missing binary rather than
// about the weights.
//
// TODO: credentials. ArtifactSource.CredentialRef names the secret this should read, and nothing
// mounts or reads it yet, so only anonymous sources fetch today.
// TODO: verify ArtifactDesired.Digest once the digest algorithm over a multi-file artifact is
// pinned down; checking the wrong thing is worse than checking nothing.
const fetchScript = `import os, sys, urllib.parse, urllib.request
uri, dest = %q, %q
os.makedirs(dest, exist_ok=True)
u = urllib.parse.urlparse(uri)
if u.scheme in ("http", "https"):
    out = os.path.join(dest, os.path.basename(u.path) or "weights")
    urllib.request.urlretrieve(uri, out)
elif u.scheme == "s3":
    import boto3
    boto3.client("s3").download_file(u.netloc, u.path.lstrip("/"), os.path.join(dest, os.path.basename(u.path)))
else:
    sys.exit("unsupported source scheme: " + u.scheme)
print("cached at", dest)
`

// PrefetchJob renders the one-shot weight download for an artifact.
//
// This is a correctness gate, not a warm-up optimisation. Engine workers start simultaneously and
// all resolve the same HuggingFace cache on the shared ReadWriteMany volume; huggingface_hub guards
// concurrent downloads with .lock files and that guard does not hold over NFS, so the second worker
// dies on lock acquisition and crash-loops the whole graph deployment. Running the download to
// completion in one job first means the workers only ever read.
//
// It runs on the cache pool rather than the accelerator pool: this is network and disk work, and
// there is no reason to hold a GPU idle for the length of a multi-tens-of-gigabytes pull.
//
// The job is keyed on the artifact and takes no deployment and no recipe: residency is declared by
// the cluster's warm-set, so an artifact is fetched because it should be present here, whether or
// not anything serves it yet. A second deployment of the same weights then costs nothing and reads
// residency as a precondition instead of doing the work again. The image is passed in rather than
// derived because it must be the same runtime image the workers run — a different huggingface_hub
// version lays the cache out differently and the workers would silently re-download what this job
// just fetched — and that is the caller's fact to know, not this function's.
func PrefetchJob(art wire.ArtifactDesired, image string, opts Options) (*batchv1.Job, error) {
	if image == "" {
		return nil, fmt.Errorf("artifact %s has no image to fetch with", art.ArtifactID)
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
			Name:      PrefetchJobName(art.ArtifactID),
			Namespace: opts.Namespace,
			Labels: map[string]string{
				"hangar.dev/artifact":   art.ArtifactID,
				"hangar.dev/managed-by": "hangar-operator",
			},
		},
		Spec: batchv1.JobSpec{
			// A failed pull is nearly always transient — a rate limit or a dropped
			// connection — so it retries rather than waiting for a human.
			BackoffLimit: ptr(int32(3)),
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: map[string]string{
						"hangar.dev/artifact":   art.ArtifactID,
						"hangar.dev/managed-by": "hangar-operator",
					},
				},
				Spec: corev1.PodSpec{
					RestartPolicy: corev1.RestartPolicyOnFailure,
					NodeSelector:  map[string]string{opts.PoolLabelKey: opts.CachePoolValue},
					Tolerations: []corev1.Toleration{{
						Key:      opts.CacheTaintKey,
						Operator: corev1.TolerationOpEqual,
						Value:    "true",
						Effect:   corev1.TaintEffectNoSchedule,
					}},
					Volumes: []corev1.Volume{{
						Name: "model-cache",
						VolumeSource: corev1.VolumeSource{
							PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: opts.ModelCachePVC},
						},
					}},
					Containers: []corev1.Container{{
						Name:    "fetch",
						Image:   image,
						Command: []string{"python3", "-c"},
						Args:    []string{command},
						Env: []corev1.EnvVar{
							{Name: "HF_HOME", Value: "/models/hf"},
							// Progress bars in a job log are noise that hides the
							// one line worth reading.
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

// prefetchResources is what one download asks the scheduler for.
//
// Requests only for CPU, because a throttled download is a slower download and nothing else. The
// memory limit is a backstop against a fetch that buffers rather than streams, and is derived from
// the request so that raising one raises the other.
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

	ceiling := memory.DeepCopy()
	ceiling.Add(memory)

	return corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: ceiling},
	}, nil
}

// PrefetchJobName is the object name for an artifact's prefetch job.
//
// Derived from the artifact id rather than the recipe or deployment, because the download is per
// set of weights: two recipes over one artifact want the same job, and the second reconcile should
// find the first one's rather than start a competing pull.
func PrefetchJobName(artifactID string) string {
	name := prefetchNamePrefix + Name(artifactID)
	if len(name) > maxObjectName {
		name = strings.TrimRight(name[:maxObjectName], "-")
	}
	return name
}

// prefetchCommand chooses the fetch strategy from the artifact's sources.
//
// A HuggingFace source wins over every other kind, whatever its priority, because it is the one
// the engine can resolve by repo id against the shared HF_HOME; anything else has to land at a
// path, and that path is /models/<artifact-id> so it agrees with what modelRef serves. Within a
// kind the lowest Priority wins, since the plane numbers its preferred source 0.
func prefetchCommand(art wire.ArtifactDesired) (string, error) {
	var hf, other *wire.ArtifactSource
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

	if hf != nil {
		return fmt.Sprintf(hfScript, strings.TrimPrefix(hf.URI, "hf://")), nil
	}
	if other != nil {
		if art.ArtifactID == "" {
			return "", fmt.Errorf("artifact has no id to cache under")
		}
		return fmt.Sprintf(fetchScript, other.URI, "/models/"+art.ArtifactID), nil
	}
	return "", fmt.Errorf("artifact %s declares no source to fetch from", art.ArtifactID)
}
