package reconcile

import (
	"context"
	"strings"
	"testing"

	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const testCacheID = "cch_01j9x2hhhhhhhhhhhhhhhhhhhh"

func warmSet(entries ...wire.CacheWarmEntry) wire.CacheDesired {
	return wire.CacheDesired{
		ClusterID:      "clu_01j9x2aaaaaaaaaaaaaaaaaaaa",
		Entries:        entries,
		EvictionPolicy: "lru",
		CapacityBytes:  2 << 40,
		UpdatedAt:      "2026-08-23T15:04:05.000Z",
	}
}

func prefetchJob(t *testing.T, c client.Client, artifactID string) (*batchv1.Job, bool) {
	t.Helper()
	var job batchv1.Job
	key := types.NamespacedName{Namespace: testRender.Namespace, Name: render.PrefetchJobName(artifactID)}
	if err := c.Get(context.Background(), key, &job); err != nil {
		return nil, false
	}
	return &job, true
}

// TestWarmSetDispatchesAPrefetchPerArtifact is the inversion the package doc describes: an
// artifact is fetched because the cluster should hold it, not because a deployment asked.
func TestWarmSetDispatchesAPrefetchPerArtifact(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID, Priority: 1}))
	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "recipe", testRecipeID, testRecipeDesired()),
	)

	cond := readyCondition(t, out)
	if cond.Status != metav1.ConditionTrue || cond.Reason != "WarmSetDispatched" {
		t.Fatalf("condition = %s/%s, want True/WarmSetDispatched", cond.Status, cond.Reason)
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); !ok {
		t.Fatal("no prefetch job was created for the warm-set entry")
	}
}

// TestAWarmSetEntryWithNoArtifactWaits — the plane named weights whose artifact has not arrived on
// this stream yet, which is a race and not an error.
func TestAWarmSetEntryWithNoArtifactWaits(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, out, _ := runReconcile(t, cache, declared(t, "recipe", testRecipeID, testRecipeDesired()))

	cond := readyCondition(t, out)
	if cond.Status == metav1.ConditionTrue {
		t.Fatalf("an unfetchable warm set reported ready: %s/%s", cond.Reason, cond.Message)
	}
	if cond.Reason != "WarmSetIncomplete" {
		t.Errorf("reason = %q, want WarmSetIncomplete", cond.Reason)
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); ok {
		t.Error("a prefetch was dispatched for an artifact this cluster does not know")
	}
}

// TestAnArtifactNoRecipeServesWaitsRatherThanGuessingAnImage. The cache layout a download produces
// has to be the one the engine later expects to read; there is no safe default image, and the
// symptom of the wrong one is not an error but every worker silently re-downloading.
func TestAnArtifactNoRecipeServesWaits(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, out, _ := runReconcile(t, cache, declared(t, "artifact", testArtifactID, testArtifactDesired()))

	cond := readyCondition(t, out)
	if cond.Reason != "WarmSetIncomplete" {
		t.Fatalf("reason = %q, want WarmSetIncomplete", cond.Reason)
	}
	if !strings.Contains(cond.Message, "not yet fetchable") {
		t.Errorf("the message does not say what is missing: %q", cond.Message)
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); ok {
		t.Error("a prefetch was dispatched with a guessed image")
	}
}

// TestARecipeWithNoEngineImageIsNotAnImageToBorrow.
func TestARecipeWithNoEngineImageIsNotAnImageToBorrow(t *testing.T) {
	recipe := testRecipeDesired()
	recipe.EngineImage = nil
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "recipe", testRecipeID, recipe),
	)

	if readyCondition(t, out).Reason != "WarmSetIncomplete" {
		t.Fatal("a recipe with no engine image was treated as fetchable")
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); ok {
		t.Error("a prefetch was dispatched without an image to fetch with")
	}
}

// TestAnEmptyEngineImageIsTreatedAsAbsent — a recipe field set to "" is how an omitted value
// arrives, and it must not become an image reference of nothing.
func TestAnEmptyEngineImageIsTreatedAsAbsent(t *testing.T) {
	blank := ""
	recipe := testRecipeDesired()
	recipe.EngineImage = &blank
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	_, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "recipe", testRecipeID, recipe),
	)
	if readyCondition(t, out).Reason != "WarmSetIncomplete" {
		t.Fatal("an empty engine image was borrowed as if it named something")
	}
}

// TestAnExistingPrefetchIsLeftAlone. A Job's pod template is immutable once created, and
// re-running a completed download to close a cosmetic difference would re-pull tens of gigabytes.
func TestAnExistingPrefetchIsLeftAlone(t *testing.T) {
	existing := completedPrefetch()
	existing.Labels = map[string]string{"first-run": "yes"}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))

	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "recipe", testRecipeID, testRecipeDesired()),
		existing,
	)

	if readyCondition(t, out).Status != metav1.ConditionTrue {
		t.Fatal("a warm set whose job already exists did not report ready")
	}
	job, ok := prefetchJob(t, r.Client, testArtifactID)
	if !ok {
		t.Fatal("the prefetch job disappeared")
	}
	if job.Labels["first-run"] != "yes" {
		t.Error("the completed prefetch was recreated; tens of gigabytes were re-pulled for nothing")
	}
}

// TestAnEmptyWarmSetIsReadyRatherThanPending — a cluster told to hold nothing is holding it.
func TestAnEmptyWarmSetIsReady(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet())
	_, out, _ := runReconcile(t, cache)

	cond := readyCondition(t, out)
	if cond.Status != metav1.ConditionTrue || cond.Reason != "WarmSetDispatched" {
		t.Fatalf("an empty warm set reported %s/%s", cond.Status, cond.Reason)
	}
}

// TestAWarmSetIsPartiallyDispatched: one fetchable entry gets its job even while another waits,
// rather than the whole set stalling on its slowest member.
func TestAWarmSetIsPartiallyDispatched(t *testing.T) {
	const otherArtifact = "art_01j9x2kkkkkkkkkkkkkkkkkkkk"
	cache := declared(t, "cache", testCacheID, warmSet(
		wire.CacheWarmEntry{ArtifactID: testArtifactID},
		wire.CacheWarmEntry{ArtifactID: otherArtifact},
	))
	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "recipe", testRecipeID, testRecipeDesired()),
	)

	cond := readyCondition(t, out)
	if cond.Reason != "WarmSetIncomplete" {
		t.Fatalf("reason = %q, want WarmSetIncomplete", cond.Reason)
	}
	if !strings.Contains(cond.Message, "1 of 2") {
		t.Errorf("the message does not count what is outstanding: %q", cond.Message)
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); !ok {
		t.Error("the fetchable entry stalled behind the one that is not")
	}
	if _, ok := prefetchJob(t, r.Client, otherArtifact); ok {
		t.Error("a prefetch was dispatched for an artifact this cluster does not know")
	}
}

// TestAnUndecodableWarmSetFailsWithoutRetrying — a payload this operator cannot read will not
// become readable by trying again.
func TestAnUndecodableWarmSetFailsWithoutRetrying(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet())
	cache.Spec.Payload.Raw = []byte(`{"entries": "not a list"}`)
	_, out, result := runReconcile(t, cache)

	cond := readyCondition(t, out)
	if cond.Status != metav1.ConditionFalse || cond.Reason != "PayloadUndecodable" {
		t.Fatalf("condition = %s/%s, want False/PayloadUndecodable", cond.Status, cond.Reason)
	}
	if result.Requeue || result.RequeueAfter != 0 {
		t.Error("an undecodable payload was scheduled for a retry that cannot help")
	}
}
