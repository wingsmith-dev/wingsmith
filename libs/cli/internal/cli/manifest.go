package cli

import (
	"encoding/json"
	"fmt"
	"io"
	"strings"

	"github.com/spf13/cobra"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/output"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/ui"
	yaml "go.yaml.in/yaml/v2"
)

// manifestCollection maps what a user types for a kind ("workload", "Workloads",
// "wl") to its collection under /v1/manifests.
func manifestCollection(kind string) (string, error) {
	switch strings.ToLower(kind) {
	case "workload", "workloads", "wl":
		return "workloads", nil
	case "endpoint", "endpoints", "ep":
		return "endpoints", nil
	case "artifact", "artifacts":
		return "artifacts", nil
	}
	return "", errs.New(errs.CodeUsage, "unknown kind %q", kind).WithCause("kinds: workload, endpoint, artifact")
}

func newGetCmd(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:   "get <kind> <name>",
		Short: "Print one object as a plane.wingsmith.dev/v1alpha1 manifest",
		Long: `Print a Workload, Endpoint or Artifact as the manifest the plane renders:
metadata, the declared spec and the derived status.

Workloads and endpoints are read in --project. An artifact is read in --project
when it is private to it, and from the public catalogue otherwise.

Human output is YAML; -o json and -o yaml print the manifest alone.`,
		Example: `  wings get workload chat --project core
  wings get artifact gpt-oss-120b -o yaml`,
		Args: cobra.ExactArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			collection, err := manifestCollection(args[0])
			if err != nil {
				return err
			}
			name := args[1]
			project := ""
			if app.projectSet {
				project = app.Project
			}
			if collection != "artifacts" && project == "" {
				return errs.New(errs.CodeUsage, "a %s lives in a project", strings.TrimSuffix(collection, "s")).
					WithFlag("project")
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			m, err := app.Backend.Manifest(ctx, collection, project, name)
			if collection == "artifacts" && project != "" {
				if e := errs.As(err); e != nil && e.Code == errs.CodeNotFound {
					m, err = app.Backend.Manifest(ctx, collection, "", name)
				}
			}
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    m,
				Names:   []string{fmt.Sprintf("%s/%s", strings.TrimSuffix(collection, "s"), name)},
				Human: func(w io.Writer, s ui.Style, width int) {
					_ = writeManifestYAML(w, m)
				},
			})
		},
	}
	return group(cmd, "demand")
}

func writeManifestYAML(w io.Writer, m api.Manifest) error {
	data, err := json.Marshal(m)
	if err != nil {
		return err
	}
	var generic any
	if err := yaml.Unmarshal(data, &generic); err != nil {
		return err
	}
	out, err := yaml.Marshal(generic)
	if err != nil {
		return err
	}
	_, err = w.Write(out)
	return err
}
