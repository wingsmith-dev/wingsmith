package reconcile

import (
	"context"
	"fmt"
	"strconv"
	"time"

	batchv1 "k8s.io/api/batch/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

// reconcileCache makes the cluster's declared warm-set resident.
//
// Fetching is driven by the warm-set rather than by any deployment, and that inversion is the
// whole point. Weights are per-cluster and deployments are many, so a fetch owned by a deployment
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
			// arrives; this one ends only when somebody gives a recipe an engine image, so the
			// reason has to travel or the deployment waits forever on a silent cluster.
			r.reportResidency(ctx, entry.ArtifactID, artifact.SizeBytes, "NoRuntimeImage",
				fmt.Sprintf("no recipe on this cluster serving %s names an engine image to fetch it with; "+
					"the download cannot start until one does", entry.ArtifactID))
			continue
		}
		if err := r.ensurePrefetch(ctx, res, *artifact, image); err != nil {
			return ctrl.Result{}, err
		}
		r.reportResidency(ctx, entry.ArtifactID, artifact.SizeBytes, "", "")
	}

	if pending > 0 {
		return r.wait(ctx, res, "WarmSetIncomplete",
			fmt.Sprintf("%d of %d warm-set entries are not yet fetchable", pending, len(warm.Entries)))
	}
	return r.ready(ctx, res, "WarmSetDispatched",
		fmt.Sprintf("%d artifacts have a prefetch job", len(warm.Entries)))
}

// ensurePrefetch creates an artifact's download job if it does not already exist.
//
// Create rather than apply, and never an update. A Job's pod template is immutable once created,
// so a server-side apply that differed in any field would be rejected by the API server on every
// pass; and re-running a completed download to close a cosmetic difference would re-pull tens of
// gigabytes for nothing. An artifact is content-addressed, so a job that succeeded for one is
// still correct.
func (r *SyncedResourceReconciler) ensurePrefetch(ctx context.Context, owner *v1alpha1.SyncedResource, art wire.ArtifactDesired, image string) error {
	key := types.NamespacedName{Namespace: r.Render.Namespace, Name: render.PrefetchJobName(art.ArtifactID)}
	var existing batchv1.Job
	switch err := r.Client.Get(ctx, key, &existing); {
	case err == nil:
		return nil
	case !apierrors.IsNotFound(err):
		return fmt.Errorf("read prefetch job for %s: %w", art.ArtifactID, err)
	}

	job, err := render.PrefetchJob(art, image, r.Render)
	if err != nil {
		return fmt.Errorf("render prefetch job for %s: %w", art.ArtifactID, err)
	}
	// Labelled with its declared origin for the same reason the graph deployment is: the Job is
	// created in the workload namespace and the warm-set that asked for it is not, so the watch
	// that wakes this loop when the download finishes has nothing else to follow back.
	render.OwnedMeta(job, owner)
	if err := r.Client.Create(ctx, job); err != nil && !apierrors.IsAlreadyExists(err) {
		return fmt.Errorf("create prefetch job for %s: %w", art.ArtifactID, err)
	}
	r.Log.Info("fetching weights", "artifact", art.ArtifactID, "job", key.Name)
	return nil
}

// runtimeImage finds the image to fetch an artifact with.
//
// It is a recipe's engine image, not a general-purpose downloader, because the cache layout a
// download produces has to be the one the engine later expects to read. A different version of the
// same client can lay the same weights out differently, and the symptom is not an error — it is
// every worker silently re-downloading what is already on disk.
//
// An artifact in the warm-set that no recipe serves has no such image to borrow, and there is no
// safe default, so it waits. That is the honest answer: the cluster was told to hold weights and
// not told what will read them.
func (r *SyncedResourceReconciler) runtimeImage(ctx context.Context, artifactID string) (string, error) {
	var list v1alpha1.SyncedResourceList
	if err := r.Client.List(ctx, &list, client.InNamespace(r.Namespace)); err != nil {
		return "", fmt.Errorf("list declared state: %w", err)
	}
	for i := range list.Items {
		if list.Items[i].Spec.Kind != "recipe" {
			continue
		}
		var recipe wire.RecipeDesired
		if err := decodePayload(&list.Items[i], &recipe); err != nil {
			r.Log.Warn("skipping undecodable recipe", "name", list.Items[i].Name, "error", err)
			continue
		}
		if recipe.ArtifactID == artifactID && recipe.EngineImage != nil && *recipe.EngineImage != "" {
			return *recipe.EngineImage, nil
		}
	}
	return "", nil
}

// reportResidency tells the plane whether one artifact's weights are on this cluster yet.
//
// Read from the prefetch Job, which is the same source the deployment path gates on. A count kept
// alongside it would be a second copy of a fact the API server already holds, and the two would
// disagree the first time a Job was deleted by hand — so residency has one reading here, whoever
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

	state, bytes := "absent", int64(0)
	var job batchv1.Job
	key := types.NamespacedName{Namespace: r.Render.Namespace, Name: render.PrefetchJobName(artifactID)}
	switch err := r.Client.Get(ctx, key, &job); {
	case apierrors.IsNotFound(err):
	case err != nil:
		r.Log.Warn("could not read prefetch job for residency", "artifact", artifactID, "error", err)
		return
	case job.Status.Succeeded > 0:
		state, bytes = "resident", size
	case job.Status.Active > 0 || job.Status.Failed > 0:
		state = "fetching"
	}

	if reason == "" {
		switch state {
		case "resident":
			reason, message = "Resident", "the weights are on this cluster"
		case "fetching":
			reason, message = "Fetching", "the download is running"
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
