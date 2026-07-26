package serving

import (
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/plane"
)

// The two rules that make an unadorned `"stream": true` carry a usage frame.
func TestStreamOptionsInjection(t *testing.T) {
	policy := &plane.Policy{Version: 2, Rules: []plane.Rule{
		{Route: chatRoute, Stream: boolp(true), Key: "stream_options", Default: map[string]any{"include_usage": true}},
		{Route: chatRoute, Stream: boolp(true), Key: "stream_options.include_usage", Default: true},
	}}
	for _, tc := range []struct{ name, in, want string }{
		{"absent", `{"model":"m","stream":true}`, `{"model":"m","stream":true,"stream_options":{"include_usage":true}}`},
		{"empty parent", `{"model":"m","stream":true,"stream_options":{}}`, `{"model":"m","stream":true,"stream_options":{"include_usage":true}}`},
		{"other keys", `{"model":"m","stream":true,"stream_options":{"x":1}}`, `{"model":"m","stream":true,"stream_options":{"x":1,"include_usage":true}}`},
		{"explicit false kept", `{"model":"m","stream":true,"stream_options":{"include_usage":false}}`, `{"model":"m","stream":true,"stream_options":{"include_usage":false}}`},
		{"non-stream untouched", `{"model":"m"}`, `{"model":"m"}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := rewriteBody(policy, chatRoute, []byte(tc.in))
			if err != nil {
				t.Fatalf("rewrite: %v", err)
			}
			assertSameJSON(t, got, tc.want)
		})
	}
}

func boolp(b bool) *bool { return &b }
