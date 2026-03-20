package observe

import (
	"context"
	"fmt"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// Labels and annotations the workload discoverer reads.
const (
	// labelManaged marks a workload this plane declared. Its presence is the whole test for
	// "we put this here": anything carrying it is already a deployment we know about, and
	// reporting it as a discovery would offer an operator the chance to adopt something they
	// already own.
	labelManaged = "hangar.dev/deployment-id"

	// labelModel and annotationModel are where a workload says which model it serves, when it
	// says so at all. A hint for the human reading the list, never an input to placement.
	labelModel      = "hangar.dev/model"
	annotationModel = "hangar.dev/model"

	// labelPodTemplateHash is set by the Deployment controller on every ReplicaSet it owns and
	// on their pods. It is what lets a pod be traced back to its Deployment without listing
	// ReplicaSets: the ReplicaSet's name is the Deployment's name with this hash appended.
	labelPodTemplateHash = "pod-template-hash"

	kindDeployment  = "deployment"
	kindStatefulSet = "statefulset"
	kindPod         = "pod"
)

// workloadKey identifies one discovered workload within a namespace.
type workloadKey struct {
	Namespace string
	Kind      string
	Name      string
}

// podUsage is what the pods belonging to one workload add up to.
type podUsage struct {
	devicesPerReplica int
	devicesTotal      int
	replicas          int
	ready             int
	nodes             map[string]bool
}

// +kubebuilder:rbac:groups=apps,resources=deployments;statefulsets,verbs=get;list;watch

// discoverWorkloads reports what the cluster is already running on its accelerators without the
// plane having asked for any of it.
//
// Read-only in the strongest sense: it lists, it does not adopt, it sets no owner reference and it
// writes no label. Nothing here can cause a pod to restart, which is the property that makes it
// safe to run against a cluster full of loaded models.
func (k *KubeDiscoverer) discoverWorkloads(ctx context.Context, pods *corev1.PodList) ([]Workload, error) {
	usage := map[workloadKey]*podUsage{}
	for i := range pods.Items {
		p := &pods.Items[i]
		if !schedulable(p) || managed(p.Labels) {
			continue
		}
		devices := podDevices(p)
		if devices == 0 {
			continue
		}
		key := workloadKeyFor(p)
		u := usage[key]
		if u == nil {
			u = &podUsage{nodes: map[string]bool{}}
			usage[key] = u
		}
		u.replicas++
		u.devicesTotal += devices
		if devices > u.devicesPerReplica {
			u.devicesPerReplica = devices
		}
		if podReady(p) {
			u.ready++
		}
		if p.Spec.NodeName != "" {
			u.nodes[p.Spec.NodeName] = true
		}
	}
	if len(usage) == 0 {
		return nil, nil
	}

	controllers, err := k.controllers(ctx)
	if err != nil {
		return nil, err
	}

	out := make([]Workload, 0, len(usage))
	for key, u := range usage {
		w := Workload{
			Namespace:         key.Namespace,
			Name:              key.Name,
			Kind:              key.Kind,
			Images:            []string{},
			ReplicasDesired:   u.replicas,
			ReplicasReady:     u.ready,
			DevicesPerReplica: u.devicesPerReplica,
			DevicesTotal:      u.devicesTotal,
			Nodes:             keys(u.nodes),
			Labels:            map[string]string{},
		}
		// The controller is the authority on how many replicas were asked for; the pods are
		// the authority on how many devices are actually held. A workload scaled to four with
		// one pod pending is two different facts, and collapsing them would hide the pending
		// one.
		if c, ok := controllers[key]; ok {
			w.ReplicasDesired = c.desired
			w.ReplicasReady = c.ready
			w.Images = c.images
			w.Labels = c.labels
			w.Model = c.model
		}
		out = append(out, w)
	}
	return out, nil
}

// controller is the declared half of a discovered workload, as its own cluster records it.
type controller struct {
	desired int
	ready   int
	images  []string
	labels  map[string]string
	model   string
}

func (k *KubeDiscoverer) controllers(ctx context.Context) (map[workloadKey]controller, error) {
	out := map[workloadKey]controller{}

	var deployments appsv1.DeploymentList
	if err := k.Client.List(ctx, &deployments); err != nil {
		return nil, fmt.Errorf("list deployments: %w", err)
	}
	for i := range deployments.Items {
		d := &deployments.Items[i]
		if managed(d.Labels) {
			continue
		}
		desired := 1
		if d.Spec.Replicas != nil {
			desired = int(*d.Spec.Replicas)
		}
		key := workloadKey{Namespace: d.Namespace, Kind: kindDeployment, Name: d.Name}
		out[key] = controller{
			desired: desired,
			ready:   int(d.Status.ReadyReplicas),
			images:  templateImages(&d.Spec.Template),
			labels:  exportedWorkloadLabels(d.Labels),
			model:   modelHint(d.ObjectMeta, &d.Spec.Template),
		}
	}

	var sets appsv1.StatefulSetList
	if err := k.Client.List(ctx, &sets); err != nil {
		return nil, fmt.Errorf("list statefulsets: %w", err)
	}
	for i := range sets.Items {
		s := &sets.Items[i]
		if managed(s.Labels) {
			continue
		}
		desired := 1
		if s.Spec.Replicas != nil {
			desired = int(*s.Spec.Replicas)
		}
		key := workloadKey{Namespace: s.Namespace, Kind: kindStatefulSet, Name: s.Name}
		out[key] = controller{
			desired: desired,
			ready:   int(s.Status.ReadyReplicas),
			images:  templateImages(&s.Spec.Template),
			labels:  exportedWorkloadLabels(s.Labels),
			model:   modelHint(s.ObjectMeta, &s.Spec.Template),
		}
	}
	return out, nil
}

// workloadKeyFor traces a pod back to the thing that owns it.
//
// A pod owned by a ReplicaSet is a Deployment's pod, and the Deployment's name is the ReplicaSet's
// with the pod-template-hash suffix removed — which is why this needs no permission to read
// ReplicaSets. An owner this does not recognise is reported under its own kind rather than
// dropped: a cluster is entitled to run models under a CRD we have never heard of, and an
// inventory that silently omits them is worse than one that names them oddly.
func workloadKeyFor(p *corev1.Pod) workloadKey {
	owner := metav1.GetControllerOf(p)
	if owner == nil {
		return workloadKey{Namespace: p.Namespace, Kind: kindPod, Name: p.Name}
	}
	kind := strings.ToLower(owner.Kind)
	name := owner.Name
	if kind == "replicaset" {
		if hash := p.Labels[labelPodTemplateHash]; hash != "" {
			name = strings.TrimSuffix(name, "-"+hash)
		}
		kind = kindDeployment
	}
	return workloadKey{Namespace: p.Namespace, Kind: kind, Name: name}
}

// schedulable is whether a pod still holds what it was given. A pod that has finished — succeeded
// or failed — has released its accelerators and is not part of anything running.
func schedulable(p *corev1.Pod) bool {
	if p.Spec.NodeName == "" {
		return false
	}
	return p.Status.Phase != corev1.PodSucceeded && p.Status.Phase != corev1.PodFailed
}

func podReady(p *corev1.Pod) bool {
	for _, c := range p.Status.Conditions {
		if c.Type == corev1.PodReady {
			return c.Status == corev1.ConditionTrue
		}
	}
	return false
}

// podDevices counts the accelerators a pod holds, init containers included: an init container's
// request is held for the pod's whole lifetime, so a workload that asks for its devices there
// holds them just as firmly.
func podDevices(p *corev1.Pod) int {
	total := 0
	for _, c := range p.Spec.Containers {
		if q, ok := c.Resources.Limits[resourceGPU]; ok {
			total += int(q.Value())
		}
	}
	for _, c := range p.Spec.InitContainers {
		if q, ok := c.Resources.Limits[resourceGPU]; ok && int(q.Value()) > total {
			total = int(q.Value())
		}
	}
	return total
}

func managed(labels map[string]string) bool {
	return labels[labelManaged] != ""
}

func templateImages(t *corev1.PodTemplateSpec) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, c := range t.Spec.Containers {
		if c.Image == "" || seen[c.Image] {
			continue
		}
		seen[c.Image] = true
		out = append(out, c.Image)
	}
	return out
}

// modelHint reads what the workload claims to serve, preferring the object's own metadata over its
// pod template's. Empty when nothing says.
func modelHint(meta metav1.ObjectMeta, t *corev1.PodTemplateSpec) string {
	return firstNonEmpty(
		meta.Annotations[annotationModel],
		meta.Labels[labelModel],
		t.Annotations[annotationModel],
		t.Labels[labelModel],
	)
}

// exportedWorkloadLabels narrows a workload's labels to the ones worth showing beside it. A
// Kubernetes object carries dozens and the sighting is not a mirror of it.
func exportedWorkloadLabels(in map[string]string) map[string]string {
	out := map[string]string{}
	for k, v := range in {
		if rest, ok := strings.CutPrefix(k, "hangar.dev/"); ok {
			out[rest] = v
			continue
		}
		if rest, ok := strings.CutPrefix(k, "app.kubernetes.io/"); ok {
			out[rest] = v
		}
	}
	return out
}

func keys(in map[string]bool) []string {
	out := make([]string, 0, len(in))
	for k := range in {
		out = append(out, k)
	}
	return out
}
