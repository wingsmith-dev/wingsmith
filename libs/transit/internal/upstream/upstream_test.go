package upstream

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestParseProfile(t *testing.T) {
	p, err := ParseProfile("50ms", "2ms", "12")
	if err != nil || p.TTFT != 50*time.Millisecond || p.ITL != 2*time.Millisecond || p.Tokens != 12 || p.TokenBytes == 0 {
		t.Fatalf("profile %+v err %v", p, err)
	}
	for _, bad := range [][3]string{{"x", "", ""}, {"", "x", ""}, {"", "", "x"}} {
		if _, err := ParseProfile(bad[0], bad[1], bad[2]); err == nil {
			t.Errorf("ParseProfile(%q) should fail", bad)
		}
	}
}

func TestAStreamCarriesOneChunkPerTokenAndBothStamps(t *testing.T) {
	front := New(Profile{Tokens: 3})
	srv := httptest.NewServer(front.Handler())
	defer srv.Close()

	resp, err := http.Post(srv.URL+"/v1/chat/completions", "application/json", strings.NewReader(`{"model":"m","stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.Header.Get(HeaderReceived) == "" {
		t.Fatal("no receive header")
	}
	content, done := 0, false
	scanner := bufio.NewScanner(resp.Body)
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.Contains(line, `"delta":{"content"`):
			content++
			if !strings.Contains(line, "transit_received_ns") || !strings.Contains(line, "transit_write_ns") {
				t.Fatalf("chunk without both stamps: %s", line)
			}
		case line == "data: [DONE]":
			done = true
		}
	}
	if content != 3 || !done {
		t.Fatalf("content chunks %d done %v", content, done)
	}
	if s := front.Stats(); s.Served != 1 || s.InFlight != 0 {
		t.Fatalf("stats %+v", s)
	}
}

func TestACallerHangingUpIsCountedAsAborted(t *testing.T) {
	front := New(Profile{TTFT: 5 * time.Second})
	srv := httptest.NewServer(front.Handler())
	defer srv.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, srv.URL+"/v1/chat/completions", strings.NewReader(`{"model":"m","stream":true}`))
	if _, err := http.DefaultClient.Do(req); err == nil {
		t.Fatal("want the request cut off")
	}
	deadline := time.Now().Add(2 * time.Second)
	for front.Stats().Aborted != 1 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if s := front.Stats(); s.Aborted != 1 || s.PeakInFlight != 1 {
		t.Fatalf("stats %+v", s)
	}
}
