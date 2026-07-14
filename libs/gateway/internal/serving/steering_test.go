package serving

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// arrival is what the cluster end of the hop received.
type arrival struct {
	header http.Header
	body   []byte
}

func steeringUpstream(t *testing.T) (*httptest.Server, *arrival) {
	t.Helper()
	seen := &arrival{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.header = r.Header.Clone()
		seen.body, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)
	return server, seen
}

func sendThrough(t *testing.T, route routing.Route, body string, header http.Header) {
	t.Helper()
	handler := New(Config{Router: stubResolver{routes: map[string]routing.Route{"chat": route}}})
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(body))
	for name, values := range header {
		req.Header[name] = values
	}
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body)
	}
}

// TestACallersSteeringHeadersNeverReachTheCluster is the queue jump and the worker pin:
// Dynamo acts on every one of these, and httputil.ReverseProxy would carry each of
// them into the cluster verbatim.
func TestACallersSteeringHeadersNeverReachTheCluster(t *testing.T) {
	server, seen := steeringUpstream(t)
	steering := http.Header{
		"X-Dynamo-Worker-Instance-Id":      {"42"},
		"X-Dynamo-Prefill-Instance-Id":     {"7"},
		"X-Dynamo-Dp-Rank":                 {"3"},
		"X-Dynamo-Request-Priority":        {"100"},
		"X-Dynamo-Request-Strict-Priority": {"0"},
		"X-Dynamo-Meta-Policy-Class":       {"latency"},
		"X-Dynamo-Request-Id":              {"3f1c8a52-6b0e-4d2a-9d8e-0c5b7a1e2f40"},
		"X-Dynamo-Session-Id":              {"probe-1"},
		"X-Worker-Instance-Id":             {"42"},
		"X-Data-Parallel-Rank":             {"3"},
		"X-Claude-Code-Session-Id":         {"cc-1"},
		"X-Claude-Code-Agent-Id":           {"agent-1"},
		"Thread-Id":                        {"codex-1"},
		"X-Session-Id":                     {"opencode-1"},
		"x-dynamo-prefill-dp-rank":         {"1"},
		"X_dynamo_request_priority":        {"100"},
	}
	kept := http.Header{
		"User-Agent":       {"OpenAI/Python 1.99.0"},
		"X-Stainless-Lang": {"python"},
		"X-Request-Id":     {"trace-7f3c"},
		"X-Tenant-Id":      {"acme"},
	}
	sent := http.Header{}
	for name, values := range steering {
		sent[name] = values
	}
	for name, values := range kept {
		sent[name] = values
	}

	sendThrough(t, routeTo(t, server.URL, nil), `{"model":"chat"}`, sent)

	for name := range steering {
		if got := seen.header.Get(name); got != "" {
			t.Errorf("%s reached the cluster as %q", name, got)
		}
	}
	for name, values := range kept {
		if got := seen.header.Values(name); !slices.Equal(got, values) {
			t.Errorf("%s = %q, want %q: it steers nothing and was dropped", name, got, values)
		}
	}
}

// TestTheGatewaysOwnHeadersSurviveTheStrip is the ordering the strip depends on: it
// runs on what the caller sent, and what this hop stamps is written afterwards, even
// under a name inside the runtime's namespace.
func TestTheGatewaysOwnHeadersSurviveTheStrip(t *testing.T) {
	server, seen := steeringUpstream(t)
	route := routeTo(t, server.URL, http.Header{"X-Dynamo-Meta-Tier": {"gold"}})
	route.Upstreams[0].DeploymentID = "dep_01j9x2"

	sendThrough(t, route, `{"model":"chat"}`, http.Header{
		"X-Dynamo-Meta-Tier":      {"platinum"},
		tunnel.HeaderDeploymentID: {"dep_somebody_elses"},
	})

	if got := seen.header.Values("X-Dynamo-Meta-Tier"); !slices.Equal(got, []string{"gold"}) {
		t.Errorf("X-Dynamo-Meta-Tier = %q, want only the credential's own value", got)
	}
	if got := seen.header.Get(tunnel.HeaderDeploymentID); got != "dep_01j9x2" {
		t.Errorf("%s = %q, want the resolved deployment", tunnel.HeaderDeploymentID, got)
	}
}

// TestACallersRoutingHintsInNvextNeverReachTheCluster is the body spelling of the same
// controls, which the runtime reads when there is no header.
func TestACallersRoutingHintsInNvextNeverReachTheCluster(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{
			name: "routing fields go and the rest of nvext stays",
			body: `{"model":"chat","messages":[{"role":"user","content":"hi"}],"nvext":{` +
				`"backend_instance_id":1,"prefill_worker_id":2,"decode_worker_id":3,"dp_rank":0,` +
				`"prefill_dp_rank":0,"agent_hints":{"priority":100,"strict_priority":0,"osl":64},` +
				`"routing_constraints":{"required_taints":["gold"]},"router":{"ttft_target":50},` +
				`"cache_salt":"acme","max_thinking_tokens":512}}`,
			want: `{"cache_salt":"acme","max_thinking_tokens":512}`,
		},
		{
			name: "an nvext left empty is removed",
			body: `{"model":"chat","nvext":{"agent_hints":{"strict_priority":0}}}`,
		},
		{
			name: "a repeated nvext cannot hide the copy that steers behind one that does not",
			body: `{"model":"chat","nvext":{"backend_instance_id":1},"nv\u0065xt":{"cache_salt":"acme"}}`,
			want: `{"cache_salt":"acme"}`,
		},
		{
			name: "nor can the copy that steers come last",
			body: `{"model":"chat","nvext":{"cache_salt":"acme"},"nvext":{"prefill_worker_id":2}}`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server, seen := steeringUpstream(t)
			sendThrough(t, routeTo(t, server.URL, nil), tc.body, nil)

			for _, key := range steeringHints {
				if strings.Contains(string(seen.body), `"`+key+`"`) {
					t.Errorf("%s reached the cluster: %s", key, seen.body)
				}
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(seen.body, &fields); err != nil {
				t.Fatalf("forwarded body is not JSON: %v: %s", err, seen.body)
			}
			if string(fields["model"]) != `"chat"` {
				t.Errorf("model = %s, want it untouched", fields["model"])
			}
			nvext, present := fields["nvext"]
			if tc.want == "" {
				if present {
					t.Errorf("nvext = %s, want it absent", nvext)
				}
				return
			}
			var got, want any
			_ = json.Unmarshal(nvext, &got)
			_ = json.Unmarshal([]byte(tc.want), &want)
			if !reflect.DeepEqual(got, want) {
				t.Errorf("nvext = %s, want %s", nvext, tc.want)
			}
		})
	}
}

// TestARequestThatDoesNotSteerIsForwardedByteForByte is the cost side: the strip must
// not turn into a re-encode of every request, including one that uses a harmless part
// of nvext.
func TestARequestThatDoesNotSteerIsForwardedByteForByte(t *testing.T) {
	header := http.Header{
		"Content-Type": {"application/json"},
		"User-Agent":   {"OpenAI/Python 1.99.0"},
		"X-Request-Id": {"trace-7f3c"},
	}
	for _, body := range []string{
		`{ "stream": false,  "model" : "chat", "messages": [{"role":"user","content":"café <b>&"}]}`,
		`{"nvext": {"cache_salt": "acme", "max_thinking_tokens": 512} , "model":"chat"}`,
	} {
		server, seen := steeringUpstream(t)
		sendThrough(t, routeTo(t, server.URL, nil), body, header)

		if string(seen.body) != body {
			t.Errorf("body = %s, want the bytes that arrived: %s", seen.body, body)
		}
		for name, values := range header {
			if got := seen.header.Values(name); !slices.Equal(got, values) {
				t.Errorf("%s = %q, want %q", name, got, values)
			}
		}
	}
}

func TestSteeringHeadersAreMatchedWhateverTheirSpelling(t *testing.T) {
	for name, want := range map[string]bool{
		"X-Dynamo-Request-Id":        true,
		"x-dynamo-meta-policy-class": true,
		"X-DYNAMO-SOME-FUTURE-KNOB":  true,
		"X_Dynamo_Session_Id":        true,
		"X-Prefill-Dp-Rank":          true,
		"Thread-Id":                  true,
		"X-Codex-Turn-Metadata":      true,
		"X-Dynamo":                   false,
		"X-Request-Id":               false,
		"X-Tenant-Id":                false,
		"X-Thread-Id":                false,
		"Authorization":              false,
		"X-Hangar-Origin":            false,
		tunnel.HeaderDeploymentID:    false,
	} {
		if got := isSteeringHeader(name); got != want {
			t.Errorf("isSteeringHeader(%q) = %v, want %v", name, got, want)
		}
	}
}
