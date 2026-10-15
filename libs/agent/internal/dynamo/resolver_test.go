package dynamo

import (
	"context"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	corev1client "k8s.io/client-go/kubernetes/typed/core/v1"
	k8stesting "k8s.io/client-go/testing"
)

const testNamespace = "hangar-serving"

func frontendService(name string, ports ...corev1.ServicePort) *corev1.Service {
	return service(name, map[string]string{ComponentLabel: componentFrontend}, ports...)
}

func service(name string, selector map[string]string, ports ...corev1.ServicePort) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: testNamespace},
		Spec:       corev1.ServiceSpec{Selector: selector, Ports: ports},
	}
}

func services(t *testing.T, objects ...runtime.Object) corev1client.ServiceInterface {
	t.Helper()
	return fake.NewClientset(objects...).CoreV1().Services(testNamespace)
}

func TestDiscovery(t *testing.T) {
	cases := []struct {
		name     string
		endpoint string
		objects  []runtime.Object
		want     string
		wantErr  error
	}{
		{
			name:     "an explicit endpoint wins and discovery never runs",
			endpoint: "http://inference-frontend-frontend:8000",
			objects:  []runtime.Object{frontendService("qwen3-agg-frontend")},
			want:     "http://inference-frontend-frontend:8000",
		},
		{
			name:     "an explicit endpoint may be a bare host and port",
			endpoint: "inference.svc:9000",
			want:     "http://inference.svc:9000",
		},
		{
			name:    "the service is found by dynamo's component label, not by name",
			objects: []runtime.Object{frontendService("qwen3-8-27b-agg-frontend")},
			want:    "http://qwen3-8-27b-agg-frontend.hangar-serving.svc.cluster.local:8000",
		},
		{
			name: "the port named http is preferred over the ones that are not",
			objects: []runtime.Object{frontendService("graph-frontend",
				corev1.ServicePort{Name: "metrics", Port: 9090},
				corev1.ServicePort{Name: "http", Port: 8080},
			)},
			want: "http://graph-frontend.hangar-serving.svc.cluster.local:8080",
		},
		{
			name: "a single unnamed port is the http port",
			objects: []runtime.Object{frontendService("graph-frontend",
				corev1.ServicePort{Port: 3000},
			)},
			want: "http://graph-frontend.hangar-serving.svc.cluster.local:3000",
		},
		{
			name: "services that do not select the frontend component are ignored",
			objects: []runtime.Object{
				service("etcd", map[string]string{"app": "etcd"}),
				service("worker", map[string]string{ComponentLabel: "Worker"}),
			},
			wantErr: ErrNoFrontend,
		},
		{
			name:    "no frontend at all is not an address",
			wantErr: ErrNoFrontend,
		},
		{
			name: "two frontends is an error rather than a guess",
			objects: []runtime.Object{
				frontendService("one-frontend"),
				frontendService("two-frontend"),
			},
			wantErr: errAmbiguous,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			resolver, err := New(Config{
				Endpoint:  tc.endpoint,
				Namespace: testNamespace,
				Services:  services(t, tc.objects...),
			})
			if err != nil {
				t.Fatalf("new: %v", err)
			}

			refreshErr := resolver.Refresh(context.Background())
			switch {
			case tc.wantErr == nil && refreshErr != nil:
				t.Fatalf("refresh: %v", refreshErr)
			case errors.Is(tc.wantErr, ErrNoFrontend):
				if !errors.Is(refreshErr, ErrNoFrontend) {
					t.Fatalf("refresh err = %v, want one wrapping ErrNoFrontend", refreshErr)
				}
			case tc.wantErr == errAmbiguous:
				if refreshErr == nil {
					t.Fatal("refresh succeeded, want an error naming both frontends")
				}
				for _, name := range []string{"one-frontend", "two-frontend"} {
					if !strings.Contains(refreshErr.Error(), name) {
						t.Fatalf("refresh err = %q, want it to name %s", refreshErr, name)
					}
				}
			}

			got, err := resolver.URL()
			if tc.wantErr != nil {
				if !errors.Is(err, ErrNoFrontend) {
					t.Fatalf("url err = %v, want ErrNoFrontend while nothing is known", err)
				}
				return
			}
			if err != nil {
				t.Fatalf("url: %v", err)
			}
			if got.String() != tc.want {
				t.Fatalf("url = %q, want %q", got, tc.want)
			}
		})
	}
}

// errAmbiguous is a test-only marker for the two-frontends case, which produces
// a message rather than a sentinel: there is nothing a caller can do with it
// programmatically, and the names are the useful part.
var errAmbiguous = errors.New("ambiguous")

// TestTheAddressFollowsTheServiceWhenAGraphIsRedeployed is the case the label
// lookup exists for: a redeploy replaces the frontend Service under a new name,
// and nothing is reconfigured.
func TestTheAddressFollowsTheServiceWhenAGraphIsRedeployed(t *testing.T) {
	ctx := context.Background()
	client := fake.NewClientset(frontendService("old-graph-frontend"))
	resolver, err := New(Config{Namespace: testNamespace, Services: client.CoreV1().Services(testNamespace)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	if err := resolver.Refresh(ctx); err != nil {
		t.Fatalf("first refresh: %v", err)
	}
	first, _ := resolver.URL()
	if first.Host != "old-graph-frontend.hangar-serving.svc.cluster.local:8000" {
		t.Fatalf("first host = %q", first.Host)
	}

	if err := client.CoreV1().Services(testNamespace).Delete(ctx, "old-graph-frontend", metav1.DeleteOptions{}); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := client.CoreV1().Services(testNamespace).Create(ctx, frontendService("new-graph-frontend"), metav1.CreateOptions{}); err != nil {
		t.Fatalf("create: %v", err)
	}

	if err := resolver.Refresh(ctx); err != nil {
		t.Fatalf("second refresh: %v", err)
	}
	second, _ := resolver.URL()
	if second.Host != "new-graph-frontend.hangar-serving.svc.cluster.local:8000" {
		t.Fatalf("second host = %q, want the redeployed service", second.Host)
	}
}

// TestAFailedLookupKeepsTheAddressAlreadyLoaded pins the reason discovery is off
// the request path. An API server that is unreachable is not an inference
// outage, and the frontend it last pointed at is still answering.
func TestAFailedLookupKeepsTheAddressAlreadyLoaded(t *testing.T) {
	ctx := context.Background()
	client := fake.NewClientset(frontendService("graph-frontend"))
	resolver, err := New(Config{Namespace: testNamespace, Services: client.CoreV1().Services(testNamespace)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(ctx); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	client.PrependReactor("list", "services", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("the api server is gone")
	})
	if err := resolver.Refresh(ctx); err == nil {
		t.Fatal("refresh succeeded against an api server that is down")
	}

	kept, err := resolver.URL()
	if err != nil {
		t.Fatalf("url after a failed refresh: %v", err)
	}
	if kept.Host != "graph-frontend.hangar-serving.svc.cluster.local:8000" {
		t.Fatalf("host = %q, want the last known address", kept.Host)
	}
}

// TestAnExplicitEndpointNeedsNoCluster is what lets the agent run where the
// frontend is not a Dynamo graph at all.
func TestAnExplicitEndpointNeedsNoCluster(t *testing.T) {
	resolver, err := New(Config{Endpoint: "http://vllm:8000"})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	got, err := resolver.URL()
	if err != nil {
		t.Fatalf("url: %v", err)
	}
	if got.String() != "http://vllm:8000" {
		t.Fatalf("url = %q", got)
	}

	// Run must not block a caller that started it unconditionally.
	done := make(chan struct{})
	go func() { resolver.Run(context.Background()); close(done) }()
	<-done
}

func TestWithoutAnEndpointOrAClientTheResolverRefusesToStart(t *testing.T) {
	if _, err := New(Config{Namespace: testNamespace}); err == nil {
		t.Fatal("new succeeded with neither an endpoint nor a client")
	}
	if _, err := New(Config{Endpoint: "http://%zz"}); err == nil {
		t.Fatal("new accepted an endpoint that is not a URL")
	}
}

// deploymentFrontend is a frontend Service labelled the way Dynamo labels the
// Service it derives from a component whose pod template the operator stamped:
// the ownership labels land on the Service's own metadata.
func deploymentFrontend(name, deploymentID string, ports ...corev1.ServicePort) *corev1.Service {
	svc := frontendService(name, ports...)
	svc.Labels = map[string]string{DeploymentIDLabel: deploymentID}
	return svc
}

// TestEachDeploymentsFrontendIsFoundByItsLabel is the whole mechanism. Nothing
// polls anything for a list of models: the operator puts the deployment id on
// the frontend's pod template, Dynamo copies it onto the Service, and the id the
// gateway already resolved is the key that reads it back.
func TestEachDeploymentsFrontendIsFoundByItsLabel(t *testing.T) {
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t,
		frontendService("inference-frontend"),
		deploymentFrontend("kimi-k3-site-c-frontend", "dep_01j9x2"),
		deploymentFrontend("qwen3-32b-frontend", "dep_01k4b7"),
	)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	for deployment, want := range map[string]string{
		"dep_01j9x2": "http://kimi-k3-site-c-frontend.hangar-serving.svc.cluster.local:8000",
		"dep_01k4b7": "http://qwen3-32b-frontend.hangar-serving.svc.cluster.local:8000",
		// A deployment this cluster renders no frontend for, and a request that
		// names none at all, both land on the shared frontend. That fallback is
		// the migration: it is what every request does today.
		"dep_never_heard_of": "http://inference-frontend.hangar-serving.svc.cluster.local:8000",
		"":                   "http://inference-frontend.hangar-serving.svc.cluster.local:8000",
	} {
		got, err := resolver.URLFor(deployment)
		if err != nil {
			t.Fatalf("URLFor(%q): %v", deployment, err)
		}
		if got.String() != want {
			t.Fatalf("URLFor(%q) = %q, want %q", deployment, got, want)
		}
	}

	shared, err := resolver.URL()
	if err != nil {
		t.Fatalf("URL: %v", err)
	}
	if shared.Host != "inference-frontend.hangar-serving.svc.cluster.local:8000" {
		t.Fatalf("URL = %q, want the frontend no deployment owns", shared.Host)
	}
}

// TestTheLabelIsReadWhereverDynamoPutIt covers both placements of the same copy.
// Reading only one of them and being wrong would send every request to the
// shared frontend, which is a feature that does nothing with nothing anywhere
// reporting a failure — the one outcome worth spending a branch to avoid.
func TestTheLabelIsReadWhereverDynamoPutIt(t *testing.T) {
	onTheSelector := frontendService("selector-labelled-frontend")
	onTheSelector.Spec.Selector[DeploymentIDLabel] = "dep_selector"

	// The shared frontend is in the fixture so that a placement this misses
	// cannot pass by falling back onto an address that happens to be the right
	// one — which is exactly how a per-deployment lookup fails: quietly, on the
	// shared frontend, answering.
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t,
		frontendService("inference-frontend"),
		deploymentFrontend("metadata-labelled-frontend", "dep_metadata"),
		onTheSelector,
	)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	for deployment, want := range map[string]string{
		"dep_metadata": "metadata-labelled-frontend.hangar-serving.svc.cluster.local:8000",
		"dep_selector": "selector-labelled-frontend.hangar-serving.svc.cluster.local:8000",
	} {
		got, err := resolver.URLFor(deployment)
		if err != nil {
			t.Fatalf("URLFor(%q): %v", deployment, err)
		}
		if got.Host != want {
			t.Fatalf("URLFor(%q) = %q, want %q", deployment, got.Host, want)
		}
	}
}

// TestOneSharedFrontendAnswersEveryDeployment is what lets this ship dark. A
// cluster running exactly what every cluster runs today has one unlabelled
// frontend, and every question — with a deployment id, without one, with one
// nobody has heard of — has to produce the address it produced before there was
// a set at all.
func TestOneSharedFrontendAnswersEveryDeployment(t *testing.T) {
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t,
		frontendService("inference-frontend"),
	)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	want := "http://inference-frontend.hangar-serving.svc.cluster.local:8000"
	shared, err := resolver.URL()
	if err != nil {
		t.Fatalf("URL: %v", err)
	}
	if shared.String() != want {
		t.Fatalf("URL = %q, want %q", shared, want)
	}
	for _, deployment := range []string{"", "dep_01j9x2", "dep_anything"} {
		got, err := resolver.URLFor(deployment)
		if err != nil {
			t.Fatalf("URLFor(%q): %v", deployment, err)
		}
		if got.String() != want {
			t.Fatalf("URLFor(%q) = %q, want the one frontend this cluster runs", deployment, got)
		}
	}
}

// TestADeploymentWithNoFrontendAndNoFallbackIsItsOwnRefusal keeps two facts
// apart that would otherwise wear one error. A cluster serving nothing and a
// cluster serving everything except this one model are different incidents:
// the first is every model on the cluster, the second is one deployment that
// has not finished coming up, and a reader who cannot tell them apart chases
// the wrong one.
func TestADeploymentWithNoFrontendAndNoFallbackIsItsOwnRefusal(t *testing.T) {
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t,
		deploymentFrontend("kimi-k3-site-c-frontend", "dep_01j9x2"),
	)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(context.Background()); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	if _, err := resolver.URLFor("dep_01j9x2"); err != nil {
		t.Fatalf("URLFor the deployment that has a frontend: %v", err)
	}

	_, err = resolver.URLFor("dep_still_starting")
	if !errors.Is(err, ErrNoDeploymentFrontend) {
		t.Fatalf("URLFor an unrendered deployment = %v, want ErrNoDeploymentFrontend", err)
	}
	if errors.Is(err, ErrNoFrontend) {
		t.Fatal("the two refusals must not be the same sentinel; a caller has to tell them apart")
	}
	if !strings.Contains(err.Error(), "dep_still_starting") {
		t.Fatalf("err = %q, want it to name the deployment", err)
	}

	if _, err := resolver.URL(); !errors.Is(err, ErrNoFrontend) {
		t.Fatalf("URL with no shared frontend = %v, want ErrNoFrontend", err)
	}
}

// TestBeforeAnythingIsDiscoveredTheRefusalIsAboutTheCluster is the cold start,
// and it is the one moment the two refusals are trivial to swap. Nothing has
// been found yet, so there is no shared frontend to fall back to and no
// per-deployment one to match — which reads locally like the deployment case and
// is in fact the cluster case. An agent between starting and its first discovery
// pass, or one whose first pass met an API server that was down, would otherwise
// answer every caller for every model with "this model is mid-rollout, retry"
// while the truth is that this cluster is serving nothing at all, and the
// distinction is the entire reason there are two sentinels.
func TestBeforeAnythingIsDiscoveredTheRefusalIsAboutTheCluster(t *testing.T) {
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	for _, when := range []string{"before the first pass", "after a pass that found nothing"} {
		for _, deployment := range []string{"", "dep_01j9x2"} {
			_, err := resolver.URLFor(deployment)
			if !errors.Is(err, ErrNoFrontend) {
				t.Fatalf("%s, URLFor(%q) = %v, want ErrNoFrontend", when, deployment, err)
			}
		}
		if err := resolver.Refresh(context.Background()); err == nil {
			t.Fatal("refresh succeeded against a cluster with no frontend in it")
		}
	}
}

// TestTwoFrontendsClaimingOneDeploymentIsStillAnErrorRatherThanAGuess is the
// half of the old refusal that survives. Plurality is answerable now because a
// Service says which deployment it fronts; two Services saying the same thing
// is the original ambiguity with none of the new information.
func TestTwoFrontendsClaimingOneDeploymentIsStillAnErrorRatherThanAGuess(t *testing.T) {
	resolver, err := New(Config{Namespace: testNamespace, Services: services(t,
		frontendService("inference-frontend"),
		deploymentFrontend("first-frontend", "dep_01j9x2"),
		deploymentFrontend("second-frontend", "dep_01j9x2"),
	)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}

	refreshErr := resolver.Refresh(context.Background())
	if refreshErr == nil {
		t.Fatal("refresh succeeded with two frontends claiming one deployment")
	}
	for _, want := range []string{"first-frontend", "second-frontend", "dep_01j9x2"} {
		if !strings.Contains(refreshErr.Error(), want) {
			t.Fatalf("refresh err = %q, want it to name %s", refreshErr, want)
		}
	}
	if !errors.Is(refreshErr, ErrAmbiguousFrontend) {
		t.Errorf("refresh err = %v, want ErrAmbiguousFrontend", refreshErr)
	}
	// The shared frontend in the same pass is still served, and the conflicted
	// deployment is refused rather than sent to it.
	if got, err := resolver.URL(); err != nil || !strings.Contains(got.Host, "inference-frontend") {
		t.Fatalf("url = %v, %v, want the shared frontend from the same pass", got, err)
	}
	if _, err := resolver.URLFor("dep_01j9x2"); !errors.Is(err, ErrNoDeploymentFrontend) {
		t.Fatalf("url for the conflicted deployment = %v, want ErrNoDeploymentFrontend", err)
	}
}

// TestOneConflictedDeploymentDoesNotStallTheOthers pins that a conflict is
// scoped to its own key. A deployment that gains a second frontend keeps the
// address it had, and every other deployment, including one that moved in the
// same pass, follows the cluster.
func TestOneConflictedDeploymentDoesNotStallTheOthers(t *testing.T) {
	ctx := context.Background()
	client := services(t,
		deploymentFrontend("a-frontend", "dep_a"),
		deploymentFrontend("b-frontend", "dep_b"),
	)
	resolver, err := New(Config{Namespace: testNamespace, Services: client})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(ctx); err != nil {
		t.Fatalf("first refresh: %v", err)
	}

	// dep_a gains a second frontend; dep_b is redeployed under a new name.
	if _, err := client.Create(ctx, deploymentFrontend("a-second-frontend", "dep_a"), metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := client.Delete(ctx, "b-frontend", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Create(ctx, deploymentFrontend("b-new-frontend", "dep_b"), metav1.CreateOptions{}); err != nil {
		t.Fatal(err)
	}

	if err := resolver.Refresh(ctx); !errors.Is(err, ErrAmbiguousFrontend) {
		t.Fatalf("second refresh = %v, want ErrAmbiguousFrontend", err)
	}
	if got, err := resolver.URLFor("dep_b"); err != nil || !strings.HasPrefix(got.Host, "b-new-frontend.") {
		t.Errorf("dep_b = %v, %v, want the new frontend from this pass", got, err)
	}
	if got, err := resolver.URLFor("dep_a"); err != nil || !strings.HasPrefix(got.Host, "a-frontend.") {
		t.Errorf("dep_a = %v, %v, want the address it had before the conflict", got, err)
	}
}

// TestAFailedLookupKeepsThePerDeploymentFrontendsToo extends the reason
// discovery is off the request path to the set. Keeping only the shared address
// across an API-server outage would silently move every per-model request onto
// the shared frontend — which answers, with a model-not-found for a model that
// is running perfectly well.
func TestAFailedLookupKeepsThePerDeploymentFrontendsToo(t *testing.T) {
	ctx := context.Background()
	client := fake.NewClientset(
		frontendService("inference-frontend"),
		deploymentFrontend("kimi-k3-site-c-frontend", "dep_01j9x2"),
	)
	resolver, err := New(Config{Namespace: testNamespace, Services: client.CoreV1().Services(testNamespace)})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	if err := resolver.Refresh(ctx); err != nil {
		t.Fatalf("refresh: %v", err)
	}

	client.PrependReactor("list", "services", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, nil, errors.New("the api server is gone")
	})
	if err := resolver.Refresh(ctx); err == nil {
		t.Fatal("refresh succeeded against an api server that is down")
	}

	kept, err := resolver.URLFor("dep_01j9x2")
	if err != nil {
		t.Fatalf("URLFor after a failed refresh: %v", err)
	}
	if kept.Host != "kimi-k3-site-c-frontend.hangar-serving.svc.cluster.local:8000" {
		t.Fatalf("host = %q, want the deployment's own frontend", kept.Host)
	}
}

// TestAnExplicitEndpointAnswersForEveryDeployment keeps the override absolute. A
// cluster that fronts its models with something Dynamo never labelled has one
// address and no labels to read, and a per-deployment question must not turn
// that into a refusal.
func TestAnExplicitEndpointAnswersForEveryDeployment(t *testing.T) {
	resolver, err := New(Config{Endpoint: "http://vllm:8000"})
	if err != nil {
		t.Fatalf("new: %v", err)
	}
	got, err := resolver.URLFor("dep_01j9x2")
	if err != nil {
		t.Fatalf("URLFor: %v", err)
	}
	if got.String() != "http://vllm:8000" {
		t.Fatalf("URLFor = %q, want the override", got)
	}
}
