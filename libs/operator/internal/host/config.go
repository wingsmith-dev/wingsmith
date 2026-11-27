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
	if err := os.MkdirAll(dir, 0o755); err != nil {
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
