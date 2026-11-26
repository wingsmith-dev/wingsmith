package reconcile

import (
	"context"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/render"
	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/wire"
)

const tokenProject = "prj_01j9x2pppppppppppppppppppp"

func privateArtifactDesired() wire.ArtifactDesired {
	art := testArtifactDesired()
	art.OwnerProject = tokenProject
	ref := "acme-hf"
	art.Sources[0].CredentialRef = &ref
	return art
}

func sourceSecret(project, token string) *corev1.Secret {
	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name: "acme-hf", Namespace: "wingsmith-system",
			Labels: map[string]string{render.ProjectLabel: render.Name(project)},
		},
		Data: map[string][]byte{render.HFTokenKey: []byte(token)},
	}
}

func projectSecret(t *testing.T, r *SyncedResourceReconciler) (*corev1.Secret, bool) {
	t.Helper()
	var s corev1.Secret
	err := r.Client.Get(context.Background(), render.ProjectTokenSecret(testRender.Namespace, tokenProject), &s)
	return &s, err == nil
}

func TestAPrivateArtifactsTokenIsMaterialisedBeforeItsDownload(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, _, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, privateArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		sourceSecret(tokenProject, "hf_secret"),
	)

	secret, ok := projectSecret(t, r)
	if !ok {
		t.Fatal("no project token Secret was materialised")
	}
	if string(secret.Data[render.HFTokenKey]) != "hf_secret" || secret.Namespace != testRender.Namespace {
		t.Fatalf("secret = %s/%s %v", secret.Namespace, secret.Name, secret.Data)
	}
	job, ok := prefetchJob(t, r.Client, testArtifactID)
	if !ok {
		t.Fatal("no prefetch job")
	}
	from := job.Spec.Template.Spec.Containers[0].EnvFrom
	if len(from) != 1 || from[0].SecretRef.Name != secret.Name {
		t.Fatalf("prefetch envFrom = %+v", from)
	}
}

func TestARotatedTokenIsCopiedAgain(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	key := render.ProjectTokenSecret(testRender.Namespace, tokenProject)
	stale := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace},
		Data:       map[string][]byte{render.HFTokenKey: []byte("old")},
	}
	r, _, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, privateArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
		sourceSecret(tokenProject, "new"), stale,
	)
	if secret, _ := projectSecret(t, r); string(secret.Data[render.HFTokenKey]) != "new" {
		t.Fatalf("token = %q, want the rotated one", secret.Data[render.HFTokenKey])
	}
}

// A missing Secret, or one the admin has not granted to this project, holds the download back
// instead of letting it run anonymously or with somebody else's token.
func TestADownloadWaitsForAUsableToken(t *testing.T) {
	cases := map[string][]client.Object{
		"missing":       {},
		"other project": {sourceSecret("prj_01j9x2qqqqqqqqqqqqqqqqqqqq", "hf_other")},
		"no token":      {sourceSecret(tokenProject, "")},
	}
	for name, extra := range cases {
		t.Run(name, func(t *testing.T) {
			rep := &recordingReporter{}
			cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
			objects := []client.Object{cache,
				declared(t, "artifact", testArtifactID, privateArtifactDesired()),
				declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
			}
			objects = append(objects, extra...)
			r, out, _ := runReconcileWith(t, rep, objects...)

			if _, ok := prefetchJob(t, r.Client, testArtifactID); ok {
				t.Fatal("a prefetch job started without a usable token")
			}
			if _, ok := projectSecret(t, r); ok {
				t.Fatal("a project Secret was materialised from an unusable source")
			}
			if frame := lastCache(t, rep, testArtifactID); frame.Reason != "CredentialUnavailable" || frame.Message == "" {
				t.Fatalf("frame = %s: %s", frame.Reason, frame.Message)
			}
			if reason := conditionReason(out); reason != "WarmSetIncomplete" {
				t.Fatalf("condition = %s", reason)
			}
		})
	}
}

func TestAPublicArtifactNeedsNoProjectSecret(t *testing.T) {
	cache := declared(t, "cache", testCacheID, warmSet(wire.CacheWarmEntry{ArtifactID: testArtifactID}))
	r, _, _ := runReconcile(t, cache,
		declared(t, "artifact", testArtifactID, testArtifactDesired()),
		declared(t, "deployment", testDeploymentID, testDeploymentDesired()),
	)
	var secrets corev1.SecretList
	if err := r.Client.List(context.Background(), &secrets); err != nil || len(secrets.Items) != 0 {
		t.Fatalf("secrets = %d, %v", len(secrets.Items), err)
	}
}
