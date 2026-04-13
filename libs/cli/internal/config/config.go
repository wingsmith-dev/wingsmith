// Package config resolves settings from flags, environment, and the config
// file, in that order of precedence.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	yaml "go.yaml.in/yaml/v2"
)

// DefaultServer is the production control plane.
const DefaultServer = "https://plane.hangar.dev"

// Context is one named server + credential + tenancy set. Named after the
// kubeconfig notion because operators already carry that vocabulary.
type Context struct {
	Server  string `yaml:"server,omitempty" json:"server,omitempty"`
	Token   string `yaml:"token,omitempty" json:"-"`
	Project string `yaml:"project,omitempty" json:"project,omitempty"`
	User    string `yaml:"user,omitempty" json:"user,omitempty"`
}

// File is the on-disk config document.
type File struct {
	CurrentContext string             `yaml:"current_context" json:"current_context"`
	Contexts       map[string]Context `yaml:"contexts" json:"contexts"`
	Output         string             `yaml:"output,omitempty" json:"output,omitempty"`
	Fake           bool               `yaml:"fake,omitempty" json:"fake,omitempty"`

	path string
}

// Path returns the file the config was loaded from.
func (f *File) Path() string { return f.path }

// DefaultPath is the config location, honouring HANGAR_CONFIG and XDG.
func DefaultPath() string {
	if p := os.Getenv("HANGAR_CONFIG"); p != "" {
		return p
	}
	if d := os.Getenv("XDG_CONFIG_HOME"); d != "" {
		return filepath.Join(d, "hangar", "config.yaml")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "hangar-config.yaml"
	}
	return filepath.Join(home, ".config", "hangar", "config.yaml")
}

// StateDir is where the CLI keeps mutable local state (fake backend data).
func StateDir() string {
	if p := os.Getenv("HANGAR_STATE_DIR"); p != "" {
		return p
	}
	if d := os.Getenv("XDG_STATE_HOME"); d != "" {
		return filepath.Join(d, "hangar")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".hangar-state"
	}
	return filepath.Join(home, ".local", "state", "hangar")
}

// Load reads the config file, returning an empty document when absent.
func Load(path string) (*File, error) {
	if path == "" {
		path = DefaultPath()
	}
	f := &File{path: path, Contexts: map[string]Context{}, CurrentContext: "default"}
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return f, nil
	}
	if err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "cannot read config at %s", path)
	}
	if err := yaml.Unmarshal(data, f); err != nil {
		return nil, errs.Wrap(err, errs.CodeInternal, "config at %s is not valid YAML", path)
	}
	f.path = path
	if f.Contexts == nil {
		f.Contexts = map[string]Context{}
	}
	if f.CurrentContext == "" {
		f.CurrentContext = "default"
	}
	return f, nil
}

// Save writes the config back to disk with restrictive permissions.
func (f *File) Save() error {
	if err := os.MkdirAll(filepath.Dir(f.path), 0o700); err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot create config directory")
	}
	data, err := yaml.Marshal(f)
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot serialise config")
	}
	if err := os.WriteFile(f.path, data, 0o600); err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot write config to %s", f.path)
	}
	return nil
}

// Context returns the named context, or an empty one when unknown.
func (f *File) Context(name string) Context {
	if name == "" {
		name = f.CurrentContext
	}
	return f.Contexts[name]
}

// SetContext stores a context.
func (f *File) SetContext(name string, c Context) {
	if f.Contexts == nil {
		f.Contexts = map[string]Context{}
	}
	f.Contexts[name] = c
}

// ContextNames lists contexts in stable order.
func (f *File) ContextNames() []string {
	out := make([]string, 0, len(f.Contexts))
	for k := range f.Contexts {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// settable enumerates the dotted keys `hgr config set` accepts. Keeping this
// explicit means a typo is an error instead of a silently ignored setting.
var settable = []string{
	"current_context", "output", "fake",
	"context.server", "context.token", "context.project", "context.user",
}

// Keys lists every settable configuration key.
func Keys() []string { return append([]string(nil), settable...) }

// Get reads a dotted key.
func (f *File) Get(key string) (string, error) {
	c := f.Context("")
	switch key {
	case "current_context":
		return f.CurrentContext, nil
	case "output":
		return f.Output, nil
	case "fake":
		return strconv.FormatBool(f.Fake), nil
	case "context.server":
		return c.Server, nil
	case "context.project":
		return c.Project, nil
	case "context.user":
		return c.User, nil
	case "context.token":
		if c.Token == "" {
			return "", nil
		}
		return Redact(c.Token), nil
	}
	return "", unknownKey(key)
}

// Set writes a dotted key.
func (f *File) Set(key, value string) error {
	c := f.Context("")
	name := f.CurrentContext
	switch key {
	case "current_context":
		f.CurrentContext = value
	case "output":
		f.Output = value
	case "fake":
		b, err := strconv.ParseBool(value)
		if err != nil {
			return errs.New(errs.CodeUsage, "fake must be true or false")
		}
		f.Fake = b
	case "context.server":
		c.Server = value
		f.SetContext(name, c)
	case "context.token":
		c.Token = value
		f.SetContext(name, c)
	case "context.project":
		c.Project = value
		f.SetContext(name, c)
	case "context.user":
		c.User = value
		f.SetContext(name, c)
	default:
		return unknownKey(key)
	}
	return nil
}

// Unset clears a dotted key.
func (f *File) Unset(key string) error { return f.Set(key, "") }

func unknownKey(key string) error {
	e := errs.New(errs.CodeUsage, "%q is not a configuration key", key)
	var near []string
	for _, k := range settable {
		if strings.Contains(k, key) {
			near = append(near, k)
		}
	}
	if len(near) > 0 {
		e.WithFix("Did you mean "+strings.Join(near, ", "), "")
	}
	return e.WithFix("List every key", "hgr config list")
}

// Redact masks a credential for display.
func Redact(s string) string {
	if len(s) <= 10 {
		return strings.Repeat("*", len(s))
	}
	return s[:6] + strings.Repeat("*", 8) + s[len(s)-4:]
}

// EnvBool reads a boolean environment variable.
func EnvBool(key string) bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv(key)))
	return v == "1" || v == "true" || v == "yes" || v == "on"
}

// Describe renders a one-line summary of a context for listings.
func (c Context) Describe() string {
	server := c.Server
	if server == "" {
		server = DefaultServer
	}
	auth := "no credentials"
	if c.Token != "" {
		auth = Redact(c.Token)
	}
	return fmt.Sprintf("%s  %s  project=%s", server, auth, orDash(c.Project))
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}
