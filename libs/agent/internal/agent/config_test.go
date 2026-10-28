package agent

import (
	"strings"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/packages/tunnel"
)

// minimal is a configuration that loads, so a case can break exactly one thing.
func minimal() map[string]string {
	return map[string]string{
		"WINGSMITH_CLUSTER_ID":         "clu_123",
		"WINGSMITH_TUNNEL_GATEWAY":     "gw.wingsmith.svc:9443",
		"WINGSMITH_ENROLMENT_TOKEN":    "tok_123",
		"WINGSMITH_LOG_FORMAT":         "text",
		"WINGSMITH_TUNNEL_TLS":         "false",
		"WINGSMITH_TUNNEL_CA_FILE":     "",
		"WINGSMITH_INFERENCE_ENDPOINT": "",
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
	if cfg.Gateway != "gw.wingsmith.svc:9443" {
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
	if cfg.ShutdownGrace != 120*time.Second || cfg.DrainTimeout != 115*time.Second {
		t.Errorf("grace %s, drain %s, want 2m0s and 1m55s", cfg.ShutdownGrace, cfg.DrainTimeout)
	}
	if warnings := cfg.Warnings(); len(warnings) != 1 || !strings.Contains(warnings[0], "WINGSMITH_TUNNEL_TLS") {
		t.Errorf("warnings = %q, want only the one about TLS being off", warnings)
	}
}

// The drain is cut from the grace period the pod is given, the way the gateway's phases are, so
// raising terminationGracePeriodSeconds and WINGSMITH_SHUTDOWN_GRACE together is enough. A drain set
// explicitly is kept as given and warned about when it outlasts the grace period.
func TestTheDrainFollowsTheShutdownGrace(t *testing.T) {
	env := minimal()
	env["WINGSMITH_SHUTDOWN_GRACE"] = "10m"
	cfg, err := load(t, env)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.DrainTimeout != 10*time.Minute-ShutdownMargin {
		t.Errorf("drain = %s, want the grace less %s", cfg.DrainTimeout, ShutdownMargin)
	}

	env["WINGSMITH_DRAIN_TIMEOUT"] = "15m"
	cfg, err = load(t, env)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.DrainTimeout != 15*time.Minute {
		t.Errorf("drain = %s, want the 15m set explicitly", cfg.DrainTimeout)
	}
	warned := false
	for _, warning := range cfg.Warnings() {
		warned = warned || strings.Contains(warning, "WINGSMITH_DRAIN_TIMEOUT")
	}
	if !warned {
		t.Errorf("a drain longer than the grace period was not warned about: %q", cfg.Warnings())
	}
}

// A cluster is configured with one address and never with a list of replicas. The chart used to
// render a comma-separated list here, so a value that still carries one must fail loudly, not
// be read as a hostname with commas in it.
func TestLoadRefusesAListOfGateways(t *testing.T) {
	env := minimal()
	env["WINGSMITH_TUNNEL_GATEWAY"] = "gw-0.gw:9443,gw-1.gw:9443"
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
		{"no cluster", map[string]string{"WINGSMITH_CLUSTER_ID": ""}, "WINGSMITH_CLUSTER_ID"},
		{"no gateway", map[string]string{"WINGSMITH_TUNNEL_GATEWAY": ""}, "WINGSMITH_TUNNEL_GATEWAY"},
		{"gateway without port", map[string]string{"WINGSMITH_TUNNEL_GATEWAY": "gw.wingsmith.svc"}, "WINGSMITH_TUNNEL_GATEWAY"},
		{"gateway with a named port", map[string]string{"WINGSMITH_TUNNEL_GATEWAY": "gw.wingsmith.svc:tunnel"}, "numeric port"},
		{"no token", map[string]string{"WINGSMITH_ENROLMENT_TOKEN": ""}, "WINGSMITH_ENROLMENT_TOKEN"},
		{"no connections", map[string]string{"WINGSMITH_TUNNEL_CONNS_PER_GATEWAY": "0"}, "WINGSMITH_TUNNEL_CONNS_PER_GATEWAY"},
		{"backoff inverted", map[string]string{"WINGSMITH_BACKOFF_MIN": "10s", "WINGSMITH_BACKOFF_MAX": "1s"}, "WINGSMITH_BACKOFF_MAX"},
		{"no dial timeout", map[string]string{"WINGSMITH_TUNNEL_DIAL_TIMEOUT": "0s"}, "WINGSMITH_TUNNEL_DIAL_TIMEOUT"},
		{"no handshake timeout", map[string]string{"WINGSMITH_TUNNEL_HANDSHAKE_TIMEOUT": "0s"}, "WINGSMITH_TUNNEL_HANDSHAKE_TIMEOUT"},
		{"no drain", map[string]string{"WINGSMITH_DRAIN_TIMEOUT": "0s"}, "WINGSMITH_DRAIN_TIMEOUT"},
		{"grace inside the margin", map[string]string{"WINGSMITH_SHUTDOWN_GRACE": "5s"}, "WINGSMITH_SHUTDOWN_GRACE"},
		{"CA without TLS", map[string]string{"WINGSMITH_TUNNEL_CA_FILE": "/etc/ca.pem"}, "WINGSMITH_TUNNEL_CA_FILE"},
		{"bad log format", map[string]string{"WINGSMITH_LOG_FORMAT": "logfmt"}, "WINGSMITH_LOG_FORMAT"},
		{"bad log level", map[string]string{"WINGSMITH_LOG_LEVEL": "chatty"}, "WINGSMITH_LOG_LEVEL"},
		{"negative staleness", map[string]string{"WINGSMITH_FRONTEND_DISCOVERY_STALE_AFTER": "-1m"}, "WINGSMITH_FRONTEND_DISCOVERY_STALE_AFTER"},
		{"staleness inside one interval", map[string]string{
			"WINGSMITH_FRONTEND_DISCOVERY_INTERVAL":    "1m",
			"WINGSMITH_FRONTEND_DISCOVERY_STALE_AFTER": "30s",
		}, "WINGSMITH_FRONTEND_DISCOVERY_STALE_AFTER"},
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
		ShutdownGrace:    time.Minute,
		DrainTimeout:     time.Second,
		HealthAddr:       ":8081",
		LogFormat:        "json",
		// The bound Load would have filled in. It is required, not
		// zero-means-default so that a hand-built Config cannot arrive at the
		// innermost hop with a smaller bound than the gateway's.
		ResponseHeaderTimeout: tunnel.DefaultResponseHeaderTimeout,
	}

	err := base.validate()
	if err == nil || !strings.Contains(err.Error(), "WINGSMITH_WORKLOAD_NAMESPACE") {
		t.Fatalf("validate with neither = %v, want a failure naming WINGSMITH_WORKLOAD_NAMESPACE", err)
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
// it: a smaller value here would fire first and hand every long non-streamed
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
		{name: "below the gateway's default is refused", value: "5m", wantErr: "WINGSMITH_RESPONSE_HEADER_TIMEOUT"},
		{name: "zero is refused, not read as no bound", value: "0s", wantErr: "WINGSMITH_RESPONSE_HEADER_TIMEOUT"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := minimal()
			env["WINGSMITH_RESPONSE_HEADER_TIMEOUT"] = tc.value
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
// WINGSMITH_TUNNEL_TLS=flase quietly meant TLS on and WINGSMITH_TUNNEL_INSECURE_SKIP_VERIFY=ture meant off.
func TestLoadRefusesValuesItCannotParse(t *testing.T) {
	for key, value := range map[string]string{
		"WINGSMITH_TUNNEL_TLS":                  "flase",
		"WINGSMITH_TUNNEL_INSECURE_SKIP_VERIFY": "ture",
		"WINGSMITH_TUNNEL_CONNS_PER_GATEWAY":    "two",
		"WINGSMITH_TUNNEL_DIAL_TIMEOUT":         "10",
		"WINGSMITH_DRAIN_TIMEOUT":               "a minute",
		"WINGSMITH_SHUTDOWN_GRACE":              "two minutes",
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
		{"tls off", Config{TLSEnabled: false}, "WINGSMITH_TUNNEL_TLS"},
		{"skip verify", Config{TLSEnabled: true, TLSInsecureSkipVerify: true}, "WINGSMITH_TUNNEL_INSECURE_SKIP_VERIFY"},
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
