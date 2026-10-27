//go:build interop

package interop

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	stdsync "sync"
	"syscall"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/hangar-dev/hangar/libs/operator/internal/checkpoint"
	syncpkg "github.com/hangar-dev/hangar/libs/operator/internal/sync"
	"github.com/hangar-dev/hangar/libs/operator/internal/wire"
)

const (
	clusterID = "clu_01J9X2AAAAAAAAAAAAAAAAAAAA"

	typeDesired  = "deployment.desired"
	typeObserved = "node.observed"
)

// repoRoot locates the checkout from this file's own path, so the harness does not depend on the
// working directory a test runner happens to choose.
func repoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(filepath.Dir(file)))))
}

func fixturePayload(t *testing.T, typeName string) json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(repoRoot(), "libs", "packages", "contract", "fixtures", typeName+".json"))
	if err != nil {
		t.Fatalf("read fixture %s: %v", typeName, err)
	}
	var document struct {
		Payload json.RawMessage `json:"payload"`
	}
	if err := json.Unmarshal(raw, &document); err != nil {
		t.Fatalf("decode fixture %s: %v", typeName, err)
	}
	return document.Payload
}

// ---------------------------------------------------------------------------- the hub process

type hub struct {
	t    *testing.T
	cmd  *exec.Cmd
	port int
	// token is the enrolment token the hub minted for clusterID at startup, the only credential
	// it opens a session for.
	token string
}

// hubScript is the Python launcher that wraps the plane's hub with the control surface these
// tests drive. It lives beside them so the gate needs nothing outside libs/.
const hubScript = "libs/operator/internal/interop/testdata/hub.py"

// startHub launches the Python hub on an ephemeral port, enrols clusterID, and waits for /health.
//
// The launcher is run from libs/plane so that uv resolves the plane's environment and the hub it
// wraps is the plane's own.
func startHub(t *testing.T, args ...string) *hub {
	t.Helper()
	root := repoRoot()
	script := filepath.Join(root, filepath.FromSlash(hubScript))
	full := append([]string{"run", "python", script, "--port", "0", "--cluster-id", clusterID}, args...)
	cmd := exec.Command("uv", full...)
	cmd.Dir = filepath.Join(root, "libs", "plane")
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("stdout pipe: %v", err)
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		t.Fatalf("stderr pipe: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start hub: %v (is uv installed?)", err)
	}

	h := &hub{t: t, cmd: cmd}
	logs := make(chan string, 1)
	go func() {
		body, _ := io.ReadAll(stderr)
		logs <- string(body)
	}()
	t.Cleanup(func() {
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		_ = cmd.Wait()
		if !t.Failed() {
			return
		}
		select {
		case body := <-logs:
			t.Logf("hub stderr:\n%s", body)
		case <-time.After(5 * time.Second):
		}
	})

	scanner := bufio.NewScanner(stdout)
	deadline := time.After(60 * time.Second)
	type announced struct {
		port  int
		token string
	}
	found := make(chan announced, 1)
	go func() {
		var token string
		for scanner.Scan() {
			line := scanner.Text()
			if v, ok := strings.CutPrefix(line, "HUB_TOKEN "); ok {
				token = v
			}
			if port, ok := strings.CutPrefix(line, "HUB_PORT "); ok {
				n, err := strconv.Atoi(port)
				if err == nil {
					found <- announced{port: n, token: token}
					return
				}
			}
		}
	}()
	select {
	case a := <-found:
		h.port, h.token = a.port, a.token
	case <-deadline:
		t.Fatal("hub did not announce a port within 60s")
	}
	if h.token == "" {
		t.Fatal("hub announced a port without an enrolment token")
	}

	h.waitHealthy()
	return h
}

func (h *hub) base() string { return fmt.Sprintf("http://127.0.0.1:%d", h.port) }

func (h *hub) url() string { return fmt.Sprintf("ws://127.0.0.1:%d/v1/hub/sync", h.port) }

// waitHealthy polls /health instead of sleeping, so the harness never races the interpreter.
func (h *hub) waitHealthy() {
	h.t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(h.base() + "/health")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	h.t.Fatal("hub never became healthy")
}

type frameInfo struct {
	Type    string  `json:"type"`
	ID      string  `json:"id"`
	Seq     *uint64 `json:"seq"`
	Ack     *uint64 `json:"ack"`
	ReplyTo *string `json:"reply_to"`
}

type resyncInfo struct {
	Phase     string `json:"phase"`
	RequestID string `json:"request_id"`
	Count     *int   `json:"count"`
	Complete  *bool  `json:"complete"`
}

type sessionInfo struct {
	SessionID       string `json:"session_id"`
	ClusterID       string `json:"cluster_id"`
	NextSeq         uint64 `json:"next_seq"`
	ExpectedSeq     uint64 `json:"expected_seq"`
	AckCursor       uint64 `json:"ack_cursor"`
	ConnectionCount int    `json:"connection_count"`
	ReplayPending   int    `json:"replay_pending"`
}

type hubState struct {
	Connections int          `json:"connections"`
	AuthHeaders []string     `json:"auth_headers"`
	ClosedByHub int          `json:"closed_by_hub"`
	Session     *sessionInfo `json:"session"`
	Sessions    int          `json:"sessions"`
	Sent        []frameInfo  `json:"sent"`
	Received    struct {
		Observed   []frameInfo  `json:"observed"`
		Events     []frameInfo  `json:"events"`
		Operations []frameInfo  `json:"operations"`
		Resyncs    []resyncInfo `json:"resyncs"`
	} `json:"received"`
}

func (h *hub) control(path string, out any) {
	h.t.Helper()
	resp, err := http.Get(h.base() + path)
	if err != nil {
		h.t.Fatalf("control %s: %v", path, err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		h.t.Fatalf("control %s: %s: %s", path, resp.Status, body)
	}
	if out != nil {
		if err := json.Unmarshal(body, out); err != nil {
			h.t.Fatalf("control %s: decode %s: %v", path, body, err)
		}
	}
}

func (h *hub) state() hubState {
	h.t.Helper()
	var s hubState
	h.control("/control/state", &s)
	return s
}

// push asks the hub to send count declarations down the live session and returns their sequence
// numbers.
func (h *hub) push(typeName string, count int) []uint64 {
	h.t.Helper()
	var out struct {
		Seqs []uint64 `json:"seqs"`
	}
	h.control(fmt.Sprintf("/control/push?type=%s&n=%d", typeName, count), &out)
	return out.Seqs
}

// pushGap burns a sequence number before sending, which is the one thing a correct hub never
// does and the only way to prove the operator closes on a hole instead of accepting it.
func (h *hub) pushGap(typeName string) {
	h.t.Helper()
	h.control(fmt.Sprintf("/control/push?type=%s&n=1&gap=1", typeName), nil)
}

func (h *hub) duplicate() { h.t.Helper(); h.control("/control/duplicate", nil) }

func (h *hub) kill() { h.t.Helper(); h.control("/control/kill", nil) }

func (h *hub) forget() { h.t.Helper(); h.control("/control/forget", nil) }

func (h *hub) failTypes(types ...string) {
	h.t.Helper()
	h.control("/control/fail?types="+strings.Join(types, ","), nil)
}

func (h *hub) mute(on bool) {
	h.t.Helper()
	value := "0"
	if on {
		value = "1"
	}
	h.control("/control/mute?on="+value, nil)
}

func (h *hub) sentTypes(typeName string) []frameInfo {
	h.t.Helper()
	var out []frameInfo
	for _, f := range h.state().Sent {
		if f.Type == typeName {
			out = append(out, f)
		}
	}
	return out
}

// dialRaw opens a WebSocket to the hub without the client, for the cases that need a peer which
// misbehaves in a way the client never would.
func (h *hub) dialRaw(ctx context.Context) *websocket.Conn {
	h.t.Helper()
	conn, _, err := websocket.Dial(ctx, h.url(), &websocket.DialOptions{
		HTTPHeader: http.Header{"Authorization": []string{"Bearer " + h.token}},
	})
	if err != nil {
		h.t.Fatalf("raw dial: %v", err)
	}
	return conn
}

func writeRaw(t *testing.T, ctx context.Context, conn *websocket.Conn, e *wire.Envelope) {
	t.Helper()
	body, err := json.Marshal(e)
	if err != nil {
		t.Fatalf("marshal raw frame: %v", err)
	}
	if err := conn.Write(ctx, websocket.MessageText, body); err != nil {
		t.Fatalf("write raw frame: %v", err)
	}
}

func readRaw(t *testing.T, ctx context.Context, conn *websocket.Conn) *wire.Envelope {
	t.Helper()
	_, body, err := conn.Read(ctx)
	if err != nil {
		t.Fatalf("read raw frame: %v", err)
	}
	var e wire.Envelope
	if err := json.Unmarshal(body, &e); err != nil {
		t.Fatalf("decode raw frame: %v", err)
	}
	return &e
}

// ---------------------------------------------------------------------------- durable cursor

// fileStore is a checkpoint.Store on disk. The resume claim is about a cursor that survives the
// process, so an in-memory store would prove nothing: a second client instance reads this file
// and nothing else.
type fileStore struct {
	mu   stdsync.Mutex
	path string
}

type cursorFile struct {
	SessionID  string `json:"session_id"`
	AppliedSeq uint64 `json:"applied_seq"`
	Lossy      bool   `json:"lossy"`
}

func newFileStore(t *testing.T) *fileStore {
	return &fileStore{path: filepath.Join(t.TempDir(), "cursor.json")}
}

func (s *fileStore) Load(context.Context) (checkpoint.State, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.load()
}

func (s *fileStore) load() (checkpoint.State, error) {
	body, err := os.ReadFile(s.path)
	if os.IsNotExist(err) {
		return checkpoint.State{}, nil
	}
	if err != nil {
		return checkpoint.State{}, err
	}
	var c cursorFile
	if err := json.Unmarshal(body, &c); err != nil {
		return checkpoint.State{}, err
	}
	return checkpoint.State{SessionID: c.SessionID, AppliedSeq: c.AppliedSeq, Lossy: c.Lossy}, nil
}

func (s *fileStore) Save(_ context.Context, next checkpoint.State) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	old, err := s.load()
	if err != nil {
		return err
	}
	if old.SessionID == next.SessionID {
		next.Lossy = next.Lossy || old.Lossy
	}
	return s.write(next)
}

func (s *fileStore) SetLossy(_ context.Context, lossy bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	state, err := s.load()
	if err != nil {
		return err
	}
	state.Lossy = lossy
	return s.write(state)
}

func (s *fileStore) write(state checkpoint.State) error {
	body, err := json.Marshal(cursorFile{
		SessionID: state.SessionID, AppliedSeq: state.AppliedSeq, Lossy: state.Lossy,
	})
	if err != nil {
		return err
	}
	return os.WriteFile(s.path, body, 0o600)
}

func (s *fileStore) mustLoad(t *testing.T) checkpoint.State {
	t.Helper()
	state, err := s.Load(context.Background())
	if err != nil {
		t.Fatalf("load cursor: %v", err)
	}
	return state
}

// ---------------------------------------------------------------------------- the client

// recorder is the operator's handler. It records what was delivered, can refuse a chosen sequence
// number to exercise the withheld ack, and streams its own observed snapshot when the hub could
// not honour a resume.
type recorder struct {
	mu       stdsync.Mutex
	frames   []frameInfo
	refuse   map[uint64]int
	resyncs  int
	client   *syncpkg.Client
	observed json.RawMessage
	t        *testing.T
}

func newRecorder(t *testing.T) *recorder {
	return &recorder{refuse: map[uint64]int{}, observed: fixturePayload(t, typeObserved), t: t}
}

func (r *recorder) Handle(_ context.Context, e *syncpkg.Envelope) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if e.Seq != nil {
		if left := r.refuse[*e.Seq]; left > 0 {
			r.refuse[*e.Seq] = left - 1
			return fmt.Errorf("interop: refusing seq %d once", *e.Seq)
		}
	}
	r.frames = append(r.frames, frameInfo{Type: e.Type, Seq: e.Seq, Ack: e.Ack, ReplyTo: e.ReplyTo})
	return nil
}

// Resync opens the operator's own half of a full resync: resync.begin, the snapshot, and the
// stream.end that closes it, all sharing one reply_to.
func (r *recorder) Resync(context.Context) error {
	r.mu.Lock()
	client := r.client
	r.resyncs++
	r.mu.Unlock()
	if client == nil {
		return nil
	}
	requestID, err := client.SendCorrelated(syncpkg.TypeResyncBegin, "", wire.ResyncBegin{Scope: "supply"}, "")
	if err != nil {
		return err
	}
	for i := 0; i < 2; i++ {
		if _, err := client.SendCorrelated(typeObserved, fmt.Sprintf("node-%d", i), r.observed, requestID); err != nil {
			return err
		}
	}
	_, err = client.SendCorrelated(syncpkg.TypeStreamEnd, "", wire.StreamEnd{Count: 2, Complete: true}, requestID)
	return err
}

// clientRef returns the client this recorder was attached to.
func (r *recorder) clientRef() *syncpkg.Client {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.client
}

func (r *recorder) refuseOnce(seq uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.refuse[seq] = 1
}

func (r *recorder) snapshot() []frameInfo {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]frameInfo(nil), r.frames...)
}

func (r *recorder) resyncCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.resyncs
}

// seqsOf returns the sequence numbers of the recorded sequenced frames, in delivery order.
func seqsOf(frames []frameInfo) []uint64 {
	out := make([]uint64, 0, len(frames))
	for _, f := range frames {
		if f.Seq != nil {
			out = append(out, *f.Seq)
		}
	}
	return out
}

type runner struct {
	client *syncpkg.Client
	cancel context.CancelFunc
	done   chan struct{}
}

// startClient runs one client instance until stop is called. Timings are compressed so a test
// that waits on a heartbeat or a liveness deadline takes milliseconds, not a minute.
func startClient(t *testing.T, h *hub, store checkpoint.Store, handler syncpkg.Handler, tune func(*syncpkg.Options)) *runner {
	t.Helper()
	opts := syncpkg.Options{
		HubURL:            h.url(),
		ClusterID:         clusterID,
		Token:             h.token,
		OperatorVersion:   "interop",
		HeartbeatInterval: 100 * time.Millisecond,
		LivenessTimeout:   time.Second,
		BackoffMin:        20 * time.Millisecond,
		BackoffMax:        100 * time.Millisecond,
		Handler:           handler,
		Checkpointer:      checkpoint.Adapt(store),
	}
	if tune != nil {
		tune(&opts)
	}
	client := syncpkg.NewClient(opts)
	if r, ok := handler.(*recorder); ok {
		r.mu.Lock()
		r.client = client
		r.mu.Unlock()
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := client.Run(ctx); err != nil && ctx.Err() == nil {
			t.Errorf("client run: %v", err)
		}
	}()
	run := &runner{client: client, cancel: cancel, done: done}
	t.Cleanup(run.stop)
	return run
}

func (r *runner) stop() {
	r.cancel()
	select {
	case <-r.done:
	case <-time.After(10 * time.Second):
	}
}

// waitFor polls a condition instead of sleeping a guessed interval.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// stayTrue asserts a condition holds for the whole window, which is how "nothing happened" is
// tested without trusting a single sample.
func stayTrue(t *testing.T, what string, window time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(window)
	for time.Now().Before(deadline) {
		if !cond() {
			t.Fatalf("%s stopped holding", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
