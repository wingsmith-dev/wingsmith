package api

import "context"

// Backend is everything the CLI can ask of the control plane. The fake and
// the HTTP client both implement it; no command talks to a transport.
//
// It grows one method per implemented noun. Nouns that are registered but not
// implemented never reach a backend at all.
type Backend interface {
	Whoami(ctx context.Context) (Identity, error)
	Clusters(ctx context.Context, opts ListOpts) ([]Cluster, error)
	Cluster(ctx context.Context, ref string) (Cluster, error)
	Nodes(ctx context.Context, opts ListOpts) ([]Node, error)
	Node(ctx context.Context, ref string) (Node, error)
	Devices(ctx context.Context, opts ListOpts) ([]Device, error)
	Device(ctx context.Context, ref string) (Device, error)
	Pools(ctx context.Context, opts ListOpts) ([]Pool, error)
	Pool(ctx context.Context, ref string) (Pool, error)

	// Simulated reports whether the results are synthetic.
	Simulated() bool
}
