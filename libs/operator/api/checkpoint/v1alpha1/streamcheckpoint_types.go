package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// StreamCheckpointSpec is the durable resume cursor for the operator's hub stream.
//
// It is held in spec, not status because it is written by the sync loop and read by
// nothing else: it is neither declared state from the plane nor observed state from the cluster,
// and a single spec write keeps the three fields mutually consistent in one atomic update.
type StreamCheckpointSpec struct {
	// SessionID is the hub-issued session the cursor belongs to. A cursor is only meaningful
	// within its session, so the two are never stored apart.
	// +kubebuilder:validation:MinLength=1
	SessionID string `json:"sessionID"`

	// AppliedSeq is the highest *contiguous* inbound sequence number the operator has durably
	// applied. It is never advanced across a gap: a hole means every later frame is unapplied,
	// whatever its sequence number.
	// +kubebuilder:validation:Minimum=0
	AppliedSeq int64 `json:"appliedSeq"`

	// Lossy records that the local replay buffer discarded an unacknowledged frame. Once set,
	// the operator must ask for a fresh session, not claim it can resume, and the flag
	// survives a restart so that a crash cannot launder it away.
	// +optional
	Lossy bool `json:"lossy,omitempty"`

	// UpdatedAt is diagnostic only and is never used for ordering or comparison.
	// +optional
	UpdatedAt *metav1.Time `json:"updatedAt,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:shortName=streamcp
// +kubebuilder:printcolumn:name="Session",type=string,JSONPath=`.spec.sessionID`
// +kubebuilder:printcolumn:name="Applied",type=integer,JSONPath=`.spec.appliedSeq`
// +kubebuilder:printcolumn:name="Lossy",type=boolean,JSONPath=`.spec.lossy`

// StreamCheckpoint is the durable resume cursor, stored in the cluster so that it survives an
// operator restart with no control-plane connectivity.
type StreamCheckpoint struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec StreamCheckpointSpec `json:"spec,omitempty"`
}

// +kubebuilder:object:root=true

// StreamCheckpointList is a list of StreamCheckpoint.
type StreamCheckpointList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []StreamCheckpoint `json:"items"`
}

func init() {
	SchemeBuilder.Register(&StreamCheckpoint{}, &StreamCheckpointList{})
}
