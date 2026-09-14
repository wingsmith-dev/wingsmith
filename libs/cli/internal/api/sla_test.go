package api

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"testing"
	"time"
)

// TestTheSLAReportDecodesExactly holds the wire type to a response the plane's
// own encoder produced, so a field the client invents or misses fails here.
func TestTheSLAReportDecodesExactly(t *testing.T) {
	raw := readFixture(t, "sla-report.json")
	var report SLAReport
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&report); err != nil {
		t.Fatalf("the plane sends a field the client does not have: %v", err)
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	diffKeys(t, "sla-report.json", 0, raw, encoded)

	if report.Met == nil || !*report.Met {
		t.Error("the fixture's verdict did not survive the decode")
	}
	if report.Objective == nil || report.Objective.SuccessRate == nil || *report.Objective.SuccessRate != 0.999 {
		t.Errorf("objective = %+v", report.Objective)
	}
}

func TestTheSLAReportIsAskedOfTheMetricsSurfaceWithThePeriod(t *testing.T) {
	var got *http.Request
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		got = r
		_, _ = w.Write(readFixture(t, "sla-report.json"))
	})
	from := time.Date(2026, 9, 15, 0, 0, 0, 0, time.FixedZone("BRT", -3*3600))
	if _, err := c.EndpointSLA(context.Background(), "kimi/k3", SLAQuery{From: &from}); err != nil {
		t.Fatal(err)
	}
	if got.URL.EscapedPath() != "/v1/metrics/endpoints/kimi%2Fk3/sla" {
		t.Errorf("path = %s", got.URL.EscapedPath())
	}
	if q := got.URL.Query(); q.Get("from") != "2026-09-15T03:00:00Z" || q.Has("to") || q.Has("month") {
		t.Errorf("query = %s: the period is sent in UTC and only what was given is sent", got.URL.RawQuery)
	}
}

func TestAMonthIsSentAsTheMonth(t *testing.T) {
	var query string
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		query = r.URL.RawQuery
		_, _ = w.Write(readFixture(t, "sla-report.json"))
	})
	if _, err := c.EndpointSLA(context.Background(), "chat", SLAQuery{Month: "2026-09"}); err != nil {
		t.Fatal(err)
	}
	if query != "month=2026-09" {
		t.Errorf("query = %q", query)
	}
}
