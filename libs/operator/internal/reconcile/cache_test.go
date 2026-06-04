package reconcile

import (
	"context"
	"strconv"
	"strings"
	"testing"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
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

// prefetchJobs is every download job claiming one artifact, found the way the reconciler finds
// them. By label and not by name, so that a test asserting "one job exists" keeps asserting it
// after the name changes shape again.
func prefetchJobs(t *testing.T, c client.Client, artifactID string) []batchv1.Job {
	t.Helper()
	var jobs batchv1.JobList
	if err := c.List(context.Background(), &jobs,
		client.InNamespace(testRender.Namespace),
		client.MatchingLabels{render.ArtifactLabel: artifactID},
	); err != nil {
		t.Fatalf("list prefetch jobs: %v", err)
	}
	return jobs.Items
}

func prefetchJob(t *testing.T, c client.Client, artifactID string) (*batchv1.Job, bool) {
	t.Helper()
	jobs := prefetchJobs(t, c, artifactID)
	if len(jobs) == 0 {
		return nil, false
	}
	return &jobs[0], true
}

// TestWarmSetDispatchesAPrefetchPerArtifact is the inversion the package doc describes: an
// artifact is fetched because the cluster should hold it, not because a deployment asked.
func TestWarmSetDispatchesAPrefetchPerArtifact(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID, Priority: 1}))
	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
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
	r, out, _ := runReconcile(t, cache, declared(t, "deployment", testDeploymentID, testDeploymentDesired()))

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

// TestAnArtifactNoDeploymentServesWaitsRatherThanGuessingAnImage. The cache layout a download
// produces has to be the one the engine later expects to read; there is no safe default image, and
// the symptom of the wrong one is not an error but every worker silently re-downloading.
func TestAnArtifactNoDeploymentServesWaits(t *testing.T) {
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

// TestADeploymentWithNoEngineRuntimeImageIsNotAnImageToBorrow.
func TestADeploymentWithNoEngineRuntimeImageIsNotAnImageToBorrow(t *testing.T) {
	dep := testDeploymentDesired()
	dep.Serving.EngineRuntimeImage = nil
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, dep),
	)

	if readyCondition(t, out).Reason != "WarmSetIncomplete" {
		t.Fatal("a deployment with no engine image was treated as fetchable")
	}
	if _, ok := prefetchJob(t, r.Client, testArtifactID); ok {
		t.Error("a prefetch was dispatched without an image to fetch with")
	}
}

// TestAnEmptyEngineRuntimeImageIsTreatedAsAbsent — a serving field set to "" is how an omitted value
// arrives, and it must not become an image reference of nothing.
func TestAnEmptyEngineRuntimeImageIsTreatedAsAbsent(t *testing.T) {
	blank := ""
	dep := testDeploymentDesired()
	dep.Serving.EngineRuntimeImage = &blank
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	_, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, dep),
	)
	if readyCondition(t, out).Reason != "WarmSetIncomplete" {
		t.Fatal("an empty engine image was borrowed as if it named something")
	}
}

// TestAnExistingPrefetchIsLeftAlone. A Job's pod template is immutable once created, and
// re-running a completed download to close a cosmetic difference would re-pull tens of gigabytes.
func TestAnExistingPrefetchIsLeftAlone(t *testing.T) {
	existing := completedPrefetch()
	existing.Labels["first-run"] = "yes"
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))

	r, out, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
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
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
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

// lastCache is the most recent thing said about an artifact's residency.
func lastCache(t *testing.T, r *recordingReporter, artifactID string) wire.CacheObserved {
	t.Helper()
	for i := len(r.cacheFrames) - 1; i >= 0; i-- {
		if r.cacheFrames[i].ArtifactID == artifactID {
			return r.cacheFrames[i]
		}
	}
	t.Fatalf("nothing was reported about %s", artifactID)
	return wire.CacheObserved{}
}

// TestAStalledWarmSetEntrySaysWhyItIsStalled is the gap that let a workload hang indefinitely with
// no explanation anywhere the plane could read. The operator knew the download would never start —
// there is no image to start it with — and kept that entirely to itself, reporting a bare `absent`
// that is indistinguishable from a download about to begin.
func TestAStalledWarmSetEntrySaysWhyItIsStalled(t *testing.T) {
	dep := testDeploymentDesired()
	dep.Serving.EngineRuntimeImage = nil
	rep := &recordingReporter{}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, dep),
	)

	frame := lastCache(t, rep, testArtifactID)
	if frame.State != "absent" {
		t.Errorf("state = %q, want absent", frame.State)
	}
	if frame.Reason != "NoRuntimeImage" {
		t.Errorf("reason = %q, want NoRuntimeImage", frame.Reason)
	}
	if frame.Message == "" {
		t.Error("a reason with no message leaves the reader to guess what to do about it")
	}
}

// TestAnUndeclaredArtifactSaysSo — the other stalled entry, and a different action: this one
// resolves on its own when the frame arrives, so it must not read like the one that never does.
func TestAnUndeclaredArtifactSaysSo(t *testing.T) {
	rep := &recordingReporter{}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	runReconcileWith(t, rep, cache)

	if got := lastCache(t, rep, testArtifactID).Reason; got != "ArtifactNotDeclared" {
		t.Errorf("reason = %q, want ArtifactNotDeclared", got)
	}
}

// TestADispatchedPrefetchReportsItIsPending — a warm-set entry that is fetchable but whose job has
// not run yet is also `absent`, and it is the one `absent` that needs no action at all.
func TestADispatchedPrefetchReportsItIsPending(t *testing.T) {
	rep := &recordingReporter{}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
	)

	frame := lastCache(t, rep, testArtifactID)
	if frame.Reason != "PrefetchPending" {
		t.Errorf("reason = %q, want PrefetchPending", frame.Reason)
	}
	if frame.Resident {
		t.Error("a queued download reported the weights as resident")
	}
}

// TestResidencyIsReportedOncePerTransition — the report is suppressed when it would repeat itself,
// so the plane's event log is transitions and not a heartbeat every fifteen seconds.
func TestResidencyIsReportedOncePerTransition(t *testing.T) {
	rep := &recordingReporter{}
	dep := testDeploymentDesired()
	dep.Serving.EngineRuntimeImage = nil
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	objects := []client.Object{
		cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, dep),
	}
	r, _, _ := runReconcileWith(t, rep, objects...)
	before := len(rep.cacheFrames)

	req := types.NamespacedName{Namespace: cache.GetNamespace(), Name: cache.GetName()}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: req}); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}

	if len(rep.cacheFrames) != before {
		t.Errorf("frames = %d, want %d: an unchanged residency was reported twice", len(rep.cacheFrames), before)
	}
}

// legacyPrefetch is the job an operator from before the rename left running on a live cluster: the
// old `prefetch-<id>` name, and the artifact label every version has always written.
func legacyPrefetch(status batchv1.JobStatus) *batchv1.Job {
	return &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "prefetch-" + render.Name(testArtifactID),
			Namespace: testRender.Namespace,
			Labels: map[string]string{
				render.ArtifactLabel:  testArtifactID,
				render.ManagedByLabel: "hangar-operator",
			},
		},
		Status: status,
	}
}

// TestAPrefetchUnderTheOldNameIsStillFound is the migration this rename had to survive. Every job
// already running on every cluster was named before the model led the name, and a lookup that
// rebuilt the string would have seen none of them: this reconcile would have started a second pull
// into the same shared cache — the concurrent download the job exists to prevent — while reporting
// the weights it was already holding as absent.
func TestAPrefetchUnderTheOldNameIsStillFound(t *testing.T) {
	existing := legacyPrefetch(batchv1.JobStatus{Active: 1})
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))

	rep := &recordingReporter{}
	r, _, _ := runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		existing,
	)

	jobs := prefetchJobs(t, r.Client, testArtifactID)
	if len(jobs) != 1 {
		names := make([]string, 0, len(jobs))
		for i := range jobs {
			names = append(names, jobs[i].Name)
		}
		t.Fatalf("%d jobs pulling one artifact into one cache: %v", len(jobs), names)
	}
	if jobs[0].Name != existing.Name {
		t.Errorf("job = %q, want the one that was already running", jobs[0].Name)
	}
	if got := lastCache(t, rep, testArtifactID).State; got != "fetching" {
		t.Errorf("state = %q, want fetching: a running download read as absent", got)
	}
}

// TestWeightsUnderTheOldNameAreResident — the same migration read from the deployment side, where
// missing the job holds every deployment of those weights behind a fetch that already finished.
func TestWeightsUnderTheOldNameAreResident(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet())
	r, _, _ := runReconcile(t, cache, legacyPrefetch(batchv1.JobStatus{Succeeded: 1}))

	resident, err := r.weightsResident(context.Background(), testArtifactID)
	if err != nil {
		t.Fatalf("weightsResident: %v", err)
	}
	if !resident {
		t.Error("weights on disk read as absent because the job was named before the rename")
	}
}

// TestOneArtifactResolvesToOneJob. Two jobs for one artifact is a state this operator does not
// create but can inherit — a rename, a hand-run pull — and residency that flickered with list
// order would report the weights arriving and leaving on alternate reconciles.
func TestOneArtifactResolvesToOneJob(t *testing.T) {
	done := legacyPrefetch(batchv1.JobStatus{Succeeded: 1})
	running := completedPrefetch()
	running.Status = batchv1.JobStatus{Active: 1}
	running.CreationTimestamp = metav1.NewTime(done.CreationTimestamp.Add(-time.Hour))
	cache := declared(t, "cache", testCacheID, warmSet())
	r, _, _ := runReconcile(t, cache, done, running)

	job, err := r.prefetchJobFor(context.Background(), testArtifactID)
	if err != nil {
		t.Fatalf("prefetchJobFor: %v", err)
	}
	if job == nil || job.Name != done.Name {
		t.Fatalf("job = %v, want the succeeded one whatever else is in flight", job)
	}

	resident, err := r.weightsResident(context.Background(), testArtifactID)
	if err != nil || !resident {
		t.Errorf("weightsResident = %v, %v; a finished download must outrank a running one", resident, err)
	}
}

// failedPrefetch is a download that has given up: the JobFailed condition set, which is what the
// API server writes once a Job is past its BackoffLimit.
func failedPrefetch(attempts int) *batchv1.Job {
	job := completedPrefetch()
	job.Status = batchv1.JobStatus{
		Failed: 3,
		Conditions: []batchv1.JobCondition{{
			Type:    batchv1.JobFailed,
			Status:  corev1.ConditionTrue,
			Reason:  "BackoffLimitExceeded",
			Message: "Job has reached the specified backoff limit",
		}},
	}
	if attempts > 0 {
		job.Annotations = map[string]string{render.AttemptAnnotation: strconv.Itoa(attempts)}
	}
	return job
}

// TestAFailedDownloadIsStartedAgain is the deadlock this pair of fixes exists for. A 306 GiB model
// on site-b sat eight hours behind a Job that had tipped over its BackoffLimit when the node under it
// went NotReady. Nothing retried it, because the reconciler read the existence of a Job object as
// the download being handled, and no status was ever consulted.
func TestAFailedDownloadIsStartedAgain(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, _, _ := runReconcileWith(t, &recordingReporter{}, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		failedPrefetch(0),
	)

	jobs := prefetchJobs(t, r.Client, testArtifactID)
	if len(jobs) != 1 {
		t.Fatalf("jobs = %d, want 1: the failed download was not replaced by exactly one running one", len(jobs))
	}
	if prefetchFailure(&jobs[0]) != "" {
		t.Fatal("the job left behind is the one that had already failed")
	}
	if got := jobs[0].Annotations[render.AttemptAnnotation]; got != "1" {
		t.Errorf("attempt = %q, want 1: the restart was not counted, so the bound cannot hold", got)
	}

	// The bound must actually advance across restarts, which is the whole reason the count rides on
	// the object: a replacement that started again from zero would retry for ever.
	req := types.NamespacedName{Namespace: cache.GetNamespace(), Name: cache.GetName()}
	if _, err := r.Reconcile(context.Background(), ctrl.Request{NamespacedName: req}); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if jobs := prefetchJobs(t, r.Client, testArtifactID); len(jobs) != 1 {
		t.Errorf("jobs = %d, want 1: a healthy download was replaced a second time", len(jobs))
	}
}

// TestTheRestartBoundAdvancesAcrossFailures — each restart has to count the last one, or the bound
// is a bound in name only and an unfetchable artifact pulls on a registry for ever.
func TestTheRestartBoundAdvancesAcrossFailures(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, _, _ := runReconcileWith(t, &recordingReporter{}, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		failedPrefetch(1),
	)

	jobs := prefetchJobs(t, r.Client, testArtifactID)
	if len(jobs) != 1 {
		t.Fatalf("jobs = %d, want 1", len(jobs))
	}
	if got := jobs[0].Annotations[render.AttemptAnnotation]; got != "2" {
		t.Errorf("attempt = %q, want 2: the restart started the count again instead of continuing it", got)
	}
}

// TestADownloadThatKeepsFailingIsGivenUpOn — weights that cannot be fetched at all fail identically
// every time, and an unbounded retry there is this cluster pulling on somebody else's registry for
// ever. Giving up has to leave the warm set saying so rather than reporting itself dispatched.
func TestADownloadThatKeepsFailingIsGivenUpOn(t *testing.T) {
	rep := &recordingReporter{}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, out, _ := runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		failedPrefetch(maxPrefetchAttempts-1),
	)

	if jobs := prefetchJobs(t, r.Client, testArtifactID); len(jobs) != 1 {
		t.Errorf("jobs = %d, want 1: the exhausted download was cleared away and tried again", len(jobs))
	}
	if got := conditionReason(out); got != "WarmSetIncomplete" {
		t.Errorf("reason = %q, want WarmSetIncomplete: the warm set called itself dispatched over a download nothing will run", got)
	}
}

// TestAFailedDownloadIsNotReportedAsRunning — the failure that made the deadlock take investigation
// rather than a glance. A Job past its BackoffLimit was folded in with the active ones, so the
// cluster went on telling the plane the download was running long after it had stopped trying.
func TestAFailedDownloadIsNotReportedAsRunning(t *testing.T) {
	rep := &recordingReporter{}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		failedPrefetch(maxPrefetchAttempts-1),
	)

	frame := lastCache(t, rep, testArtifactID)
	if frame.State == "fetching" {
		t.Error("a download that had given up was reported as still running")
	}
	if frame.Reason != "PrefetchFailed" {
		t.Errorf("reason = %q, want PrefetchFailed", frame.Reason)
	}
	if !strings.Contains(frame.Message, "backoff limit") {
		t.Errorf("message = %q, want the job's own account of the failure", frame.Message)
	}
	if frame.Resident {
		t.Error("a failed download reported the weights as resident")
	}
}

// TestPodsFailingWhileTheJobHasAttemptsLeftIsStillFetching — the condition and not a pod count is
// what says a download has stopped. Restarting on a dead pod would throw away a download in
// progress every time one is preempted.
func TestPodsFailingWhileTheJobHasAttemptsLeftIsStillFetching(t *testing.T) {
	rep := &recordingReporter{}
	job := completedPrefetch()
	job.Status = batchv1.JobStatus{Active: 1, Failed: 1}
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, _, _ := runReconcileWith(t, rep, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		job,
	)

	if jobs := prefetchJobs(t, r.Client, testArtifactID); len(jobs) != 1 {
		t.Errorf("jobs = %d, want 1: a download still retrying was cleared away", len(jobs))
	}
	if frame := lastCache(t, rep, testArtifactID); frame.State != "fetching" {
		t.Errorf("state = %q, want fetching", frame.State)
	}
}

// conditionReason is the reason the reconciler last set on a synced resource.
func conditionReason(res *v1alpha1.SyncedResource) string {
	if n := len(res.Status.Conditions); n > 0 {
		return res.Status.Conditions[n-1].Reason
	}
	return ""
}
