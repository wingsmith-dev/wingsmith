package cli

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
)

// Rendering helpers shared by every noun. Nothing here decides anything: the
// plane derives `behind`, `over_limit`, `within_declared_bounds` and the rest
// so that the console and `hgr` cannot disagree, and these only draw them.

func orDash(s string) string {
	if strings.TrimSpace(s) == "" {
		return "-"
	}
	return s
}

func str(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func num(p *int) string {
	if p == nil {
		return "-"
	}
	return strconv.Itoa(*p)
}

func boolWord(v bool) string {
	if v {
		return "yes"
	}
	return "no"
}

func yesNo(s ui.Style, v bool) string {
	if v {
		return s.Green("yes")
	}
	return s.Amber("no")
}

func warnIf(s ui.Style, v bool) string {
	if v {
		return s.Amber("yes")
	}
	return s.Dim("no")
}

func ago(t *time.Time) string {
	if t == nil {
		return "never"
	}
	return api.Age(*t)
}

func agoAt(t time.Time) string { return api.Age(t) }

// labelString renders a label map the way every listing prints one. Labels
// come off the wire as arbitrary JSON, so values are formatted rather than
// asserted to be strings.
func labelString(labels map[string]any) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]string, 0, len(keys))
	for _, k := range keys {
		out = append(out, fmt.Sprintf("%s=%v", k, labels[k]))
	}
	return strings.Join(out, " ")
}

// capabilityString lists the capabilities a cluster reports as true.
func capabilityString(caps map[string]any) string {
	keys := make([]string, 0, len(caps))
	for k, v := range caps {
		if b, ok := v.(bool); ok && !b {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return strings.Join(keys, " ")
}

func mib(v int) string {
	if v <= 0 {
		return "-"
	}
	return fmt.Sprintf("%dGB", v/1024)
}

func mibPtr(v *int) string {
	if v == nil {
		return "-"
	}
	return mib(*v)
}

func bytesHuman(v int64) string {
	switch {
	case v <= 0:
		return "-"
	case v < 1<<30:
		return fmt.Sprintf("%dMB", v>>20)
	case v < 1<<40:
		return fmt.Sprintf("%.1fGB", float64(v)/float64(1<<30))
	default:
		return fmt.Sprintf("%.1fTB", float64(v)/float64(1<<40))
	}
}

// clusterPhase colours the declared phase and says when reachability
// contradicts it. The two are never folded into one value: "someone disabled
// this" and "the network is down" need different responses.
func clusterPhase(s ui.Style, c api.Cluster) string {
	switch {
	case c.Phase == api.PhaseActive && !c.Reachable:
		return s.Amber("active") + " " + s.Dim("(unreachable)")
	case c.Phase == api.PhaseActive:
		return s.Green(c.Phase)
	case c.Phase == api.PhaseDraining, c.Phase == api.PhasePending:
		return s.Amber(c.Phase)
	case c.Phase == api.PhaseDisabled:
		return s.Dim(c.Phase)
	}
	return c.Phase
}

func syncState(s ui.Style, state string) string {
	switch state {
	case "connected":
		return s.Green(state)
	case "disconnected":
		return s.Red(state)
	case "never_connected":
		return s.Dim(state)
	}
	return state
}

func healthColor(s ui.Style, health string) string {
	switch health {
	case api.HealthHealthy:
		return s.Green(health)
	case api.HealthDegraded:
		return s.Amber(health)
	case api.HealthUnhealthy:
		return s.Red(health)
	default:
		return s.Dim(health)
	}
}

func conditionColor(s ui.Style, condition string) string {
	switch condition {
	case api.NodeReady:
		return s.Green(condition)
	case api.NodeNotReady:
		return s.Red(condition)
	default:
		return s.Amber(condition)
	}
}

func countCell(s ui.Style, n int) string {
	if n == 0 {
		return "0"
	}
	return s.Amber(strconv.Itoa(n))
}

func phaseColor(s ui.Style, phase string) string {
	switch phase {
	case "running", "succeeded", "active", "held", "ready":
		return s.Green(phase)
	case "pending", "queued", "admitted", "placed", "rolling", "draining":
		return s.Amber(phase)
	case "failed", "expired":
		return s.Red(phase)
	case "cancelled", "released", "disabled":
		return s.Dim(phase)
	}
	return phase
}

func severityColor(s ui.Style, severity string) string {
	switch severity {
	case "error", "critical":
		return s.Red(severity)
	case "warning":
		return s.Amber(severity)
	case "info":
		return s.Dim(severity)
	}
	return severity
}
