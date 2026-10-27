package render

import (
	"fmt"
	"sort"
	"strings"

	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// The overlay allowlists. They mirror POD_OVERLAY_KEYS, CONTAINER_OVERLAY_KEYS,
// DYNAMO_OVERLAY_KEYS and ENV_OVERLAY_SOURCES in the plane's workloads/validation.py, which
// refuses the same overlays when a workload is written. The operator checks again because it is
// the one applying the merge with cluster privileges, and a plane that skipped the check, or an
// older one, must not be able to set a pod's securityContext, volumes, tolerations or image
// through it. Keep the two lists equal.
var (
	podOverlayKeys = keySet("containers", "terminationGracePeriodSeconds", "imagePullSecrets")

	containerOverlayKeys = keySet(
		"name", "args", "command", "env", "ports", "readinessProbe", "livenessProbe",
		"startupProbe", "imagePullPolicy", "workingDir",
	)

	// No components: lists replace in the graph merge, so a component list would replace the
	// rendered one, with replica counts and device requests placement never counted.
	dynamoOverlayKeys = keySet("annotations", "envs")

	// An env entry may read the pod's own fields and nothing else; a secretKeyRef or
	// configMapKeyRef would read any Secret or ConfigMap in the workload namespace.
	envOverlaySources = keySet("fieldRef", "resourceFieldRef")
)

func keySet(keys ...string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// checkOverlay refuses an overlay that sets anything outside the allowlists, before any of it is
// merged.
func checkOverlay(overlay wire.Overlay) error {
	if len(overlay.PodSpec) > 0 {
		if err := checkPodOverlay(overlay.PodSpec, "overlay.pod_spec"); err != nil {
			return err
		}
	}
	if len(overlay.Dynamo) == 0 {
		return nil
	}
	if err := refuseKeys(overlay.Dynamo, dynamoOverlayKeys, "overlay.dynamo"); err != nil {
		return err
	}
	if envs, ok := overlay.Dynamo["envs"]; ok {
		return checkEnvOverlay(envs, "overlay.dynamo.envs")
	}
	return nil
}

func checkPodOverlay(pod map[string]any, where string) error {
	if err := refuseKeys(pod, podOverlayKeys, where); err != nil {
		return err
	}
	raw, ok := pod["containers"]
	if !ok {
		return nil
	}
	containers, ok := raw.([]any)
	if !ok {
		return fmt.Errorf("%s.containers must be a list", where)
	}
	for i, entry := range containers {
		at := fmt.Sprintf("%s.containers[%d]", where, i)
		container, ok := entry.(map[string]any)
		if !ok {
			return fmt.Errorf("%s must be an object", at)
		}
		if err := refuseKeys(container, containerOverlayKeys, at); err != nil {
			return err
		}
		if env, ok := container["env"]; ok {
			if err := checkEnvOverlay(env, at+".env"); err != nil {
				return err
			}
		}
	}
	return nil
}

func checkEnvOverlay(raw any, where string) error {
	env, ok := raw.([]any)
	if !ok {
		return fmt.Errorf("%s must be a list", where)
	}
	for i, entry := range env {
		at := fmt.Sprintf("%s[%d]", where, i)
		item, ok := entry.(map[string]any)
		if !ok {
			return fmt.Errorf("%s must be an object", at)
		}
		source, ok := item["valueFrom"]
		if !ok || source == nil {
			continue
		}
		from, ok := source.(map[string]any)
		if !ok {
			return fmt.Errorf("%s.valueFrom must be an object", at)
		}
		if err := refuseKeys(from, envOverlaySources, at+".valueFrom"); err != nil {
			return err
		}
	}
	return nil
}

func refuseKeys(value map[string]any, allowed map[string]bool, where string) error {
	var refused []string
	for key := range value {
		if !allowed[key] {
			refused = append(refused, key)
		}
	}
	if len(refused) == 0 {
		return nil
	}
	sort.Strings(refused)
	names := make([]string, 0, len(allowed))
	for key := range allowed {
		names = append(names, key)
	}
	sort.Strings(names)
	return fmt.Errorf("%s may not set %s: the overlay is limited to %s",
		where, strings.Join(refused, ", "), strings.Join(names, ", "))
}
