package reconcile

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const watchedDeploymentID = "dep_01j9x2wwwwwwwwwwwwwwwwwwww"

func TestADeploymentPodEnqueuesTheDeclarationThatRenderedIt(t *testing.T) {
	r := &SyncedResourceReconciler{Namespace: "hangar-system"}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name:      "qwen-frontend-0",
		Namespace: "dynamo",
		Labels:    map[string]string{deploymentIDLabel: watchedDeploymentID},
	}}

	requests := r.declaredForPod(context.Background(), pod)
	if len(requests) != 1 {
		t.Fatalf("requests = %v, want one declaration", requests)
	}
	got := requests[0].NamespacedName
	if got.Namespace != "hangar-system" || got.Name != resourceName(watchedDeploymentID) {
		t.Errorf("request = %s, want hangar-system/%s", got, resourceName(watchedDeploymentID))
	}
}

func TestAPodWithoutADeploymentIDEnqueuesNothing(t *testing.T) {
	r := &SyncedResourceReconciler{Namespace: "hangar-system"}
	if got := r.declaredForPod(context.Background(), &corev1.Pod{}); len(got) != 0 {
		t.Errorf("requests = %v, want none", got)
	}
}

func TestPodStatusKeyMovesWhenAnImagePullFails(t *testing.T) {
	pod := &corev1.Pod{
		Spec: corev1.PodSpec{NodeName: "cpu-0"},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			ContainerStatuses: []corev1.ContainerStatus{{
				Name: "main",
				State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
					Reason: "ContainerCreating",
				}},
			}},
		},
	}
	before := podStatusKey(pod)
	pod.Status.ContainerStatuses[0].State.Waiting.Reason = "ImagePullBackOff"

	if after := podStatusKey(pod); after == before {
		t.Fatal("ImagePullBackOff did not move the observed pod status")
	}
}

func TestPodStatusKeyIgnoresAChangingBackoffMessage(t *testing.T) {
	pod := &corev1.Pod{Status: corev1.PodStatus{
		Phase: corev1.PodPending,
		ContainerStatuses: []corev1.ContainerStatus{{
			Name: "main",
			State: corev1.ContainerState{Waiting: &corev1.ContainerStateWaiting{
				Reason:  "ImagePullBackOff",
				Message: "back-off 10s",
			}},
		}},
	}}
	before := podStatusKey(pod)
	pod.Status.ContainerStatuses[0].State.Waiting.Message = "back-off 20s"

	if after := podStatusKey(pod); after != before {
		t.Fatal("a changing backoff message would enqueue the same failure repeatedly")
	}
}
