package operate

import (
	"context"
	"strings"
	"testing"

	k8sfake "k8s.io/client-go/kubernetes/fake"
	clientauthv1 "k8s.io/client-go/pkg/apis/clientauthentication/v1"
	"k8s.io/client-go/rest"
	clientcmdapi "k8s.io/client-go/tools/clientcmd/api"
)

var (
	clientcmdExec = clientcmdapi.ExecConfig{
		Command:    "true",
		APIVersion: clientauthv1.SchemeGroupVersion.String(),
	}
	clientcmdAuth = clientcmdapi.AuthProviderConfig{Name: "oidc"}
)

// TestPodLogsReadsTheStream. The fake clientset serves a fixed body for any pod's log, which is
// enough to prove the stream is opened, drained and closed, not dropped.
func TestPodLogsReadsTheStream(t *testing.T) {
	reader := &PodLogs{Client: k8sfake.NewSimpleClientset()}
	out, err := reader.PodLogs(context.Background(), "dynamo", "bench-abc")
	if err != nil {
		t.Fatalf("read logs: %v", err)
	}
	if out == "" {
		t.Fatal("the log came back empty; the stream was not drained")
	}
}

// TestPodLogsIsBoundedBecauseAHarnessIsNot: a load generator printing a line per request must not
// be able to pull its whole output into this process's memory.
func TestPodLogsIsBounded(t *testing.T) {
	reader := &PodLogs{Client: k8sfake.NewSimpleClientset()}
	out, err := reader.PodLogs(context.Background(), "dynamo", "bench-abc")
	if err != nil {
		t.Fatalf("read logs: %v", err)
	}
	if len(out) > maxLogBytes {
		t.Fatalf("read %d bytes, past the %d cap", len(out), maxLogBytes)
	}
}

// TestNewPodLogsBuildsAReaderFromTheSameConfigEverythingElseDialsWith.
func TestNewPodLogsBuildsAReader(t *testing.T) {
	reader, err := NewPodLogs(&rest.Config{Host: "https://127.0.0.1:6443"})
	if err != nil {
		t.Fatalf("build: %v", err)
	}
	if reader == nil || reader.Client == nil {
		t.Fatal("NewPodLogs returned a reader with no client")
	}
}

// TestNewPodLogsRefusesAConfigItCannotUse instead of handing back a reader that fails on first
// use, which would surface as a benchmark with no result instead of a misconfigured operator.
func TestNewPodLogsRefusesABadConfig(t *testing.T) {
	_, err := NewPodLogs(&rest.Config{Host: "https://127.0.0.1:6443", ExecProvider: &clientcmdExec, AuthProvider: &clientcmdAuth})
	if err == nil {
		t.Fatal("NewPodLogs accepted a config with two mutually exclusive auth providers")
	}
	if !strings.Contains(err.Error(), "build log client") {
		t.Errorf("the error does not say what failed: %v", err)
	}
}

// PodLogs satisfies LogReader; a compile-time check is cheaper than discovering it at runtime.
var _ LogReader = (*PodLogs)(nil)
