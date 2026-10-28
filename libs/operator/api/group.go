// Package api holds what every API version of the operator's custom resources shares.
package api

// Group is the API group of every custom resource this operator defines: SyncedResource and
// StreamCheckpoint.
//
// It is the one place Go code names the group. Kubebuilder markers cannot reference a constant,
// so the `+groupName` markers in api/v1alpha1 and api/checkpoint/v1alpha1 and the
// `+kubebuilder:rbac` markers in internal/reconcile and internal/checkpoint spell it literally;
// changing the group means changing this line and those markers, then running `make manifests`
// and copying deploy/crd into the chart.
const Group = "wingsmith.dev"
