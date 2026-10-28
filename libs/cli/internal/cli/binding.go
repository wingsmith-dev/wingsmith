package cli

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/spf13/cobra"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/api"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/errs"
	"github.com/wingsmith-dev/wingsmith/libs/cli/internal/ui"
)

func tenantNoun() noun[api.Tenant] {
	return noun[api.Tenant]{
		name:    "tenant",
		aliases: []string{"tenants"},
		group:   "binding",
		short:   "The top of the ownership tree",
		long: `A tenant is the top of the ownership tree. Projects belong to a tenant and
quota attaches to projects, so nothing can be deployed or promoted until a
tenant exists.

A tenant also says how its inference traffic reaches the gateway (ingress,
auth and host).`,
		example: `  wings tenant ls
  wings tenant get acme`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Tenant], error) {
			return b.Tenants(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Tenant, error) { return b.Tenant(ctx, ref) },
		keys:  func(t api.Tenant) []string { return []string{t.Slug, t.ID} },
		empty: "no tenants",
		columns: []ui.Column{
			{Title: "slug"},
			{Title: "id", Wide: true},
			{Title: "name"},
			{Title: "projects", Align: ui.AlignRight},
			{Title: "enabled"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, t api.Tenant) []string {
			return []string{
				t.Slug, t.ID, t.Name, strconv.Itoa(t.ProjectCount),
				yesNo(s, t.Enabled), agoAt(t.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled tenants (--enabled=false for the rest)")
			f.bindSearch(cmd, "substring match on slug or name")
		},
		detail: func(app *App, w io.Writer, s ui.Style, t api.Tenant) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(t.Slug), s.Dim(t.ID))
			kv := app.KV()
			kv.Add("name", t.Name)
			kv.Add("enabled", yesNo(s, t.Enabled))
			kv.Add("projects", strconv.Itoa(t.ProjectCount))
			kv.Add("created", agoAt(t.CreatedAt))
			kv.Add("revision", t.Revision)
			kv.Render(w)
		},
		next: func(t api.Tenant) []errs.Action {
			return []errs.Action{
				{Description: "Its projects", Command: "wings project ls --tenant " + t.Slug},
			}
		},
	}
}

func projectNoun() noun[api.Project] {
	return noun[api.Project]{
		name:    "project",
		aliases: []string{"projects"},
		group:   "binding",
		short:   "The unit quota and priority attach to",
		long: `A project is the unit of tenancy that quota and priority attach to.

Each project shows its tenant. enabled is the project's own switch;
effective_enabled is false when either the project or its tenant is disabled.`,
		example: `  wings project ls
  wings project ls --tenant acme
  wings project get core`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Project], error) {
			return b.Projects(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Project, error) { return b.Project(ctx, ref) },
		keys:  func(p api.Project) []string { return []string{p.Slug, p.ID} },
		empty: "no projects",
		columns: []ui.Column{
			{Title: "slug"},
			{Title: "id", Wide: true},
			{Title: "name"},
			{Title: "tenant"},
			{Title: "default priority"},
			{Title: "enabled"},
			{Title: "effective"},
			{Title: "age", Align: ui.AlignRight},
		},
		row: func(s ui.Style, p api.Project) []string {
			return []string{
				p.Slug, p.ID, p.Name, p.Tenant.Slug, orDash(str(p.DefaultPriorityClass)),
				boolWord(p.Enabled), yesNo(s, p.EffectiveEnabled), agoAt(p.CreatedAt),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().StringVar(&f.tenant, "tenant", "", "limit to one tenant (slug or id)")
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled projects (--enabled=false for the rest)")
			f.bindSearch(cmd, "substring match on slug or name")
		},
		detail: func(app *App, w io.Writer, s ui.Style, p api.Project) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(p.Slug), s.Dim(p.ID))
			kv := app.KV()
			kv.Add("name", p.Name)
			kv.Addf("tenant", "%s (%s)", p.Tenant.Slug, p.Tenant.Name)
			kv.Add("tenant enabled", yesNo(s, p.Tenant.Enabled))
			kv.Add("default priority class", orDash(str(p.DefaultPriorityClass)))
			kv.Add("enabled", yesNo(s, p.Enabled))
			kv.Add("effective enabled", yesNo(s, p.EffectiveEnabled))
			kv.Add("created", agoAt(p.CreatedAt))
			kv.Add("revision", p.Revision)
			kv.Render(w)
		},
		next: func(p api.Project) []errs.Action {
			return []errs.Action{
				{Description: "Its quotas", Command: "wings quota ls --project " + p.Slug},
				{Description: "What it is holding", Command: "wings allocation ls --project " + p.Slug + " --live"},
			}
		},
	}
}

func quotaNoun() noun[api.Quota] {
	return noun[api.Quota]{
		name:    "quota",
		aliases: []string{"quotas"},
		group:   "binding",
		short:   "Device ceiling per project and device class",
		long: `A quota caps how many devices of one class a project may hold, with current
usage beside it.

Placement checks the ceiling when it admits a workload: limit, or the lower of
limit and guaranteed when over-quota is not enabled. guaranteed is not reserved
for the project against other projects, and the over-quota weight is recorded
but nothing reads it: there is no fair-share division of spare capacity yet.

A quota with no cluster applies across every cluster, and its usage is summed
across all of them. over_limit and headroom are computed by the plane.`,
		example: `  wings quota ls
  wings quota ls --project core --class h100-80
  wings quota ls -o json | jq '.items[] | select(.over_limit)'`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Quota], error) {
			return b.Quotas(ctx, o)
		},
		get:    func(ctx context.Context, b api.Backend, ref string) (api.Quota, error) { return b.Quota(ctx, ref) },
		keys:   func(q api.Quota) []string { return []string{q.ID} },
		empty:  "no quotas declared",
		getArg: "<quota-id>",
		columns: []ui.Column{
			{Title: "id"},
			{Title: "project"},
			{Title: "cluster"},
			{Title: "class"},
			{Title: "guaranteed", Align: ui.AlignRight},
			{Title: "limit", Align: ui.AlignRight},
			{Title: "used", Align: ui.AlignRight},
			{Title: "over quota", Align: ui.AlignRight},
			{Title: "headroom", Align: ui.AlignRight},
			{Title: "over limit"},
			{Title: "weight", Align: ui.AlignRight, Wide: true},
		},
		row: func(s ui.Style, q api.Quota) []string {
			return []string{
				q.ID, q.ProjectSlug, orDash(str(q.ClusterName)), q.DeviceClass,
				strconv.Itoa(q.GuaranteedDevices), num(q.LimitDevices),
				strconv.Itoa(q.Usage.AllocatedDevices), strconv.Itoa(q.Usage.OverQuotaDevices),
				num(q.HeadroomDevices), warnIf(s, q.OverLimit),
				strconv.FormatFloat(q.OverQuotaWeight, 'g', -1, 64),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			f.byProject = true
			cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster")
			cmd.Flags().StringVar(&f.class, "class", "", "filter by device class")
			cmd.Flags().BoolVar(&f.overQuotaEnabled, "over-quota-enabled", false, "only quotas that may burst")
		},
		detail: func(app *App, w io.Writer, s ui.Style, q api.Quota) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(q.ProjectSlug+"/"+q.DeviceClass), s.Dim(q.ID))

			fmt.Fprintf(w, "\n%s\n", s.Dim("declared"))
			kv := app.KV()
			kv.Add("project", q.ProjectSlug)
			kv.Add("cluster", orDefault(str(q.ClusterName), "all clusters"))
			kv.Add("device class", q.DeviceClass)
			kv.Add("guaranteed", countWord(q.GuaranteedDevices, "device"))
			kv.Add("limit", num(q.LimitDevices))
			kv.Addf("over-quota weight", "%g", q.OverQuotaWeight)
			kv.Add("over-quota enabled", yesNo(s, q.OverQuotaEnabled))
			kv.Add("revision", q.Revision)
			kv.Render(w)

			fmt.Fprintf(w, "\n%s\n", s.Dim("usage"))
			ukv := app.KV()
			ukv.Addf("allocated devices", "%d", q.Usage.AllocatedDevices)
			ukv.Addf("live allocations", "%d", q.Usage.LiveAllocations)
			ukv.Addf("over-quota devices", "%d", q.Usage.OverQuotaDevices)
			ukv.Add("headroom", num(q.HeadroomDevices))
			ukv.Add("over limit", warnIf(s, q.OverLimit))
			ukv.Render(w)
		},
		next: func(q api.Quota) []errs.Action {
			return []errs.Action{{Description: "What is holding it", Command: "wings allocation ls --project " + q.ProjectSlug + " --class " + q.DeviceClass + " --live"}}
		},
	}
}

func priorityClassNoun() noun[api.PriorityClass] {
	return noun[api.PriorityClass]{
		name:    "priority-class",
		aliases: []string{"priority-classes", "priority"},
		group:   "binding",
		short:   "Admission order between workloads",
		long: `A priority class is a value in an ordering, plus two switches: preemptible
(workloads in it may be evicted) and preempts (they may evict others).

Preemption is not implemented yet. The classes order admission today, and the
switches are recorded for when it is.`,
		example: `  wings priority-class ls
  wings priority-class ls --preempts
  wings priority-class get production`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.PriorityClass], error) {
			return b.PriorityClasses(ctx, o)
		},
		get: func(ctx context.Context, b api.Backend, ref string) (api.PriorityClass, error) {
			return b.PriorityClass(ctx, ref)
		},
		keys:  func(p api.PriorityClass) []string { return []string{p.Name, p.ID} },
		empty: "no priority classes declared",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "value", Align: ui.AlignRight},
			{Title: "preemptible"},
			{Title: "preempts"},
			{Title: "default"},
			{Title: "description"},
		},
		row: func(s ui.Style, p api.PriorityClass) []string {
			return []string{
				p.Name, p.ID, strconv.Itoa(p.Value), boolWord(p.Preemptible), boolWord(p.Preempts),
				boolWord(p.IsDefault), orDash(str(p.Description)),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().BoolVar(&f.preemptible, "preemptible", false, "only preemptible classes")
			cmd.Flags().BoolVar(&f.preempts, "preempts", false, "only classes that preempt others")
			f.bindSearch(cmd, "substring match on name")
		},
		detail: func(app *App, w io.Writer, s ui.Style, p api.PriorityClass) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(p.Name), s.Dim(p.ID))
			kv := app.KV()
			kv.Addf("value", "%d", p.Value)
			kv.Add("preemptible", yesNo(s, p.Preemptible))
			kv.Add("preempts", yesNo(s, p.Preempts))
			kv.Add("default", boolWord(p.IsDefault))
			kv.Add("description", orDash(str(p.Description)))
			kv.Add("revision", p.Revision)
			kv.Render(w)
		},
	}
}

func policyNoun() noun[api.Policy] {
	return noun[api.Policy]{
		name:    "policy",
		aliases: []string{"policies"},
		group:   "binding",
		short:   "Placement constraints and what they bind to",
		long: `A policy limits where work may be placed.

Allow-lists and deny-lists are separate. An empty allow-list means no
restriction, and a deny entry still applies when the allow-list is widened. A
policy with no attachments has no effect, so attachments are shown with it.`,
		example: `  wings policy ls
  wings policy ls --region eu-west-1
  wings policy get eu-only`,
		list: func(ctx context.Context, b api.Backend, o api.ListOpts) (api.Page[api.Policy], error) {
			return b.Policies(ctx, o)
		},
		get:   func(ctx context.Context, b api.Backend, ref string) (api.Policy, error) { return b.Policy(ctx, ref) },
		keys:  func(p api.Policy) []string { return []string{p.Name, p.ID} },
		empty: "no policies declared",
		columns: []ui.Column{
			{Title: "name"},
			{Title: "id", Wide: true},
			{Title: "regions"},
			{Title: "residencies"},
			{Title: "classes"},
			{Title: "denied clusters"},
			{Title: "attachments", Align: ui.AlignRight},
			{Title: "enabled"},
		},
		row: func(s ui.Style, p api.Policy) []string {
			return []string{
				p.Name, p.ID, listCell(p.AllowedRegions), listCell(p.AllowedResidencies),
				listCell(p.AllowedDeviceClasses), listCell(p.DeniedClusters),
				strconv.Itoa(len(p.Attachments)), boolWord(p.Enabled),
			}
		},
		bind: func(f *filters, cmd *cobra.Command) {
			cmd.Flags().BoolVar(&f.enabled, "enabled", false, "only enabled policies")
			cmd.Flags().StringVar(&f.region, "region", "", "policies that allow one region")
			cmd.Flags().StringVar(&f.class, "class", "", "policies that allow one device class")
			cmd.Flags().StringVar(&f.kind, "subject-type", "", "filter by attachment subject type")
			cmd.Flags().StringVar(&f.subject, "subject", "", "filter by attached subject")
			f.bindSearch(cmd, "substring match on name")
		},
		detail: func(app *App, w io.Writer, s ui.Style, p api.Policy) {
			fmt.Fprintf(w, "%s %s\n", s.Bold(p.Name), s.Dim(p.ID))
			kv := app.KV()
			kv.Add("description", orDash(str(p.Description)))
			kv.Add("allowed regions", listCell(p.AllowedRegions))
			kv.Add("allowed residencies", listCell(p.AllowedResidencies))
			kv.Add("allowed device classes", listCell(p.AllowedDeviceClasses))
			kv.Add("allowed clusters", listCell(p.AllowedClusters))
			kv.Add("denied clusters", listCell(p.DeniedClusters))
			kv.Add("rules", labelString(p.Rules))
			kv.Add("enabled", yesNo(s, p.Enabled))
			kv.Add("revision", p.Revision)
			kv.Render(w)

			if len(p.Attachments) == 0 {
				fmt.Fprintf(w, "\n%s\n", s.Amber("attached to nothing: this policy constrains nothing"))
				return
			}
			fmt.Fprintf(w, "\n%s\n", s.Dim("attachments"))
			t := app.Table(
				ui.Column{Title: "subject type"},
				ui.Column{Title: "subject"},
				ui.Column{Title: "name"},
				ui.Column{Title: "row id", Wide: true},
			)
			for _, a := range p.Attachments {
				t.Row(a.SubjectType, orDash(str(a.Subject)), orDash(str(a.SubjectName)), a.SubjectID)
			}
			t.Render(w)
		},
	}
}

func listCell(v []string) string {
	if len(v) == 0 {
		return "-"
	}
	return strings.Join(v, ",")
}
