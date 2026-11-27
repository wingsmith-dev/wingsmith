package render

import (
	"fmt"
	"regexp"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// The model cache layout on the shared volume:
//
//	/models/hf                  public Hugging Face artifacts (HF_HOME)
//	/models/<artifact-id>       public artifacts fetched from any other source
//	/models/p/<project>/hf      one project's private Hugging Face artifacts
//	/models/p/<project>/<id>    one project's private artifacts from any other source
//
// A pod mounts the one directory its artifact lives under, by subPath, and never the volume root,
// so a serving pod of one project cannot open another project's private weights. There is no
// dedup across scopes: a private copy of a public repo is fetched again under the project.
const (
	modelCacheRoot = "/models"
	publicHFDir    = "hf"
	projectDir     = "p"
)

// HFTokenKey is the key a project token Secret holds the Hugging Face token under, so the Secret
// reads as environment through envFrom like the cluster-wide hf-token-secret.
const HFTokenKey = "HF_TOKEN"

// ProjectLabel marks the per-project token Secret with the project it belongs to.
const ProjectLabel = "wingsmith.dev/project"

// projectID is what a project id may look like to be used as a directory and a Secret name. The
// plane mints `prj_` plus lowercase base32; anything else is refused, not put on a path.
var projectID = regexp.MustCompile(`^[a-z0-9][a-z0-9_]{0,62}$`)

// artifactID is the shape of a plane id (`art_<ULID>` and friends): one path component, never
// "..", since the id becomes a directory and a volume subPath.
var artifactID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

func checkArtifactID(art wire.ArtifactDesired) error {
	if !artifactID.MatchString(art.ArtifactID) {
		return fmt.Errorf("artifact id %q is not a valid id", art.ArtifactID)
	}
	return nil
}

// ProjectTokenSecret is the Secret a project's Hugging Face token is materialised into. A function
// of the namespace and the project, not of the single workload namespace, so per-project
// namespaces can come later without renaming anything.
func ProjectTokenSecret(namespace, project string) types.NamespacedName {
	return types.NamespacedName{Namespace: namespace, Name: "hf-" + Name(project)}
}

// Private says whether an artifact belongs to one project, not the public catalogue.
func Private(art wire.ArtifactDesired) bool { return art.OwnerProject != "" }

// cacheDir is the directory under /models an artifact's bytes live in, relative to the volume
// root: the subPath a pod mounts it by.
func cacheDir(art wire.ArtifactDesired) (string, error) {
	if art.ArtifactID != "" {
		if err := checkArtifactID(art); err != nil {
			return "", err
		}
	}
	if Private(art) {
		if !projectID.MatchString(art.OwnerProject) {
			return "", fmt.Errorf("artifact %s: owner project %q is not a valid project id", art.ArtifactID, art.OwnerProject)
		}
		return projectDir + "/" + art.OwnerProject, nil
	}
	if hf, _ := prefetchSource(art); hf != nil {
		return publicHFDir, nil
	}
	if art.ArtifactID == "" {
		return "", fmt.Errorf("no artifact to serve")
	}
	return art.ArtifactID, nil
}

// hfHome is HF_HOME for a pod serving or fetching this artifact.
func hfHome(art wire.ArtifactDesired) string {
	if Private(art) {
		return modelCacheRoot + "/" + projectDir + "/" + art.OwnerProject + "/" + publicHFDir
	}
	return HFHome
}

// pathDir is where an artifact fetched from a non-HF source lands.
func pathDir(art wire.ArtifactDesired) string {
	if Private(art) {
		return modelCacheRoot + "/" + projectDir + "/" + art.OwnerProject + "/" + art.ArtifactID
	}
	return modelCacheRoot + "/" + art.ArtifactID
}

// modelCacheMount mounts the artifact's own cache directory at the same path it has under the
// volume root, so every path the render hands an engine is the same with or without subPath.
func modelCacheMount(art wire.ArtifactDesired, readOnly bool) (corev1.VolumeMount, error) {
	dir, err := cacheDir(art)
	if err != nil {
		return corev1.VolumeMount{}, err
	}
	return corev1.VolumeMount{
		Name: "model-cache", MountPath: modelCacheRoot + "/" + dir, SubPath: dir, ReadOnly: readOnly,
	}, nil
}

// publicHFMount mounts only the public Hugging Face cache, for a pod with no artifact of its own.
func publicHFMount(readOnly bool) corev1.VolumeMount {
	return corev1.VolumeMount{Name: "model-cache", MountPath: HFHome, SubPath: publicHFDir, ReadOnly: readOnly}
}

// hfTokenEnvFrom is the token a pod reads as environment. A public artifact keeps the optional
// cluster-wide hf-token-secret. A private one gets its project's Secret when the plane named a
// credential, and nothing otherwise: the cluster-wide token is never lent to a project's fetch.
func hfTokenEnvFrom(art wire.ArtifactDesired, namespace string) []corev1.EnvFromSource {
	if !Private(art) {
		return []corev1.EnvFromSource{{
			SecretRef: &corev1.SecretEnvSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: "hf-token-secret"},
				Optional:             ptr(true),
			},
		}}
	}
	if CredentialRef(art) == "" {
		return nil
	}
	return []corev1.EnvFromSource{{
		SecretRef: &corev1.SecretEnvSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: ProjectTokenSecret(namespace, art.OwnerProject).Name},
		},
	}}
}

// CredentialRef is the secret reference of the source a private artifact is fetched from, or
// empty. Public artifacts read the cluster-wide token and ignore it.
func CredentialRef(art wire.ArtifactDesired) string {
	if !Private(art) {
		return ""
	}
	hf, other := prefetchSource(art)
	src := hf
	if src == nil {
		src = other
	}
	if src == nil || src.CredentialRef == nil {
		return ""
	}
	return *src.CredentialRef
}

// servingEnvFrom is the token a serving pod reads. A private artifact's workers get none: they
// read weights the prefetch job already put in the project's cache, with HF_HUB_OFFLINE set.
func servingEnvFrom(art wire.ArtifactDesired) []corev1.EnvFromSource {
	if Private(art) {
		return nil
	}
	return hfTokenEnvFrom(art, "")
}
