package reconcile

import "testing"

// A role that is entirely absent must not read as a shortfall that will resolve itself.
//
// The arithmetic is what hides it: a 1P:2D group with two ready decode workers and no prefill has
// two of three workers ready, which is a shortfall like any other and reads as degraded on its way
// up. It is not on its way up — a decode worker with no prefill peer has nothing to decode from,
// so nothing is serving at all — and without `roleShort` the totals cannot tell the two apart.
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
