package reconcile

import (
	"context"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// A role that is entirely absent must not read as a shortfall that will resolve itself.
//
// The arithmetic is what hides it: a 1P:2D group with two ready decode workers and no prefill has
// two of three workers ready, which is a shortfall like any other and reads as degraded on its way
// up. It is not on its way up, a decode worker with no prefill peer has nothing to decode from,
// so nothing is serving at all, and without `roleShort` the totals cannot tell the two apart.
func TestARoleThatIsShortIsDegradedEvenWhenTheTotalsWouldNotSaySo(t *testing.T) {
	full := rollout{desired: 3, ready: 3}
	if got := full.phase(); got != phaseRunning {
		t.Fatalf("phase = %s, want %s when every role is ready", got, phaseRunning)
	}
	short := rollout{desired: 3, ready: 3, roleShort: true}
	if got := short.phase(); got != phaseDegraded {
		t.Fatalf("phase = %s, want %s when a role is short", got, phaseDegraded)
	}
}

// An aggregated deployment never sets it, so nothing about its reported phase moves.
func TestAnAggregatedRolloutIsUnaffectedByRoleReadiness(t *testing.T) {
	for _, tc := range []struct {
		name string
		out  rollout
		want string
	}{
		{"complete", rollout{desired: 2, ready: 2}, phaseRunning},
		{"short", rollout{desired: 2, ready: 1}, phaseDegraded},
		{"nothing ready", rollout{desired: 2, ready: 0}, phasePlaced},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.out.phase(); got != tc.want {
				t.Fatalf("phase = %s, want %s", got, tc.want)
			}
		})
	}
}

// The split the totals cannot express, read back off the pods and put on the wire.
//
// `2 of 3 ready` is the same pair of integers for "one prefill missing" and "one decode missing",
// and the two are a deployment that serves nothing and a deployment that serves slowly. The
// per-role counts are the only thing that separates them, which is why they are reported beside
// the scalars, not left to be inferred from the replica frames.
func TestADisaggregatedRolloutReportsEachRolesOwnCounts(t *testing.T) {
	dep := disaggregatedDesired()
	pods := append(roleWorkerPods(render.PrefillComponent, 1), roleWorkerPods(render.WorkerComponent, 1)...)
	out := observedRolloutFor(t, dep, pods...)

	want := []wire.RoleObserved{
		{Role: "prefill", Desired: 1, Ready: 1},
		{Role: "decode", Desired: 2, Ready: 1},
	}
	if !reflect.DeepEqual(out.roles, want) {
		t.Fatalf("roles = %+v, want %+v", out.roles, want)
	}
	// The scalars stay the rollup, which is what makes the field additive instead of a
	// replacement: a plane that reads only these is right, less precisely.
	if out.desired != 3 || out.ready != 2 {
		t.Fatalf("scalars = %d of %d, want 2 of 3", out.ready, out.desired)
	}
	if !out.roleShort {
		t.Fatal("a short decode role must still set roleShort")
	}
}

// An aggregated deployment sends no roles at all, and empty is not the same claim as absent.
//
// A single entry would restate `ready of desired` under a second name and make every deployment in
// the fleet look like one with a declared ratio; an empty array would say this deployment's roles
// are known to be none. Neither is what "the question does not apply" means.
func TestAnAggregatedRolloutReportsNoRolesAtAll(t *testing.T) {
	out := observedRolloutFor(t, testDeploymentDesired(), readyPods(2)...)
	if out.roles != nil {
		t.Fatalf("roles = %+v, want nil for an aggregated deployment", out.roles)
	}
}

func disaggregatedDesired() wire.DeploymentDesired {
	dep := testDeploymentDesired()
	dep.MinReplicas = 1
	dep.RoleReplicas = map[string]int{"prefill": 1, "decode": 2}
	dep.Serving.Topology = "disaggregated"
	dep.Serving.Roles = []wire.RoleSpec{{Role: "prefill"}, {Role: "decode"}}
	return dep
}

func roleWorkerPods(component string, n int) []*corev1.Pod {
	out := readyPods(n)
	for i, pod := range out {
		pod.Name = fmt.Sprintf("%s-%s-abc-%d", render.Name(testDeploymentID), strings.ToLower(component), i)
		pod.Labels[componentNameLabel] = component
	}
	return out
}

func observedRolloutFor(t *testing.T, dep wire.DeploymentDesired, pods ...*corev1.Pod) rollout {
	t.Helper()
	objects := make([]client.Object, 0, len(pods))
	for _, pod := range pods {
		objects = append(objects, pod)
	}
	r := &SyncedResourceReconciler{
		Client:    fake.NewClientBuilder().WithScheme(clientgoscheme.Scheme).WithObjects(objects...).Build(),
		Scheme:    clientgoscheme.Scheme,
		Log:       slog.Default(),
		Namespace: "hangar-system",
		Render:    testRender,
		ClusterID: "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
	}
	out, err := r.observeRollout(context.Background(), dep)
	if err != nil {
		t.Fatalf("observe: %v", err)
	}
	return out
}
