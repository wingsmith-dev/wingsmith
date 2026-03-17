// Package v1alpha1 contains the operator's placeholder API types.
//
// The group is deliberately namespaced under placeholder.hangar.dev rather than the real
// hangar.dev group so that this skeleton cannot collide with the domain object model, which
// is owned by a separate track. Every type here is expected to be deleted wholesale once the
// real CRD field sets land.
//
// +kubebuilder:object:generate=true
// +groupName=placeholder.hangar.dev
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// GroupVersion is the group/version for the placeholder API.
	GroupVersion = schema.GroupVersion{Group: "placeholder.hangar.dev", Version: "v1alpha1"}

	// SchemeBuilder registers the placeholder types with a runtime scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the placeholder types to a runtime scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
