package cli

import (
	"fmt"
	"io"
	"os"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

// newLoginCmd trades the admin password for a short-lived session token and
// stores it in the active context, so subsequent commands are authenticated.
//
// It runs before a token exists, which is why it is annotated auth-optional and
// builds its own client with an empty token rather than reaching for the
// backend setup refused to construct.
func newLoginCmd(app *App) *cobra.Command {
	var (
		password string
		slug     string
	)
	cmd := &cobra.Command{
		Use:   "login [password]",
		Short: "Trade the admin password for a session token",
		Long: `Log in to the control plane and store a session token in the active context.

The password is read from, in order: the positional argument, --password,
$HANGAR_ADMIN_PASSWORD, then an interactive no-echo prompt. The token it returns
is short-lived (~900s) and signed; re-run this when it expires. The long-lived
alternative is a static operator token pasted with 'hgr config set context.token'.

The token is never printed, in any output mode.`,
		Example: `  hgr login
  hgr login --slug admin
  HANGAR_ADMIN_PASSWORD=… hgr login --no-input`,
		Args:        cobra.MaximumNArgs(1),
		Annotations: map[string]string{annotationAuthOptional: "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			pw := password
			if len(args) == 1 {
				pw = args[0]
			}
			if pw == "" {
				pw = os.Getenv("HANGAR_ADMIN_PASSWORD")
			}
			if pw == "" {
				if app.NoInput {
					return errs.New(errs.CodeInputRequired, "a password is required and prompting is disabled").
						WithCause("no argument, no --password, and $HANGAR_ADMIN_PASSWORD is unset").
						WithFix("Pass it in the environment", "HANGAR_ADMIN_PASSWORD=… hgr login")
				}
				got, err := app.Ask.Secret("admin password", "--password")
				if err != nil {
					return err
				}
				pw = got
			}
			if pw == "" {
				return errs.New(errs.CodeInputRequired, "an empty password cannot be used to log in")
			}

			server, err := app.server()
			if err != nil {
				return err
			}
			client, err := api.NewClient(server, "", app.Project, Version, app.Timeout)
			if err != nil {
				return err
			}

			ctx, cancel := app.RequestContext()
			defer cancel()
			res, err := client.Login(ctx, api.LoginRequest{Password: pw, Slug: slug})
			if err != nil {
				return err
			}

			// Persist to the stored context, not to the flag/env overlay, so the
			// token round-trips exactly like `hgr config set context.token`.
			name := app.Cfg.CurrentContext
			stored := app.Cfg.Context("")
			// The token belongs to the server that issued it, so a login with
			// --server records that server beside it.
			stored.Server = server
			stored.Token = res.AccessToken
			if res.RefreshToken != "" {
				stored.RefreshToken = res.RefreshToken
			}
			app.Cfg.SetContext(name, stored)
			if err := app.Cfg.Save(); err != nil {
				return err
			}

			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data: map[string]any{
					"server":     server,
					"kind":       res.Kind,
					"expires_in": res.ExpiresIn,
				},
				Names: []string{server},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s logged in to %s as %s, session expires in %ds\n",
						s.Tick(), s.Bold(server), res.Kind, res.ExpiresIn)
				},
				Next: []errs.Action{
					{Description: "See what the CLI is pointed at", Command: "hgr whoami"},
				},
			})
		},
	}
	fl := cmd.Flags()
	fl.StringVar(&password, "password", "", "admin password (prefer $HANGAR_ADMIN_PASSWORD or the prompt)")
	fl.StringVar(&slug, "slug", "admin", "principal slug to log in as")
	return group(cmd, "meta")
}

// newLogoutCmd clears the stored session token and best-effort revokes it on
// the plane. It is auth-optional so it still clears a token the plane has
// already expired.
func newLogoutCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:         "logout",
		Short:       "Clear the stored session token",
		Long:        `Remove the token from the active context and best-effort revoke it server-side.`,
		Args:        cobra.NoArgs,
		Annotations: map[string]string{annotationAuthOptional: "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			server := app.Context.Server
			if app.Context.Token != "" && server != "" {
				if client, err := api.NewClient(server, app.Context.Token, app.Project, Version, app.Timeout); err == nil {
					ctx, cancel := app.RequestContext()
					_ = client.Logout(ctx, app.Context.RefreshToken)
					cancel()
				}
			}

			name := app.Cfg.CurrentContext
			stored := app.Cfg.Context("")
			stored.Token = ""
			stored.RefreshToken = ""
			app.Cfg.SetContext(name, stored)
			if err := app.Cfg.Save(); err != nil {
				return err
			}

			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    map[string]any{"server": server},
				Names:   []string{server},
				Human: func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s logged out of %s\n", s.Tick(), s.Bold(server))
				},
			})
		},
	}
	return group(cmd, "meta")
}
