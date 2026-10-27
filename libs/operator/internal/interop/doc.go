// Package interop runs the real Go operator client against the real Python hub over a real
// socket. Every other test in this repository exercises one side against a fake of the other,
// which is exactly the arrangement in which two components agree perfectly and still cannot talk.
//
// The suite is behind the `interop` build tag and needs both toolchains, so `go test ./...`
// leaves it alone. Run it with `just interop` from the repository root.
//
// It lives inside the operator module because the client it drives is an internal package: Go's
// internal rule puts the driver here or nowhere. The hub half of the harness, the process wiring
// and the control surface the tests drive it through, is testdata/hub.py beside this file. It
// enrols the test cluster with a freshly minted per-cluster enrolment token, the only credential
// the hub opens a session for, and announces the token beside its port.
package interop
