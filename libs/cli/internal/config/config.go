// Package config resolves settings from flags, environment, and the config
// file, in that order of precedence.
package config

import (
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	yaml "go.yaml.in/yaml/v2"
)

// There is no default server. Wingsmith is self-hosted, so any URL compiled in
// would belong to somebody else, and the first thing `wings login` sends to it
// is the admin password.

// Context is one named server + credential + tenancy set. Named after the
// kubeconfig notion because operators already carry that vocabulary.
type Context struct {
	Server       string `yaml:"server,omitempty" json:"server,omitempty"`
	Token        string `yaml:"token,omitempty" json:"-"`
	RefreshToken string `yaml:"refresh_token,omitempty" json:"-"`
	Project      string `yaml:"project,omitempty" json:"project,omitempty"`
	User         string `yaml:"user,omitempty" json:"user,omitempty"`
}

// File is the on-disk config document.
type File struct {
	CurrentContext string             `yaml:"current_context" json:"current_context"`
	Contexts       map[string]Context `yaml:"contexts" json:"contexts"`
	Output         string             `yaml:"output,omitempty" json:"output,omitempty"`
	Fake           bool               `yaml:"fake,omitempty" json:"fake,omitempty"`

	path string
	// saved is the current_context Save writes, changed only by
	// Set("current_context"). A --context or WINGSMITH_CONTEXT for one command
	// moves CurrentContext but never becomes the default when that command
	// writes the file.
	saved string
}

// Path returns the file the config was loaded from.
func (f *File) Path() string { return f.path }

// DefaultPath is the config location, honouring WINGSMITH_CONFIG and XDG.
func DefaultPath() string {
	if p := os.Getenv("WINGSMITH_CONFIG"); p != "" {
		return p
	}
	if d := os.Getenv("XDG_CONFIG_HOME"); d != "" {
		return filepath.Join(d, "wingsmith", "config.yaml")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "wingsmith-config.yaml"
	}
	return filepath.Join(home, ".config", "wingsmith", "config.yaml")
}

// StateDir is where the CLI keeps mutable local state (fake backend data).
func StateDir() string {
	if p := os.Getenv("WINGSMITH_STATE_DIR"); p != "" {
		return p
	}
	if d := os.Getenv("XDG_STATE_HOME"); d != "" {
		return filepath.Join(d, "wingsmith")
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".wingsmith-state"
	}
	return filepath.Join(home, ".local", "state", "wingsmith")
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
	f.saved = f.CurrentContext
	return f, nil
}

// Override makes name the current context for this process only. Save keeps
// writing the context the file names.
func (f *File) Override(name string) {
	if f.saved == "" {
		f.saved = f.CurrentContext
	}
	f.CurrentContext = name
}

// Save writes the config back to disk, readable by the owner only.
//
// The file holds tokens, so it is written to a 0600 temporary file beside the
// target and renamed over it. A file that already existed with looser
// permissions comes out 0600, and a crash mid-write leaves the old file whole
// instead of a truncated one. A symlinked config is written through to the
// file it points at, so a dotfiles checkout keeps its link.
func (f *File) Save() error {
	target := f.path
	if resolved, err := filepath.EvalSymlinks(target); err == nil {
		target = resolved
	}
	dir := filepath.Dir(target)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot create config directory")
	}
	doc := *f
	if f.saved != "" {
		doc.CurrentContext = f.saved
	}
	data, err := yaml.Marshal(&doc)
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot serialise config")
	}
	tmp, err := os.CreateTemp(dir, ".config-*.yaml")
	if err != nil {
		return errs.Wrap(err, errs.CodeInternal, "cannot write config to %s", f.path)
	}
	// Removing the temp file only matters when the rename never happened.
	defer func() { _ = os.Remove(tmp.Name()) }()
	werr := tmp.Chmod(0o600)
	if werr == nil {
		_, werr = tmp.Write(data)
	}
	if werr == nil {
		werr = tmp.Sync()
	}
	if cerr := tmp.Close(); werr == nil {
		werr = cerr
	}
	if werr == nil {
		werr = os.Rename(tmp.Name(), target)
	}
	if werr != nil {
		return errs.Wrap(werr, errs.CodeInternal, "cannot write config to %s", f.path)
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

// settable enumerates the dotted keys `wings config set` accepts. Keeping this
// explicit means a typo is an error instead of a silently ignored setting.
var settable = []string{
	"current_context", "output", "fake",
	"context.server", "context.token", "context.refresh_token", "context.project", "context.user",
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
	case "context.refresh_token":
		if c.RefreshToken == "" {
			return "", nil
		}
		return Redact(c.RefreshToken), nil
	}
	return "", unknownKey(key)
}

// ValidateOutput checks an output format before it is stored. The config
// package cannot import the renderer, so the command layer supplies it; nil
// accepts anything.
var ValidateOutput func(string) error

var contextName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// ValidateServer checks a control plane URL: absolute, http or https, with a
// host. A value that fails here would otherwise be stored and then fail every
// command that reads it.
func ValidateServer(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return errs.New(errs.CodeUsage, "%q is not a control plane URL", raw).
			WithCause("use an absolute http:// or https:// URL, e.g. http://localhost:8000")
	}
	return nil
}

// Set writes a dotted key. Values are checked here, so a bad one is refused
// when it is typed instead of being stored and then failing every later command.
// An empty value clears the key and is always accepted.
func (f *File) Set(key, value string) error {
	c := f.Context("")
	name := f.CurrentContext
	switch key {
	case "current_context":
		if value == "" {
			value = "default"
		}
		if !contextName.MatchString(value) {
			return errs.New(errs.CodeUsage, "%q is not a context name", value).
				WithCause("letters, digits, '.', '_' and '-', starting with a letter or digit")
		}
		f.CurrentContext = value
		f.saved = value
	case "output":
		if value != "" && ValidateOutput != nil {
			if err := ValidateOutput(value); err != nil {
				return err
			}
		}
		f.Output = value
	case "fake":
		if value == "" {
			f.Fake = false
			return nil
		}
		b, err := strconv.ParseBool(value)
		if err != nil {
			return errs.New(errs.CodeUsage, "fake must be true or false")
		}
		f.Fake = b
	case "context.server":
		if value != "" {
			if err := ValidateServer(value); err != nil {
				return err
			}
		}
		c.Server = strings.TrimRight(value, "/")
		f.SetContext(name, c)
	case "context.token":
		c.Token = value
		f.SetContext(name, c)
	case "context.refresh_token":
		c.RefreshToken = value
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
	return e.WithFix("List every key", "wings config list")
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
