package cli

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

// newLoginCmd trades the admin password for a session and stores it in the
// active context, so subsequent commands are authenticated.
//
// The password never travels on the command line, where it would land in shell
// history and in the process table: it comes from stdin, the environment, or a
// no-echo prompt.
func newLoginCmd(app *App) *cobra.Command {
	var (
		passwordStdin bool
		slug          string
	)
	cmd := &cobra.Command{
		Use:   "login",
		Short: "Log in to the control plane as the admin",
		Long: `Log in to the control plane and store the session in the active context.

Human access to a plane is a single admin login. The password is read from
stdin with --password-stdin, else from $HANGAR_ADMIN_PASSWORD, else from a
no-echo prompt. It is never accepted as an argument.

The session is an access token that lasts about 15 minutes and a refresh token
that renews it. The CLI renews it on its own when it expires; run login again
when the refresh token is refused too. The server you log in to is stored
beside the session, so --server on login sets it for later commands.

Neither token is ever printed, in any output mode.`,
		Example: `  hgr login --server http://localhost:8000
  printf '%s' "$ADMIN_PASSWORD" | hgr login --password-stdin
  HANGAR_ADMIN_PASSWORD=… hgr login --no-input`,
		Args:        cobra.NoArgs,
		Annotations: map[string]string{annotationAuthOptional: "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			server := "fake://local"
			if app.Backend == nil || !app.Backend.Simulated() {
				s, err := app.server()
				if err != nil {
					return err
				}
				server = s
			}

			pw, err := readPassword(app, passwordStdin)
			if err != nil {
				return err
			}

			ctx, cancel := app.RequestContext()
			defer cancel()
			res, err := app.Backend.Login(ctx, api.LoginRequest{Password: pw, Slug: slug})
			if err != nil {
				return err
			}

			result := &output.Result{
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
			}
			if app.Backend.Simulated() {
				result.Warnings = append(result.Warnings, "the fake backend issued this session, so nothing was stored")
				return app.Emit(result)
			}

			// Persist to the stored context, not to the flag/env overlay, so the
			// session round-trips exactly like `hgr config set context.token`.
			// The tokens belong to the server that issued them, so it is
			// recorded beside them.
			name := app.Cfg.CurrentContext
			stored := app.Cfg.Context("")
			stored.Server = server
			stored.Token = res.AccessToken
			stored.RefreshToken = res.RefreshToken
			app.Cfg.SetContext(name, stored)
			if err := app.Cfg.Save(); err != nil {
				return err
			}
			return app.Emit(result)
		},
	}
	fl := cmd.Flags()
	fl.BoolVar(&passwordStdin, "password-stdin", false, "read the password from stdin")
	fl.StringVar(&slug, "slug", "admin", "principal to log in as")
	return group(cmd, "meta")
}

// readPassword takes the password from stdin, the environment or a prompt, in
// that order. One trailing newline is dropped from stdin, so `echo` works.
func readPassword(app *App, fromStdin bool) (string, error) {
	var pw string
	switch {
	case fromStdin:
		line, err := bufio.NewReader(os.Stdin).ReadString('\n')
		if err != nil && err != io.EOF {
			return "", errs.Wrap(err, errs.CodeInputRequired, "cannot read the password from stdin")
		}
		pw = strings.TrimRight(line, "\r\n")
	case os.Getenv("HANGAR_ADMIN_PASSWORD") != "":
		pw = os.Getenv("HANGAR_ADMIN_PASSWORD")
	case app.NoInput:
		return "", errs.New(errs.CodeInputRequired, "a password is required and prompting is disabled").
			WithCause("--password-stdin was not given and $HANGAR_ADMIN_PASSWORD is unset").
			WithFlag("--password-stdin").
			WithFix("Pipe it in", "hgr login --password-stdin < password-file")
	default:
		got, err := app.Ask.Secret("admin password", "--password-stdin")
		if err != nil {
			return "", err
		}
		pw = got
	}
	if pw == "" {
		return "", errs.New(errs.CodeInputRequired, "an empty password cannot be used to log in")
	}
	return pw, nil
}

// newLogoutCmd revokes the session on the plane and clears it from the active
// context. It is auth-optional so it still clears a session the plane has
// already expired, and it clears the local copy even when revocation fails —
// but it says so, because a session that was not revoked is still usable until
// its refresh token expires.
func newLogoutCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "logout",
		Short: "Revoke the session and clear it from the context",
		Long: `Revoke the stored refresh token on the plane and remove both tokens from the
active context.

If the plane cannot be reached or refuses, the tokens are still removed locally
and a warning says the session was not revoked.`,
		Args:        cobra.NoArgs,
		Annotations: map[string]string{annotationAuthOptional: "true"},
		RunE: func(cmd *cobra.Command, args []string) error {
			server := app.Context.Server
			result := &output.Result{
				Command: cmdPath(cmd),
				Names:   []string{server},
			}
			if app.Backend != nil && app.Backend.Simulated() {
				result.Data = map[string]any{"server": "fake://local", "revoked": false}
				result.Warnings = []string{"the fake backend holds no session, so nothing was revoked or cleared"}
				result.Human = func(w io.Writer, s ui.Style, width int) {
					fmt.Fprintf(w, "%s nothing to log out of\n", s.Tick())
				}
				return app.Emit(result)
			}

			stored := app.Cfg.Context("")
			revoked := false
			switch {
			case stored.RefreshToken == "" && stored.Token == "":
			case stored.RefreshToken == "":
				result.Warnings = append(result.Warnings,
					"no refresh token is stored, so nothing was revoked on the plane; the access token expires on its own")
			case app.Backend == nil:
				result.Warnings = append(result.Warnings,
					"no server is configured, so the session was not revoked on the plane")
			default:
				ctx, cancel := app.RequestContext()
				err := app.Backend.Logout(ctx, stored.RefreshToken)
				cancel()
				if err != nil {
					result.Warnings = append(result.Warnings,
						"the session was not revoked on the plane: "+errs.As(err).Message)
				} else {
					revoked = true
				}
			}

			stored.Token = ""
			stored.RefreshToken = ""
			app.Cfg.SetContext(app.Cfg.CurrentContext, stored)
			if err := app.Cfg.Save(); err != nil {
				return err
			}

			result.Data = map[string]any{"server": server, "revoked": revoked}
			result.Human = func(w io.Writer, s ui.Style, width int) {
				if revoked {
					fmt.Fprintf(w, "%s logged out of %s\n", s.Tick(), s.Bold(orDash(server)))
					return
				}
				fmt.Fprintf(w, "%s cleared the local session for %s\n", s.Tick(), s.Bold(orDash(server)))
			}
			return app.Emit(result)
		},
	}
	return group(cmd, "meta")
}
