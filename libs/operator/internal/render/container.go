package render

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/intstr"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// The custom-worker runtime (P8.2, ADR 0031): a plain apps/v1 Deployment and a Service per
// deployment, no Dynamo. The worker is its own frontend, so its pods and Service carry
// RoleLabel=frontend and the agent's resolver finds it the way it finds a Dynamo frontend.
const (
	// RuntimeLabel marks objects rendered by a non-Dynamo runtime.
	RuntimeLabel = "wingsmith.dev/runtime"
	// FrontendRole is the RoleLabel value of whatever answers requests for a deployment.
	FrontendRole = "frontend"
	// ContainerHTTPPort is the port name a custom worker serves on and the Service exposes.
	ContainerHTTPPort = "http"
	// containerName is the worker container's name inside the pod.
	containerName = "worker"
)

// The variables the platform sets on every custom worker. Args may reference them as $(NAME),
// which the kubelet expands.
const (
	EnvModelPath       = "WS_MODEL_PATH"
	EnvServedModelName = "WS_SERVED_MODEL_NAME"
	EnvPort            = "WS_PORT"
)

// ContainerObjects are what one container deployment renders to.
type ContainerObjects struct {
	Deployment *appsv1.Deployment
	Service    *corev1.Service
	// PodMonitor is a monitoring.coreos.com/v1 PodMonitor, nil unless Options.PodMonitorEnabled
	// and the worker declares metrics. Unstructured so the operator needs no prometheus-operator
	// module.
	PodMonitor map[string]any
}

// PodMonitorGVK is the prometheus-operator kind a container worker's metrics are declared with.
var PodMonitorGVK = schema.GroupVersionKind{Group: "monitoring.coreos.com", Version: "v1", Kind: "PodMonitor"}

// containerLabels are the ownership labels plus the runtime and role, on the Deployment, its pod
// template and the Service alike.
func containerLabels(in Inputs) map[string]string {
	out := map[string]string{RuntimeLabel: wire.RuntimeContainer, RoleLabel: FrontendRole}
	for k, v := range ownershipLabels(in, workerRole{}) {
		out[k] = v.(string)
	}
	return out
}

// containerSelector is the stable subset the Deployment and Service select on. The project label
// is left out: a selector is immutable, and nothing about the deployment id can move.
func containerSelector(in Inputs) map[string]string {
	return map[string]string{
		deploymentIDLabel: in.Deployment.DeploymentID,
		RuntimeLabel:      wire.RuntimeContainer,
	}
}

// Container renders a `runtime: container` deployment.
func Container(in Inputs, opts Options) (ContainerObjects, error) {
	dep := in.Deployment
	c := dep.Serving.Container
	if !dep.Serving.IsContainer() {
		return ContainerObjects{}, fmt.Errorf("deployment %s is not a container runtime", dep.DeploymentID)
	}
	if Disaggregated(dep) || in.Layout.nodes() > 1 {
		return ContainerObjects{}, fmt.Errorf("deployment %s: the container runtime runs one aggregated pod per replica", dep.DeploymentID)
	}
	// A container worker runs an arbitrary image the plane names, so unlike the Dynamo render it
	// fails closed: no prefix list means no container workloads.
	if len(opts.ImagePrefixes) == 0 {
		return ContainerObjects{}, fmt.Errorf("deployment %s: container workloads need WINGSMITH_ALLOWED_IMAGE_PREFIXES set on the operator (chart: operator.images.allowedPrefixes)", dep.DeploymentID)
	}
	if !strings.Contains(c.Image, "@sha256:") && !opts.ContainerImageTagsAllowed {
		return ContainerObjects{}, fmt.Errorf("deployment %s: image %s must be pinned by digest (@sha256:...); set WINGSMITH_CONTAINER_IMAGE_TAGS_ALLOWED on the operator to allow tags", dep.DeploymentID, c.Image)
	}
	if !imageAllowed(c.Image, opts.ImagePrefixes) {
		return ContainerObjects{}, fmt.Errorf("deployment %s: image %s is outside the allowed prefixes", dep.DeploymentID, c.Image)
	}
	port := 0
	for _, p := range c.Ports {
		if p.Name == ContainerHTTPPort {
			port = p.ContainerPort
		}
	}
	if port == 0 {
		return ContainerObjects{}, fmt.Errorf("deployment %s: the container declares no port named %s", dep.DeploymentID, ContainerHTTPPort)
	}
	modelPath, isPath, err := modelRef(in.Artifact)
	if err != nil {
		return ContainerObjects{}, err
	}
	if !isPath {
		return ContainerObjects{}, fmt.Errorf("deployment %s: a container worker serves a pinned artifact (hf://repo@revision or a cached path)", dep.DeploymentID)
	}
	mount, err := modelCacheMount(in.Artifact, true)
	if err != nil {
		return ContainerObjects{}, err
	}

	container, err := workerContainer(in, c, port, modelPath, mount)
	if err != nil {
		return ContainerObjects{}, err
	}

	labels := containerLabels(in)
	name := WorkloadName(dep)
	replicas := int32(Replicas(dep))
	surge, unavailable := intstr.FromString(maxSurge), intstr.FromString(maxUnavailable)
	podAnnotations := map[string]string{}
	if m := c.Metrics; m != nil {
		podAnnotations["prometheus.io/scrape"] = "true"
		podAnnotations["prometheus.io/path"] = m.Path
		for _, p := range c.Ports {
			if p.Name == m.Port {
				podAnnotations["prometheus.io/port"] = strconv.Itoa(p.ContainerPort)
			}
		}
	}
	if len(podAnnotations) == 0 {
		podAnnotations = nil
	}

	deployment := &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        name,
			Namespace:   opts.Namespace,
			Labels:      labels,
			Annotations: inputAnnotations(in),
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Selector: &metav1.LabelSelector{MatchLabels: containerSelector(in)},
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge: &surge, MaxUnavailable: &unavailable,
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels, Annotations: podAnnotations},
				Spec: corev1.PodSpec{
					AutomountServiceAccountToken: ptr(false),
					EnableServiceLinks:           ptr(false),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr(true),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					ImagePullSecrets: imagePullSecrets(opts.EnginePullSecrets),
					NodeSelector:     podNodeSelector(in, opts),
					Tolerations:      WorkerTolerations(opts),
					Volumes: []corev1.Volume{{
						Name: "model-cache",
						VolumeSource: corev1.VolumeSource{
							PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
								ClaimName: opts.ModelCachePVC, ReadOnly: true,
							},
						},
					}},
					Containers: []corev1.Container{container},
				},
			},
		},
	}

	service := &corev1.Service{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "Service"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        name,
			Namespace:   opts.Namespace,
			Labels:      labels,
			Annotations: inputAnnotations(in),
		},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: containerSelector(in),
			Ports: []corev1.ServicePort{{
				Name:       ContainerHTTPPort,
				Port:       int32(port),
				TargetPort: intstr.FromString(ContainerHTTPPort),
				Protocol:   corev1.ProtocolTCP,
			}},
		},
	}
	out := ContainerObjects{Deployment: deployment, Service: service}
	if m := c.Metrics; m != nil && opts.PodMonitorEnabled {
		selector := map[string]any{}
		for k, v := range containerSelector(in) {
			selector[k] = v
		}
		meta := map[string]any{"name": name, "namespace": opts.Namespace, "labels": stringMap(labels), "annotations": stringMap(inputAnnotations(in))}
		out.PodMonitor = map[string]any{
			"apiVersion": PodMonitorGVK.GroupVersion().String(),
			"kind":       PodMonitorGVK.Kind,
			"metadata":   meta,
			"spec": map[string]any{
				"selector":            map[string]any{"matchLabels": selector},
				"podMetricsEndpoints": []any{map[string]any{"port": m.Port, "path": m.Path}},
			},
		}
	}
	return out, nil
}

func stringMap(in map[string]string) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// inputAnnotations are the inputs the render was built from, compared before any write, the same
// three the Dynamo render stamps.
func inputAnnotations(in Inputs) map[string]string {
	return map[string]string{
		GenerationAnnotation:    strconv.FormatInt(in.Deployment.Generation, 10),
		ServingDigestAnnotation: in.Deployment.Serving.Digest,
		LayoutAnnotation:        in.Layout.Fingerprint(),
	}
}

func workerContainer(in Inputs, c *wire.ContainerSpec, port int, modelPath string, mount corev1.VolumeMount) (corev1.Container, error) {
	dep := in.Deployment
	devices := in.Layout.devices(dep)
	requests := corev1.ResourceList{GPUResource: *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	limits := corev1.ResourceList{GPUResource: *resource.NewQuantity(int64(devices), resource.DecimalSI)}
	if n := dep.Serving.Resources.CPUCores; n != nil {
		requests[corev1.ResourceCPU] = *resource.NewQuantity(int64(*n), resource.DecimalSI)
	}
	if n := dep.Serving.Resources.MemoryMiB; n != nil {
		requests[corev1.ResourceMemory] = *resource.NewQuantity(*n*1024*1024, resource.BinarySI)
	}
	if n := dep.Serving.Resources.DiskGiB; n != nil {
		requests[corev1.ResourceEphemeralStorage] = *resource.NewQuantity(int64(*n)*1024*1024*1024, resource.BinarySI)
	}

	// Injected first so a declared variable can reference them; the plane refuses a declared one
	// that reuses an injected name.
	env := []corev1.EnvVar{
		{Name: EnvModelPath, Value: modelPath},
		{Name: EnvServedModelName, Value: servedModelName(in)},
		{Name: EnvPort, Value: strconv.Itoa(port)},
		{Name: "HF_HUB_OFFLINE", Value: "1"},
	}
	for _, e := range c.Env {
		env = append(env, corev1.EnvVar{Name: e.Name, Value: e.Value})
	}
	ports := make([]corev1.ContainerPort, 0, len(c.Ports))
	for _, p := range c.Ports {
		ports = append(ports, corev1.ContainerPort{Name: p.Name, ContainerPort: int32(p.ContainerPort), Protocol: corev1.ProtocolTCP})
	}

	readiness, err := probe(c.ReadinessProbe)
	if err != nil {
		return corev1.Container{}, fmt.Errorf("readiness probe: %w", err)
	}
	if readiness == nil {
		readiness = &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
				Path: readinessPath(c.Protocol), Port: intstr.FromString(ContainerHTTPPort),
			}},
			PeriodSeconds:    10,
			FailureThreshold: 3,
		}
	}
	liveness, err := probe(c.LivenessProbe)
	if err != nil {
		return corev1.Container{}, fmt.Errorf("liveness probe: %w", err)
	}
	startup, err := probe(c.StartupProbe)
	if err != nil {
		return corev1.Container{}, fmt.Errorf("startup probe: %w", err)
	}

	return corev1.Container{
		Name:            containerName,
		Image:           c.Image,
		Command:         c.Command,
		Args:            c.Args,
		Env:             env,
		Ports:           ports,
		ReadinessProbe:  readiness,
		LivenessProbe:   liveness,
		StartupProbe:    startup,
		Resources:       corev1.ResourceRequirements{Requests: requests, Limits: limits},
		VolumeMounts:    []corev1.VolumeMount{mount},
		SecurityContext: containerSecurityContext(),
	}, nil
}

// containerSecurityContext is what every custom worker runs under: no root, no capabilities, no
// privilege of any kind. The plane does not let a workload loosen it.
func containerSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		RunAsNonRoot:             ptr(true),
		Privileged:               ptr(false),
		AllowPrivilegeEscalation: ptr(false),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
	}
}

// probe decodes a corev1.Probe-shaped object. An exec probe is refused: it is a second command
// nobody reviewed, and the plane refuses it too.
func probe(in map[string]any) (*corev1.Probe, error) {
	if len(in) == 0 {
		return nil, nil
	}
	raw, err := json.Marshal(in)
	if err != nil {
		return nil, err
	}
	var out corev1.Probe
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&out); err != nil {
		return nil, err
	}
	if out.Exec != nil {
		return nil, fmt.Errorf("exec probes are not allowed")
	}
	return &out, nil
}

// Unstructured converts a typed object for server-side apply.
func Unstructured(obj runtime.Object) (map[string]any, error) {
	return runtime.DefaultUnstructuredConverter.ToUnstructured(obj)
}

// readinessPath is the default readiness probe path for a worker's protocol. Only the
// OpenAI chat family is required to list its model on `/v1/models`; the media servers
// (diffusers, SGLang-diffusion, Whisper and TTS wrappers) often do not, and answer `/health`.
func readinessPath(protocol string) string {
	switch protocol {
	case "", "openai-chat":
		return "/v1/models"
	default:
		return "/health"
	}
}
