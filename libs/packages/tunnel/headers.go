package tunnel

// HeaderDeploymentID names the deployment a request was already routed to, and
// it is the one piece of routing state that crosses this wire.
//
// It is declared here instead of in the gateway or the agent, for the same
// reason the module exists: neither end owns the protocol, and a constant that
// lived in one of them would make the other an importer of its private
// vocabulary. Both ends read this file, so neither can rename it alone.
//
// It exists because the two decisions are made in different places. The gateway
// is the only hop that parses the body, and by the time it forwards a request it
// has already joined that body's `model` against the routing table and picked
// one deployment on one cluster. Inside the cluster the same request is bytes
// aimed at a ClusterIP, and the agent would otherwise have to re-derive what the
// gateway already knew, by asking each frontend what models it serves, which is
// a poll, a cache, and a second answer that can disagree with the first. Naming
// the deployment on the request costs one header and cannot disagree.
//
// An absent or empty value means the caller could not name one. That is an
// ordinary case and never an error: a gateway older than this constant sends
// nothing, an upstream the plane published without a deployment id has nothing
// to send, and anything reaching the agent that is not the gateway never had the
// answer. A reader must treat it as "choose for me" and fall back to whatever it
// serves by default, not as a claim that the request belongs to no deployment.
//
// It is the gateway's to write and nobody else's. httputil.ReverseProxy copies
// the inbound headers onto the outbound request verbatim, so a value a caller
// sent under this name travels the whole way unless the gateway overwrites or
// deletes it on every request, which is why the gateway does so
// unconditionally, and why a reader must never trust this header from anywhere
// but a tunnel.
const HeaderDeploymentID = "X-Hangar-Deployment-Id"
