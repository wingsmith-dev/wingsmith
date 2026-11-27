package render

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

var updateGolden = flag.Bool("update", false, "rewrite the render golden files")

func containerInputs() Inputs {
	in := testInputs()
	in.Deployment.MinReplicas = 1
	in.Deployment.Placement.DevicesPerReplica = 1
	in.Deployment.Serving = wire.ServingSpec{
		Engine:  wire.RuntimeContainer,
		Digest:  "svg_" + strings.Repeat("c", 64),
		Runtime: wire.RuntimeContainer,
		Container: &wire.ContainerSpec{
			Image:    "ghcr.io/acme/worker@sha256:" + strings.Repeat("a", 64),
			Command:  []string{"python", "-m", "worker"},
			Args:     []string{"--model", "$(WS_MODEL_PATH)", "--port", "$(WS_PORT)"},
			Env:      []wire.EnvVar{{Name: "LOG_LEVEL", Value: "info"}},
			Ports:    []wire.ContainerPort{{Name: "http", ContainerPort: 8000}},
			Protocol: "openai-chat",
			Metrics:  &wire.MetricsSpec{Port: "http", Path: "/metrics"},
		},
	}
	art := hfArtifact()
	art.Sources = []wire.ArtifactSource{{Kind: "hf", URI: "hf://Qwen/Qwen3-8B@" + strings.Repeat("f", 40)}}
	in.Artifact = art
	return in
}

func golden(t *testing.T, name string, v any) {
	t.Helper()
	got, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := filepath.Join("testdata", name)
	if *updateGolden {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s (run with -update): %v", path, err)
	}
	if string(want) != string(got) {
		t.Fatalf("%s differs from the render; run go test ./internal/render -run Golden -update and review\n%s", path, got)
	}
}

// containerOptions are testOptions with the prefix list a container render requires.
func containerOptions() Options {
	opts := testOptions()
	opts.ImagePrefixes = []string{"ghcr.io/acme"}
	return opts
}

func TestContainerGolden(t *testing.T) {
	objs, err := Container(containerInputs(), containerOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	golden(t, "container-deployment.json", objs.Deployment)
	golden(t, "container-service.json", objs.Service)
}

func TestContainerInjectsTheModelAndHardensThePod(t *testing.T) {
	objs, err := Container(containerInputs(), containerOptions())
	if err != nil {
		t.Fatal(err)
	}
	pod := objs.Deployment.Spec.Template.Spec
	c := pod.Containers[0]
	for name, want := range map[string]string{
		EnvModelPath:       "/models/hf/hub/models--Qwen--Qwen3-8B/snapshots/" + strings.Repeat("f", 40),
		EnvServedModelName: "qwen3-8b",
		EnvPort:            "8000",
		"HF_HUB_OFFLINE":   "1",
	} {
		if got, _ := envValue(c, name); got != want {
			t.Errorf("%s = %q, want %q", name, got, want)
		}
	}
	if m := c.VolumeMounts; len(m) != 1 || !m[0].ReadOnly || m[0].SubPath != "hf" {
		t.Errorf("model mount = %+v, want one read-only subPath mount", m)
	}
	if pod.HostNetwork || *pod.AutomountServiceAccountToken || !*pod.SecurityContext.RunAsNonRoot {
		t.Error("pod must not use the host network, mount a token, or run as root")
	}
	for _, v := range pod.Volumes {
		if v.HostPath != nil {
			t.Errorf("volume %s is a hostPath", v.Name)
		}
	}
	sc := c.SecurityContext
	if *sc.Privileged || *sc.AllowPrivilegeEscalation || sc.Capabilities.Drop[0] != "ALL" {
		t.Errorf("security context = %+v", sc)
	}
	if c.ReadinessProbe == nil || c.ReadinessProbe.HTTPGet.Path != "/v1/models" || c.ReadinessProbe.HTTPGet.Port.StrVal != "http" {
		t.Errorf("default readiness = %+v", c.ReadinessProbe)
	}
	if got := c.Resources.Limits[GPUResource]; got.Value() != 1 {
		t.Errorf("gpu limit = %v", got.Value())
	}
	for _, labels := range []map[string]string{objs.Deployment.Labels, objs.Service.Labels, objs.Deployment.Spec.Template.Labels} {
		if labels[RoleLabel] != FrontendRole || labels[RuntimeLabel] != "container" || labels[deploymentIDLabel] != "dep_01J9X2" {
			t.Errorf("labels = %v", labels)
		}
	}
	if p := objs.Service.Spec.Ports; len(p) != 1 || p[0].Name != "http" || p[0].Port != 8000 {
		t.Errorf("service ports = %+v", p)
	}
}

func TestContainerRefuses(t *testing.T) {
	cases := map[string]func(*Inputs, *Options){
		"no allowed prefixes":                func(_ *Inputs, o *Options) { o.ImagePrefixes = nil },
		"image by tag":                       func(in *Inputs, _ *Options) { in.Deployment.Serving.Container.Image = "ghcr.io/acme/worker:1.0" },
		"image outside the allowed prefixes": func(_ *Inputs, o *Options) { o.ImagePrefixes = []string{"nvcr.io"} },
		"no http port": func(in *Inputs, _ *Options) {
			in.Deployment.Serving.Container.Ports = []wire.ContainerPort{{Name: "grpc", ContainerPort: 9000}}
		},
		"unpinned artifact": func(in *Inputs, _ *Options) {
			in.Artifact.Sources = []wire.ArtifactSource{{Kind: "hf", URI: "hf://Qwen/Qwen3-8B"}}
		},
		"exec probe": func(in *Inputs, _ *Options) {
			in.Deployment.Serving.Container.LivenessProbe = map[string]any{"exec": map[string]any{"command": []any{"true"}}}
		},
		"multi-node layout": func(in *Inputs, _ *Options) { in.Layout = Layout{NodesPerReplica: 2, DevicesPerNode: 8} },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			in, opts := containerInputs(), containerOptions()
			mutate(&in, &opts)
			if _, err := Container(in, opts); err == nil {
				t.Fatal("rendered; want a refusal")
			}
		})
	}
}

func TestContainerTagAllowedByOperatorFlag(t *testing.T) {
	in, opts := containerInputs(), containerOptions()
	in.Deployment.Serving.Container.Image = "ghcr.io/acme/worker:1.0"
	opts.ContainerImageTagsAllowed = true
	if _, err := Container(in, opts); err != nil {
		t.Fatalf("render: %v", err)
	}
}

func TestContainerDeclaredProbeIsKept(t *testing.T) {
	in := containerInputs()
	in.Deployment.Serving.Container.ReadinessProbe = map[string]any{
		"httpGet":       map[string]any{"path": "/ready", "port": "http"},
		"periodSeconds": float64(5),
	}
	objs, err := Container(in, containerOptions())
	if err != nil {
		t.Fatal(err)
	}
	p := objs.Deployment.Spec.Template.Spec.Containers[0].ReadinessProbe
	if p.HTTPGet.Path != "/ready" || p.PeriodSeconds != 5 {
		t.Errorf("readiness = %+v", p)
	}
}

func TestContainerDefaultReadinessFollowsTheProtocol(t *testing.T) {
	for protocol, want := range map[string]string{
		"openai-chat":                 "/v1/models",
		"openai-images":               "/health",
		"openai-audio-speech":         "/health",
		"openai-audio-transcriptions": "/health",
		"openai-videos":               "/health",
	} {
		in := containerInputs()
		in.Deployment.Serving.Container.Protocol = protocol
		objs, err := Container(in, containerOptions())
		if err != nil {
			t.Fatal(err)
		}
		if got := objs.Deployment.Spec.Template.Spec.Containers[0].ReadinessProbe.HTTPGet.Path; got != want {
			t.Errorf("%s readiness path = %s, want %s", protocol, got, want)
		}
	}
}
