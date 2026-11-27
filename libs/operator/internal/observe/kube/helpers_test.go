package kube

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/wingsmith-dev/wingsmith/libs/operator/internal/observe"
	syncpkg "github.com/wingsmith-dev/wingsmith/libs/operator/internal/sync"
)

const testCluster = "clu_01j9x2aaaaaaaaaaaaaaaaaaaa"

type frame struct {
	typ     syncpkg.MessageType
	subject string
	payload any
}

type recordingSender struct{ frames []frame }

func (r *recordingSender) Send(t syncpkg.MessageType, subject string, payload any) error {
	_, err := r.SendCorrelated(t, subject, payload, "")
	return err
}

func (r *recordingSender) SendCorrelated(t syncpkg.MessageType, subject string, payload any, _ string) (string, error) {
	r.frames = append(r.frames, frame{typ: t, subject: subject, payload: payload})
	return "id", nil
}

func (r *recordingSender) ofType(t syncpkg.MessageType) []frame {
	var out []frame
	for _, f := range r.frames {
		if f.typ == t {
			out = append(out, f)
		}
	}
	return out
}

func newTestObserver(t *testing.T, d observe.Discoverer) *observe.Observer {
	t.Helper()
	return observe.New(&observe.Observer{
		ClusterID:       testCluster,
		OperatorVersion: "0.4.2",
		Discoverer:      d,
		Sender:          &recordingSender{},
		Interval:        time.Hour,
		Log:             slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
}

func recorder(t *testing.T, o *observe.Observer) *recordingSender {
	t.Helper()
	r, ok := o.Sender.(*recordingSender)
	if !ok {
		t.Fatal("observer is not driving a recording sender")
	}
	return r
}
