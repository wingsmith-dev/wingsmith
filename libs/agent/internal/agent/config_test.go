package agent

import (
	"strings"
	"testing"
	"time"

	"github.com/hangar-dev/hangar/libs/packages/tunnel"
)

// minimal is a configuration that loads, so a case can break exactly one thing.
func minimal() map[string]string {
	return map[string]string{
		"HANGAR_CLUSTER_ID":         "clu_123",
		"HANGAR_TUNNEL_GATEWAY":     "gw.hangar.svc:9443",
		"HANGAR_ENROLMENT_TOKEN":    "tok_123",
		"HANGAR_LOG_FORMAT":         "text",
		"HANGAR_TUNNEL_TLS":         "false",
		"HANGAR_TUNNEL_CA_FILE":     "",
		"HANGAR_INFERENCE_ENDPOINT": "",
	}
}

func load(t *testing.T, env map[string]string) (*Config, error) {
	t.Helper()
	for key, value := range env {
		t.Setenv(key, value)
	}
	return Load()
}

func TestLoadDefaults(t *testing.T) {
	cfg, err := load(t, minimal())
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.Gateway != "gw.hangar.svc:9443" {
		t.Fatalf("gateway = %q, want the one address given", cfg.Gateway)
	}
	if cfg.ConnsPerGateway != 2 {
		t.Errorf("ConnsPerGateway = %d, want 2", cfg.ConnsPerGateway)
	}
	if cfg.BackoffMin != time.Second || cfg.BackoffMax != 30*time.Second {
		t.Errorf("backoff = %s..%s, want 1s..30s", cfg.BackoffMin, cfg.BackoffMax)
	}
	if cfg.WorkloadNamespace != "dynamo" {
		t.Errorf("WorkloadNamespace = %q, want dynamo", cfg.WorkloadNamespace)
	}
	if cfg.HealthAddr != ":8081" {
		t.Errorf("HealthAddr = %q, want :8081", cfg.HealthAddr)
	}
	if cfg.DiscoveryStaleAfter != 5*time.Minute {
		t.Errorf("DiscoveryStaleAfter = %s, want 5m", cfg.DiscoveryStaleAfter)
	}
}

// A cluster is configured with one address and never with a list of replicas. The chart used to
// render a comma-separated list here, so a value that still carries one must fail loudly rather than
// be read as a hostname with commas in it.
func TestLoadRefusesAListOfGateways(t *testing.T) {
	env := minimal()
	env["HANGAR_TUNNEL_GATEWAY"] = "gw-0.gw:9443,gw-1.gw:9443"
	if _, err := load(t, env); err == nil {
		t.Fatal("Load accepted a comma-separated list where one address belongs")
	}
}

func TestValidateNamesTheSetting(t *testing.T) {
	cases := []struct {
		name string
		set  map[string]string
		want string
	}{
		{"no cluster", map[string]string{"HANGAR_CLUSTER_ID": ""}, "HANGAR_CLUSTER_ID"},
		{"no gateway", map[string]string{"HANGAR_TUNNEL_GATEWAY": ""}, "HANGAR_TUNNEL_GATEWAY"},
		{"gateway without port", map[string]string{"HANGAR_TUNNEL_GATEWAY": "gw.hangar.svc"}, "HANGAR_TUNNEL_GATEWAY"},
		{"gateway with a named port", map[string]string{"HANGAR_TUNNEL_GATEWAY": "gw.hangar.svc:tunnel"}, "numeric port"},
		{"no token", map[string]string{"HANGAR_ENROLMENT_TOKEN": ""}, "HANGAR_ENROLMENT_TOKEN"},
		{"no connections", map[string]string{"HANGAR_TUNNEL_CONNS_PER_GATEWAY": "0"}, "HANGAR_TUNNEL_CONNS_PER_GATEWAY"},
		{"backoff inverted", map[string]string{"HANGAR_BACKOFF_MIN": "10s", "HANGAR_BACKOFF_MAX": "1s"}, "HANGAR_BACKOFF_MAX"},
		{"no dial timeout", map[string]string{"HANGAR_TUNNEL_DIAL_TIMEOUT": "0s"}, "HANGAR_TUNNEL_DIAL_TIMEOUT"},
		{"no handshake timeout", map[string]string{"HANGAR_TUNNEL_HANDSHAKE_TIMEOUT": "0s"}, "HANGAR_TUNNEL_HANDSHAKE_TIMEOUT"},
		{"no drain", map[string]string{"HANGAR_DRAIN_TIMEOUT": "0s"}, "HANGAR_DRAIN_TIMEOUT"},
		{"CA without TLS", map[string]string{"HANGAR_TUNNEL_CA_FILE": "/etc/ca.pem"}, "HANGAR_TUNNEL_CA_FILE"},
		{"bad log format", map[string]string{"HANGAR_LOG_FORMAT": "logfmt"}, "HANGAR_LOG_FORMAT"},
		{"bad log level", map[string]string{"HANGAR_LOG_LEVEL": "chatty"}, "HANGAR_LOG_LEVEL"},
		{"negative staleness", map[string]string{"HANGAR_FRONTEND_DISCOVERY_STALE_AFTER": "-1m"}, "HANGAR_FRONTEND_DISCOVERY_STALE_AFTER"},
		{"staleness inside one interval", map[string]string{
			"HANGAR_FRONTEND_DISCOVERY_INTERVAL":    "1m",
			"HANGAR_FRONTEND_DISCOVERY_STALE_AFTER": "30s",
		}, "HANGAR_FRONTEND_DISCOVERY_STALE_AFTER"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			env := minimal()
			for key, value := range tc.set {
				env[key] = value
			}
			// A blank means "unset" to env(), which is how a case removes a required setting.
			_, err := load(t, env)
			if err == nil {
				t.Fatalf("Load succeeded, want a failure naming %s", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %q does not name %q", err, tc.want)
			}
		})
	}
}

// Frontend discovery needs somewhere to look, and an explicit endpoint is the other way to say
// where. The namespace default means neither is reachable through Load, so this checks validate.
func TestValidateWantsANamespaceOrAnEndpoint(t *testing.T) {
	base := Config{
		ClusterID:        "clu_123",
		Gateway:          "gw.gw:9443",
		EnrolmentToken:   "tok_123",
		ConnsPerGateway:  1,
		DialTimeout:      time.Second,
		HandshakeTimeout: time.Second,
		BackoffMin:       time.Second,
		BackoffMax:       time.Second,
		DrainTimeout:     time.Second,
		HealthAddr:       ":8081",
		LogFormat:        "json",
		// The bound Load would have filled in. It is required rather than
		// zero-means-default so that a hand-built Config cannot arrive at the
		// innermost hop with a smaller bound than the gateway's.
		ResponseHeaderTimeout: tunnel.DefaultResponseHeaderTimeout,
	}

	err := base.validate()
	if err == nil || !strings.Contains(err.Error(), "HANGAR_WORKLOAD_NAMESPACE") {
		t.Fatalf("validate with neither = %v, want a failure naming HANGAR_WORKLOAD_NAMESPACE", err)
	}

	withEndpoint := base
	withEndpoint.InferenceEndpoint = "http://dynamo-frontend.dynamo.svc.cluster.local:8000"
	if err := withEndpoint.validate(); err != nil {
		t.Fatalf("validate with an endpoint: %v", err)
	}
}

// The agent is the innermost of three hops arming a response-header timer, and
// only the smallest of the three is ever observed. So it reads the same variable
// name the gateway does, defaults to the same number, and refuses anything below
// it — a smaller value here would fire first and hand every long non-streamed
// completion back as a 502 at a wall nothing in the gateway names.
func TestTheResponseHeaderBoundIsSharedWithTheGateway(t *testing.T) {
	for _, tc := range []struct {
		name    string
		value   string
		want    time.Duration
		wantErr string
	}{
		{name: "unset takes the gateway's default", value: "", want: tunnel.DefaultResponseHeaderTimeout},
		{name: "a raised value is kept", value: "90m", want: 90 * time.Minute},
		{name: "the default itself is accepted", value: tunnel.DefaultResponseHeaderTimeout.String(), want: tunnel.DefaultResponseHeaderTimeout},
		{name: "below the gateway's default is refused", value: "5m", wantErr: "HANGAR_RESPONSE_HEADER_TIMEOUT"},
		{name: "zero is refused rather than read as no bound", value: "0s", wantErr: "HANGAR_RESPONSE_HEADER_TIMEOUT"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := minimal()
			env["HANGAR_RESPONSE_HEADER_TIMEOUT"] = tc.value
			cfg, err := load(t, env)
			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("Load accepted %q, which fires before the gateway's own bound", tc.value)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("error = %v, want it to name %s", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			if cfg.ResponseHeaderTimeout != tc.want {
				t.Fatalf("ResponseHeaderTimeout = %s, want %s", cfg.ResponseHeaderTimeout, tc.want)
			}
			// The config carries the fleet budget; what the local proxy arms is
			// one margin above it, so the hops outside this one give up first.
			if tunnel.InnerHopTimeout(cfg.ResponseHeaderTimeout) <= cfg.ResponseHeaderTimeout {
				t.Fatal("the agent's own bound does not outlast the budget it shares with the gateway")
			}
		})
	}
}

// A value that is set and unreadable fails the load. It used to fall back to the default, so
// HANGAR_TUNNEL_TLS=flase quietly meant TLS on and HANGAR_TUNNEL_INSECURE_SKIP_VERIFY=ture meant off.
func TestLoadRefusesValuesItCannotParse(t *testing.T) {
	for key, value := range map[string]string{
		"HANGAR_TUNNEL_TLS":                  "flase",
		"HANGAR_TUNNEL_INSECURE_SKIP_VERIFY": "ture",
		"HANGAR_TUNNEL_CONNS_PER_GATEWAY":    "two",
		"HANGAR_TUNNEL_DIAL_TIMEOUT":         "10",
		"HANGAR_DRAIN_TIMEOUT":               "a minute",
	} {
		t.Run(key, func(t *testing.T) {
			env := minimal()
			env[key] = value
			_, err := load(t, env)
			if err == nil {
				t.Fatalf("Load accepted %s=%q", key, value)
			}
			if !strings.Contains(err.Error(), key) {
				t.Errorf("error %q does not name %s", err, key)
			}
		})
	}
}

func TestWarningsNameTheWeakenedSetting(t *testing.T) {
	cases := []struct {
		name string
		cfg  Config
		want string
	}{
		{"tls off", Config{TLSEnabled: false}, "HANGAR_TUNNEL_TLS"},
		{"skip verify", Config{TLSEnabled: true, TLSInsecureSkipVerify: true}, "HANGAR_TUNNEL_INSECURE_SKIP_VERIFY"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			warnings := tc.cfg.Warnings()
			if len(warnings) != 1 || !strings.Contains(warnings[0], tc.want) {
				t.Errorf("warnings = %q, want one naming %s", warnings, tc.want)
			}
		})
	}
	if warnings := (&Config{TLSEnabled: true}).Warnings(); len(warnings) != 0 {
		t.Errorf("a verified TLS config warned: %q", warnings)
	}
}
