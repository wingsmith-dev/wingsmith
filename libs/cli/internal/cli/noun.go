package cli

import (
	"context"
	"fmt"
	"io"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/hangar-dev/hangar/libs/cli/internal/api"
	"github.com/hangar-dev/hangar/libs/cli/internal/errs"
	"github.com/hangar-dev/hangar/libs/cli/internal/output"
	"github.com/hangar-dev/hangar/libs/cli/internal/ui"
	"github.com/spf13/cobra"
)

// noun describes one collection at the command surface: how to list it, how
// to resolve one of them, and how each is drawn. Everything else (the
// envelope, pagination, output formats, next actions) is the same for every
// noun and lives here once.
type noun[T any] struct {
	name    string
	aliases []string
	// plurally overrides the pluralised name in listings and messages, for a
	// noun whose plural the rule cannot reach: "discovered" pluralises to
	// "discovereds", which is not a thing anyone would say.
	plurally string
	group    string
	short    string
	long     string
	example  string

	list func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[T], error)
	get  func(ctx context.Context, b api.Backend, ref string) (T, error)

	columns []ui.Column
	row     func(s ui.Style, item T) []string
	// keys are the strings a reference may name, most human first. They are
	// what client-side prefix resolution matches against.
	keys   func(item T) []string
	detail func(app *App, w io.Writer, s ui.Style, item T)
	next   func(item T) []errs.Action
	empty  string
	// bind registers the filters this noun's endpoint actually accepts.
	bind func(f *filters, cmd *cobra.Command)
	// groupKey buckets a listing for `--group-by`. Grouping is client-side:
	// the API has no aggregate endpoint, and inventing one would put a
	// second, slower answer beside the listing it was derived from.
	groupKey func(by string, item T) (key, kind string, err error)
	tally    func(g *listGroup, item T)
	// getArg names the argument in help, e.g. "<node>".
	getArg string
	// extra are subcommands beyond ls and get. A noun whose object has a
	// second question worth asking about one instance (not a different
	// listing of many) hangs it here instead of becoming a bespoke command
	// tree and losing the shared flags, output modes and reference resolution.
	extra func(app *App) []*cobra.Command
}

func (n noun[T]) command(app *App) *cobra.Command {
	cmd := &cobra.Command{
		Use:     n.name,
		Aliases: n.aliases,
		Short:   n.short,
		Long:    n.long,
	}
	group(cmd, n.group)
	cmd.AddCommand(n.listCmd(app), n.getCmd(app))
	if n.extra != nil {
		cmd.AddCommand(n.extra(app)...)
	}
	return cmd
}

func (n noun[T]) listCmd(app *App) *cobra.Command {
	f := &filters{}
	cmd := &cobra.Command{
		Use:     "ls",
		Aliases: []string{"list"},
		Short:   "List " + n.pluralName(),
		Example: n.example,
		Args:    cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			return n.runList(app, cmd, f)
		},
	}
	f.bindCommon(cmd)
	if n.bind != nil {
		n.bind(f, cmd)
	}
	return cmd
}

func (n noun[T]) runList(app *App, cmd *cobra.Command, f *filters) error {
	opts, err := f.opts(cmd)
	if err != nil {
		return err
	}
	if f.byProject && app.projectSet {
		opts.Project = app.Project
	}
	ctx, cancel := app.RequestContext()
	defer cancel()

	step := app.Report.Step("listing %s", n.pluralName())
	page, err := n.list(ctx, app.Backend, opts)
	if err != nil {
		step.Fail("failed")
		return err
	}
	step.Done("%s", n.count(page.Count))

	names := make([]string, 0, len(page.Items))
	rows := make([][]string, 0, len(page.Items))
	for _, item := range page.Items {
		names = append(names, n.label(item))
		rows = append(rows, n.row(app.Style, item))
	}

	var data any = page
	human := n.table(app, page)
	if f.groupBy != "" {
		groups, membership, err := n.buckets(f.groupBy, page.Items)
		if err != nil {
			return err
		}
		data = groupedPage[T]{
			Items: page.Items, Count: page.Count, NextCursor: page.NextCursor,
			GroupBy: f.groupBy, Groups: groups,
		}
		human = n.groupedTable(app, page, groups, membership)
	}

	return app.Emit(&output.Result{
		Command: cmdPath(cmd),
		Data:    data,
		Names:   names,
		Columns: columnTitles(n.columns),
		Rows:    rows,
		Human:   human,
		Next:    n.listNext(page),
	})
}

// listGroup is one bucket of a grouped listing. It carries its own counts so
// nothing downstream has to re-aggregate to answer a capacity question.
type listGroup struct {
	Key         string `json:"key"`
	Kind        string `json:"kind,omitempty"`
	Count       int    `json:"count"`
	Healthy     int    `json:"healthy"`
	Allocatable int    `json:"allocatable"`
}

// groupedPage is the machine contract of a grouped listing: the same envelope
// with the buckets beside it, never instead of it.
type groupedPage[T any] struct {
	Items      []T         `json:"items"`
	Count      int         `json:"count"`
	NextCursor string      `json:"next_cursor,omitempty"`
	GroupBy    string      `json:"group_by"`
	Groups     []listGroup `json:"groups"`
}

func (n noun[T]) buckets(by string, items []T) ([]listGroup, map[string][]T, error) {
	if n.groupKey == nil {
		return nil, nil, errs.New(errs.CodeUsage, "%s listings cannot be grouped", n.pluralName())
	}
	index := map[string]*listGroup{}
	membership := map[string][]T{}
	var order []string
	for _, item := range items {
		key, kind, err := n.groupKey(by, item)
		if err != nil {
			return nil, nil, err
		}
		g, ok := index[key]
		if !ok {
			g = &listGroup{Key: key, Kind: kind}
			index[key] = g
			order = append(order, key)
		}
		g.Count++
		if n.tally != nil {
			n.tally(g, item)
		}
		membership[key] = append(membership[key], item)
	}
	sort.Strings(order)
	groups := make([]listGroup, 0, len(order))
	for _, k := range order {
		groups = append(groups, *index[k])
	}
	return groups, membership, nil
}

func (n noun[T]) groupedTable(app *App, page api.Page[T], groups []listGroup, membership map[string][]T) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(page.Items) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim(orDefault(n.empty, "no "+n.pluralName()+" match")))
			return
		}
		for i, g := range groups {
			if i > 0 {
				fmt.Fprintln(w)
			}
			head := g.Key
			if g.Kind != "" {
				head += " " + s.Dim("("+g.Kind+")")
			}
			fmt.Fprintf(w, "%s  %s\n", s.Bold(head),
				s.Dim(fmt.Sprintf("%d · %d healthy · %d allocatable", g.Count, g.Healthy, g.Allocatable)))
			t := app.Table(n.columns...)
			for _, item := range membership[g.Key] {
				t.Row(n.row(s, item)...)
			}
			t.Render(w)
		}
		if page.NextCursor != "" {
			fmt.Fprintf(w, "\n%s\n", s.Dim(fmt.Sprintf("%d shown · more pages remain", page.Count)))
		}
	}
}

func (n noun[T]) table(app *App, page api.Page[T]) func(io.Writer, ui.Style, int) {
	return func(w io.Writer, s ui.Style, width int) {
		if len(page.Items) == 0 {
			fmt.Fprintf(w, "%s\n", s.Dim(orDefault(n.empty, "no "+n.pluralName()+" match")))
			return
		}
		t := app.Table(n.columns...)
		for _, item := range page.Items {
			t.Row(n.row(s, item)...)
		}
		t.Render(w)
		if page.NextCursor != "" {
			fmt.Fprintf(w, "\n%s\n", s.Dim(fmt.Sprintf("%d shown · more pages remain", page.Count)))
		}
	}
}

// listNext points at the next page when there is one. A listing that has more
// behind it and says nothing is a listing that lies by omission.
func (n noun[T]) listNext(page api.Page[T]) []errs.Action {
	var out []errs.Action
	if page.NextCursor != "" {
		out = append(out,
			errs.Action{Description: "Next page", Command: fmt.Sprintf("hgr %s ls --cursor %s", n.name, page.NextCursor)},
			errs.Action{Description: "Every page", Command: fmt.Sprintf("hgr %s ls --all", n.name)},
		)
	}
	if len(page.Items) > 0 {
		out = append(out, errs.Action{Description: "Inspect one", Command: fmt.Sprintf("hgr %s get %s", n.name, n.label(page.Items[0]))})
	}
	return out
}

func (n noun[T]) getCmd(app *App) *cobra.Command {
	arg := orDefault(n.getArg, "<"+n.name+">")
	return &cobra.Command{
		Use:     "get " + arg,
		Aliases: []string{"show", "describe"},
		Short:   "Show one " + n.name + " in detail",
		Args:    cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			ctx, cancel := app.RequestContext()
			defer cancel()
			item, err := n.resolve(ctx, app, args[0])
			if err != nil {
				return err
			}
			return app.Emit(&output.Result{
				Command: cmdPath(cmd),
				Data:    item,
				Names:   []string{n.label(item)},
				Human: func(w io.Writer, s ui.Style, width int) {
					n.detail(app, w, s, item)
				},
				Next: n.nextFor(item),
			})
		},
	}
}

// resolve turns a reference into one object, falling back to prefix matching
// over the collection. The API does not match prefixes by design: a server
// that guesses at ambiguous input eventually guesses wrong. So the guessing,
// and the AMBIGUOUS error when there is nothing to guess, happen here.
func (n noun[T]) resolve(ctx context.Context, app *App, ref string) (T, error) {
	return n.lookup(ctx, app, ref, strings.HasPrefix)
}

// resolveExact is resolve for a verb that changes or destroys something. It
// matches a whole id or name and never a prefix: `workload rm ch` deleting
// `chat` because nothing else started with "ch" is a guess nobody asked for.
func (n noun[T]) resolveExact(ctx context.Context, app *App, ref string) (T, error) {
	item, err := n.lookup(ctx, app, ref, func(key, ref string) bool { return key == ref })
	if errs.As(err) != nil && errs.As(err).Code == errs.CodeNotFound {
		e := errs.As(err).WithCause("a verb that changes something needs the full id or name, never a prefix")
		list := "hgr " + n.name + " ls"
		if !slices.ContainsFunc(e.Remediation, func(a errs.Action) bool { return a.Command == list }) {
			e = e.WithFix("Find the full name", list)
		}
		err = e
	}
	return item, err
}

func (n noun[T]) lookup(ctx context.Context, app *App, ref string, match func(key, ref string) bool) (T, error) {
	item, err := n.get(ctx, app.Backend, ref)
	if err == nil {
		return item, nil
	}
	if errs.As(err).Code != errs.CodeNotFound || n.keys == nil {
		return item, err
	}
	page, listErr := n.list(ctx, app.Backend, api.ListOpts{All: true})
	if listErr != nil {
		return item, err
	}
	var hits []T
	for _, candidate := range page.Items {
		for _, key := range n.keys(candidate) {
			if key != "" && match(key, ref) {
				hits = append(hits, candidate)
				break
			}
		}
	}
	switch len(hits) {
	case 1:
		return hits[0], nil
	case 0:
		return item, err
	default:
		labels := make([]string, 0, len(hits))
		for _, h := range hits {
			labels = append(labels, n.label(h))
		}
		var zero T
		return zero, errs.New(errs.CodeAmbiguous, "%q matches %d %s", ref, len(hits), n.pluralName()).
			WithCause(strings.Join(labels, ", "))
	}
}

func (n noun[T]) label(item T) string {
	if n.keys == nil {
		return ""
	}
	keys := n.keys(item)
	if len(keys) == 0 {
		return ""
	}
	return keys[0]
}

func (n noun[T]) nextFor(item T) []errs.Action {
	if n.next == nil {
		return nil
	}
	return n.next(item)
}

// --- filters ---------------------------------------------------------------

// filters holds every dimension any list verb can bind. A command registers
// only the flags its endpoint accepts, so an unsupported filter is a usage
// error instead of a parameter the server quietly ignores.
type filters struct {
	limit  int
	cursor string
	all    bool
	search string
	labels []string

	cluster    string
	pool       string
	node       string
	class      string
	arch       string
	health     string
	phase      string
	region     string
	tenant     string
	kind       string
	typ        string
	name       string
	engine     string
	artifact   string
	condition  string
	domain     string
	target     string
	deployment string
	workload   string
	harness    string
	actor      string
	subject    string
	severity   string
	device     string
	replica    string
	namespace  string
	status     string
	format     string
	endpoint   string
	rule       string
	request    string
	scope      string
	digest     string

	ready            bool
	cordoned         bool
	allocatable      bool
	enabled          bool
	behind           bool
	spansNodes       bool
	overQuota        bool
	live             bool
	preemptible      bool
	preempts         bool
	overQuotaEnabled bool
	present          bool
	enforced         bool
	fleetDefault     bool

	since string
	until string

	groupBy string

	// byProject marks a listing whose endpoint takes a project filter. The
	// filter is the global --project (or HANGAR_PROJECT, or context.project),
	// not a flag of its own, so the configured project reaches it.
	byProject bool
}

func (f *filters) bindCommon(cmd *cobra.Command) {
	fl := cmd.Flags()
	fl.IntVar(&f.limit, "limit", 0, "maximum results in one page")
	fl.StringVar(&f.cursor, "cursor", "", "continue from a previous page's next_cursor")
	fl.BoolVarP(&f.all, "all", "a", false, "follow pagination cursors and return every page")
}

func (f *filters) bindSearch(cmd *cobra.Command, help string) {
	cmd.Flags().StringVar(&f.search, "search", "", help)
}

func (f *filters) bindLabels(cmd *cobra.Command) {
	cmd.Flags().StringArrayVarP(&f.labels, "label", "l", nil, "filter by label (KEY=VALUE, repeatable)")
}

func (f *filters) opts(cmd *cobra.Command) (api.ListOpts, error) {
	labels, err := api.ParseKV(f.labels, "label")
	if err != nil {
		return api.ListOpts{}, err
	}
	since, err := parseTime(f.since, "since")
	if err != nil {
		return api.ListOpts{}, err
	}
	until, err := parseTime(f.until, "until")
	if err != nil {
		return api.ListOpts{}, err
	}
	return api.ListOpts{
		Limit: f.limit, Cursor: f.cursor, All: f.all, Search: f.search, Labels: labels,
		Cluster: f.cluster, Pool: f.pool, Node: f.node, Class: f.class, Health: f.health,
		Architecture: f.arch,
		Phase:        f.phase, Region: f.region, Tenant: f.tenant, Kind: f.kind,
		Type: f.typ, Name: f.name, Engine: f.engine, Artifact: f.artifact,
		Condition: f.condition, TopologyDomain: f.domain, Target: f.target, Actor: f.actor,
		Subject: f.subject, Severity: f.severity, Device: f.device, Replica: f.replica,
		Namespace: f.namespace, Status: f.status, Format: f.format,
		Deployment: f.deployment, Workload: f.workload, Harness: f.harness,
		Endpoint: f.endpoint, Rule: f.rule, Request: f.request, Scope: f.scope,
		ServingDigest: f.digest,

		Ready:            optBool(cmd, "ready", f.ready),
		Cordoned:         optBool(cmd, "cordoned", f.cordoned),
		Allocatable:      optBool(cmd, "allocatable", f.allocatable),
		Enabled:          optBool(cmd, "enabled", f.enabled),
		Behind:           optBool(cmd, "behind", f.behind),
		SpansNodes:       optBool(cmd, "spans-nodes", f.spansNodes),
		OverQuota:        optBool(cmd, "over-quota", f.overQuota),
		Live:             optBool(cmd, "live", f.live),
		Preemptible:      optBool(cmd, "preemptible", f.preemptible),
		Preempts:         optBool(cmd, "preempts", f.preempts),
		Present:          optBool(cmd, "present", f.present),
		OverQuotaEnabled: optBool(cmd, "over-quota-enabled", f.overQuotaEnabled),
		Enforced:         optBool(cmd, "enforced", f.enforced),
		FleetDefault:     optBool(cmd, "fleet-default", f.fleetDefault),

		Since: since,
		Until: until,
	}, nil
}

// optBool distinguishes "the caller asked for false" from "the caller did not
// ask", which a bare bool cannot: `--cordoned=false` means only uncordoned
// nodes, and omitting it means all of them.
func optBool(cmd *cobra.Command, name string, value bool) *bool {
	if cmd == nil || !cmd.Flags().Changed(name) {
		return nil
	}
	v := value
	return &v
}

// parseTime reads a point in the past: a duration ago, or an RFC 3339 time.
// It is what --since and --until mean.
func parseTime(raw, what string) (*time.Time, error) {
	return parseDurationInstant(raw, what, -1)
}

// parseDeadline reads a point in the future: a duration from now, or an RFC
// 3339 time. A deadline already behind us would expire the operation before it
// started, so one is refused.
func parseDeadline(raw, what string) (*time.Time, error) {
	t, err := parseDurationInstant(raw, what, +1)
	if err != nil || t == nil {
		return t, err
	}
	if !t.After(time.Now()) {
		return nil, errs.New(errs.CodeUsage, "--%s %q is in the past", what, raw).
			WithCause("a deadline is a duration from now, such as 30m, or a future RFC 3339 time")
	}
	return t, nil
}

// parseDurationInstant turns a duration into a time `direction` of now: -1 for ago,
// +1 for from now. A negative duration is refused instead of silently
// flipping which side of now it lands on.
func parseDurationInstant(raw, what string, direction time.Duration) (*time.Time, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	if d, err := time.ParseDuration(raw); err == nil {
		if d <= 0 {
			return nil, errs.New(errs.CodeUsage, "--%s %q must be a positive duration", what, raw)
		}
		t := time.Now().UTC().Add(direction * d)
		return &t, nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return nil, errs.New(errs.CodeUsage, "--%s %q is neither a duration nor an RFC 3339 timestamp", what, raw).
			WithCause("try --" + what + " 30m or --" + what + " 2026-08-19T12:00:00Z")
	}
	t = t.UTC()
	return &t, nil
}

// --- shared rendering ------------------------------------------------------

// deleted is what every rm emits in a machine format: the object that is gone,
// by id and by name. An rm that printed nothing under -o json would leave a
// script unable to tell success from a command that never ran.
func deleted(id, name string) map[string]any {
	return map[string]any{"id": id, "name": name, "deleted": true}
}

func columnTitles(cols []ui.Column) []string {
	out := make([]string, 0, len(cols))
	for _, c := range cols {
		out = append(out, c.Title)
	}
	return out
}

// pluralName is what a listing of this noun is called.
// count is "1 cluster" or "3 clusters".
func (n noun[T]) count(c int) string {
	if c == 1 {
		return "1 " + n.name
	}
	return fmt.Sprintf("%d %s", c, n.pluralName())
}

func (n noun[T]) pluralName() string {
	if n.plurally != "" {
		return n.plurally
	}
	return plural(n.name)
}

func plural(noun string) string {
	if strings.HasSuffix(noun, "s") || strings.HasSuffix(noun, "y") {
		if strings.HasSuffix(noun, "y") {
			return strings.TrimSuffix(noun, "y") + "ies"
		}
		return noun + "es"
	}
	return noun + "s"
}

func orDefault(v, fallback string) string {
	if strings.TrimSpace(v) == "" {
		return fallback
	}
	return v
}
