package cli

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
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

// exitStatus ends a command with a status and nothing more to print: the
// command already reported what happened (apply's failed documents, diff's diff).
type exitStatus int

func (e exitStatus) Error() string { return fmt.Sprintf("exit status %d", int(e)) }

// manifestSources are the -f and -R flags apply and diff share.
type manifestSources struct {
	files     []string
	recursive bool
}

func (m *manifestSources) bind(cmd *cobra.Command) {
	cmd.Flags().StringArrayVarP(&m.files, "file", "f", nil, "manifest file or directory, YAML or JSON, several documents allowed; - reads stdin (repeatable)")
	cmd.Flags().BoolVarP(&m.recursive, "recursive", "R", false, "read directories given to -f recursively")
}

// read returns every document from every source, in the order given: files in
// a directory by name, subdirectories only with -R.
func (m *manifestSources) read(stdin io.Reader) ([]sourced, error) {
	if len(m.files) == 0 {
		return nil, errs.New(errs.CodeUsage, "needs a manifest file").WithFlag("file")
	}
	var out []sourced
	add := func(name string, data []byte) error {
		docs, err := parseManifests(data)
		if err != nil {
			return errs.Wrap(err, errs.CodeUsage, "%s: %s", name, errs.As(err).Message)
		}
		for _, d := range docs {
			out = append(out, sourced{source: name, doc: d})
		}
		return nil
	}
	for _, f := range m.files {
		if f == "-" {
			data, err := io.ReadAll(stdin)
			if err != nil {
				return nil, errs.Wrap(err, errs.CodeUsage, "cannot read stdin")
			}
			if err := add("stdin", data); err != nil {
				return nil, err
			}
			continue
		}
		info, err := os.Stat(f)
		if err != nil {
			return nil, errs.Wrap(err, errs.CodeUsage, "cannot read %s", f)
		}
		paths := []string{f}
		if info.IsDir() {
			if paths, err = manifestFiles(f, m.recursive); err != nil {
				return nil, errs.Wrap(err, errs.CodeUsage, "cannot read %s", f)
			}
		}
		for _, p := range paths {
			data, err := os.ReadFile(p)
			if err != nil {
				return nil, errs.Wrap(err, errs.CodeUsage, "cannot read %s", p)
			}
			if err := add(p, data); err != nil {
				return nil, err
			}
		}
	}
	if len(out) == 0 {
		return nil, errs.New(errs.CodeUsage, "no manifests in %s", strings.Join(m.files, ", "))
	}
	return out, nil
}

// manifestFiles lists the .yaml, .yml and .json files under dir, sorted by path.
func manifestFiles(dir string, recursive bool) ([]string, error) {
	var out []string
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if p != dir && !recursive {
				return filepath.SkipDir
			}
			return nil
		}
		switch strings.ToLower(filepath.Ext(p)) {
		case ".yaml", ".yml", ".json":
			out = append(out, p)
		}
		return nil
	})
	sort.Strings(out)
	return out, err
}

type sourced struct {
	source string
	doc    api.Manifest
}

func (a *App) projectFlag() string {
	if a.projectSet {
		return a.Project
	}
	return ""
}

// applyOutcome is one document's result, in apply's JSON output.
type applyOutcome struct {
	Ref    string       `json:"ref"`
	Source string       `json:"source"`
	Result string       `json:"result,omitempty"`
	Error  *errs.Error  `json:"error,omitempty"`
	Object api.Manifest `json:"object,omitempty"`
}

func newApplyCmd(app *App) *cobra.Command {
	var src manifestSources
	var ifMatch string
	cmd := &cobra.Command{
		Use:   "apply -f <file|dir|->",
		Short: "Create or replace Workloads and Endpoints from manifests",
		Long: `Send each manifest to the plane with PUT, which creates the object or replaces
what it declares. The plane keeps the last manifest applied to each object, so a
field removed from the file goes back to its default on the next apply; a field
never declared keeps its stored value, and applying the same file twice changes
nothing. Each object prints as created, configured or unchanged.

Workloads are applied before Endpoints, so an endpoint's workloadRef has
something to resolve, and otherwise in file order. A document that fails does
not stop the rest: every result is printed and apply exits 1 if any failed.

A document without metadata.namespace is applied in --project. A document with
metadata.resourceVersion is applied only if the object is still at that
revision; --if-match does the same for a single document. Artifact manifests
are not applied yet: register artifacts with 'wings artifact create'.`,
		Example: `  wings apply -f chat.yaml
  wings apply -f deploy/ -R
  cat chat.yaml | wings apply -f -`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) (err error) {
			// An error must not exit 1, which means "differs": emit it here and
			// lift an unmapped or internal code to 2.
			defer func() {
				var status exitStatus
				if err == nil || errors.As(err, &status) || errs.As(err).ExitCode() >= 2 {
					return
				}
				app.Out.EmitError(cmdPath(cmd), err)
				err = exitStatus(2)
			}()
			docs, err := src.read(cmd.InOrStdin())
			if err != nil {
				return err
			}
			plan := applyPlan(docs, app.projectFlag())
			if ifMatch != "" && len(plan) != 1 {
				return errs.New(errs.CodeUsage, "--if-match needs exactly one document, not %d", len(plan)).WithFlag("if-match")
			}
			ctx, cancel := app.RequestContext()
			defer cancel()
			var outcomes []applyOutcome
			var names []string
			failed := 0
			for _, p := range plan {
				o := applyOutcome{Ref: p.ref, Source: p.source}
				err := p.err
				if err == nil {
					o.Object, o.Result, err = app.Backend.ApplyManifest(ctx, p.collection, p.project, p.name, p.doc, api.ApplyOptions{IfMatch: ifMatch})
				}
				if err != nil {
					o.Error, o.Object = errs.As(err), nil
					failed++
				} else {
					names = append(names, p.ref)
				}
				outcomes = append(outcomes, o)
			}
			stderr := cmd.ErrOrStderr()
			err = app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    outcomes,
				Names:   names,
				Human: func(w io.Writer, s ui.Style, width int) {
					for _, o := range outcomes {
						if o.Error != nil {
							fmt.Fprintf(stderr, "%s failed: %s\n", o.Ref, o.Error.Message)
							continue
						}
						fmt.Fprintln(w, o.Ref+" "+o.Result)
					}
				},
			})
			if err != nil {
				return err
			}
			if failed > 0 {
				return exitStatus(1)
			}
			return nil
		},
	}
	src.bind(cmd)
	cmd.Flags().StringVar(&ifMatch, "if-match", "", "apply only if the object is still at this resourceVersion")
	return group(cmd, "demand")
}

func newDiffCmd(app *App) *cobra.Command {
	var src manifestSources
	cmd := &cobra.Command{
		Use:   "diff -f <file|dir|->",
		Short: "Show what apply would change",
		Long: `Read each object as the plane holds it, ask the plane what applying the
manifest would make it (a dry-run PUT that keeps nothing), and print a unified
YAML diff of the two: spec and metadata, without the plane-managed keys and
status. An object that does not exist yet diffs against nothing.

Exits 0 when nothing would change, 1 when something would, and above 1 on an
error, like diff(1).`,
		Example: `  wings diff -f chat.yaml
  wings diff -f deploy/ -R && echo up to date`,
		Args: cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			docs, err := src.read(cmd.InOrStdin())
			if err != nil {
				return err
			}
			plan := applyPlan(docs, app.projectFlag())
			ctx, cancel := app.RequestContext()
			defer cancel()
			var out strings.Builder
			differs := false
			for _, p := range plan {
				if p.err != nil {
					return p.err
				}
				live, err := app.Backend.Manifest(ctx, p.collection, p.project, p.name)
				if e := errs.As(err); err != nil && e.Code == errs.CodeNotFound {
					live, err = nil, nil
				}
				if err != nil {
					return err
				}
				merged, _, err := app.Backend.ApplyManifest(ctx, p.collection, p.project, p.name, p.doc, api.ApplyOptions{DryRun: true})
				if err != nil {
					return err
				}
				a, err := diffYAML(live)
				if err != nil {
					return err
				}
				b, err := diffYAML(merged)
				if err != nil {
					return err
				}
				if d := unifiedDiff("live/"+p.ref, "merged/"+p.ref, a, b); d != "" {
					differs = true
					out.WriteString(d)
				}
			}
			fmt.Fprint(cmd.OutOrStdout(), out.String())
			if differs {
				return exitStatus(1)
			}
			return nil
		},
	}
	src.bind(cmd)
	return group(cmd, "demand")
}

// managedMetadata are the keys the plane writes, which a diff leaves out.
var managedMetadata = []string{"uid", "generation", "creationTimestamp", "resourceVersion"}

const lastAppliedAnnotation = "plane.wingsmith.dev/last-applied"

// diffYAML is the part of a manifest diff compares, as YAML: no status and no
// plane-managed metadata. A nil manifest is the empty string.
func diffYAML(m api.Manifest) (string, error) {
	if m == nil {
		return "", nil
	}
	view := api.Manifest{}
	for k, v := range m {
		if k != "status" {
			view[k] = v
		}
	}
	if meta, ok := m["metadata"].(map[string]any); ok {
		trimmed := map[string]any{}
		for k, v := range meta {
			trimmed[k] = v
		}
		for _, k := range managedMetadata {
			delete(trimmed, k)
		}
		if notes, ok := trimmed["annotations"].(map[string]any); ok {
			kept := map[string]any{}
			for k, v := range notes {
				if k != lastAppliedAnnotation {
					kept[k] = v
				}
			}
			trimmed["annotations"] = kept
			if len(kept) == 0 {
				delete(trimmed, "annotations")
			}
		}
		view["metadata"] = trimmed
	}
	var b bytes.Buffer
	if err := writeManifestYAML(&b, view); err != nil {
		return "", errs.Wrap(err, errs.CodeInternal, "cannot render manifest")
	}
	return b.String(), nil
}

type applyItem struct {
	collection, project, name, ref, source string
	doc                                    api.Manifest
	// err is why the document cannot be sent; apply reports it and moves on.
	err error
}

// applyPlan orders the documents Workloads first, then Endpoints, each in file
// order. A document that cannot be sent keeps its place with err set.
func applyPlan(docs []sourced, project string) []applyItem {
	var workloads, endpoints, bad []applyItem
	for i, d := range docs {
		doc := d.doc
		kind, _ := doc["kind"].(string)
		meta, _ := doc["metadata"].(map[string]any)
		name, _ := meta["name"].(string)
		namespace, _ := meta["namespace"].(string)
		if namespace == "" {
			namespace = project
		}
		item := applyItem{project: namespace, name: name, doc: doc, source: d.source}
		item.ref = fmt.Sprintf("%s/%s", strings.ToLower(kind), name)
		switch {
		case name == "":
			item.ref = fmt.Sprintf("%s document %d", d.source, i+1)
			item.err = errs.New(errs.CodeUsage, "document %d has no metadata.name", i+1)
		case kind == "Workload":
			item.collection = "workloads"
		case kind == "Endpoint":
			item.collection = "endpoints"
		case kind == "Artifact":
			item.err = errs.New(errs.CodeUsage, "artifact/%s: Artifact manifests cannot be applied yet", name).
				WithFix("Register the artifact instead", "wings artifact create")
		default:
			item.err = errs.New(errs.CodeUsage, "document %d has unknown kind %q", i+1, kind).
				WithCause("kinds: Workload, Endpoint")
		}
		if item.err == nil && namespace == "" {
			item.err = errs.New(errs.CodeUsage, "%s has no metadata.namespace", item.ref).WithFlag("project")
		}
		switch {
		case item.err != nil:
			bad = append(bad, item)
		case item.collection == "workloads":
			workloads = append(workloads, item)
		default:
			endpoints = append(endpoints, item)
		}
	}
	return append(append(workloads, endpoints...), bad...)
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
