package fake

import (
	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
)

// The declared-against-declared arithmetic a serving quota answers with,
// transcribed from the plane so that `--fake` divides a commitment and grades
// it against the standard the same way the server does.

const defaultAvailabilityObjective = "availability-2026.1"

// The floors the acceptance standard asks a served model to commit to.
// Compared against what was declared, never against what was measured.
const (
	standardRPM         = 1_000
	standardTPM         = int64(10_000_000)
	standardSuccessRate = 0.999
)

const (
	minObjectiveWindow = 60
	maxObjectiveWindow = 3_600
)

var availabilityObjectives = map[string]bool{defaultAvailabilityObjective: true}

// checkServingQuota refuses what the plane refuses. A fake that accepted
// everything would teach a command line the plane then rejects.
func checkServingQuota(q api.ServingQuota) error {
	switch q.LimitScope {
	case api.ScopePerReplica, api.ScopeDivided:
	default:
		return errs.New(errs.CodeUsage, "unknown limit scope %q", q.LimitScope).
			WithCause("per_replica or divided")
	}
	switch q.OnExceed {
	case api.OnExceedReject:
		if q.QueueTimeoutMS != nil {
			return errs.New(errs.CodeUsage, "queue-timeout means nothing unless --on-exceed queue")
		}
	case api.OnExceedQueue:
		if q.QueueTimeoutMS == nil {
			return errs.New(errs.CodeUsage, "--on-exceed queue needs a --queue-timeout to bound the wait")
		}
		if *q.QueueTimeoutMS <= 0 {
			return errs.New(errs.CodeUsage, "--queue-timeout must be positive")
		}
	default:
		return errs.New(errs.CodeUsage, "unknown on-exceed %q", q.OnExceed).WithCause("reject or queue")
	}
	if !availabilityObjectives[q.AvailabilityObjective] {
		return errs.New(errs.CodeUsage, "unknown availability objective %q", q.AvailabilityObjective)
	}
	if q.ObjectiveWindowSeconds < minObjectiveWindow || q.ObjectiveWindowSeconds > maxObjectiveWindow {
		return errs.New(errs.CodeUsage, "the objective window must be between %ds and %ds", minObjectiveWindow, maxObjectiveWindow)
	}
	for name, value := range map[string]*int{
		"committed-rpm": q.CommittedRPM, "max-concurrent": q.MaxConcurrentRequests,
		"max-output-tokens": q.MaxOutputTokens, "burst-rpm": q.BurstRPM,
	} {
		if value != nil && *value <= 0 {
			return errs.New(errs.CodeUsage, "--%s must be positive, or left off entirely", name).
				WithCause("an uncommitted limit is not a limit of zero")
		}
	}
	if q.CommittedTPM != nil && *q.CommittedTPM <= 0 {
		return errs.New(errs.CodeUsage, "--committed-tpm must be positive, or left off entirely").
			WithCause("an uncommitted limit is not a limit of zero")
	}
	return nil
}

// standardOf compares a declared commitment against the standard's three
// floors. An uncommitted field is a shortfall: the standard asks for a floor,
// and declaring nothing is not clearing it.
func standardOf(q api.ServingQuota) api.ServingQuotaStandard {
	shortfalls := []string{}
	if q.CommittedRPM == nil || *q.CommittedRPM < standardRPM {
		shortfalls = append(shortfalls, "committed_rpm")
	}
	if q.CommittedTPM == nil || *q.CommittedTPM < standardTPM {
		shortfalls = append(shortfalls, "committed_tpm")
	}
	if q.SuccessRateObjective == nil || *q.SuccessRateObjective < standardSuccessRate {
		shortfalls = append(shortfalls, "success_rate_objective")
	}
	return api.ServingQuotaStandard{Meets: len(shortfalls) == 0, Shortfalls: shortfalls}
}

func declaredLimits(q api.ServingQuota) api.ResolvedLimits {
	return api.ResolvedLimits{
		CommittedRPM: q.CommittedRPM, BurstRPM: q.BurstRPM, RampUpSeconds: q.RampUpSeconds,
		CommittedTPM: q.CommittedTPM, MaxConcurrentRequests: q.MaxConcurrentRequests,
		MaxOutputTokens: q.MaxOutputTokens, OnExceed: q.OnExceed,
		QueueTimeoutMS: q.QueueTimeoutMS, LimitScope: q.LimitScope, Enforced: q.Enforced,
	}
}

// effectiveLimits is what the replica at index enforces once the scope is
// applied. Under per_replica the declared set is returned unchanged (that is
// what the mode means) instead of divided by one, so the two modes cannot
// converge by accident at a single replica.
//
// max_output_tokens is intentionally not divided: it is a ceiling on what one
// request may ask for, not an allowance spent over a minute.
func effectiveLimits(q api.ServingQuota, replicas, index int) api.ResolvedLimits {
	if q.LimitScope != api.ScopeDivided {
		return declaredLimits(q)
	}
	out := declaredLimits(q)
	out.CommittedRPM = share(q.CommittedRPM, replicas, index)
	out.BurstRPM = share(q.BurstRPM, replicas, index)
	out.MaxConcurrentRequests = share(q.MaxConcurrentRequests, replicas, index)
	out.CommittedTPM = share64(q.CommittedTPM, replicas, index)
	return out
}

// share is one replica's slice of a fleet-wide limit. Largest remainder instead
// of a floor: the floor alone throws away limit%replicas of what was sold on
// every uneven split. An uncommitted limit stays uncommitted: dividing null
// would turn "not limited on this axis" into a number, and at any fleet larger
// than the limit that number would be zero.
func share(limit *int, replicas, index int) *int {
	if limit == nil {
		return nil
	}
	base := *limit / replicas
	if index < *limit%replicas {
		base++
	}
	return &base
}

func share64(limit *int64, replicas, index int) *int64 {
	if limit == nil {
		return nil
	}
	base := *limit / int64(replicas)
	if int64(index) < *limit%int64(replicas) {
		base++
	}
	return &base
}

// starvedReplicas counts the replicas that would enforce a hard zero under
// `divided`. Reported, not refused: an operator lowering a fleet below a
// sold rate needs to see the consequence.
func starvedReplicas(q api.ServingQuota, replicas int) int {
	if q.LimitScope != api.ScopeDivided {
		return 0
	}
	worst := 0
	for _, limit := range []*int{q.CommittedRPM, q.BurstRPM, q.MaxConcurrentRequests} {
		if limit != nil && *limit < replicas && replicas-*limit > worst {
			worst = replicas - *limit
		}
	}
	if q.CommittedTPM != nil && *q.CommittedTPM < int64(replicas) {
		if short := replicas - int(*q.CommittedTPM); short > worst {
			worst = short
		}
	}
	return worst
}

func intOr(v *int, fallback int) int {
	if v == nil {
		return fallback
	}
	return *v
}
