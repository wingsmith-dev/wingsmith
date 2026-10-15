// Package reconcile owns the second of the operator's two loops: CRDs -> Kubernetes objects.
//
// The split from internal/sync is load-bearing. The sync loop never creates a Kubernetes object
// and the reconcile loop never touches the stream; the CRDs between them are the durable local
// copy of desired state, which is what lets a cluster keep running through a control-plane
// outage.
package reconcile

import (
	"fmt"
	"log/slog"

	"github.com/go-logr/logr"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/log"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	"github.com/hangar-dev/hangar/libs/operator/api"
	checkpointv1alpha1 "github.com/hangar-dev/hangar/libs/operator/api/checkpoint/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/config"
	"github.com/hangar-dev/hangar/libs/operator/internal/render"
)

// Scheme is the runtime scheme shared by the manager and every client built from it.
func Scheme() (*runtime.Scheme, error) {
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		return nil, err
	}
	if err := v1alpha1.AddToScheme(s); err != nil {
		return nil, err
	}
	if err := checkpointv1alpha1.AddToScheme(s); err != nil {
		return nil, err
	}
	return s, nil
}

// NewManager builds the controller-runtime manager. Health and readiness endpoints are served by
// internal/health rather than the manager so that they stay up while the manager is starting.
func NewManager(cfg *config.Config, logger *slog.Logger) (ctrl.Manager, error) {
	scheme, err := Scheme()
	if err != nil {
		return nil, fmt.Errorf("build scheme: %w", err)
	}
	ctrl.SetLogger(logr.FromSlogHandler(logger.Handler()))
	log.SetLogger(logr.FromSlogHandler(logger.Handler()))

	restCfg, err := ctrl.GetConfig()
	if err != nil {
		return nil, fmt.Errorf("load kubeconfig: %w", err)
	}

	mgr, err := ctrl.NewManager(restCfg, ctrl.Options{
		Scheme:                  scheme,
		Metrics:                 metricsserver.Options{BindAddress: cfg.MetricsAddr},
		LeaderElection:          cfg.LeaderElection,
		LeaderElectionID:        "hangar-operator." + api.Group,
		LeaderElectionNamespace: cfg.Namespace,
		// Safe because the process exits as soon as the manager stops, and every loop that needs
		// the lease runs inside the manager. Releasing on the way out lets the replacement pod
		// take over at once instead of waiting out the lease.
		LeaderElectionReleaseOnCancel: true,
		// Both namespaces, not one. Declared state lives in the operator's own namespace and
		// what it renders lives in the workload namespace, and the reconciler reads back what
		// it wrote — a prefetch job's completion is the gate a deployment waits on. A cache
		// scoped to the first namespace alone would report those objects as absent rather than
		// as unreadable, which is the failure that looks like a logic bug for a day.
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{
				cfg.Namespace:         {},
				cfg.WorkloadNamespace: {},
			},
		},
	})
	if err != nil {
		return nil, fmt.Errorf("build manager: %w", err)
	}
	return mgr, nil
}

// SetupWithManager registers every controller this operator runs and returns the reconciler.
//
// The reconciler is returned rather than kept private because it reports upward as well as
// reconciling downward, and the sync client it reports through does not exist yet at this point —
// it takes the store and the observer as its own options. Binding after construction is how the
// observe loop resolves the same cycle.
func SetupWithManager(mgr ctrl.Manager, cfg *config.Config, logger *slog.Logger) (*SyncedResourceReconciler, error) {
	r := &SyncedResourceReconciler{
		Client:    mgr.GetClient(),
		Reader:    mgr.GetAPIReader(),
		Scheme:    mgr.GetScheme(),
		Log:       logger.With("controller", "syncedresource"),
		Namespace: cfg.Namespace,
		Render:    RenderOptions(cfg),
		ClusterID: cfg.ClusterID,
		// Wired here and nowhere else, so that every test builds a reconciler that asks no
		// frontend anything and the one binary that runs against a real cluster asks.
		Frontend: NewHTTPFrontendModels(),
	}
	if err := r.SetupWithManager(mgr); err != nil {
		return nil, err
	}
	return r, nil
}

// RenderOptions lifts the per-cluster rendering conventions out of configuration.
func RenderOptions(cfg *config.Config) render.Options {
	// Copied rather than pointed at the Config, so that the rendered options cannot be changed
	// by anything that later edits the configuration they came from.
	globalDynamoNamespace := cfg.FrontendGlobalDynamoNamespace
	return render.Options{
		Namespace:           cfg.WorkloadNamespace,
		PoolLabelKey:        cfg.PoolLabelKey,
		GPUPoolValue:        cfg.GPUPoolValue,
		ModelCachePVC:       cfg.ModelCachePVC,
		LocalModelCacheDir:  cfg.LocalModelCacheDir,
		CachePoolValue:      cfg.CachePoolValue,
		CacheTaintKey:       cfg.CacheTaintKey,
		CacheTaintValue:     cfg.CacheTaintValue,
		PrefetchCPU:         cfg.PrefetchCPU,
		PrefetchMemory:      cfg.PrefetchMemory,
		PrefetchMemoryLimit: cfg.PrefetchMemoryLimit,
		MultinodeEnabled:    cfg.Multinode,
		GPUTolerationKey:    cfg.GPUTolerationKey,
		GPUTolerationValue:  cfg.GPUTolerationValue,
		Fabric:              cfg.Fabric,
		FabricDevices:       cfg.FabricDevices,
		FabricResource:      cfg.FabricResource,
		FabricPrivileged:    cfg.FabricPrivileged,
		NIXLBackends:        cfg.NIXLBackends,
		EnginePullSecrets:   cfg.EnginePullSecrets,

		FrontendEnabled:         cfg.FrontendEnabled,
		FrontendImage:           cfg.FrontendImage,
		FrontendReplicas:        cfg.FrontendReplicas,
		FrontendPullSecrets:     cfg.FrontendPullSecrets,
		FrontendCPU:             cfg.FrontendCPU,
		FrontendMemory:          cfg.FrontendMemory,
		FrontendMemoryLimit:     cfg.FrontendMemoryLimit,
		AnthropicAPIEnabled:     cfg.FrontendAnthropicAPI,
		AnthropicStripPreamble:  cfg.FrontendAnthropicStripPreamble,
		FrontendGlobalNamespace: &globalDynamoNamespace,
	}
}
