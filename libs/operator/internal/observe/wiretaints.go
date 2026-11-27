package observe

import "github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"

// taints converts discovered taints into their wire form.
func taints(in []Taint) []wire.NodeTaint {
	out := make([]wire.NodeTaint, 0, len(in))
	for _, t := range in {
		out = append(out, wire.NodeTaint{Key: t.Key, Value: t.Value, Effect: t.Effect})
	}
	return out
}
