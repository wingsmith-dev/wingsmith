package reconcile

import (
	"context"
	"fmt"
	"slices"
	"strconv"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// cacheResync is how long a dispatched warm set waits before it is checked again.
//
// Every other path here wakes on an event: a warm-set frame arrives, a prefetch Job changes, a pod
// moves. A warm set that has dispatched every download waits on nothing and therefore returns no
// requeue at all, and that is the state the cluster can get stuck in: a Job that leaves outside
// of a warm-set change takes its own wake-up with it, so nothing looks again and the warm set goes
// on reporting itself dispatched over an artifact with no download running. A deployment gated on
// those weights then waits forever on a completion that can no longer come. It is the same
// deadlock the attempt counter closed for a *failed* download, reached by a job that is not failed
// but absent: deleted by hand, cleaned out with a namespace, or collected by a TTL.
//
// Five minutes, because this is a backstop and not the mechanism: the watches are still what makes
// the loop responsive, and this only has to be shorter than a person's patience. A cluster holding
// a handful of artifacts pays one list of Jobs per interval for it.
const cacheResync = 5 * time.Minute

// reconcileCache makes the cluster's declared warm-set resident.
//
// Fetching is driven by the warm-set, not by any deployment, and that inversion is what
// matters. Weights are per-cluster and deployments are many, so a fetch owned by a deployment
// would run once per deployment of the same model, race with its siblings over a shared
// filesystem, and leave the second deployment of a model paying the first one's cost again. Here
// an artifact is fetched because the cluster should hold it, and a deployment only ever reads the
// result.
func (r *SyncedResourceReconciler) reconcileCache(ctx context.Context, res *v1alpha1.SyncedResource) (ctrl.Result, error) {
	var warm wire.CacheDesired
	if err := decodePayload(res, &warm); err != nil {
		return r.fail(ctx, res, "PayloadUndecodable", err.Error())
	}

	pending := 0
	for _, entry := range warm.Entries {
		artifact, err := r.artifact(ctx, entry.ArtifactID)
		if err != nil {
			return ctrl.Result{}, err
		}
		if artifact == nil {
			pending++
			// Reported even so. "The cluster was told to hold weights it has never heard
			// described" is the answer to why a deployment is not starting, and it is only an
			// answer if it leaves the operator.
			r.reportResidency(ctx, entry.ArtifactID, 0, "ArtifactNotDeclared",
				fmt.Sprintf("artifact %s has not arrived on this cluster yet", entry.ArtifactID))
			continue
		}
		image, err := r.runtimeImage(ctx, entry.ArtifactID)
		if err != nil {
			return ctrl.Result{}, err
		}
		if image == "" {
			pending++
			// The one gate with no self-healing path. Every other wait here ends when a frame
			// arrives; this one ends only when somebody gives a deployment an engine image, so
			// the reason has to travel or the deployment waits forever on a silent cluster.
			r.reportResidency(ctx, entry.ArtifactID, artifact.SizeBytes, "NoRuntimeImage",
				fmt.Sprintf("no deployment on this cluster serving %s names an engine image to fetch it with; "+
					"the download cannot start until one does", entry.ArtifactID))
			continue
		}
		exhausted, err := r.ensurePrefetch(ctx, res, *artifact, image)
		if err != nil {
			return ctrl.Result{}, err
		}
		if exhausted {
			pending++
		}
		r.reportResidency(ctx, entry.ArtifactID, artifact.SizeBytes, "", "")
	}

	if err := r.prunePrefetch(ctx, res, warm); err != nil {
		return ctrl.Result{}, err
	}

	if pending > 0 {
		return r.wait(ctx, res, "WarmSetIncomplete",
			fmt.Sprintf("%d of %d warm-set entries are not yet fetchable", pending, len(warm.Entries)))
	}
	// Ready, and still asking to be called again; see cacheResync. r.ready reports no requeue
	// because for a deployment there is nothing left to check; a warm set is never finished
	// with, because what it dispatched can go away underneath it.
	result, err := r.ready(ctx, res, "WarmSetDispatched",
		fmt.Sprintf("%d artifacts have a prefetch job", len(warm.Entries)))
	if err != nil {
		return result, err
	}
	result.RequeueAfter = cacheResync
	return result, nil
}

// prunePrefetch removes the download jobs of artifacts the warm set no longer names.
//
// The warm set is a full-object frame and the whole of what this cluster must hold, so an artifact
// absent from it is one the plane has stopped asking for: a workload deleted, a deployment drained
// to another cluster, a pin withdrawn. Nothing else removes these: a prefetch Job is created in the
// workload namespace and the SyncedResource that asked for it lives in another, so it carries a
// label back to its origin and not an owner reference, and Kubernetes garbage-collects on owner
// references alone. Deleting the workload therefore left the download running, or its completed
// pod sitting in `kubectl get pods` under the name of a model nobody had asked for since,
// indistinguishable from something still being served.
//
// Only the Job goes. The weights it wrote stay on the shared cache, which is the point of a cache:
// deploying the same model again should find them there. What is removed is the claim that this
// cluster is *asked* to hold them, and reclaiming the disk is eviction's business, not this one's.
//
// Foreground propagation for a job that is still running, because the pod under it is holding a
// write into the shared filesystem open and letting the delete return before it stops leaves the
// download racing whatever comes next.
func (r *SyncedResourceReconciler) prunePrefetch(ctx context.Context, owner *v1alpha1.SyncedResource, warm wire.CacheDesired) error {
	declared := make(map[string]struct{}, len(warm.Entries))
	for _, entry := range warm.Entries {
		declared[entry.ArtifactID] = struct{}{}
	}

	var jobs batchv1.JobList
	if err := r.Client.List(ctx, &jobs,
		client.InNamespace(r.Render.Namespace),
		client.HasLabels{render.ArtifactLabel},
	); err != nil {
		return fmt.Errorf("read prefetch jobs: %w", err)
	}

	policy := metav1.DeletePropagationForeground
	for i := range jobs.Items {
		job := &jobs.Items[i]
		artifactID := job.Labels[render.ArtifactLabel]
		if _, ok := declared[artifactID]; ok {
			continue
		}
		if !job.DeletionTimestamp.IsZero() {
			continue
		}
		mine, err := r.prefetchIsPrunable(ctx, owner, job)
		if err != nil {
			return err
		}
		if !mine {
			continue
		}
		if err := r.Client.Delete(ctx, job, &client.DeleteOptions{PropagationPolicy: &policy}); err != nil &&
			!apierrors.IsNotFound(err) {
			return fmt.Errorf("prune prefetch job %s: %w", job.Name, err)
		}
		r.Log.Info("dropping a download nothing asks for", "artifact", artifactID, "job", job.Name)
		r.forgetReported(artifactID)
	}
	return nil
}

// prefetchIsPrunable reports whether this warm set is entitled to delete a download it does not
// declare.
//
// "Not in my warm set" is not the same as "nothing asks for it", and treating the two as one has a
// cluster tearing up its own work. A renamed warm set (say from `cache-clu-<cluster id>` to the
// cluster's name) leaves two SyncedResources of kind cache, each holding a different set. A prune
// that listed every Job in the workload namespace and deleted the ones its own set did not name
// would have the stale object delete the live object's downloads and the live object recreate
// them, once every reconcile. A model in the middle of a multi-hundred-gigabyte pull would never
// finish one: its Job replaced every few minutes and the deployment gated on it waiting on a
// download that kept starting over.
//
// So a job with an owner that still exists and is not this one is left alone. The owner is read
// back, not trusted from the label, because the case that has to keep working is exactly the
// rename: once the stale object is gone, its jobs are prunable again and the leak closes itself. A
// job carrying no owner label at all predates the labels and is pruned as before: it belongs to no
// live claim, and refusing to touch it would strand it for ever.
func (r *SyncedResourceReconciler) prefetchIsPrunable(ctx context.Context, owner *v1alpha1.SyncedResource, job *batchv1.Job) (bool, error) {
	name := job.Labels[render.OwnerNameLabel]
	if name == "" || name == owner.GetName() {
		return true, nil
	}

	namespace := job.Labels[render.OwnerNamespaceLabel]
	if namespace == "" {
		namespace = owner.GetNamespace()
	}

	var claimant v1alpha1.SyncedResource
	err := r.Client.Get(ctx, types.NamespacedName{Namespace: namespace, Name: name}, &claimant)
	if apierrors.IsNotFound(err) {
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("read the owner of prefetch job %s: %w", job.Name, err)
	}
	r.Log.Info("leaving a download another warm set owns", "artifact", job.Labels[render.ArtifactLabel],
		"job", job.Name, "owner", name)
	return false, nil
}

// maxPrefetchAttempts bounds how many times a download is started again after failing outright.
//
// A bound and not a retry loop, because the two failures behind this are opposite and only one of
// them is worth repeating. A download killed with the node under it should start again: nothing
// about the artifact is wrong. A download of weights that cannot be fetched at all (a repository
// that has moved, a gated one nobody holds the token for) fails identically every time, and an
// unbounded retry there is this cluster pulling on somebody else's registry for ever, at whatever
// fraction of a large model it reaches before dying.
//
// Four instead of one, because the Job already spends its own BackoffLimit on the transient case:
// reaching here means three pod attempts have already failed, so what this counts is whole
// downloads lost, and the loss that motivated it (a node going NotReady) takes every attempt on
// that node with it at once.
const maxPrefetchAttempts = 4

// ensurePrefetch creates an artifact's download job if it does not already exist, and starts it
// again if the last one failed outright.
//
// Create, not apply, and never an update. A Job's pod template is immutable once created,
// so a server-side apply that differed in any field would be rejected by the API server on every
// pass; and re-running a completed download to close a cosmetic difference would re-pull tens of
// gigabytes for nothing. An artifact is content-addressed, so a job that succeeded for one is
// still correct.
//
// A *failed* one is not, and treating the mere existence of a Job object as a handled download is
// a deadlock with no way out of it. A Job that tips over its BackoffLimit when the node under it
// goes NotReady is never retried, the warm set reports itself Ready over it, and the deployment
// gate waits on a completion that can no longer come. Nothing in the cluster is wrong by then,
// only the record of it.
//
// It returns whether the artifact has run out of attempts, which the caller counts as pending. A
// download that will not be tried again must not leave the warm set reporting itself dispatched.
func (r *SyncedResourceReconciler) ensurePrefetch(ctx context.Context, owner *v1alpha1.SyncedResource, art wire.ArtifactDesired, image string) (exhausted bool, err error) {
	existing, err := r.prefetchJobFor(ctx, art.ArtifactID)
	if err != nil {
		return false, err
	}

	attempt, failure := 0, ""
	if existing != nil {
		if failure = prefetchFailure(existing); failure == "" {
			return false, nil
		}
		attempt = prefetchAttempt(existing) + 1
		if attempt >= maxPrefetchAttempts {
			r.Log.Error("giving up on a download that keeps failing", "artifact", art.ArtifactID,
				"job", existing.Name, "attempts", attempt, "failure", failure)
			return true, nil
		}
	}

	job, err := render.PrefetchJob(art, image, r.Render)
	if err != nil {
		return false, fmt.Errorf("render prefetch job for %s: %w", art.ArtifactID, err)
	}
	if attempt > 0 {
		job.Name = render.PrefetchAttemptName(art, attempt)
		job.Annotations[render.AttemptAnnotation] = strconv.Itoa(attempt)
	}
	// Labelled with its declared origin for the same reason the graph deployment is: the Job is
	// created in the workload namespace and the warm-set that asked for it is not, so the watch
	// that wakes this loop when the download finishes has nothing else to follow back.
	render.OwnedMeta(job, owner)
	if err := r.Client.Create(ctx, job); err != nil && !apierrors.IsAlreadyExists(err) {
		return false, fmt.Errorf("create prefetch job for %s: %w", art.ArtifactID, err)
	}

	if existing == nil {
		r.Log.Info("fetching weights", "artifact", art.ArtifactID, "job", job.Name)
		return false, nil
	}

	// Deleted after the replacement exists, and never before it. The count of how many times this
	// download has been restarted lives on the Job, so a pass that deleted first would hand the
	// next one a clean slate and the bound would never advance.
	//
	// Background propagation, not foreground: a Job carrying JobFailed has already given up
	// and has no pod still writing into the shared cache, so there is nothing here to wait for, and
	// waiting would hold the outgoing name against the incoming one.
	policy := metav1.DeletePropagationBackground
	if err := r.Client.Delete(ctx, existing, &client.DeleteOptions{PropagationPolicy: &policy}); err != nil && !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("delete failed prefetch job for %s: %w", art.ArtifactID, err)
	}
	r.Log.Info("restarting a failed download", "artifact", art.ArtifactID,
		"job", job.Name, "replaced", existing.Name, "attempt", attempt, "failure", failure)
	return false, nil
}

// prefetchFailure is the Job's own account of why it failed, and empty when it has not.
//
// Read from the JobFailed condition, not from Status.Failed, because those two say
// different things. Status.Failed counts pods that have died, and a Job with one dead pod and two
// attempts left is still running: restarting it there would throw away a download in progress on
// every transient pod failure. The condition is set only once the Job has given up.
func prefetchFailure(job *batchv1.Job) string {
	for _, condition := range job.Status.Conditions {
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			if condition.Message != "" {
				return condition.Message
			}
			if condition.Reason != "" {
				return condition.Reason
			}
			return "the download failed"
		}
	}
	return ""
}

// prefetchAttempt reads how many times this download has already been restarted.
func prefetchAttempt(job *batchv1.Job) int {
	n, err := strconv.Atoi(job.Annotations[render.AttemptAnnotation])
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// prefetchJobFor finds an artifact's download job by label, not by reconstructing its name.
//
// By label because the name is a rendering and not a key. It leads with the model now and it did
// not before, so a lookup that rebuilt the string would see none of the jobs already running on a
// live cluster, and the consequences are not cosmetic. ensurePrefetch would create a second job
// pulling the same weights into the same shared cache, which is the concurrent download the job
// exists to prevent; reportResidency and weightsResident would call weights absent that are on
// disk, and hold every deployment of them behind a fetch that already finished.
//
// One artifact must resolve to one job on every pass, and this operator can inherit a state it does
// not create: a rename, a hand-run pull, a job restored from a backup. A succeeded job therefore
// wins over any other, because completed weights are the fact worth reporting whatever else is in
// flight, and the oldest wins among equals. Creation timestamps are only second-granular, so the
// name breaks a tie, not list order: residency must not flicker between two answers on
// alternate reconciles.
func (r *SyncedResourceReconciler) prefetchJobFor(ctx context.Context, artifactID string) (*batchv1.Job, error) {
	var jobs batchv1.JobList
	if err := r.Client.List(ctx, &jobs,
		client.InNamespace(r.Render.Namespace),
		client.MatchingLabels{render.ArtifactLabel: artifactID},
	); err != nil {
		return nil, fmt.Errorf("read prefetch job for %s: %w", artifactID, err)
	}

	var best *batchv1.Job
	for i := range jobs.Items {
		if job := &jobs.Items[i]; best == nil || prefetchOutranks(job, best) {
			best = job
		}
	}
	return best, nil
}

// prefetchOutranks orders two jobs claiming one artifact: succeeded first, then whichever has not
// failed, then oldest, then name.
//
// The failed test comes before the age test and not after it, because the one moment two jobs
// legitimately share an artifact is a restart, where the outgoing one is both older and dead. Age
// alone would answer with the corpse: reporting the artifact as failed for as long as the delete
// took, and handing ensurePrefetch back the same job it had just replaced.
func prefetchOutranks(job, over *batchv1.Job) bool {
	if (job.Status.Succeeded > 0) != (over.Status.Succeeded > 0) {
		return job.Status.Succeeded > 0
	}
	if failed, otherFailed := prefetchFailure(job) != "", prefetchFailure(over) != ""; failed != otherFailed {
		return !failed
	}
	if !job.CreationTimestamp.Equal(&over.CreationTimestamp) {
		return job.CreationTimestamp.Before(&over.CreationTimestamp)
	}
	return job.Name < over.Name
}

// runtimeImage finds the image to fetch an artifact with.
//
// It is the engine image of a deployment that serves the artifact, not a general-purpose
// downloader, because the cache layout a download produces has to be the one the engine later
// expects to read. A different version of the same client can lay the same weights out
// differently, and the symptom is not an error: it is every worker silently re-downloading what
// is already on disk.
//
// An artifact in the warm-set that no deployment on this cluster serves has no such image to
// borrow, and there is no safe default, so it waits. That is the accurate answer: the cluster was
// told to hold weights and not told what will read them.
func (r *SyncedResourceReconciler) runtimeImage(ctx context.Context, artifactID string) (string, error) {
	var list v1alpha1.SyncedResourceList
	if err := r.Client.List(ctx, &list, client.InNamespace(r.Namespace)); err != nil {
		return "", fmt.Errorf("list declared state: %w", err)
	}
	for i := range list.Items {
		if list.Items[i].Spec.Kind != "deployment" {
			continue
		}
		var dep wire.DeploymentDesired
		if err := decodePayload(&list.Items[i], &dep); err != nil {
			r.Log.Warn("skipping undecodable deployment", "name", list.Items[i].Name, "error", err)
			continue
		}
		if !slices.Contains(dep.ArtifactIDs, artifactID) {
			continue
		}
		if image := dep.Serving.RuntimeImage(); image != nil && *image != "" {
			return *image, nil
		}
	}
	return "", nil
}

// reportResidency tells the plane whether one artifact's weights are on this cluster yet.
//
// Read from the prefetch Job, which is the same source the deployment path gates on. A count kept
// alongside it would be a second copy of a fact the API server already holds, and the two would
// disagree the first time a Job was deleted by hand. So residency has one reading here, whoever
// is asking.
//
// Reported at the cluster tier with no per-node rows, because the fetch lands on one shared
// volume: there is no node this is true of and not true of. bytesResident is the artifact's
// declared size once the Job has completed, which is what "the whole thing is on disk" means for
// a download that either finishes or does not.
//
// Failures are swallowed for the same reason the deployment report's are: this is a progress
// report, and losing one must never turn a reconcile that otherwise succeeded into a retry.
func (r *SyncedResourceReconciler) reportResidency(ctx context.Context, artifactID string, size int64, reason, message string) {
	reporter := r.reporter()
	if reporter == nil {
		return
	}

	job, err := r.prefetchJobFor(ctx, artifactID)
	if err != nil {
		r.Log.Warn("could not read prefetch job for residency", "artifact", artifactID, "error", err)
		return
	}

	state, bytes, failure := "absent", int64(0), ""
	switch {
	case job == nil:
	case job.Status.Succeeded > 0:
		state, bytes = "resident", size
	default:
		// A Job that has given up stays `absent`, because absent is what the weights are. Only
		// the reason separates it from one that has not started, and that is the whole repair:
		// this arm used to fold a failed download into `fetching`, so a cluster that had stopped
		// trying went on telling the plane the download was running. Nothing was going to
		// contradict it, and the deployment behind it waited on a completion that could not come.
		//
		// Failed pods still read as `fetching` while the Job has attempts left, because it is
		// still fetching: the condition below, and not a pod count, is what says it has stopped.
		failure = prefetchFailure(job)
		if failure == "" && (job.Status.Active > 0 || job.Status.Failed > 0) {
			state = "fetching"
		}
	}

	if reason == "" {
		switch {
		case state == "resident":
			reason, message = "Resident", "the weights are on this cluster"
		case state == "fetching":
			reason, message = "Fetching", "the download is running"
		case failure != "":
			reason, message = "PrefetchFailed", "the download failed and has not been started again yet: "+failure
		default:
			reason, message = "PrefetchPending", "the download has not started yet"
		}
	}

	key2 := reportKey{phase: state + "/" + reason, message: strconv.FormatInt(bytes, 10)}
	if r.lastReported(artifactID) == key2 {
		return
	}

	payload := wire.CacheObserved{
		ClusterID:     r.ClusterID,
		ArtifactID:    artifactID,
		State:         state,
		Resident:      state == "resident",
		BytesResident: bytes,
		Nodes:         []wire.CacheNodeResidency{},
		Reason:        reason,
		Message:       message,
		ObservedAt:    time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := reporter.Send(TypeCacheObserved, artifactID, payload); err != nil {
		r.Log.Warn("could not report cache residency", "artifact", artifactID, "error", err)
		return
	}
	r.markReported(artifactID, key2)
}
