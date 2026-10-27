// Package workload holds the one thing both loops must agree on: what it means for a running
// workload to be unchanged.
//
// The observation loop publishes the digest, the plane records the digest it adopted, and the
// reconcile loop refuses to touch an adopted object whose digest has moved. Three places, one
// definition: a second implementation that disagreed by a whitespace would turn the adoption gate
// into a permanent false alarm, which is the same as no gate at all.
package workload

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"

	corev1 "k8s.io/api/core/v1"
)

// Fingerprint digests what a workload is running, and not how much of it.
//
// The pod template alone goes in: image, command, args, environment, resources, mounts. Replica
// count, status and the nodes it landed on stay out. That boundary is the whole value of the
// digest: scaling an adopted workload must not look like its definition changed, because scaling
// is free and redefining it costs a reload of the weights. A digest that moved with the replica
// count would make every scale look like a rollout, and a gate that cries wolf is ignored within a
// week.
func Fingerprint(spec *corev1.PodSpec) string {
	encoded, err := json.Marshal(spec)
	if err != nil {
		// A pod spec that will not marshal is not something to guess about: an empty digest
		// matches no recorded one, so the workload fails the adoption gate closed.
		return ""
	}
	sum := sha256.Sum256(encoded)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// FingerprintPod digests a bare pod: one with no controller above it. Scheduling decisions are
// cleared first: which node it landed on is where it is running, not what it is.
func FingerprintPod(p *corev1.Pod) string {
	spec := p.Spec.DeepCopy()
	spec.NodeName = ""
	spec.NodeSelector = nil
	return Fingerprint(spec)
}
