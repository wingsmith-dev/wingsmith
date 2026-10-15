// Package cli wires the command tree. Commands never touch a transport or a
// terminal directly: they ask the App for a backend, build an output.Result,
// and hand it back.
package cli

import (
	"context"
	"os"
	"os/signal"
	"slices"
	"strings"
	"syscall"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/config"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/fake"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

// Version is stamped at build time.
var (
	Version = "0.1.0-dev"
	Commit  = "unknown"
	Built   = "unknown"
)

// App is the resolved session: configuration, streams, and the backend. It is
// constructed empty, populated by the root command's PersistentPreRunE, and
// shared by reference with every subcommand.
type App struct {
	Ctx     context.Context
	Cfg     *config.File
	Context config.Context
	Backend api.Backend

	Out    *output.Renderer
	Report ui.Reporter
	Ask    *ui.Prompter
	Style  ui.Style
	Width  int

	Format  output.Format
	Agent   bool
	Quiet   bool
	Yes     bool
	Force   bool
	NoInput bool
	Debug   bool
	Timeout time.Duration

	Project string
	// projectSet is whether a project was named anywhere (flag, environment or
	// context) rather than falling back to "default", so a command that writes
	// can refuse to guess.
	projectSet bool

	// authOptional is set for commands that must run before a token exists —
	// `login` above all — so setup does not refuse them for want of one.
	authOptional bool
	// local is set for commands that never talk to the control plane: config,
	// context, explain, version, help and completion. They work on a fresh
	// install with no server and no token.
	local bool

	flags globalFlags
}

const (
	// annotationAuthOptional marks a command that setup must not gate on a token.
	annotationAuthOptional = "auth_optional"
	// annotationLocal marks a command, and everything under it, that needs no
	// backend at all.
	annotationLocal = "local"
)

// localCmd marks a command as never reaching the control plane.
func localCmd(cmd *cobra.Command) *cobra.Command {
	if cmd.Annotations == nil {
		cmd.Annotations = map[string]string{}
	}
	cmd.Annotations[annotationLocal] = "true"
	return cmd
}

// isLocal reports whether a command, or any command above it, is local. Help
// and completion are cobra's own and are recognised by name.
func isLocal(cmd *cobra.Command) bool {
	for c := cmd; c != nil; c = c.Parent() {
		if c.Annotations[annotationLocal] == "true" {
			return true
		}
		switch c.Name() {
		case "help", "completion", cobra.ShellCompRequestCmd, cobra.ShellCompNoDescRequestCmd:
			return true
		}
	}
	return false
}

type globalFlags struct {
	output     string
	context    string
	project    string
	server     string
	token      string
	configPath string
	timeout    string
	quiet      bool
	debug      bool
	yes        bool
	force      bool
	noInput    bool
	noColor    bool
	agent      bool
	fake       bool
}

// Execute builds and runs the command tree, returning the process exit code.
func Execute() int {
	app := &App{}
	root := newRootCmd(app)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	err := root.ExecuteContext(ctx)
	if err == nil {
		return 0
	}
	if app.Out == nil {
		app.bootstrapFallback()
	}
	if ctx.Err() != nil && errs.As(err).Code == errs.CodeInternal {
		err = errs.New(errs.CodeInterrupted, "interrupted")
	}
	return app.Out.EmitError(commandPath(root), err)
}

func commandPath(root *cobra.Command) string {
	cmd, _, err := root.Find(os.Args[1:])
	if err != nil || cmd == nil {
		return "hgr"
	}
	return strings.TrimPrefix(cmd.CommandPath(), "hgr ")
}

// cmdPath is the canonical invocation path of a command, without the binary
// name, so the reported `command` always matches what was actually run.
func cmdPath(cmd *cobra.Command) string {
	return strings.TrimPrefix(cmd.CommandPath(), cmd.Root().Name()+" ")
}

// bootstrapFallback builds the renderer for failures that happen before setup
// finished, so a bad flag still answers in the right shape.
func (a *App) bootstrapFallback() {
	agent := a.flags.agent || config.EnvBool("HANGAR_AGENT")
	format := output.FormatHuman
	if agent {
		format = output.FormatJSON
	}
	a.Style = ui.DetectStyle(os.Stderr, a.flags.noColor || agent)
	a.Width = ui.Width(os.Stderr)
	a.Out = output.New(os.Stdout, os.Stderr, a.Style, format, "", a.Width, agent, a.flags.quiet)
}

func newRootCmd(app *App) *cobra.Command {
	root := &cobra.Command{
		Use:   "hgr",
		Short: "Hangar — control plane for many GPU clusters and many models",
		Long: `hgr is the Hangar command line.

Enrol and operate GPU clusters, publish model artifacts, declare workloads and
place deployments, and put endpoints in front of them — all through the Hangar
control plane's API, which is the only thing this binary talks to.

Every command is noun-verb and every command has a machine contract: pipe it
to jq with -o json, or set HANGAR_AGENT=1 for a stable JSON envelope with
suggested next actions.

Nothing is running yet? Add --fake for a complete offline control plane.`,
		SilenceUsage:      true,
		SilenceErrors:     true,
		DisableAutoGenTag: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			return app.setup(cmd)
		},
		RunE: func(cmd *cobra.Command, args []string) error {
			return cmd.Help()
		},
	}

	f := root.PersistentFlags()
	g := &app.flags
	f.StringVarP(&g.output, "output", "o", "", "output format: auto|human|wide|json|jsonl|yaml|tsv|name|template=…|jsonpath=…")
	f.StringVar(&g.context, "context", "", "named context from the config file")
	f.StringVar(&g.project, "project", "", "project recorded in output metadata (not sent to the plane, which does not scope by it)")
	f.StringVar(&g.server, "server", "", "control plane base URL")
	f.StringVar(&g.token, "token", "", "API token (prefer HANGAR_TOKEN)")
	f.StringVar(&g.configPath, "config", "", "path to the config file")
	f.StringVar(&g.timeout, "timeout", "", "per-request timeout (default 60s)")
	f.BoolVarP(&g.quiet, "quiet", "q", false, "print only identifiers and errors")
	f.BoolVar(&g.debug, "debug", false, "print stack traces, timings, and raw payloads")
	f.BoolVarP(&g.yes, "yes", "y", false, "assume yes to confirmation prompts")
	f.BoolVar(&g.force, "force", false, "required for destructive operations without a terminal")
	f.BoolVar(&g.noInput, "no-input", false, "never prompt; missing input becomes an error")
	f.BoolVar(&g.noColor, "no-color", false, "disable ANSI colour")
	f.BoolVar(&g.agent, "agent", false, "agent mode: JSON envelope, no prompts, no animation")
	f.BoolVar(&g.fake, "fake", false, "use the built-in offline control plane")

	root.AddGroup(
		&cobra.Group{ID: "supply", Title: "Supply — what capacity exists:"},
		&cobra.Group{ID: "demand", Title: "Demand — what should be running:"},
		&cobra.Group{ID: "binding", Title: "Binding — tenancy and limits:"},
		&cobra.Group{ID: "machinery", Title: "Machinery — operating the fleet:"},
		&cobra.Group{ID: "meta", Title: "CLI:"},
	)

	config.ValidateOutput = func(v string) error {
		_, _, err := output.ParseFormat(v)
		return err
	}

	register(app, root)
	root.SetHelpCommandGroupID("meta")
	root.SetCompletionCommandGroupID("meta")
	return root
}

func (a *App) setup(cmd *cobra.Command) error {
	g := a.flags
	a.authOptional = cmd.Annotations[annotationAuthOptional] == "true"
	a.local = isLocal(cmd)

	cfg, err := config.Load(firstNonEmpty(g.configPath, os.Getenv("HANGAR_CONFIG")))
	if err != nil {
		return err
	}
	a.Cfg = cfg

	if name := firstNonEmpty(g.context, os.Getenv("HANGAR_CONTEXT")); name != "" {
		cfg.CurrentContext = name
		if _, ok := cfg.Contexts[name]; !ok && name != "default" {
			return errs.New(errs.CodeNotFound, "no context named %q", name).
				WithFix("List contexts", "hgr context ls")
		}
	}
	a.Context = cfg.Context("")
	if v := firstNonEmpty(g.server, os.Getenv("HANGAR_SERVER")); v != "" {
		a.Context.Server = v
	}
	if v := firstNonEmpty(g.token, os.Getenv("HANGAR_TOKEN")); v != "" {
		a.Context.Token = v
	}
	a.Project = firstNonEmpty(g.project, os.Getenv("HANGAR_PROJECT"), a.Context.Project)
	a.projectSet = a.Project != ""
	if !a.projectSet {
		a.Project = "default"
	}

	a.Agent = g.agent || config.EnvBool("HANGAR_AGENT")
	a.Quiet = g.quiet || config.EnvBool("HANGAR_QUIET")
	a.Yes = g.yes || config.EnvBool("HANGAR_YES")
	a.Force = g.force
	a.Debug = g.debug || config.EnvBool("HANGAR_DEBUG")
	a.NoInput = g.noInput || config.EnvBool("HANGAR_NO_INPUT") || a.Agent || !ui.StdinIsTerminal()

	a.Timeout = 60 * time.Second
	if v := firstNonEmpty(g.timeout, os.Getenv("HANGAR_TIMEOUT")); v != "" {
		d, err := api.ParseDuration(v)
		if err != nil {
			return err
		}
		a.Timeout = d
	}

	format, formatArg, err := a.resolveFormat()
	if err != nil {
		return err
	}
	a.Format = format

	noColor := g.noColor || a.Agent || format.Machine()
	a.Style = ui.DetectStyle(os.Stderr, noColor)
	a.Width = ui.Width(os.Stdout)
	a.Out = output.New(os.Stdout, os.Stderr, a.Style, format, formatArg, a.Width, a.Agent, a.Quiet)

	animate := ui.IsTerminal(os.Stderr) && !a.Agent && a.Style.Level != ui.ColorNone
	silent := a.Agent || a.Quiet || format.Machine()
	a.Report = ui.NewReporter(os.Stderr, a.Style, animate, silent)
	a.Out.Reporter = a.Report

	a.Ask = ui.NewPrompter(os.Stdin, os.Stderr, a.Style, !a.NoInput)
	a.Ctx = cmd.Context()

	if a.local {
		a.Out.AddMeta("project", a.Project)
		return nil
	}
	if err := a.connect(); err != nil {
		return err
	}
	if a.Backend.Simulated() {
		a.Out.AddMeta("simulated", true)
	}
	a.Out.AddMeta("project", a.Project)
	return nil
}

// resolveFormat applies the precedence rule and resolves `auto`.
//
// A bad format stored in the config file does not lock the user out of the
// command that fixes it: a local command falls back to `auto`, and every other
// command says which key to repair.
func (a *App) resolveFormat() (output.Format, string, error) {
	explicit := firstNonEmpty(a.flags.output, os.Getenv("HANGAR_OUTPUT"))
	raw := firstNonEmpty(explicit, a.Cfg.Output, string(output.FormatAuto))
	f, arg, err := output.ParseFormat(raw)
	if err != nil && explicit == "" {
		if a.local {
			f, arg, err = output.FormatAuto, "", nil
		} else {
			return "", "", errs.As(err).
				WithCausef("output %q is set in %s", a.Cfg.Output, a.Cfg.Path()).
				WithFix("Reset it", "hgr config set output auto")
		}
	}
	if err != nil {
		return "", "", err
	}
	if f == output.FormatAuto {
		if ui.IsTerminal(os.Stdout) {
			f = output.FormatHuman
		} else {
			f = output.FormatJSON
		}
	}
	return f, arg, nil
}

// UseFake reports whether the offline backend was requested.
func (a *App) UseFake() bool {
	return a.flags.fake || config.EnvBool("HANGAR_FAKE") || a.Cfg.Fake
}

// connect selects the backend. The fake is opt-in: silently simulating a
// control plane the caller believes is real is worse than failing to reach it.
func (a *App) connect() error {
	if a.UseFake() {
		b, err := fake.New(os.Getenv("HANGAR_FAKE_STATE"))
		if err != nil {
			return err
		}
		a.Backend = b
		return nil
	}
	server, err := a.server()
	if err != nil {
		return err
	}
	if a.Context.Token == "" && !a.authOptional {
		return errs.New(errs.CodeAuthRequired, "no token for context %q", a.Cfg.CurrentContext).
			WithCausef("looked in --token, HANGAR_TOKEN, and %s", a.Cfg.Path()).
			WithFix("Log in", "hgr login").
			WithFix("Or work offline", rerun("--fake"))
	}
	c, err := api.NewClient(server, a.Context.Token, a.Project, Version, a.Timeout)
	if err != nil {
		return err
	}
	a.Backend = c
	return nil
}

// server is the control plane URL for this invocation. There is no default:
// Hangar is self-hosted, and a compiled-in URL would be somebody else's.
func (a *App) server() (string, error) {
	if a.Context.Server == "" {
		return "", errs.New(errs.CodeUsage, "no control plane URL configured for context %q", a.Cfg.CurrentContext).
			WithCausef("looked in --server, HANGAR_SERVER, and %s", a.Cfg.Path()).
			WithFlag("--server").
			WithFix("Set one", "hgr config set context.server http://localhost:8000").
			WithFix("Or work offline", rerun("--fake"))
	}
	if err := config.ValidateServer(a.Context.Server); err != nil {
		return "", err
	}
	return a.Context.Server, nil
}

// --- helpers used by every command ----------------------------------------

// Emit renders a successful result.
func (a *App) Emit(res *output.Result) error { return a.Out.Emit(res) }

// Human reports whether rich terminal output should be drawn.
func (a *App) Human() bool { return !a.Agent && !a.Format.Machine() && !a.Quiet }

// Table builds a table sized to the terminal, honouring `-o wide`.
func (a *App) Table(cols ...ui.Column) *ui.Table {
	return ui.NewTable(a.Style, a.Width, a.Format == output.FormatWide, cols...)
}

// KV builds a detail block.
func (a *App) KV() *ui.KV { return ui.NewKV(a.Style) }

// RequestContext returns a context carrying the configured per-request timeout.
func (a *App) RequestContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(a.Ctx, a.Timeout)
}

// Confirm gates a destructive operation.
func (a *App) Confirm(question string) error {
	if a.Yes {
		return nil
	}
	if a.NoInput && a.Force {
		return nil
	}
	if a.NoInput {
		return errs.New(errs.CodeConfirmationRequired, "%s", question).
			WithCause("this operation is destructive and no interactive terminal is available").
			WithFlag("--yes").
			WithFix("Re-run confirming the operation", rerun("--yes"))
	}
	return a.Ask.Confirm(question, false)
}

// rerun reconstructs the current invocation as a command the caller can paste.
func rerun(extra ...string) string {
	parts := append([]string{"hgr"}, os.Args[1:]...)
	for _, f := range extra {
		if !slices.Contains(parts, f) {
			parts = append(parts, f)
		}
	}
	for i, p := range parts {
		parts[i] = shellQuote(p)
	}
	return strings.Join(parts, " ")
}

func shellQuote(s string) string {
	if s != "" && !strings.ContainsAny(s, " \t\n\v\"'\\$`*?[]|&;<>(){}#~!") {
		return s
	}
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}
