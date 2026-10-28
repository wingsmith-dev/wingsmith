package reconcile

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
)

const watchedDeploymentID = "dep_01j9x2wwwwwwwwwwwwwwwwwwww"

func TestADeploymentPodEnqueuesTheDeclarationThatRenderedIt(t *testing.T) {
	r := &SyncedResourceReconciler{Namespace: "wingsmith-system"}
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
	if got.Namespace != "wingsmith-system" || got.Name != resourceName(watchedDeploymentID) {
		t.Errorf("request = %s, want wingsmith-system/%s", got, resourceName(watchedDeploymentID))
	}
}

func TestAPodWithoutADeploymentIDEnqueuesNothing(t *testing.T) {
	r := &SyncedResourceReconciler{Namespace: "wingsmith-system"}
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

// TestTheGraphWatchIsOnlyRegisteredWhereDynamoIsServed: watching a kind the API server does not
// know fails the controller at startup, which crash-looped the operator on a cluster without
// Dynamo.
func TestTheGraphWatchIsOnlyRegisteredWhereDynamoIsServed(t *testing.T) {
	without := apimeta.NewDefaultRESTMapper(nil)
	if served, err := servesGraph(without); err != nil || served {
		t.Fatalf("a cluster without the CRD: served=%v err=%v, want false and no error", served, err)
	}
	with := apimeta.NewDefaultRESTMapper(nil)
	with.Add(render.GVK, apimeta.RESTScopeNamespace)
	if served, err := servesGraph(with); err != nil || !served {
		t.Fatalf("a cluster with the CRD: served=%v err=%v, want true and no error", served, err)
	}
}
