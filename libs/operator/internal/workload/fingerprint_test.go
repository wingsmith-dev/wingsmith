package workload

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func podSpec() *corev1.PodSpec {
	return &corev1.PodSpec{
		Containers: []corev1.Container{{
			Name:    "server",
			Image:   "ghcr.io/wingsmith-dev/vllm:0.9.3",
			Command: []string{"/bin/vllm"},
			Args:    []string{"--model", "Qwen/Qwen3.5-9B"},
			Env: []corev1.EnvVar{
				{Name: "HF_HOME", Value: "/weights"},
				{Name: "VLLM_LOGGING_LEVEL", Value: "INFO"},
			},
			Resources: corev1.ResourceRequirements{
				Limits: corev1.ResourceList{"nvidia.com/gpu": resource.MustParse("1")},
			},
			VolumeMounts: []corev1.VolumeMount{{Name: "weights", MountPath: "/weights"}},
		}},
		Volumes: []corev1.Volume{{
			Name:         "weights",
			VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		}},
	}
}

func TestFingerprintIsStableAcrossCalls(t *testing.T) {
	first, second := Fingerprint(podSpec()), Fingerprint(podSpec())
	if first != second {
		t.Fatalf("two digests of the same spec disagree: %q vs %q", first, second)
	}
	if !strings.HasPrefix(first, "sha256:") {
		t.Errorf("digest is not prefixed with its algorithm: %q", first)
	}
	if len(first) != len("sha256:")+64 {
		t.Errorf("digest is not a full sha256: %q", first)
	}
}

// TestFingerprintMovesWithWhatIsRunning pins the half of the boundary that must trip the adoption
// gate: anything that changes what the workload actually is.
func TestFingerprintMovesWithWhatIsRunning(t *testing.T) {
	base := Fingerprint(podSpec())
	cases := []struct {
		what   string
		mutate func(*corev1.PodSpec)
	}{
		{"image", func(s *corev1.PodSpec) { s.Containers[0].Image = "ghcr.io/wingsmith-dev/vllm:0.9.4" }},
		{"command", func(s *corev1.PodSpec) { s.Containers[0].Command = []string{"/bin/other"} }},
		{"args", func(s *corev1.PodSpec) { s.Containers[0].Args = []string{"--model", "Qwen/Qwen3.8-27B"} }},
		{"env value", func(s *corev1.PodSpec) { s.Containers[0].Env[0].Value = "/elsewhere" }},
		{"env added", func(s *corev1.PodSpec) {
			s.Containers[0].Env = append(s.Containers[0].Env, corev1.EnvVar{Name: "EXTRA", Value: "1"})
		}},
		{"resources", func(s *corev1.PodSpec) {
			s.Containers[0].Resources.Limits["nvidia.com/gpu"] = resource.MustParse("2")
		}},
		{"mount path", func(s *corev1.PodSpec) { s.Containers[0].VolumeMounts[0].MountPath = "/models" }},
		{"volume source", func(s *corev1.PodSpec) {
			s.Volumes[0].VolumeSource = corev1.VolumeSource{
				HostPath: &corev1.HostPathVolumeSource{Path: "/mnt/weights"},
			}
		}},
		{"container added", func(s *corev1.PodSpec) {
			s.Containers = append(s.Containers, corev1.Container{Name: "sidecar", Image: "busybox"})
		}},
	}
	for _, c := range cases {
		spec := podSpec()
		c.mutate(spec)
		if got := Fingerprint(spec); got == base {
			t.Errorf("changing the %s left the digest unmoved", c.what)
		}
	}
}

// TestEnvOrderIsPartOfTheDigest records a real property instead of an aspiration: the digest is
// over the marshalled spec, so a reordered env list reads as a change. Kubernetes preserves the
// order it was given, so this does not fire on its own, but a renderer that sorts env differently
// between versions would trip the gate, and that is worth knowing before it happens.
func TestEnvOrderIsPartOfTheDigest(t *testing.T) {
	spec := podSpec()
	spec.Containers[0].Env[0], spec.Containers[0].Env[1] = spec.Containers[0].Env[1], spec.Containers[0].Env[0]
	if Fingerprint(spec) == Fingerprint(podSpec()) {
		t.Fatal("env order is not reflected in the digest; the comment above is now wrong")
	}
}

func TestFingerprintPodIgnoresWhereItLanded(t *testing.T) {
	placed := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "server-0", Namespace: "inference"},
		Spec:       *podSpec(),
	}
	placed.Spec.NodeName = "iad-1-n01"
	placed.Spec.NodeSelector = map[string]string{"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"}

	elsewhere := placed.DeepCopy()
	elsewhere.Spec.NodeName = "iad-1-n07"
	elsewhere.Spec.NodeSelector = map[string]string{"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"}

	if FingerprintPod(placed) != FingerprintPod(elsewhere) {
		t.Fatal("the same pod on two nodes digests differently; scheduling leaked into the digest")
	}
	if FingerprintPod(placed) != Fingerprint(podSpec()) {
		t.Fatal("clearing scheduling did not land on the bare spec's digest")
	}
}

// TestFingerprintPodDoesNotMutateItsArgument matters because the caller holds a pod out of an
// informer cache: writing to it would corrupt shared state for every other reader.
func TestFingerprintPodDoesNotMutateItsArgument(t *testing.T) {
	p := &corev1.Pod{Spec: *podSpec()}
	p.Spec.NodeName = "iad-1-n01"
	p.Spec.NodeSelector = map[string]string{"zone": "a"}
	FingerprintPod(p)
	if p.Spec.NodeName != "iad-1-n01" || p.Spec.NodeSelector["zone"] != "a" {
		t.Fatal("FingerprintPod wrote through to the pod it was given")
	}
}

// TestFingerprintPodIgnoresIdentityAndStatus: name, namespace, labels and status are about which
// instance this is and how it is doing, not what it is running.
func TestFingerprintPodIgnoresIdentityAndStatus(t *testing.T) {
	a := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "server-0", Namespace: "inference", Labels: map[string]string{"x": "1"}},
		Spec:       *podSpec(),
		Status:     corev1.PodStatus{Phase: corev1.PodRunning},
	}
	b := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "server-9", Namespace: "other", Labels: map[string]string{"y": "2"}},
		Spec:       *podSpec(),
		Status:     corev1.PodStatus{Phase: corev1.PodPending},
	}
	if FingerprintPod(a) != FingerprintPod(b) {
		t.Fatal("identity or status reached the digest")
	}
}

// TestEmptySpecStillDigests: the zero spec is marshallable, so it gets a real digest instead of
// the empty string that means "failed closed".
func TestEmptySpecStillDigests(t *testing.T) {
	if got := Fingerprint(&corev1.PodSpec{}); !strings.HasPrefix(got, "sha256:") {
		t.Fatalf("the zero spec did not digest: %q", got)
	}
}
