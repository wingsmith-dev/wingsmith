package render

import (
	"strings"
	"testing"
)

func TestTheOverlayAllowlistMatchesThePlanes(t *testing.T) {
	cases := []struct {
		name   string
		pod    map[string]any
		dynamo map[string]any
		refuse string
	}{
		{name: "env and probes", pod: map[string]any{
			"terminationGracePeriodSeconds": int64(120),
			"containers": []any{map[string]any{
				"name": "main", "args": []any{"--max-num-seqs", "64"},
				"env": []any{
					map[string]any{"name": "A", "value": "1"},
					map[string]any{"name": "POD", "valueFrom": map[string]any{"fieldRef": map[string]any{"fieldPath": "metadata.name"}}},
				},
			}},
		}},
		{name: "graph annotations and envs", dynamo: map[string]any{
			"annotations": map[string]any{"nvidia.com/deployment-strategy": "Recreate"},
			"envs":        []any{map[string]any{"name": "A", "value": "1"}},
		}},
		{name: "pod security context", pod: map[string]any{"securityContext": map[string]any{"runAsUser": int64(0)}},
			refuse: "overlay.pod_spec may not set securityContext"},
		{name: "host network", pod: map[string]any{"hostNetwork": true},
			refuse: "overlay.pod_spec may not set hostNetwork"},
		{name: "toleration", pod: map[string]any{"tolerations": []any{map[string]any{"operator": "Exists"}}},
			refuse: "overlay.pod_spec may not set tolerations"},
		{name: "volumes", pod: map[string]any{"volumes": []any{map[string]any{"name": "root", "hostPath": map[string]any{"path": "/"}}}},
			refuse: "overlay.pod_spec may not set volumes"},
		{name: "container resources", pod: map[string]any{"containers": []any{map[string]any{"name": "main", "resources": map[string]any{}}}},
			refuse: "overlay.pod_spec.containers[0] may not set resources"},
		{name: "container privileged", pod: map[string]any{"containers": []any{map[string]any{"name": "main", "securityContext": map[string]any{"privileged": true}}}},
			refuse: "overlay.pod_spec.containers[0] may not set securityContext"},
		{name: "secret env", pod: map[string]any{"containers": []any{map[string]any{"name": "main", "env": []any{
			map[string]any{"name": "T", "valueFrom": map[string]any{"secretKeyRef": map[string]any{"name": "s", "key": "k"}}},
		}}}}, refuse: "overlay.pod_spec.containers[0].env[0].valueFrom may not set secretKeyRef"},
		{name: "containers not a list", pod: map[string]any{"containers": map[string]any{}},
			refuse: "overlay.pod_spec.containers must be a list"},
		{name: "graph components", dynamo: map[string]any{"components": []any{}},
			refuse: "overlay.dynamo may not set components"},
		{name: "graph secret env", dynamo: map[string]any{"envs": []any{
			map[string]any{"name": "T", "valueFrom": map[string]any{"configMapKeyRef": map[string]any{"name": "c", "key": "k"}}},
		}}, refuse: "overlay.dynamo.envs[0].valueFrom may not set configMapKeyRef"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			in := testInputs()
			in.Deployment.Serving.Overlay.PodSpec = tc.pod
			in.Deployment.Serving.Overlay.Dynamo = tc.dynamo
			_, err := DynamoGraphDeployment(in, testOptions())
			if tc.refuse == "" {
				if err != nil {
					t.Fatalf("render: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.refuse) {
				t.Fatalf("error = %v, want %q", err, tc.refuse)
			}
		})
	}
}
