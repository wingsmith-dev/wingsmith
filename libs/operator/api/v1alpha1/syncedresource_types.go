package v1alpha1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SyncedResourceSpec is the durable local copy of one piece of desired state received from the
// hub. The payload is held opaquely on purpose: the operator skeleton must persist and reconcile
// desired state without knowing the domain schema, which another track defines.
type SyncedResourceSpec struct {
	// Kind is the domain object kind as named on the wire.
	// +kubebuilder:validation:MinLength=1
	Kind string `json:"kind"`

	// Revision is the plane-assigned revision of this desired state. It is monotonic per object
	// and lets the reconciler ignore stale replays after a stream resume.
	// +kubebuilder:validation:Minimum=0
	Revision int64 `json:"revision"`

	// Payload is the domain object exactly as it arrived on the wire.
	// +kubebuilder:pruning:PreserveUnknownFields
	// +optional
	Payload *apiextensionsv1.JSON `json:"payload,omitempty"`
}

// SyncedResourceStatus is the observed half, shipped back up the stream by the sync loop.
type SyncedResourceStatus struct {
	// ObservedRevision is the spec revision the reconciler last acted on.
	// +optional
	ObservedRevision int64 `json:"observedRevision,omitempty"`

	// Phase is a coarse reconcile outcome. It is not an operation: long-running, failable work
	// is modelled separately by the object-model track.
	// +optional
	Phase string `json:"phase,omitempty"`

	// Conditions carries standard reconcile conditions.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:shortName=syncres
// +kubebuilder:printcolumn:name="Kind",type=string,JSONPath=`.spec.kind`
// +kubebuilder:printcolumn:name="Revision",type=integer,JSONPath=`.spec.revision`
// +kubebuilder:printcolumn:name="Observed",type=integer,JSONPath=`.status.observedRevision`

// SyncedResource is the single placeholder CRD for this skeleton.
type SyncedResource struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SyncedResourceSpec   `json:"spec,omitempty"`
	Status SyncedResourceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// SyncedResourceList is a list of SyncedResource.
type SyncedResourceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SyncedResource `json:"items"`
}

func init() {
	SchemeBuilder.Register(&SyncedResource{}, &SyncedResourceList{})
}
