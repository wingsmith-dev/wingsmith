package main

import (
	"testing"
	"time"
)

func TestTheDefaultDrainEndsBeforeTheDefaultGracePeriod(t *testing.T) {
	plan := defaultDrainPlan(defaultShutdownGrace)
	if plan.total()+shutdownMargin > defaultShutdownGrace {
		t.Fatalf("drain total %v plus margin %v overruns the %v grace period", plan.total(), shutdownMargin, defaultShutdownGrace)
	}
	if plan.delay != 5*time.Second || plan.tunnels != 5*time.Second || plan.requests != 16*time.Second {
		t.Fatalf("plan = %+v, want 5s / 16s / 5s", plan)
	}
}

func TestALongerGracePeriodGoesToTheRequests(t *testing.T) {
	plan := defaultDrainPlan(10 * time.Minute)
	if plan.requests != 10*time.Minute-10*time.Second-metricsShutdownTimeout-shutdownMargin {
		t.Fatalf("requests = %v", plan.requests)
	}
}

func TestAShortGracePeriodStillFits(t *testing.T) {
	for _, grace := range []time.Duration{0, time.Second, 6 * time.Second, 10 * time.Second} {
		plan := defaultDrainPlan(grace)
		if plan.requests < 0 || plan.delay < 0 || plan.tunnels < 0 {
			t.Fatalf("grace %v: negative phase in %+v", grace, plan)
		}
		if grace >= 10*time.Second && plan.total()+shutdownMargin > grace {
			t.Fatalf("grace %v: plan %+v overruns it", grace, plan)
		}
	}
}

func TestExplicitPhasesOverrideTheirShare(t *testing.T) {
	t.Setenv("WINGSMITH_GATEWAY_SHUTDOWN_GRACE", "120s")
	t.Setenv("WINGSMITH_GATEWAY_DRAIN_TIMEOUT", "90s")
	plan, grace := readDrainPlan()
	if grace != 120*time.Second || plan.requests != 90*time.Second || plan.delay != 5*time.Second {
		t.Fatalf("plan = %+v, grace = %v", plan, grace)
	}
}
