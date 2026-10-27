// The plane-to-cluster inference wire, and both ends of it.
//
// Its own module and not a package inside either binary, because the gateway and the agent are the
// two ends of this protocol and neither owns it. `internal/` would have forced one of them to be the
// host and the other to import the host's module; a contract that both sides depend on equally
// belongs beside the other contracts in libs/packages.
//
// It depends on http2 and nothing else. Not the routing table, not the metrics registry, not
// Kubernetes: a change to any of those must not be able to reach the wire format.
module github.com/hangar-dev/hangar/libs/packages/tunnel

go 1.25.0

require golang.org/x/net v0.43.0

require golang.org/x/text v0.28.0 // indirect
