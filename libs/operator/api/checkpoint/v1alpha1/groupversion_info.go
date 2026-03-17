// Package v1alpha1 contains the operator's stream-checkpoint API type.
//
// The group is sync.hangar.dev, deliberately distinct from both the eventual domain group
// (hangar.dev) and the throwaway placeholder group (placeholder.hangar.dev). The checkpoint is
// operator-local transport bookkeeping with a lifetime independent of the object model: it must
// outlive the placeholder types being deleted wholesale, and it must never be mistaken for
// declared state written by the plane.
//
// +kubebuilder:object:generate=true
// +groupName=sync.hangar.dev
package v1alpha1

import (
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/scheme"
)

var (
	// GroupVersion is the group/version for the stream-checkpoint API.
	GroupVersion = schema.GroupVersion{Group: "sync.hangar.dev", Version: "v1alpha1"}

	// SchemeBuilder registers the checkpoint types with a runtime scheme.
	SchemeBuilder = &scheme.Builder{GroupVersion: GroupVersion}

	// AddToScheme adds the checkpoint types to a runtime scheme.
	AddToScheme = SchemeBuilder.AddToScheme
)
