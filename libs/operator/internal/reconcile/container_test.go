package reconcile

import (
	"context"
	"log/slog"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

func containerLabels(id string) map[string]string {
	return map[string]string{deploymentIDLabel: id, render.RuntimeLabel: wire.RuntimeContainer, render.RoleLabel: render.FrontendRole}
}

func containerObjects(name, id string) []client.Object {
	meta := metav1.ObjectMeta{Name: name, Namespace: testRender.Namespace, Labels: containerLabels(id)}
	return []client.Object{&appsv1.Deployment{ObjectMeta: meta}, &corev1.Service{ObjectMeta: meta}}
}

func containerBackend(t *testing.T, objects ...client.Object) (containerRenderer, client.Client) {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(objects...).Build()
	r := &SyncedResourceReconciler{Client: c, Log: slog.Default(), Render: testRender, ClusterID: "clu_x"}
	return containerRenderer{r.backend()}, c
}

func exists(t *testing.T, c client.Client, obj client.Object, name string) bool {
	t.Helper()
	err := c.Get(context.Background(), types.NamespacedName{Namespace: testRender.Namespace, Name: name}, obj)
	if err != nil && !apierrors.IsNotFound(err) {
		t.Fatal(err)
	}
	return err == nil
}

func TestContainerPruneKeepsTheCurrentNameAndDeleteRemovesAll(t *testing.T) {
	objects := append(containerObjects("old-name", "dep_1"), containerObjects("new-name", "dep_1")...)
	objects = append(objects, containerObjects("other", "dep_2")...)
	b, c := containerBackend(t, objects...)

	if err := b.Prune(context.Background(), wire.DeploymentDesired{DeploymentID: "dep_1"}, "new-name"); err != nil {
		t.Fatal(err)
	}
	if exists(t, c, &appsv1.Deployment{}, "old-name") || exists(t, c, &corev1.Service{}, "old-name") {
		t.Fatal("the renamed deployment's old objects survived")
	}
	if !exists(t, c, &appsv1.Deployment{}, "new-name") || !exists(t, c, &corev1.Service{}, "new-name") {
		t.Fatal("the current objects were pruned")
	}
	if err := b.Delete(context.Background(), "dep_1"); err != nil {
		t.Fatal(err)
	}
	if exists(t, c, &appsv1.Deployment{}, "new-name") {
		t.Fatal("delete left the deployment")
	}
	if !exists(t, c, &appsv1.Deployment{}, "other") {
		t.Fatal("delete removed another deployment's objects")
	}
}

func TestContainerObserveCountsReadyWorkerPods(t *testing.T) {
	pod := func(name string, ready corev1.ConditionStatus) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testRender.Namespace, Labels: containerLabels("dep_1")},
			Spec:       corev1.PodSpec{NodeName: "n1"},
			Status: corev1.PodStatus{Phase: corev1.PodRunning, Conditions: []corev1.PodCondition{
				{Type: corev1.PodReady, Status: ready},
			}},
		}
	}
	b, _ := containerBackend(t, pod("a", corev1.ConditionTrue), pod("b", corev1.ConditionFalse))
	dep := wire.DeploymentDesired{
		DeploymentID: "dep_1", MinReplicas: 2,
		Serving: wire.ServingSpec{Runtime: wire.RuntimeContainer, Container: &wire.ContainerSpec{Image: "img"}},
	}
	out, err := b.Observe(context.Background(), dep)
	if err != nil {
		t.Fatal(err)
	}
	if out.Desired != 2 || out.Ready != 1 || !out.Converging || out.Phase != phaseDegraded {
		t.Fatalf("rollout = %+v", out)
	}
}

func TestKubeBackendPicksTheRendererByRuntime(t *testing.T) {
	b := (&SyncedResourceReconciler{}).backend()
	if _, ok := b.renderer(wire.DeploymentDesired{}).(dynamoRenderer); !ok {
		t.Fatal("a deployment with no runtime is dynamo")
	}
	container := wire.DeploymentDesired{Serving: wire.ServingSpec{Runtime: wire.RuntimeContainer, Container: &wire.ContainerSpec{}}}
	if _, ok := b.renderer(container).(containerRenderer); !ok {
		t.Fatal("runtime container did not pick the container renderer")
	}
}
