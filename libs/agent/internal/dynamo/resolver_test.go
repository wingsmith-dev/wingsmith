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
