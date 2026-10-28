package reconcile

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/wingsmith-dev/wingsmith/libs/operator/api/v1alpha1"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// TestEveryDesiredTypeDecodesToAnIdentity walks the whole registry. A type that is declared
// desired but not decodable here is a frame the store would refuse and the hub would replay
// forever, so the two lists must not drift apart.
func TestEveryDesiredTypeDecodesToAnIdentity(t *testing.T) {
	cases := []struct {
		kind     string
		payload  any
		wantKind string
		wantName string
		wantRev  int64
	}{
		{
			wireDeploymentDesired,
			wire.DeploymentDesired{DeploymentID: testDeploymentID, Generation: 7},
			"deployment", resourceName(testDeploymentID), 7,
		},
		{
			wireArtifactDesired,
			wire.ArtifactDesired{ArtifactID: testArtifactID},
			"artifact", resourceName(testArtifactID), 0,
		},
		{
			wireComponentDesired,
			wire.ComponentDesired{ComponentID: "cmp_01J9X2LLLLLLLLLLLLLLLLLLLL"},
			"component", "cmp-01j9x2llllllllllllllllllll", 0,
		},
		{
			wireCacheDesired,
			wire.CacheDesired{ClusterID: "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"},
			"cache", "cache-clu-01j9x2aaaaaaaaaaaaaaaaaaaa", 0,
		},
	}
	for _, c := range cases {
		got, err := decodeDesired(frame(t, c.kind, 1, c.payload))
		if err != nil {
			t.Errorf("decode %s: %v", c.kind, err)
			continue
		}
		if got.Kind != c.wantKind || got.Name != c.wantName || got.Revision != c.wantRev {
			t.Errorf("decode %s = %s/%s rev %d, want %s/%s rev %d",
				c.kind, got.Kind, got.Name, got.Revision, c.wantKind, c.wantName, c.wantRev)
		}
		if got.Deleted {
			t.Errorf("decode %s marked the object deleted", c.kind)
		}
	}
	if len(cases)+1 != len(desiredTypes) {
		t.Errorf("desiredTypes holds %d types and this test covers %d plus the deletion; they have drifted",
			len(desiredTypes), len(cases))
	}
}

// TestADeletionIsDecodedAsOne: a deployment.deleted that decoded as an ordinary desired frame
// would re-create the object it was sent to remove.
func TestADeletionIsDecodedAsOne(t *testing.T) {
	got, err := decodeDesired(frame(t, wireDeploymentDeleted, 1,
		wire.DeploymentDeleted{DeploymentID: testDeploymentID, Generation: 9}))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !got.Deleted {
		t.Fatal("a deletion frame did not decode as a deletion")
	}
	if got.Kind != "deployment" || got.Name != resourceName(testDeploymentID) || got.Revision != 9 {
		t.Errorf("deletion decoded as %s/%s rev %d", got.Kind, got.Name, got.Revision)
	}
}

// TestAuthorityAndAdoptionSurviveTheDecode. Authority is the operator's gate (ADR 0009), and the
// digest is what the cluster checks before it will manage something it did not create: a decode
// that dropped either would open the gate by omission.
func TestAuthorityAndAdoptionSurviveTheDecode(t *testing.T) {
	payload := wire.DeploymentDesired{
		DeploymentID: testDeploymentID,
		Generation:   2,
		Authority:    string(v1alpha1.AuthorityManaged),
		Adopt: &wire.Adopt{
			Namespace:       "inference",
			Kind:            "Deployment",
			Name:            "qwen3-8b",
			SpecFingerprint: "sha256:abc123",
		},
	}
	got, err := decodeDesired(frame(t, wireDeploymentDesired, 1, payload))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Authority != string(v1alpha1.AuthorityManaged) {
		t.Errorf("authority = %q, want managed", got.Authority)
	}
	if got.Adopt == nil {
		t.Fatal("the adoption reference was dropped")
	}
	if got.Adopt.SpecFingerprint != "sha256:abc123" || got.Adopt.Name != "qwen3-8b" ||
		got.Adopt.Namespace != "inference" || got.Adopt.Kind != "Deployment" {
		t.Errorf("adoption reference decoded as %+v", got.Adopt)
	}
}

// TestADeploymentWithNoAdoptionCarriesNone: an empty ref is not the same as no ref, and an
// invented one would gate a deployment against a digest nothing published.
func TestADeploymentWithNoAdoptionCarriesNone(t *testing.T) {
	got, err := decodeDesired(frame(t, wireDeploymentDesired, 1,
		wire.DeploymentDesired{DeploymentID: testDeploymentID}))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Adopt != nil {
		t.Errorf("an ordinary deployment carries an adoption reference: %+v", got.Adopt)
	}
}

// TestAFrameWithNoIDIsRefused. resourceName("") is "", and an object stored under the empty name
// would collide with every other id-less frame of its kind.
func TestAFrameWithNoIDIsRefused(t *testing.T) {
	for _, c := range []struct {
		kind    string
		payload any
	}{
		{wireDeploymentDesired, wire.DeploymentDesired{}},
		{wireDeploymentDeleted, wire.DeploymentDeleted{}},
		{wireArtifactDesired, wire.ArtifactDesired{}},
		{wireComponentDesired, wire.ComponentDesired{}},
	} {
		if _, err := decodeDesired(frame(t, c.kind, 1, c.payload)); err == nil {
			t.Errorf("%s with no id was accepted", c.kind)
		} else if !strings.Contains(err.Error(), "no object id") {
			t.Errorf("%s refused for the wrong reason: %v", c.kind, err)
		}
	}
}

// TestACacheFrameWithNoClusterStillNamesSomething: the name is derived from a prefix plus the
// cluster id, so it is never empty and the id-less check cannot catch it. This records that.
func TestACacheFrameWithNoClusterStillNamesSomething(t *testing.T) {
	got, err := decodeDesired(frame(t, wireCacheDesired, 1, wire.CacheDesired{}))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got.Name != "cache-" {
		t.Errorf("name = %q; a cluster-less warm set no longer derives a constant name", got.Name)
	}
}

// TestAnUndecodablePayloadIsRefusedRatherThanStoredEmpty.
func TestAnUndecodablePayloadIsRefused(t *testing.T) {
	env := &syncpkg.Envelope{Type: wireDeploymentDesired, Payload: json.RawMessage(`{"generation":`)}
	if _, err := decodeDesired(env); err == nil {
		t.Fatal("a truncated payload was accepted")
	}
}

// TestATypeThatIsNotDesiredStateIsRefused. The store routes on isDesired, so this is the
// belt-and-braces half: a mismatch between the two must fail loudly, not store a blank.
func TestATypeThatIsNotDesiredStateIsRefused(t *testing.T) {
	for _, kind := range []string{"node.observed", "operation.desired", "", "deployment.observed"} {
		if _, err := decodeDesired(frame(t, kind, 1, map[string]string{})); err == nil {
			t.Errorf("%q was decoded as desired state", kind)
		}
	}
}

// TestIsDesiredAgreesWithDecodeDesired: the routing predicate and the decoder must accept exactly
// the same set, or a frame is either dropped silently or refused forever.
func TestIsDesiredAgreesWithDecodeDesired(t *testing.T) {
	for kind := range desiredTypes {
		if !isDesired(kind) {
			t.Errorf("%s is in desiredTypes but isDesired says otherwise", kind)
		}
	}
	for _, kind := range []string{"node.observed", "operation.desired", "benchmark.observed"} {
		if isDesired(syncpkg.MessageType(kind)) {
			t.Errorf("%s is not desired state", kind)
		}
	}
}

// TestResourceNameIsAValidObjectName: the API server does not accept the underscore our id prefix
// separator uses, and names are lowercase contract-wide (ADR 0002).
func TestResourceNameIsAValidObjectName(t *testing.T) {
	cases := map[string]string{
		"dep_01J9X2GGGGGGGGGGGGGGGGGGGG": "dep-01j9x2gggggggggggggggggggg",
		"art_01j9x2eeeeeeeeeeeeeeeeeeee": "art-01j9x2eeeeeeeeeeeeeeeeeeee",
		"":                               "",
		"NoPrefix":                       "noprefix",
	}
	for in, want := range cases {
		if got := resourceName(in); got != want {
			t.Errorf("resourceName(%q) = %q, want %q", in, got, want)
		}
		if strings.ContainsAny(resourceName(in), "_ABCDEFGHIJKLMNOPQRSTUVWXYZ") {
			t.Errorf("resourceName(%q) = %q is not a valid object name", in, resourceName(in))
		}
	}
}
