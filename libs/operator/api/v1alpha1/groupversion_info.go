// Package v1alpha1 contains SyncedResource, the operator's durable local copy of the desired state
// the plane sends down the hub stream.
//
// v1alpha1 has no conversion webhook and no stored-version migration. A change to the schema or
// the group before the first stable version means reinstalling the CRD (see the operator README).
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
	// GroupVersion is the group/version of SyncedResource.
	GroupVersion = schema.GroupVersion{Group: api.Group, Version: "v1alpha1"}

	// SchemeBuilder registers the types in this package with a runtime scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the types in this package to a runtime scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
