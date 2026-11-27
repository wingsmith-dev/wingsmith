package host

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// Default paths. The token is a credential and lives apart from the config, so the config can
// be read (and shipped in a support bundle) without it.
const (
	DefaultConfigPath = "/etc/wingsmith/host.json"
	DefaultTokenPath  = "/etc/wingsmith/token"
	DefaultStateDir   = "/var/lib/wingsmith"
)

// Config is what `enrol` writes and `run` reads.
type Config struct {
	// Plane is the hub's ws(s):// URL, as the plane's setup instructions print it.
	Plane string `json:"plane"`
	// Cluster is the name the host was registered under (kind=host); the hub checks it against
	// the token in the handshake.
	Cluster   string            `json:"cluster"`
	TokenPath string            `json:"token_path"`
	StateDir  string            `json:"state_dir"`
	CAFile    string            `json:"ca_file,omitempty"`
	Labels    map[string]string `json:"labels,omitempty"`
	Pool      string            `json:"pool,omitempty"`

	// Serving (P2.4). Off by default: a host enrolled before serving existed stays supply-only
	// until its operator opts in.
	Serve bool `json:"serve,omitempty"`
	// Gateway is the tunnel address (host:port) the data path dials, from the plane's setup
	// instructions. Required with Serve.
	Gateway string `json:"gateway,omitempty"`
	// GatewayCAFile verifies the gateway instead of the system roots.
	GatewayCAFile string `json:"gateway_ca_file,omitempty"`
	// GatewayPlaintext dials the tunnel without TLS. Local development only.
	GatewayPlaintext bool `json:"gateway_plaintext,omitempty"`
	// AllowedImagePrefixes is the image allowlist. Empty refuses every workload (fail closed).
	AllowedImagePrefixes []string `json:"allowed_image_prefixes,omitempty"`
	// ContainerImageTagsAllowed lets an image be named by tag instead of digest.
	ContainerImageTagsAllowed bool `json:"container_image_tags_allowed,omitempty"`
	// ModelRoot is the model cache; /var/lib/wingsmith/models when empty.
	ModelRoot string `json:"model_root,omitempty"`
	// DockerSocket is the Engine API socket; /var/run/docker.sock when empty.
	DockerSocket string `json:"docker_socket,omitempty"`
	// CDI requests GPUs as CDI devices instead of through the NVIDIA runtime hook.
	CDI bool `json:"cdi,omitempty"`

	// Model cache (P2.5).
	// PrefetchImage fetches every artifact; empty uses the image of a deployment serving it.
	PrefetchImage string `json:"prefetch_image,omitempty"`
	// CredentialsDir holds <project>.token (and _default.token) Hugging Face tokens, mode 0600;
	// /etc/wingsmith/credentials when empty.
	CredentialsDir string `json:"credentials_dir,omitempty"`
	// MaxPrefetches bounds concurrent downloads; 2 when zero.
	MaxPrefetches int `json:"max_prefetches,omitempty"`
	// CacheHighWatermark and CacheLowWatermark are the fractions of the cache filesystem in use
	// at which eviction starts and stops; 0.90 and 0.80 when zero.
	CacheHighWatermark float64 `json:"cache_high_watermark,omitempty"`
	CacheLowWatermark  float64 `json:"cache_low_watermark,omitempty"`
}

// ValidateServing refuses a serving config that could not route a request.
func (c *Config) ValidateServing() error {
	if !c.Serve {
		return nil
	}
	if strings.TrimSpace(c.Gateway) == "" {
		return errors.New("serve is on but gateway (the tunnel host:port) is not set in the host config")
	}
	if c.CacheHighWatermark < 0 || c.CacheHighWatermark > 1 || c.CacheLowWatermark < 0 || c.CacheLowWatermark > 1 ||
		(c.CacheHighWatermark > 0 && c.CacheLowWatermark > c.CacheHighWatermark) {
		return errors.New("cache_high_watermark and cache_low_watermark must be fractions between 0 and 1, low below high")
	}
	return nil
}

// Validate refuses a config that could not open a session, before anything is written.
func (c *Config) Validate(allowPlaintext bool) error {
	u, err := url.Parse(c.Plane)
	if err != nil || u.Host == "" {
		return fmt.Errorf("--plane %q is not a URL", c.Plane)
	}
	switch u.Scheme {
	case "wss":
	case "ws":
		if !allowPlaintext {
			return errors.New("--plane is ws://; the token would travel in clear. Use wss:// or pass --insecure for local development")
		}
	default:
		return fmt.Errorf("--plane must be the hub's wss:// URL, got scheme %q", u.Scheme)
	}
	if strings.TrimSpace(c.Cluster) == "" {
		return errors.New("--cluster is required: the name the host was registered under")
	}
	return nil
}

// Enrol writes the token (0600) and the config (0644), each atomically, creating their
// directories. The token is checked for shape only; whether the plane accepts it is the
// handshake's to say on the first `run`.
func Enrol(configPath string, cfg Config, token string) error {
	token = strings.TrimSpace(token)
	if !strings.HasPrefix(token, "wso1.") {
		return errors.New("that is not a cluster enrolment token (expected wso1.…)")
	}
	if err := writeAtomic(cfg.TokenPath, []byte(token+"\n"), 0o600); err != nil {
		return fmt.Errorf("write token: %w", err)
	}
	raw, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return err
	}
	if err := writeAtomic(configPath, append(raw, '\n'), 0o644); err != nil {
		return fmt.Errorf("write config: %w", err)
	}
	return nil
}

// LoadConfig reads the config and the token it points at.
func LoadConfig(path string) (Config, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return Config{}, "", fmt.Errorf("read config (run `wingsmith-host enrol` first): %w", err)
	}
	var cfg Config
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return Config{}, "", fmt.Errorf("config %s: %w", path, err)
	}
	if cfg.TokenPath == "" {
		cfg.TokenPath = DefaultTokenPath
	}
	if cfg.StateDir == "" {
		cfg.StateDir = DefaultStateDir
	}
	info, err := os.Stat(cfg.TokenPath)
	if err != nil {
		return Config{}, "", fmt.Errorf("read token: %w", err)
	}
	if info.Mode().Perm()&0o077 != 0 {
		return Config{}, "", fmt.Errorf("token %s is readable by others (%v); chmod 600 it", cfg.TokenPath, info.Mode().Perm())
	}
	token, err := os.ReadFile(cfg.TokenPath)
	if err != nil {
		return Config{}, "", fmt.Errorf("read token: %w", err)
	}
	return cfg, strings.TrimSpace(string(token)), nil
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(tmp.Name()) }()
	if err := tmp.Chmod(mode); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}
