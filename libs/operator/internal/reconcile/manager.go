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

	checkpointv1alpha1 "github.com/hangar-dev/hangar/libs/operator/api/checkpoint/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/api/v1alpha1"
	"github.com/hangar-dev/hangar/libs/operator/internal/config"
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
		LeaderElectionID:        "hangar-operator.placeholder.hangar.dev",
		LeaderElectionNamespace: cfg.Namespace,
		Cache: cache.Options{
			DefaultNamespaces: map[string]cache.Config{cfg.Namespace: {}},
		},
	})
	if err != nil {
		return nil, fmt.Errorf("build manager: %w", err)
	}
	return mgr, nil
}

// SetupWithManager registers every controller this operator runs.
func SetupWithManager(mgr ctrl.Manager, logger *slog.Logger) error {
	r := &SyncedResourceReconciler{
		Client: mgr.GetClient(),
		Scheme: mgr.GetScheme(),
		Log:    logger.With("controller", "syncedresource"),
	}
	return r.SetupWithManager(mgr)
}
