package cli

import (
	"fmt"
	"io"
	"runtime"
	"sort"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/config"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

func newWhoamiCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "whoami",
		Short: "Show what the CLI is pointed at, and whether it answers",
		Long: `Show the server and project the CLI is configured for, whether a token is
stored, and whether the plane answers.

The plane has no identity endpoint. Human access is a single admin login
(hgr login), so there is no user name to show.`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			id, err := app.Backend.Identity(ctx)
			if err != nil {
				return err
			}
			id.Project = app.Project
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    id,
				Names:   []string{id.Server},
				Human: func(w io.Writer, s ui.Style, width int) {
					kv := app.KV()
					kv.Add("server", id.Server)
					kv.Add("project", app.Project)
					kv.Add("token", boolWord(id.HasToken))
					kv.Add("reachable", yesNo(s, id.Reachable))
					if id.Simulated {
						kv.Add("backend", s.Amber("fake (offline)"))
					}
					kv.Render(w)
				},
			})
		},
	}
	return group(cmd, "meta")
}

func newContextCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     "context",
		Aliases: []string{"contexts"},
		Short:   "Server, token, and project the CLI is pointed at",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			c := app.Context
			data := map[string]any{
				"current_context": app.Cfg.CurrentContext,
				"server":          c.Server,
				"project":         app.Project,
				"authenticated":   c.Token != "",
				"simulated":       app.UseFake(),
				"config_path":     app.Cfg.Path(),
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    data,
				Human: func(w io.Writer, s ui.Style, width int) {
					kv := app.KV()
					kv.Add("context", app.Cfg.CurrentContext)
					kv.Add("server", orDefault(c.Server, s.Dim("none — hgr config set context.server <url>")))
					kv.Add("project", app.Project)
					kv.Add("token", tokenState(c))
					if app.UseFake() {
						kv.Add("backend", s.Amber("fake (offline)"))
					}
					kv.Add("config", app.Cfg.Path())
					kv.Render(w)
				},
			})
		},
	}
	cmd.AddCommand(newContextListCmd(app), newContextUseCmd(app))
	return localCmd(group(cmd, "meta"))
}

func tokenState(c config.Context) string {
	if c.Token == "" {
		return "none"
	}
	return config.Redact(c.Token)
}

func newContextListCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List configured contexts",
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			names := app.Cfg.ContextNames()
			type row struct {
				Name    string `json:"name"`
				Server  string `json:"server"`
				Project string `json:"project,omitempty"`
				Current bool   `json:"current"`
			}
			out := make([]row, 0, len(names))
			for _, n := range names {
				c := app.Cfg.Contexts[n]
				out = append(out, row{n, c.Server, c.Project, n == app.Cfg.CurrentContext})
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    out,
				Names:   names,
				Human: func(w io.Writer, s ui.Style, width int) {
					if len(out) == 0 {
						fmt.Fprintf(w, "%s\n", s.Dim("no contexts configured"))
						return
					}
					t := app.Table(
						ui.Column{Title: ""},
						ui.Column{Title: "name"},
						ui.Column{Title: "server"},
						ui.Column{Title: "project"},
					)
					for _, r := range out {
						mark := ""
						if r.Current {
							mark = s.Green("*")
						}
						t.Row(mark, r.Name, orDash(r.Server), orDash(r.Project))
					}
					t.Render(w)
				},
			})
		},
	}
}

func newContextUseCmd(app *App) *cobra.Command {
	return &cobra.Command{
		Use:   "use <name>",
		Short: "Switch the active context",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if _, ok := app.Cfg.Contexts[args[0]]; !ok {
				return errs.New(errs.CodeNotFound, "no context named %q", args[0]).
					WithFix("List contexts", "hgr context ls")
			}
			app.Cfg.CurrentContext = args[0]
			if err := app.Cfg.Save(); err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    map[string]any{"current_context": args[0]},
				Names:   []string{args[0]},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s now using %s\n", s.Tick(), s.Bold(args[0]))
				},
			})
		},
	}
}

func newConfigCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "config",
		Short: "Read and write the config file",
	}
	cmd.AddCommand(
		&cobra.Command{
			Use:     "ls",
			Aliases: []string{"list"},
			Short:   "List every configuration key and its value",
			Args:    cobra.NoArgs,
			RunE: func(cmd *cobra.Command, args []string) error {
				data := map[string]string{}
				for _, k := range config.Keys() {
					v, err := app.Cfg.Get(k)
					if err != nil {
						return err
					}
					data[k] = v
				}
				keys := config.Keys()
				sort.Strings(keys)
				return app.Emit(&output.Result{
					Command: cmdPath(cmd),
					Data:    data,
					Human: func(w io.Writer, s ui.Style, width int) {
						kv := app.KV()
						for _, k := range keys {
							kv.Add(k, data[k])
						}
						kv.Render(w)
					},
				})
			},
		},
		&cobra.Command{
			Use:   "get <key>",
			Short: "Read one configuration key",
			Args:  cobra.ExactArgs(1),
			RunE: func(cmd *cobra.Command, args []string) error {
				v, err := app.Cfg.Get(args[0])
				if err != nil {
					return err
				}
				return app.Emit(&output.Result{
					Command: cmdPath(cmd),
					Data:    map[string]string{args[0]: v},
					Names:   []string{v},
					Human: func(w io.Writer, s ui.Style, width int) {
						fmt.Fprintln(w, v)
					},
				})
			},
		},
		&cobra.Command{
			Use:   "set <key> <value>",
			Short: "Write one configuration key",
			Args:  cobra.ExactArgs(2),
			RunE: func(cmd *cobra.Command, args []string) error {
				if err := app.Cfg.Set(args[0], args[1]); err != nil {
					return err
				}
				if err := app.Cfg.Save(); err != nil {
					return err
				}
				shown, _ := app.Cfg.Get(args[0])
				return app.Emit(&output.Result{
					Command: cmdPath(cmd),
					Data:    map[string]string{args[0]: shown},
					Human: func(w io.Writer, s ui.Style, width int) {
						fmt.Fprintf(w, "%s %s = %s\n", s.Tick(), args[0], shown)
					},
				})
			},
		},
	)
	return localCmd(group(cmd, "meta"))
}

func newExplainCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "explain [code]",
		Short: "Explain an error code and its exit status",
		Args:  cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if len(args) == 0 {
				codes := errs.AllCodes()
				all := make([]errs.Explanation, 0, len(codes))
				for _, c := range codes {
					e, _ := errs.Explain(c)
					all = append(all, e)
				}
				return app.Emit(&output.Result{
					Command: cmdPath(cmd),
					Data:    all,
					Human: func(w io.Writer, s ui.Style, width int) {
						t := app.Table(
							ui.Column{Title: "code"},
							ui.Column{Title: "exit", Align: ui.AlignRight},
							ui.Column{Title: "summary"},
						)
						for _, e := range all {
							t.Row(string(e.Code), fmt.Sprint(e.Exit), e.Summary)
						}
						t.Render(w)
					},
				})
			}
			code := errs.ParseCode(args[0])
			e, ok := errs.Explain(code)
			if !ok {
				return errs.New(errs.CodeNotFound, "unknown error code %q", args[0]).
					WithFix("List every code", "hgr explain")
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    e,
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s %s\n", s.Amber(string(e.Code)), s.Dim(fmt.Sprintf("exit %d", e.Exit)))
					kv := app.KV()
					kv.Add("summary", e.Summary)
					kv.Add("detail", e.Detail)
					for i, f := range e.Fixes {
						key := ""
						if i == 0 {
							key = "fixes"
						}
						kv.Add(key, s.Cyan(f))
					}
					if e.TypicallyRetryable {
						kv.Add("retryable", "yes")
					}
					kv.Render(w)
				},
			})
		},
	}
	return localCmd(group(cmd, "meta"))
}

func newVersionCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "version",
		Short: "Print the CLI version",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			data := map[string]string{
				"version": Version,
				"commit":  Commit,
				"built":   Built,
				"go":      runtime.Version(),
				"os_arch": runtime.GOOS + "/" + runtime.GOARCH,
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    data,
				Names:   []string{Version},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "hgr %s %s %s\n", s.Bold(Version), s.Dim(s.Dot()),
						s.Dim(strings.Join([]string{Commit, runtime.GOOS + "/" + runtime.GOARCH}, " ")))
				},
			})
		},
	}
	return localCmd(group(cmd, "meta"))
}
