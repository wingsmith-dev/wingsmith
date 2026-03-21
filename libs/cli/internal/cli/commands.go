package cli

import (
	"github.com/spf13/cobra"
)

// register attaches every command to the root. The noun set is the object
// model from docs/plans/01, grouped the way the model groups it, and every
// noun now reaches the real API: nothing here is a stub.
func register(app *App, root *cobra.Command) {
	root.AddCommand(
		clusterNoun().command(app),
		nodeNoun().command(app),
		deviceNoun().command(app),
		topologyNoun().command(app),
		poolNoun().command(app),

		artifactNoun().command(app),
		recipeNoun().command(app),
		deploymentNoun().command(app),
		newDiscoveredCmd(app),
		endpointNoun().command(app),

		projectNoun().command(app),
		quotaNoun().command(app),
		priorityClassNoun().command(app),
		policyNoun().command(app),

		newOperationCmd(app),
		allocationNoun().command(app),
		componentNoun().command(app),
		eventNoun().command(app),

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
