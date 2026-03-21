package reconcile

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// desiredTypes are the hub -> operator frames that declare state this store keeps a durable local
// copy of, and prunes against a snapshot. Operations are absent on purpose: an operation is
// imperative and failable, not a declaration, and pruning one because a snapshot omitted it would
// cancel work that is still running.
var desiredTypes = map[syncpkg.MessageType]struct{}{
	wireDeploymentDesired: {},
	wireDeploymentDeleted: {},
	wireArtifactDesired:   {},
	wireComponentDesired:  {},
	wireCacheDesired:      {},
}

const (
	wireDeploymentDesired = "deployment.desired"
	wireDeploymentDeleted = "deployment.deleted"
	wireArtifactDesired   = "artifact.desired"
	wireComponentDesired  = "component.desired"
	wireCacheDesired      = "cache.desired"
)

func isDesired(t syncpkg.MessageType) bool {
	_, ok := desiredTypes[t]
	return ok
}

// decodeDesired lifts the identity of a declared object out of its frame.
//
// Revision is the hub's generation where the type carries one. Types without a generation are
// stored at revision 0, which makes every arrival authoritative — correct for a full-object
// stream whose frames are already delivered in sequence order.
func decodeDesired(e *syncpkg.Envelope) (desiredState, error) {
	d := desiredState{Object: e.Payload}
	switch e.Type {
	case wireDeploymentDesired:
		var p wire.DeploymentDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return d, fmt.Errorf("decode %s: %w", e.Type, err)
		}
		d.Kind, d.Name, d.Revision = "deployment", resourceName(p.DeploymentID), p.Generation
		if p.Adopt != nil {
			d.Adopt = &v1alpha1.AdoptRef{
				Namespace:       p.Adopt.Namespace,
				Kind:            p.Adopt.Kind,
				Name:            p.Adopt.Name,
				SpecFingerprint: p.Adopt.SpecFingerprint,
			}
		}
	case wireDeploymentDeleted:
		var p wire.DeploymentDeleted
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return d, fmt.Errorf("decode %s: %w", e.Type, err)
		}
		d.Kind, d.Name, d.Revision, d.Deleted = "deployment", resourceName(p.DeploymentID), p.Generation, true
	case wireArtifactDesired:
		var p wire.ArtifactDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return d, fmt.Errorf("decode %s: %w", e.Type, err)
		}
		d.Kind, d.Name = "artifact", resourceName(p.ArtifactID)
	case wireComponentDesired:
		var p wire.ComponentDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return d, fmt.Errorf("decode %s: %w", e.Type, err)
		}
		d.Kind, d.Name = "component", resourceName(p.ComponentID)
	case wireCacheDesired:
		var p wire.CacheDesired
		if err := json.Unmarshal(e.Payload, &p); err != nil {
			return d, fmt.Errorf("decode %s: %w", e.Type, err)
		}
		d.Kind, d.Name = "cache", resourceName("cache-"+p.ClusterID)
	default:
		return d, fmt.Errorf("not a desired-state type: %s", e.Type)
	}
	if d.Name == "" {
		return d, fmt.Errorf("%s carries no object id", e.Type)
	}
	return d, nil
}

// resourceName renders a prefixed entity id as a Kubernetes object name: lowercase, and with the
// prefix separator the API server does not accept swapped for a hyphen.
func resourceName(id string) string {
	return strings.ToLower(strings.ReplaceAll(id, "_", "-"))
}
