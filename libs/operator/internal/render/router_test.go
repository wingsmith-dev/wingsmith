package render

import (
	"math"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

func frontendEnv(t *testing.T, in Inputs) []corev1.EnvVar {
	t.Helper()
	obj, err := DynamoGraphDeployment(in, frontendOptions())
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	return componentPod(t, componentNamed(t, obj, FrontendComponent)).Containers[0].Env
}

func useRouter() *wire.RouterSpec {
	return &wire.RouterSpec{
		Mode:                 ptr("kv"),
		QueueThreshold:       ptr(1.0),
		QueuePolicy:          ptr("wspt"),
		KVOverlapScoreCredit: ptr(8.0),
	}
}

// A deployment that declares no router renders the frontend it rendered before the block existed,
// down to the order of its variables. Dynamo hashes the frontend's pod template, so anything else is
// a rolled frontend on every deployment in the fleet the day this operator ships.
func TestAnUndeclaredRouterRendersTheFrontendItAlwaysDid(t *testing.T) {
	for name, in := range map[string]Inputs{"aggregated": testInputs(), "disaggregated": disaggInputs()} {
		var got []string
		for _, env := range frontendEnv(t, in) {
			got = append(got, env.Name+"="+env.Value)
		}
		want := []string{
			"DYN_ENABLE_ANTHROPIC_API=false",
			"DYN_STRIP_ANTHROPIC_PREAMBLE=false",
			"PYTHONHASHSEED=0",
			"DYN_ROUTER_MODE=kv",
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("%s frontend env = %v, want %v", name, got, want)
		}

		empty := in
		empty.Deployment.Serving.Router = &wire.RouterSpec{}
		plain, err := DynamoGraphDeployment(in, frontendOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		blank, err := DynamoGraphDeployment(empty, frontendOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		if !reflect.DeepEqual(plain.Object, blank.Object) {
			t.Fatalf("%s: a router block with nothing set rendered differently from no block", name)
		}
	}
}

// Each declared router setting renders after the mode, in a fixed order and spelling, so the same
// declaration always renders the same template.
func TestADeclaredRouterRendersEachSettingAfterTheMode(t *testing.T) {
	in := disaggInputs()
	in.Deployment.Serving.Router = useRouter()

	env := frontendEnv(t, in)
	var got []string
	for _, e := range env[3:] {
		got = append(got, e.Name+"="+e.Value)
	}
	want := []string{
		"DYN_ROUTER_MODE=kv",
		"DYN_ROUTER_QUEUE_THRESHOLD=1.0",
		"DYN_ROUTER_QUEUE_POLICY=wspt",
		"DYN_ROUTER_KV_OVERLAP_SCORE_CREDIT=8.0",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("router env = %v, want %v", got, want)
	}
}

func TestADeclaredModeReplacesTheDefault(t *testing.T) {
	in := testInputs()
	in.Deployment.Serving.Router = &wire.RouterSpec{Mode: ptr("round-robin")}
	if got := routerMode(t, in); got != "round-robin" {
		t.Fatalf("DYN_ROUTER_MODE = %q, want round-robin", got)
	}
	for _, env := range frontendEnv(t, in) {
		if strings.HasPrefix(env.Name, "DYN_ROUTER_") && env.Name != "DYN_ROUTER_MODE" {
			t.Fatalf("undeclared setting rendered: %s=%s", env.Name, env.Value)
		}
	}
}

// The router is the frontend's business alone. A worker whose template moved because a router
// setting did is a weight reload, which on a large model is tens of minutes a worker.
func TestARouterChangeNeverReachesTheWorkers(t *testing.T) {
	for name, base := range map[string]Inputs{"aggregated": testInputs(), "disaggregated": disaggInputs()} {
		withRouter := base
		withRouter.Deployment.Serving.Router = useRouter()

		plain, err := DynamoGraphDeployment(base, frontendOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		routed, err := DynamoGraphDeployment(withRouter, frontendOptions())
		if err != nil {
			t.Fatalf("render: %v", err)
		}
		roles, err := WorkerRoles(base.Deployment)
		if err != nil {
			t.Fatalf("roles: %v", err)
		}
		for _, role := range roles {
			before := componentNamed(t, plain, role.Component)
			after := componentNamed(t, routed, role.Component)
			if !reflect.DeepEqual(before, after) {
				t.Fatalf("%s: declaring a router changed the %s component", name, role.Component)
			}
			for _, env := range componentPod(t, after).Containers[0].Env {
				if strings.HasPrefix(env.Name, "DYN_ROUTER_") {
					t.Fatalf("%s: %s carries %s", name, role.Component, env.Name)
				}
			}
		}
	}
}

func TestRouterNumbersAreSpelledAsPythonFloats(t *testing.T) {
	for in, want := range map[float64]string{0: "0.0", 1: "1.0", 8: "8.0", 0.25: "0.25", 1.5: "1.5", 100: "100.0"} {
		got, err := routerFloat("queue_threshold", in)
		if err != nil || got != want {
			t.Fatalf("routerFloat(%v) = %q, %v; want %q", in, got, err, want)
		}
	}
}

// The plane refuses all of these first. The operator refuses them again because a frontend handed a
// value Dynamo rejects exits at startup, and a render error is at least a reason on the deployment.
func TestARouterDynamoWouldRefuseIsNotRendered(t *testing.T) {
	for name, router := range map[string]*wire.RouterSpec{
		"direct mode":        {Mode: ptr("direct")},
		"unknown mode":       {Mode: ptr("kv-aware")},
		"lcfs policy":        {QueueThreshold: ptr(1.0), QueuePolicy: ptr("lcfs")},
		"negative threshold": {QueueThreshold: ptr(-0.5)},
		"infinite credit":    {KVOverlapScoreCredit: ptr(math.Inf(1))},
		"nan credit":         {KVOverlapScoreCredit: ptr(math.NaN())},
	} {
		in := testInputs()
		in.Deployment.Serving.Router = router
		if _, err := DynamoGraphDeployment(in, frontendOptions()); err == nil {
			t.Fatalf("%s: rendered without error", name)
		}
	}
}
