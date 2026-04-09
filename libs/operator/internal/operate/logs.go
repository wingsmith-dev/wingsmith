package operate

import (
	"context"
	"fmt"
	"io"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

// maxLogBytes bounds what one pod's log may contribute.
//
// A harness that decided to print a line per request would otherwise be able to pull its whole
// output into this process's memory. The cap is generous next to a JSON export and small next to
// anything that would hurt; the sentinels the renderer writes are near the end, so a truncated
// read loses the beginning, which is the human-readable half.
const maxLogBytes = 8 << 20

// The one subresource this operator reads that controller-runtime cannot serve. Without it a
// benchmark job runs to completion and its result is unreachable, so the run reports as failed
// with nothing to say about what it measured.
//
// +kubebuilder:rbac:groups="",resources=pods/log,verbs=get

// PodLogs is the client-go half of LogReader.
//
// A typed clientset and not the controller-runtime client, because logs are a subresource served
// as a stream rather than an object, and controller-runtime deals only in objects. This is the
// only place in the operator that needs one.
type PodLogs struct {
	Client kubernetes.Interface
}

// NewPodLogs builds a log reader from the same REST config everything else dials with.
func NewPodLogs(cfg *rest.Config) (*PodLogs, error) {
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("build log client: %w", err)
	}
	return &PodLogs{Client: clientset}, nil
}

// PodLogs reads a pod's log, truncated at maxLogBytes.
func (p *PodLogs) PodLogs(ctx context.Context, namespace, name string) (string, error) {
	stream, err := p.Client.CoreV1().
		Pods(namespace).
		GetLogs(name, &corev1.PodLogOptions{}).
		Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("open logs for %s/%s: %w", namespace, name, err)
	}
	defer stream.Close()

	body, err := io.ReadAll(io.LimitReader(stream, maxLogBytes))
	if err != nil {
		return "", fmt.Errorf("read logs for %s/%s: %w", namespace, name, err)
	}
	return string(body), nil
}
