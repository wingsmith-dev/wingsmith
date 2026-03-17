// Package interop runs the real Go operator client against the real Python hub over a real
// socket. Every other test in this repository exercises one side against a fake of the other,
// which is exactly the arrangement in which two components agree perfectly and still cannot talk.
//
// The suite is behind the `interop` build tag and needs both toolchains, so `go test ./...`
// leaves it alone. Run it with `just interop` from the repository root.
//
// It lives inside the operator module rather than beside the hub launcher in test/interop
// because the client it drives is an internal package: Go's internal rule puts the driver here
// or nowhere. The hub half of the harness — the process wiring and the control surface the tests
// drive it through — is test/interop/hub.py.
package interop
