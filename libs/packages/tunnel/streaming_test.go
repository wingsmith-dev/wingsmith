package tunnel

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	streamCount  = 60
	streamChunks = 8
)

// tailObserver is the shape gateway.observeUsage puts on a response body: every
// Read passes straight through and the bytes are copied into a fixed window
// afterwards, so nothing sits between a token and the caller waiting for it.
type tailObserver struct {
	src    io.ReadCloser
	seen   bytes.Buffer
	done   bool
	report func(tail []byte)
}

func (t *tailObserver) Read(p []byte) (int, error) {
	n, err := t.src.Read(p)
	if n > 0 {
		t.seen.Write(p[:n])
	}
	if err == io.EOF {
		t.finish()
	}
	return n, err
}

func (t *tailObserver) Close() error {
	t.finish()
	return t.src.Close()
}

func (t *tailObserver) finish() {
	if t.done {
		return
	}
	t.done = true
	t.report(t.seen.Bytes())
}

type observedUsage struct {
	PromptTokens     int64 `json:"prompt_tokens"`
	CompletionTokens int64 `json:"completion_tokens"`
}

// TestStreamingArrivesTokenByToken drives sixty concurrent SSE streams through a
// tunnel in lock step: the upstream writes chunk N only after the client has
// read chunk N-1.
//
// Nothing here measures a duration, because a timing assertion on a streaming
// proxy is a flake waiting to happen. Lock step is the stronger claim: any hop
// that buffered a response until it was complete would deadlock, because the
// bytes that release the next write are the ones it is holding.
//
// It also proves the response-body observation gateway.NewReverseProxy depends
// on. ModifyResponse still runs per response, resp.Request still carries the
// outbound request, and the token counts are still readable from the tail of a
// body the client has already consumed incrementally.
func TestStreamingArrivesTokenByToken(t *testing.T) {
	t.Parallel()

	locks := newGates()
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gate := locks.get(r.Header.Get("X-Stream-Id"))
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		flusher := http.NewResponseController(w)
		_ = flusher.Flush()

		for i := 0; i < streamChunks; i++ {
			select {
			case <-gate:
			case <-r.Context().Done():
				return
			case <-time.After(20 * time.Second):
				return
			}
			fmt.Fprintf(w, "data: {\"choices\":[{\"delta\":{\"content\":\"tok%d\"}}]}\n\n", i)
			_ = flusher.Flush()
		}
		fmt.Fprintf(w,
			"data: {\"usage\":{\"prompt_tokens\":11,\"completion_tokens\":%d,\"prompt_tokens_details\":{\"cached_tokens\":7}}}\n\ndata: [DONE]\n\n",
			streamChunks)
		_ = flusher.Flush()
	}))
	defer upstream.Close()

	var mu sync.Mutex
	usage := make(map[string]observedUsage)
	requests := make(map[string]bool)

	connector := newConnector(t, proxyTo(upstream.URL), Limits{})
	pool := newPool(t, PoolConfig{})
	link(t, pool, "cluster-ap", connector)

	proxy := gatewayProxy(pool, "cluster-ap", func(resp *http.Response) error {
		if resp.Request == nil {
			t.Error("ModifyResponse saw a response with no request on it")
			return nil
		}
		id := resp.Request.Header.Get("X-Stream-Id")
		mu.Lock()
		requests[id] = true
		mu.Unlock()
		resp.Body = &tailObserver{src: resp.Body, report: func(tail []byte) {
			counts, ok := lastUsage(tail)
			if !ok {
				return
			}
			mu.Lock()
			usage[id] = counts
			mu.Unlock()
		}}
		return nil
	})
	front := httptest.NewServer(proxy)
	defer front.Close()

	var wg sync.WaitGroup
	errs := make(chan error, streamCount)
	for stream := 0; stream < streamCount; stream++ {
		id := fmt.Sprintf("stream-%02d", stream)
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := runLockStepStream(front.URL, id, locks.get(id)); err != nil {
				errs <- fmt.Errorf("%s: %w", id, err)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}
	if t.Failed() {
		return
	}

	mu.Lock()
	defer mu.Unlock()
	if len(requests) != streamCount {
		t.Errorf("ModifyResponse ran for %d responses, want %d", len(requests), streamCount)
	}
	if len(usage) != streamCount {
		t.Fatalf("read token counts from %d response tails, want %d", len(usage), streamCount)
	}
	for id, counts := range usage {
		if counts.PromptTokens != 11 || counts.CompletionTokens != streamChunks {
			t.Errorf("%s: usage %+v, want prompt 11 and completion %d", id, counts, streamChunks)
		}
	}
}

// runLockStepStream releases one chunk at a time and refuses to release the next
// until it has read the last.
func runLockStepStream(base, id string, gate chan struct{}) error {
	body := strings.NewReader(`{"model":"gpt-oss-120b","stream":true}`)
	req, err := http.NewRequest(http.MethodPost, base+"/v1/chat/completions", body)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Stream-Id", id)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("status %d, fault %q", resp.StatusCode, resp.Header.Get("X-Tunnel-Fault"))
	}

	reader := bufio.NewReader(resp.Body)
	for i := 0; i < streamChunks; i++ {
		gate <- struct{}{}
		line, err := readDataLine(reader)
		if err != nil {
			return fmt.Errorf("chunk %d: %w", i, err)
		}
		want := fmt.Sprintf("tok%d", i)
		if !strings.Contains(line, want) {
			return fmt.Errorf("chunk %d was %q, want it to contain %q", i, line, want)
		}
	}

	rest, err := io.ReadAll(reader)
	if err != nil {
		return fmt.Errorf("reading the tail: %w", err)
	}
	if !strings.Contains(string(rest), `"usage"`) {
		return fmt.Errorf("tail %q carried no usage object", rest)
	}
	return nil
}

// readDataLine returns the next non-empty SSE line.
func readDataLine(reader *bufio.Reader) (string, error) {
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		if trimmed := strings.TrimSpace(line); trimmed != "" {
			return trimmed, nil
		}
	}
}

// lastUsage finds the final usage object in a response tail, the way the
// gateway's own parser does: by brace matching from the key, because the window
// starts mid-document and there is no valid document to decode.
func lastUsage(tail []byte) (observedUsage, bool) {
	needle := []byte(`"usage"`)
	at := bytes.LastIndex(tail, needle)
	if at < 0 {
		return observedUsage{}, false
	}
	rest := tail[at+len(needle):]
	start := bytes.IndexByte(rest, '{')
	if start < 0 {
		return observedUsage{}, false
	}
	depth := 0
	for i := start; i < len(rest); i++ {
		switch rest[i] {
		case '{':
			depth++
		case '}':
			depth--
			if depth == 0 {
				var counts observedUsage
				if err := json.Unmarshal(rest[start:i+1], &counts); err != nil {
					return observedUsage{}, false
				}
				return counts, true
			}
		}
	}
	return observedUsage{}, false
}
