package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
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

func newApplyCmd(app *App) *cobra.Command {
	var file string
	cmd := &cobra.Command{
		Use:   "apply -f <file>",
		Short: "Create or replace Workloads and Endpoints from manifests",
		Long: `Send each manifest in a YAML or JSON file to the plane with PUT, which creates
the object or replaces what it declares. Fields a manifest leaves out keep their
stored values, and applying the same file twice changes nothing.

Workloads are applied before Endpoints, so an endpoint's workloadRef has
something to resolve. A document without metadata.namespace is applied in
--project. Artifact manifests are not applied yet: register artifacts with
'wings artifact create'.`,
		Example: `  wings apply -f chat.yaml
  cat chat.yaml | wings apply -f -`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if file == "" {
				return errs.New(errs.CodeUsage, "apply needs a file").WithFlag("file")
			}
			var data []byte
			var err error
			if file == "-" {
				data, err = io.ReadAll(cmd.InOrStdin())
			} else {
				data, err = os.ReadFile(file)
			}
			if err != nil {
				return errs.Wrap(err, errs.CodeUsage, "cannot read %s", file)
			}
			docs, err := parseManifests(data)
			if err != nil {
				return err
			}
			project := ""
			if app.projectSet {
				project = app.Project
			}
			plan, err := applyPlan(docs, project)
			if err != nil {
				return err
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			var applied []api.Manifest
			var names, lines []string
			for _, p := range plan {
				out, created, err := app.Backend.ApplyManifest(ctx, p.collection, p.project, p.name, p.doc)
				if err != nil {
					return err
				}
				verb := "configured"
				if created {
					verb = "created"
				}
				ref := fmt.Sprintf("%s/%s", strings.TrimSuffix(p.collection, "s"), p.name)
				applied = append(applied, out)
				names = append(names, ref)
				lines = append(lines, ref+" "+verb)
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    applied,
				Names:   names,
				Human: func(w io.Writer, s ui.Style, width int) {
					for _, l := range lines {
						fmt.Fprintln(w, l)
					}
				},
			})
		},
	}
	cmd.Flags().StringVarP(&file, "file", "f", "", "manifest file, YAML or JSON, several documents allowed; - reads stdin")
	return group(cmd, "demand")
}

type applyItem struct {
	collection, project, name string
	doc                       api.Manifest
}

// applyPlan checks every document before anything is sent, then orders them:
// Workloads, then Endpoints.
func applyPlan(docs []api.Manifest, project string) ([]applyItem, error) {
	var workloads, endpoints []applyItem
	for i, doc := range docs {
		kind, _ := doc["kind"].(string)
		meta, _ := doc["metadata"].(map[string]any)
		name, _ := meta["name"].(string)
		if name == "" {
			return nil, errs.New(errs.CodeUsage, "document %d has no metadata.name", i+1)
		}
		namespace, _ := meta["namespace"].(string)
		if namespace == "" {
			namespace = project
		}
		item := applyItem{project: namespace, name: name, doc: doc}
		switch kind {
		case "Workload":
			item.collection = "workloads"
			workloads = append(workloads, item)
		case "Endpoint":
			item.collection = "endpoints"
			endpoints = append(endpoints, item)
		case "Artifact":
			return nil, errs.New(errs.CodeUsage, "artifact/%s: Artifact manifests cannot be applied yet", name).
				WithFix("Register the artifact instead", "wings artifact create")
		default:
			return nil, errs.New(errs.CodeUsage, "document %d has unknown kind %q", i+1, kind).
				WithCause("kinds: Workload, Endpoint")
		}
		if namespace == "" {
			return nil, errs.New(errs.CodeUsage, "%s/%s has no metadata.namespace", strings.ToLower(kind), name).
				WithFlag("project")
		}
	}
	return append(workloads, endpoints...), nil
}

// parseManifests reads one or more YAML (or JSON) documents into JSON-shaped maps.
func parseManifests(data []byte) ([]api.Manifest, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var docs []api.Manifest
	for {
		var raw any
		err := dec.Decode(&raw)
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, errs.Wrap(err, errs.CodeUsage, "cannot parse manifest")
		}
		if raw == nil {
			continue
		}
		doc, ok := jsonShaped(raw).(map[string]any)
		if !ok {
			return nil, errs.New(errs.CodeUsage, "document %d is not a mapping", len(docs)+1)
		}
		docs = append(docs, api.Manifest(doc))
	}
	if len(docs) == 0 {
		return nil, errs.New(errs.CodeUsage, "no manifests in the file")
	}
	return docs, nil
}

// jsonShaped turns YAML v2's map[any]any into map[string]any, all the way down.
func jsonShaped(v any) any {
	switch t := v.(type) {
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, v := range t {
			out[fmt.Sprint(k)] = jsonShaped(v)
		}
		return out
	case []any:
		for i := range t {
			t[i] = jsonShaped(t[i])
		}
		return t
	}
	return v
}
