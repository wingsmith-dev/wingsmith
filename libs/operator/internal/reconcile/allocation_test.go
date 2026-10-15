package reconcile

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/observe"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// fakeDevices is a fixed pod-to-accelerator binding, standing in for a scrape of the exporters.
type fakeDevices map[string][]string

func (f fakeDevices) DevicesForPod(namespace, name string) []string {
	return f[observe.PodKey(namespace, name)]
}

// allocatingReconciler is a reconciler with a known device binding and nothing else — the rollout
// helpers it exercises read pods that are handed to them, so no API server is involved.
func allocatingReconciler(devices DeviceIndex) *SyncedResourceReconciler {
	return &SyncedResourceReconciler{ClusterID: "ap-cluster-1", Devices: devices}
}

func gpuPod(name string, node string) corev1.Pod {
	return corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "dynamo"},
		Spec:       corev1.PodSpec{NodeName: node},
		Status: corev1.PodStatus{
			Phase:      corev1.PodRunning,
			Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}},
		},
	}
}

// TestAReplicaReportsTheCardsItHolds: a replica's allocation frame must be built and sent, or
// `bind_allocation` stays empty while the accelerators are committed.
func TestAReplicaReportsTheCardsItHolds(t *testing.T) {
	devices := fakeDevices{
		observe.PodKey("dynamo", "gemma-decode-0"): {"GPU-aaaa", "GPU-bbbb"},
	}
	r := allocatingReconciler(devices)
	dep := testDeploymentDesired()

	out := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-0", "node-a")}, dep, 1, "vllm", "0.9.1")

	if len(out.allocations) != 1 {
		t.Fatalf("built %d allocations, want 1", len(out.allocations))
	}
	got := out.allocations[0]
	if len(got.DeviceIDs) != 2 {
		t.Fatalf("allocation holds %v, want two cards", got.DeviceIDs)
	}
	if got.DeviceIDs[0] != observe.DeviceID(r.ClusterID, "GPU-aaaa") {
		t.Fatalf("device id is %q, not the id this cluster mints for that UUID", got.DeviceIDs[0])
	}
	if got.DeploymentID != dep.DeploymentID || got.ProjectID != dep.ProjectID {
		t.Fatalf("allocation is attributed to %q/%q", got.DeploymentID, got.ProjectID)
	}
	if got.ReplicaID != observe.ReplicaID(r.ClusterID, "dynamo", "gemma-decode-0") {
		t.Fatalf("allocation names replica %q", got.ReplicaID)
	}
	if !got.Exclusive {
		t.Fatal("allocation is not exclusive; the device plugin hands out whole devices")
	}
}

// TestAReplicaWithNoKnownBindingReportsNoAllocation: an empty allocation is a claim that the
// replica holds nothing, which would hand every card it is actually using back to placement.
func TestAReplicaWithNoKnownBindingReportsNoAllocation(t *testing.T) {
	r := allocatingReconciler(fakeDevices{})
	out := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-0", "node-a")}, testDeploymentDesired(), 1, "", "")
	if len(out.allocations) != 0 {
		t.Fatalf("built %+v with no binding to build it from", out.allocations)
	}
}

// TestNoDeviceIndexReportsNoAllocations: a loop that cannot read the exporters does not know what
// anything is holding, and must say nothing rather than say zero.
func TestNoDeviceIndexReportsNoAllocations(t *testing.T) {
	r := allocatingReconciler(nil)
	out := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-0", "node-a")}, testDeploymentDesired(), 1, "", "")
	if len(out.allocations) != 0 {
		t.Fatalf("built %+v with no device index", out.allocations)
	}
}

// TestADepartedReplicaHandsItsCardsBack is the half that is easy to leave out and cannot be. The
// plane holds an allocation for as long as it is reported and never prunes demand state, so an
// allocation that merely stopped being sent stayed held forever and the cards under it were never
// free again.
func TestADepartedReplicaHandsItsCardsBack(t *testing.T) {
	devices := fakeDevices{
		observe.PodKey("dynamo", "gemma-decode-0"): {"GPU-aaaa"},
	}
	r := allocatingReconciler(devices)
	dep := testDeploymentDesired()
	rep := &recordingReporter{}

	first := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-0", "node-a")}, dep, 1, "", "")
	r.reportAllocations(rep, dep.DeploymentID, first)

	// The pod is replaced: the old one is gone and a new one holds the card.
	delete(devices, observe.PodKey("dynamo", "gemma-decode-0"))
	devices[observe.PodKey("dynamo", "gemma-decode-1")] = []string{"GPU-aaaa"}
	second := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-1", "node-a")}, dep, 1, "", "")
	r.reportAllocations(rep, dep.DeploymentID, second)

	released := releaseFor(t, rep, observe.AllocationID(r.ClusterID, "dynamo", "gemma-decode-0"))
	if released.DeviceIDs == nil {
		t.Fatal("release names nil devices; the field is required and marshals to null")
	}
	if len(released.DeviceIDs) != 0 {
		t.Fatalf("release still names %v", released.DeviceIDs)
	}
	// Without these the receiving side cannot resolve the frame and drops it, and the cards it
	// was sent to free stay held.
	if released.DeploymentID != dep.DeploymentID || released.ProjectID != dep.ProjectID {
		t.Fatalf("release is unresolvable: deployment %q project %q", released.DeploymentID, released.ProjectID)
	}
}

// TestAReplicaThatStaysIsNotReleased: a deployment sitting still must not hand its own cards back
// on every pass.
func TestAReplicaThatStaysIsNotReleased(t *testing.T) {
	devices := fakeDevices{observe.PodKey("dynamo", "gemma-decode-0"): {"GPU-aaaa"}}
	r := allocatingReconciler(devices)
	dep := testDeploymentDesired()
	rep := &recordingReporter{}

	for i := 0; i < 3; i++ {
		out := r.rolloutFrom([]corev1.Pod{gpuPod("gemma-decode-0", "node-a")}, dep, 1, "", "")
		r.reportAllocations(rep, dep.DeploymentID, out)
	}
	for _, got := range rep.allocationFrames {
		if len(got.DeviceIDs) == 0 {
			t.Fatalf("a replica that never moved was released: %+v", got)
		}
	}
}

func releaseFor(t *testing.T, rep *recordingReporter, allocationID string) wire.AllocationObserved {
	t.Helper()
	for i := len(rep.allocationFrames) - 1; i >= 0; i-- {
		if rep.allocationFrames[i].AllocationID == allocationID && len(rep.allocationFrames[i].DeviceIDs) == 0 {
			return rep.allocationFrames[i]
		}
	}
	t.Fatalf("nothing released %s; frames were %+v", allocationID, rep.allocationFrames)
	return wire.AllocationObserved{}
}
