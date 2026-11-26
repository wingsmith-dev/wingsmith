package reconcile

import (
	"bytes"
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

// errTokenUnavailable is a project token the cluster cannot produce yet: the source Secret is
// missing or holds no token. The download waits instead of running anonymously.
type errTokenUnavailable struct{ reason string }

func (e errTokenUnavailable) Error() string { return e.reason }

// ensureProjectToken materialises a private artifact's Hugging Face token as the project's Secret
// in the workload namespace, which is the only Secret the prefetch job reads.
//
// The plane sends a reference, never the value: the credential's secret_ref names a Secret an
// admin applied in this operator's namespace, labelled for the project, holding the token under
// HF_TOKEN. Reads go to the API server, not the manager's cache, so the operator never lists or
// watches Secrets. An artifact with no credential needs nothing.
func (r *SyncedResourceReconciler) ensureProjectToken(ctx context.Context, art wire.ArtifactDesired) error {
	ref := render.CredentialRef(art)
	if ref == "" {
		return nil
	}
	if errs := validation.IsDNS1123Subdomain(ref); len(errs) > 0 {
		return errTokenUnavailable{fmt.Sprintf("credential reference %q is not a Secret name in %s", ref, r.Namespace)}
	}

	var source corev1.Secret
	err := r.uncached().Get(ctx, types.NamespacedName{Namespace: r.Namespace, Name: ref}, &source)
	if apierrors.IsNotFound(err) {
		return errTokenUnavailable{fmt.Sprintf("the credential's Secret %s/%s does not exist", r.Namespace, ref)}
	}
	if err != nil {
		return fmt.Errorf("read credential Secret %s/%s: %w", r.Namespace, ref, err)
	}
	// The admin grants a Secret to one project by labelling it. Without this a project could name
	// any Secret in this namespace that happens to hold an HF_TOKEN, the cluster's own included.
	if source.Labels[render.ProjectLabel] != render.Name(art.OwnerProject) {
		return errTokenUnavailable{fmt.Sprintf("the credential's Secret %s/%s is not labelled %s=%s",
			r.Namespace, ref, render.ProjectLabel, render.Name(art.OwnerProject))}
	}
	token := source.Data[render.HFTokenKey]
	if len(token) == 0 {
		return errTokenUnavailable{fmt.Sprintf("the credential's Secret %s/%s has no %s key", r.Namespace, ref, render.HFTokenKey)}
	}

	key := render.ProjectTokenSecret(r.Render.Namespace, art.OwnerProject)
	var current corev1.Secret
	err = r.uncached().Get(ctx, key, &current)
	switch {
	case apierrors.IsNotFound(err):
		secret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      key.Name,
				Namespace: key.Namespace,
				Labels: map[string]string{
					render.ManagedByLabel: "wingsmith-operator",
					render.ProjectLabel:   render.Name(art.OwnerProject),
				},
			},
			Type: corev1.SecretTypeOpaque,
			Data: map[string][]byte{render.HFTokenKey: token},
		}
		if err := r.Client.Create(ctx, secret); err != nil && !apierrors.IsAlreadyExists(err) {
			return fmt.Errorf("create project token Secret %s: %w", key, err)
		}
		r.Log.Info("materialised a project token", "project", art.OwnerProject, "secret", key.String())
		return nil
	case err != nil:
		return fmt.Errorf("read project token Secret %s: %w", key, err)
	}

	if bytes.Equal(current.Data[render.HFTokenKey], token) {
		return nil
	}
	current.Data = map[string][]byte{render.HFTokenKey: token}
	if err := r.Client.Update(ctx, &current); err != nil {
		return fmt.Errorf("update project token Secret %s: %w", key, err)
	}
	r.Log.Info("rotated a project token", "project", art.OwnerProject, "secret", key.String())
	return nil
}
