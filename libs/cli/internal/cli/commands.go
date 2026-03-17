package cli

import (
	"fmt"

	"github.com/hangar/cli/internal/api"
	"github.com/hangar/cli/internal/errs"
	"github.com/spf13/cobra"
)

// register attaches every command to the root. The noun set is the object
// model from docs/plans/01; nouns beyond `cluster` are registered as stubs so
// the surface is visible and reviewable before the API contract exists.
func register(app *App, root *cobra.Command) {
	root.AddCommand(
		newClusterCmd(app),
		newNodeCmd(app),
		newDeviceCmd(app),
		newPoolCmd(app),

		stubNoun(app, "artifact", "demand", "Immutable, content-addressed model weights", "declared"),
		stubNoun(app, "recipe", "demand", "A reproducible way to serve an artifact", "declared"),
		stubNoun(app, "deployment", "demand", "Recipe placed on a cluster with scale bounds", "declared"),
		stubNoun(app, "endpoint", "demand", "The stable public name clients call", "declared"),

		stubNoun(app, "project", "binding", "Tenancy — who owns what", "declared"),
		stubNoun(app, "quota", "binding", "Declared limits per project and device class", "declared"),

		stubNoun(app, "operation", "machinery", "Long-running imperative work: drain, warm, upgrade", "operation"),

		newContextCmd(app),
		newConfigCmd(app),
		newWhoamiCmd(app),
		newExplainCmd(app),
		newVersionCmd(app),
	)
}

// group tags a command for the grouped help listing.
func group(cmd *cobra.Command, id string) *cobra.Command {
	cmd.GroupID = id
	return cmd
}

// stubNoun registers a noun whose verbs exist in the help surface but whose
// API contract is still being written. Running one is an honest UNIMPLEMENTED
// error rather than a guess at an endpoint.
func stubNoun(app *App, noun, groupID, short, kind string) *cobra.Command {
	cmd := &cobra.Command{
		Use:     noun,
		Aliases: []string{noun + "s"},
		Short:   short,
		Long: fmt.Sprintf(`%s

%s is %s state in the Hangar object model. Its verbs are not implemented yet:
the plane's API contract for this noun is still being written, and guessing at
endpoints would bake in a shape nobody agreed to.`, short, noun, kind),
	}
	group(cmd, groupID)
	for _, verb := range []string{"ls", "get"} {
		v := verb
		sub := &cobra.Command{
			Use:   v,
			Short: fmt.Sprintf("%s %s (not implemented)", v, noun),
			RunE: func(cmd *cobra.Command, args []string) error {
				return errs.New(errs.CodeUnimplemented, "`hgr %s %s` is not implemented yet", noun, v).
					WithCause("the control plane API contract for this noun has not landed").
					WithFix("See what is implemented", "hgr cluster ls")
			},
		}
		cmd.AddCommand(sub)
	}
	return cmd
}

// --- shared list flags -----------------------------------------------------

type listFlags struct {
	all    bool
	status string
	region string
	labels []string
	search string
	limit  int
	cursor string
}

func (f *listFlags) bind(cmd *cobra.Command) {
	f.bindShared(cmd, "substring match on name, id, or region")
	fl := cmd.Flags()
	fl.StringVar(&f.status, "status", "", "filter by phase")
	fl.StringVar(&f.region, "region", "", "filter by region")
}

// bindShared binds the flags every list verb has, whatever the noun.
func (f *listFlags) bindShared(cmd *cobra.Command, searchHelp string) {
	fl := cmd.Flags()
	fl.BoolVarP(&f.all, "all", "a", false, "include disabled objects")
	fl.StringArrayVarP(&f.labels, "label", "l", nil, "filter by label (KEY=VALUE, repeatable)")
	fl.StringVar(&f.search, "search", "", searchHelp)
	fl.IntVar(&f.limit, "limit", 0, "maximum results")
	fl.StringVar(&f.cursor, "cursor", "", "pagination cursor")
}

func (f *listFlags) opts() (api.ListOpts, error) {
	labels, err := api.ParseKV(f.labels, "label")
	if err != nil {
		return api.ListOpts{}, err
	}
	return api.ListOpts{
		All: f.all, Status: f.status, Region: f.region,
		Labels: labels, Search: f.search, Limit: f.limit, Cursor: f.cursor,
	}, nil
}

// supplyFlags adds the scoping every supply noun shares. Which of them are
// bound is per-noun: a device has a node, a pool does not.
type supplyFlags struct {
	listFlags
	cluster  string
	pool     string
	node     string
	class    string
	health   string
	domain   string
	status   string
	cordoned bool
	groupBy  string
}

func (f *supplyFlags) opts() (api.ListOpts, error) {
	opts, err := f.listFlags.opts()
	if err != nil {
		return api.ListOpts{}, err
	}
	opts.Cluster = f.cluster
	opts.Pool = f.pool
	opts.Node = f.node
	opts.Class = f.class
	opts.Health = f.health
	opts.Domain = f.domain
	opts.Status = f.status
	opts.Cordoned = f.cordoned
	return opts, nil
}

func (f *supplyFlags) bindCluster(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.cluster, "cluster", "", "limit to one cluster (name, id, or unique prefix)")
}

func (f *supplyFlags) bindClass(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.class, "class", "", "filter by device class, e.g. h100-80")
}

func (f *supplyFlags) bindHealth(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.health, "health", "", "filter by health: healthy|degraded|unhealthy|unknown")
}

func (f *supplyFlags) bindDomain(cmd *cobra.Command) {
	cmd.Flags().StringVar(&f.domain, "domain", "", "filter by topology domain name, id, or kind (nvlink|nvswitch|pcie|numa)")
}

func (f *supplyFlags) bindStatus(cmd *cobra.Command, help string) {
	cmd.Flags().StringVar(&f.status, "status", "", help)
}
