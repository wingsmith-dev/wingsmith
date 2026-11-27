package serve

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/backend"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// Labels on every container this backend creates. Adoption after a restart reads them back.
const (
	LabelManagedBy  = "wingsmith.dev/managed-by"
	LabelDeployment = "wingsmith.dev/deployment-id"
	LabelProject    = "wingsmith.dev/project"
	LabelGeneration = "wingsmith.dev/generation"
	LabelDigest     = "wingsmith.dev/serving-digest"
	LabelLayout     = "wingsmith.dev/layout"
	LabelReplica    = "wingsmith.dev/replica"
	LabelGPUs       = "wingsmith.dev/gpus"
	LabelPort       = "wingsmith.dev/port"

	managedBy = "wingsmith-host"
)

// The variables every custom worker gets (P8 container contract, ADR 0031).
const (
	EnvModelPath       = "WS_MODEL_PATH"
	EnvServedModelName = "WS_SERVED_MODEL_NAME"
	EnvPort            = "WS_PORT"
)

// DefaultModelRoot is the host's model cache, laid out as the operator's /models volume (P1.4b).
const DefaultModelRoot = "/var/lib/wingsmith/models"

// containerModelRoot is where the cache appears inside a container, so WS_MODEL_PATH reads the
// same on a host as in a cluster.
const containerModelRoot = "/models"

// httpPort is the port name a custom worker serves on.
const httpPort = "http"

// Rejection reasons, from the closed set in MESSAGES.md.
const (
	reasonRenderFailed = "RenderFailed"
)

var (
	projectIDPattern  = regexp.MustCompile(`^[a-z0-9][a-z0-9_]{0,62}$`)
	artifactIDPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)
	hfRepoPattern     = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$`)
	hfRevPattern      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	envRef            = regexp.MustCompile(`\$\(([A-Za-z_][A-Za-z0-9_]*)\)`)
)

// ErrWeightsNotCached is a deployment whose weights are not in the host cache. Prefetch is P2.5;
// until then the cache is populated by hand and the deployment waits.
var ErrWeightsNotCached = errors.New("weights not cached on this host")

// Policy is the operator's image policy, applied the same way on a host.
type Policy struct {
	// ImagePrefixes is the allowlist. Empty refuses every container workload (fail closed).
	ImagePrefixes []string
	// TagsAllowed lets an image be named by tag instead of digest.
	TagsAllowed bool
}

// model is where one deployment's weights are, inside the container and on the host.
type model struct {
	// dir is the cache subdirectory mounted, relative to the cache root.
	dir string
	// path is WS_MODEL_PATH relative to the cache root.
	path string
}

func reject(format string, args ...any) error {
	return &backend.Rejected{Reason: reasonRenderFailed, Message: fmt.Sprintf(format, args...)}
}

// checkImage applies the allowlist and the digest rule.
func (p Policy) checkImage(id, image string) error {
	if len(p.ImagePrefixes) == 0 {
		return reject("deployment %s: container workloads need allowed image prefixes set on the host (allowed_image_prefixes in host.json)", id)
	}
	if !strings.Contains(image, "@sha256:") && !p.TagsAllowed {
		return reject("deployment %s: image %s must be pinned by digest (@sha256:...); set container_image_tags_allowed on the host to allow tags", id, image)
	}
	if !imageAllowed(image, p.ImagePrefixes) {
		return reject("deployment %s: image %s is outside the allowed prefixes", id, image)
	}
	return nil
}

// imageAllowed matches a prefix at a path, tag or digest boundary, as the operator does.
func imageAllowed(image string, prefixes []string) bool {
	for _, prefix := range prefixes {
		prefix = strings.TrimSpace(prefix)
		if prefix == "" || !strings.HasPrefix(image, prefix) {
			continue
		}
		if image == prefix || strings.HasSuffix(prefix, "/") {
			return true
		}
		switch image[len(prefix)] {
		case '/', ':', '@':
			return true
		}
	}
	return false
}

// modelFor resolves the artifact to the P1.4b cache layout, validating every id that becomes a
// path component.
func modelFor(art wire.ArtifactDesired) (model, error) {
	if art.ArtifactID != "" && !artifactIDPattern.MatchString(art.ArtifactID) {
		return model{}, reject("artifact id %q is not a valid id", art.ArtifactID)
	}
	base := ""
	if art.OwnerProject != "" {
		if !projectIDPattern.MatchString(art.OwnerProject) {
			return model{}, reject("artifact %s: owner project %q is not a valid project id", art.ArtifactID, art.OwnerProject)
		}
		base = "p/" + art.OwnerProject
	}
	for _, s := range art.Sources {
		if !strings.HasPrefix(s.URI, "hf://") {
			continue
		}
		repo, rev, _ := strings.Cut(strings.TrimPrefix(s.URI, "hf://"), "@")
		if rev == "" {
			return model{}, reject("artifact %s: a container worker serves a pinned artifact (hf://repo@revision)", art.ArtifactID)
		}
		if !hfRepoPattern.MatchString(repo) || strings.Contains(repo, "..") || !hfRevPattern.MatchString(rev) {
			return model{}, reject("artifact %s: %q is not a valid Hugging Face reference", art.ArtifactID, s.URI)
		}
		home := "hf"
		if base != "" {
			home = base + "/hf"
		}
		dir := home
		if base != "" {
			dir = base
		}
		return model{dir: dir, path: home + "/hub/models--" + strings.ReplaceAll(repo, "/", "--") + "/snapshots/" + rev}, nil
	}
	if art.ArtifactID == "" {
		return model{}, reject("no artifact to serve")
	}
	if base != "" {
		return model{dir: base, path: base + "/" + art.ArtifactID}, nil
	}
	return model{dir: art.ArtifactID, path: art.ArtifactID}, nil
}

// cached says the weights directory exists on the host and is not empty.
func cached(root string, m model) bool {
	entries, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(m.path)))
	return err == nil && len(entries) > 0
}

// probe is the readiness check the host runs against a replica.
type probe struct {
	Path string
	// Port is the container port probed.
	Port   int
	Scheme string
}

// readinessProbe decodes the declared probe (corev1.Probe-shaped). Only httpGet is run on a
// host; exec is refused everywhere.
func readinessProbe(c *wire.ContainerSpec, serving int) (probe, error) {
	out := probe{Path: "/health", Port: serving, Scheme: "http"}
	if len(c.ReadinessProbe) == 0 {
		return out, nil
	}
	raw, err := json.Marshal(c.ReadinessProbe)
	if err != nil {
		return out, err
	}
	var p struct {
		Exec    any `json:"exec"`
		HTTPGet *struct {
			Path   string          `json:"path"`
			Port   json.RawMessage `json:"port"`
			Scheme string          `json:"scheme"`
		} `json:"httpGet"`
	}
	if err := json.Unmarshal(raw, &p); err != nil {
		return out, err
	}
	if p.Exec != nil {
		return out, errors.New("exec probes are not allowed")
	}
	if p.HTTPGet == nil {
		return out, errors.New("a host runs httpGet readiness probes only")
	}
	if p.HTTPGet.Path != "" {
		out.Path = p.HTTPGet.Path
	}
	if strings.EqualFold(p.HTTPGet.Scheme, "https") {
		out.Scheme = "https"
	}
	if len(p.HTTPGet.Port) > 0 {
		var n int
		var name string
		switch {
		case json.Unmarshal(p.HTTPGet.Port, &n) == nil:
			out.Port = n
		case json.Unmarshal(p.HTTPGet.Port, &name) == nil:
			out.Port = 0
			for _, port := range c.Ports {
				if port.Name == name {
					out.Port = port.ContainerPort
				}
			}
			if out.Port == 0 {
				return out, fmt.Errorf("probe port %q is not a declared port", name)
			}
		}
	}
	return out, nil
}

// expand substitutes $(NAME) from env the way the kubelet does for args; unknown names stay.
func expand(in []string, env map[string]string) []string {
	if in == nil {
		return nil
	}
	out := make([]string, len(in))
	for i, s := range in {
		out[i] = envRef.ReplaceAllStringFunc(s, func(m string) string {
			if v, ok := env[m[2:len(m)-1]]; ok {
				return v
			}
			return m
		})
	}
	return out
}

func tcp(port int) string { return strconv.Itoa(port) + "/tcp" }
