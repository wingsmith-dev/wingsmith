package tunnel

import (
	"io"
	"net"
	"net/http"
	"testing"
	"time"

	"golang.org/x/net/http2"
)

// wireSettings is what one end of a tunnel actually put on the socket during the
// handshake: its SETTINGS by id, and the connection-level WINDOW_UPDATE that
// follows them.
type wireSettings struct {
	settings map[http2.SettingID]uint32
	connAdd  uint32
}

func (w wireSettings) get(t *testing.T, id http2.SettingID) uint32 {
	t.Helper()
	value, ok := w.settings[id]
	if !ok {
		t.Fatalf("%v was never advertised; settings on the wire were %v", id, w.settings)
	}
	return value
}

// readHandshake reads frames until it has the peer's SETTINGS and, if one
// follows, its connection WINDOW_UPDATE.
//
// It reads with a deadline rather than a count, because the number of frames in
// a handshake is x/net's business and not something to pin.
func readHandshake(t *testing.T, framer *http2.Framer, conn net.Conn) wireSettings {
	t.Helper()
	if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatalf("read deadline: %v", err)
	}
	observed := wireSettings{settings: make(map[http2.SettingID]uint32)}
	seenSettings := false
	for {
		frame, err := framer.ReadFrame()
		if err != nil {
			if !seenSettings {
				t.Fatalf("no SETTINGS arrived: %v", err)
			}
			return observed
		}
		switch f := frame.(type) {
		case *http2.SettingsFrame:
			if f.IsAck() {
				continue
			}
			seenSettings = true
			_ = f.ForeachSetting(func(s http2.Setting) error {
				observed.settings[s.ID] = s.Val
				return nil
			})
		case *http2.WindowUpdateFrame:
			if f.StreamID == 0 {
				observed.connAdd = f.Increment
				return observed
			}
		}
	}
}

// TestTheGatewayAdvertisesItsFlowControlOnTheWire reads the pool's own handshake
// off a raw framer.
//
// It exists because every other way of checking this is indirect. A large
// response completes with x/net's defaults too — the client's default stream
// window is transportDefaultStreamFlow, 4 MiB, not the spec's 64 KiB — so a test
// that moves 2 MiB through a tunnel proves nothing about whether the configured
// windows were ever passed on. Deleting the flow-control fields from the pool's
// http.HTTP2Config is invisible to every other test in this package and visible
// here.
//
// The values are also the argument for setting them at all: without the config
// this end asks for a 1 GiB connection window, which is memory the gateway can be
// made to hold per cluster by a connector that stops reading.
func TestTheGatewayAdvertisesItsFlowControlOnTheWire(t *testing.T) {
	t.Parallel()

	limits := Limits{
		MaxConcurrentStreams: 33,
		ConnWindow:           7 << 20,
		StreamWindow:         3 << 20,
		MaxReadFrameSize:     1 << 19,
	}
	pool := newPool(t, PoolConfig{Limits: limits})

	gatewaySide, peerSide := socketPair(t)
	// NewClientConn writes the preface, the SETTINGS and the WINDOW_UPDATE before
	// it returns, so there is nothing to synchronise with.
	if _, err := pool.t2.NewClientConn(gatewaySide); err != nil {
		t.Fatalf("client conn: %v", err)
	}

	framer := http2.NewFramer(peerSide, peerSide)
	preface := make([]byte, len(http2.ClientPreface))
	if _, err := io.ReadFull(peerSide, preface); err != nil {
		t.Fatalf("reading the client preface: %v", err)
	}
	if string(preface) != http2.ClientPreface {
		t.Fatalf("bad client preface %q", preface)
	}
	observed := readHandshake(t, framer, peerSide)

	if got := observed.get(t, http2.SettingInitialWindowSize); got != uint32(limits.StreamWindow) {
		t.Errorf("INITIAL_WINDOW_SIZE on the wire is %d, want %d", got, limits.StreamWindow)
	}
	if got := observed.get(t, http2.SettingMaxFrameSize); got != limits.MaxReadFrameSize {
		t.Errorf("MAX_FRAME_SIZE on the wire is %d, want %d", got, limits.MaxReadFrameSize)
	}
	if got := observed.connAdd; got != uint32(limits.ConnWindow) {
		t.Errorf("the connection WINDOW_UPDATE is %d, want %d", got, limits.ConnWindow)
	}

	// MAX_CONCURRENT_STREAMS is a server setting and x/net's client never sends
	// one. This is the assertion behind PoolConfig no longer configuring it: a
	// per-tunnel stream cap on this end would be a number nobody is told, and the
	// cap that exists is the connector's.
	if got, ok := observed.settings[http2.SettingMaxConcurrentStreams]; ok {
		t.Errorf("the gateway advertised MAX_CONCURRENT_STREAMS=%d; x/net's client did not use to, "+
			"so the pool can now advertise its own limit instead of learning the connector's", got)
	}
}

// TestTheConnectorAdvertisesItsFlowControlOnTheWire is the same read from the
// other end.
//
// The connector's SETTINGS are what the gateway's admission control is built on —
// Conn.Live refuses a tunnel whose MAX_CONCURRENT_STREAMS has not arrived — so
// they are worth pinning as frames rather than as a struct field.
func TestTheConnectorAdvertisesItsFlowControlOnTheWire(t *testing.T) {
	t.Parallel()

	limits := Limits{
		MaxConcurrentStreams: 21,
		ConnWindow:           6 << 20,
		StreamWindow:         2 << 20,
		MaxReadFrameSize:     1 << 18,
	}
	connector := newConnector(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}), limits)

	gatewaySide, connectorSide := socketPair(t)
	served := make(chan struct{})
	go func() {
		defer close(served)
		_ = connector.Serve(connectorSide)
	}()
	t.Cleanup(func() {
		_ = gatewaySide.Close()
		select {
		case <-served:
		case <-time.After(5 * time.Second):
			t.Error("connector Serve did not return after the tunnel closed")
		}
	})

	framer := http2.NewFramer(gatewaySide, gatewaySide)
	if _, err := io.WriteString(gatewaySide, http2.ClientPreface); err != nil {
		t.Fatalf("writing the client preface: %v", err)
	}
	if err := framer.WriteSettings(); err != nil {
		t.Fatalf("writing SETTINGS: %v", err)
	}
	observed := readHandshake(t, framer, gatewaySide)

	if got := observed.get(t, http2.SettingMaxConcurrentStreams); got != limits.MaxConcurrentStreams {
		t.Errorf("MAX_CONCURRENT_STREAMS on the wire is %d, want %d", got, limits.MaxConcurrentStreams)
	}
	if got := observed.get(t, http2.SettingInitialWindowSize); got != uint32(limits.StreamWindow) {
		t.Errorf("INITIAL_WINDOW_SIZE on the wire is %d, want %d", got, limits.StreamWindow)
	}
	if got := observed.get(t, http2.SettingMaxFrameSize); got != limits.MaxReadFrameSize {
		t.Errorf("MAX_FRAME_SIZE on the wire is %d, want %d", got, limits.MaxReadFrameSize)
	}
	if got, want := observed.connAdd, uint32(limits.ConnWindow-65535); got != want {
		t.Errorf("the connection WINDOW_UPDATE is %d, want %d", got, want)
	}
}
