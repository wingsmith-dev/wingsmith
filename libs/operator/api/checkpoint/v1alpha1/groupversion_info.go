// Package v1alpha1 contains StreamCheckpoint, the operator's durable resume cursor for the hub
// stream.
//
// It shares the operator's API group with SyncedResource but lives in a package of its own: the
// cursor is transport bookkeeping written only by the sync loop, never declared state from the
// plane, and keeping the types apart keeps the two from being mistaken for each other.
//
// +kubebuilder:object:generate=true
// +groupName=wingsmith.dev
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"

	"github.com/wingsmith-dev/wingsmith/libs/operator/api"
)

var (
	// GroupVersion is the group/version of StreamCheckpoint.
	GroupVersion = schema.GroupVersion{Group: api.Group, Version: "v1alpha1"}

	// SchemeBuilder registers the checkpoint types with a runtime scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the checkpoint types to a runtime scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
