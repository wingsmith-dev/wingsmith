package serving

import (
	"bytes"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Media usage is the image and video half of what usage.go records for text: raw facts about
// what a request asked for and what came back, never a price.
//
// It exists because the text path has nothing to read here. Dynamo 1.5.0's image and video
// response types carry no usage object the worker could fill (`NvImagesResponse` flattens an
// OpenAI `ImagesResponse` whose `usage` Dynamo leaves empty; `NvVideosResponse` has no such field
// at all), so these facts are assembled by this hop from the request it already buffered and the
// response bytes it is already forwarding.
//
// Nothing here buffers a response. A generated image or video is a base64 string that can run to
// hundreds of megabytes, so the count of outputs is taken by scanning the bytes as they pass (see
// mediaCounter) and the scalar fields — `inference_time_s`, an image response's `size` — are read
// from the same 64 KiB tail usage.go already keeps. Both fields are serialised after `data` by
// Dynamo, which is why the tail holds them.

const (
	mediaImage = "image"
	mediaVideo = "video"
)

// Where a size came from. There is no "default": Dynamo documents 832x480 for video and OpenAI
// 1024x1024 for images, but the engine behind a route may apply its own, so a size nobody stated
// is recorded as absent rather than guessed.
const (
	sizeFromRequest  = "request"
	sizeFromResponse = "response"
)

// mediaFacts is one media request's usage, filled in three places: the request stage, the
// response scan, and the forward that times it.
type mediaFacts struct {
	kind string
	// chat is an image answered as a chat completion, whose outputs are content parts.
	chat bool

	// size is "WxH" as the caller or the response stated it; sizeSource says which.
	size       string
	sizeSource string

	// requested is `n` on an image request. Informational: what counts is outputs.
	requested int64

	// seconds and frames are the video's length as the request states it: `seconds`, or
	// `nvext.num_frames` / `nvext.fps` when both are given. haveSeconds and haveFrames say whether
	// the request stated enough to know.
	seconds     float64
	haveSeconds bool
	frames      int64
	haveFrames  bool

	// outputs is how many images or videos the response carried, counted as it streamed past.
	outputs     int64
	haveOutputs bool

	// upstream is the wall time this hop spent with the request inside a cluster: from the
	// forward to the last response byte. It is the GPU time a request cost, as near as this hop
	// can measure it.
	upstream time.Duration

	// inferenceSeconds is the engine's own `inference_time_s`, when the response carried one.
	inferenceSeconds     float64
	haveInferenceSeconds bool
}

// mediaKindFor says whether a request is a media generation, and which kind.
//
// A chat completion is one only when the endpoint generates images: that is the route
// pathAnswered lets through to an image model, and the image comes back as an `image_url` part.
func mediaKindFor(path, modality string) string {
	switch {
	case path == "/v1/images/generations" || path == "/v1/images/edits":
		return mediaImage
	case path == "/v1/videos":
		return mediaVideo
	case path == "/v1/chat/completions" && modality == modalityImage:
		return mediaImage
	default:
		return ""
	}
}

// mediaRequest is the part of an image or video request body that says what was asked for.
// `seconds` is a string in OpenAI's video API and an integer in Dynamo's, so it is taken raw.
type mediaRequest struct {
	Size    string          `json:"size"`
	N       *int64          `json:"n"`
	Seconds json.RawMessage `json:"seconds"`
	NvExt   struct {
		NumFrames *int64 `json:"num_frames"`
		FPS       *int64 `json:"fps"`
	} `json:"nvext"`
}

// mediaFactsFromRequest reads what a request asked for. A body it cannot read — multipart, as an
// image edit is — yields the kind alone, and the response scan still counts what came back.
func mediaFactsFromRequest(kind string, body []byte, multipart bool) *mediaFacts {
	facts := &mediaFacts{kind: kind}
	if multipart {
		return facts
	}
	var req mediaRequest
	if err := json.Unmarshal(body, &req); err != nil {
		return facts
	}
	if validSize(req.Size) {
		facts.size, facts.sizeSource = req.Size, sizeFromRequest
	}
	if req.N != nil && *req.N > 0 {
		facts.requested = *req.N
	}
	if kind != mediaVideo {
		return facts
	}

	seconds, haveSeconds := secondsFrom(req.Seconds)
	fps := int64(0)
	if req.NvExt.FPS != nil && *req.NvExt.FPS > 0 {
		fps = *req.NvExt.FPS
	}
	switch {
	case req.NvExt.NumFrames != nil && *req.NvExt.NumFrames > 0:
		facts.frames, facts.haveFrames = *req.NvExt.NumFrames, true
		if fps > 0 {
			facts.seconds, facts.haveSeconds = float64(facts.frames)/float64(fps), true
		} else if haveSeconds {
			facts.seconds, facts.haveSeconds = seconds, true
		}
	case haveSeconds:
		facts.seconds, facts.haveSeconds = seconds, true
		if fps > 0 {
			facts.frames, facts.haveFrames = int64(seconds*float64(fps)), true
		}
	}
	return facts
}

func secondsFrom(raw json.RawMessage) (float64, bool) {
	if len(raw) == 0 {
		return 0, false
	}
	var number float64
	if json.Unmarshal(raw, &number) == nil && number > 0 {
		return number, true
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		if parsed, err := strconv.ParseFloat(strings.TrimSpace(text), 64); err == nil && parsed > 0 {
			return parsed, true
		}
	}
	return 0, false
}

// validSize accepts "WxH" with positive integers and nothing else, because the value reaches a
// log line and a caller wrote it.
func validSize(size string) bool {
	w, h, ok := strings.Cut(size, "x")
	if !ok || len(size) > 16 {
		return false
	}
	wn, errW := strconv.Atoi(w)
	hn, errH := strconv.Atoi(h)
	return errW == nil && errH == nil && wn > 0 && hn > 0
}

// mediaNeedles are the keys whose occurrences count the outputs of a response. Every generated
// image or video in `data` carries `url` or `b64_json`; an image in a chat answer is a content
// part of type `image_url`. Compact spelling, because Dynamo's serde output has no whitespace.
// A key inside a JSON string cannot match: its quotes arrive escaped.
var (
	dataNeedles = [][]byte{[]byte(`"b64_json":"`), []byte(`"url":"`)}
	chatNeedles = [][]byte{[]byte(`"type":"image_url"`)}
)

// mediaCounter counts needles in a stream without holding it.
//
// It keeps the last keep = longest-1 bytes of what it has seen. A match straddling a read boundary
// is found in carry+head, where head is the first keep bytes of the new read. A match inside the
// carry alone was counted on an earlier read, and one inside head alone is also found by the scan
// of the read itself, so both are subtracted.
type mediaCounter struct {
	needles [][]byte
	keep    int
	carry   []byte
	count   int64
}

func newMediaCounter(chat bool) *mediaCounter {
	needles := dataNeedles
	if chat {
		needles = chatNeedles
	}
	longest := 0
	for _, n := range needles {
		longest = max(longest, len(n))
	}
	return &mediaCounter{needles: needles, keep: longest - 1}
}

func (c *mediaCounter) write(b []byte) {
	if len(b) == 0 {
		return
	}
	head := b[:min(len(b), c.keep)]
	boundary := append(append(make([]byte, 0, len(c.carry)+len(head)), c.carry...), head...)
	for _, n := range c.needles {
		c.count += int64(bytes.Count(boundary, n) - bytes.Count(c.carry, n) + bytes.Count(b, n) - bytes.Count(head, n))
	}
	seen := b
	if len(b) < c.keep {
		seen = append(append(make([]byte, 0, len(c.carry)+len(b)), c.carry...), b...)
	}
	c.carry = append(c.carry[:0], seen[max(0, len(seen)-c.keep):]...)
}

// mediaTailFields are the scalars read from a media response's tail.
func mediaTailFields(tail []byte, facts *mediaFacts) {
	if value, ok := scalarAfter(tail, `"inference_time_s":`); ok {
		if seconds, err := strconv.ParseFloat(value, 64); err == nil && seconds >= 0 {
			facts.inferenceSeconds, facts.haveInferenceSeconds = seconds, true
		}
	}
	if facts.size == "" && facts.kind == mediaImage {
		if value, ok := scalarAfter(tail, `"size":`); ok {
			if size, err := strconv.Unquote(value); err == nil && validSize(size) {
				facts.size, facts.sizeSource = size, sizeFromResponse
			}
		}
	}
}

// scalarAfter returns the raw JSON scalar after the last occurrence of a key in a window.
func scalarAfter(window []byte, key string) (string, bool) {
	at := bytes.LastIndex(window, []byte(key))
	if at < 0 {
		return "", false
	}
	rest := bytes.TrimLeft(window[at+len(key):], " \t\r\n")
	end := bytes.IndexAny(rest, ",}]\r\n")
	if end <= 0 {
		return "", false
	}
	return string(bytes.TrimSpace(rest[:end])), true
}

// stageMediaFacts records what an image or video request asked for.
//
// After the endpoint's terms are applied, so a size a policy writes is the size recorded — it is
// what the engine was asked for.
func stageMediaFacts(w http.ResponseWriter, r *http.Request, req *requestState) (*http.Request, bool) {
	kind := mediaKindFor(r.URL.Path, routeModality(req.route))
	if kind == "" {
		return r, true
	}
	if obs := observationFrom(r.Context()); obs != nil {
		facts := mediaFactsFromRequest(kind, req.body, req.isMultipart)
		facts.chat = r.URL.Path == "/v1/chat/completions"
		obs.mu.Lock()
		obs.media = facts
		obs.mu.Unlock()
	}
	return r, true
}

// markUpstreamDuration records how long a media request spent inside a cluster.
func markUpstreamDuration(r *http.Request, elapsed time.Duration) {
	obs := observationFrom(r.Context())
	if obs == nil {
		return
	}
	obs.mu.Lock()
	defer obs.mu.Unlock()
	if obs.media != nil {
		obs.media.upstream = elapsed
	}
}

// mediaCounter returns a fresh output counter for a media request, or nil for any other.
func (o *observation) mediaCounter() *mediaCounter {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.media == nil {
		return nil
	}
	return newMediaCounter(o.media.chat)
}

// recordMedia stores what the response scan found.
func (o *observation) recordMedia(outputs int64, tail []byte) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.media == nil {
		return
	}
	o.media.outputs, o.media.haveOutputs = outputs, true
	mediaTailFields(tail, o.media)
}

// fields freezes media facts into access-log attributes. Absent, never zero, for anything the
// request and response did not state, for the reason the token fields are.
//
// Only a successful response has outputs, and only one with outputs is logged as media: the facts
// of a refused request describe nothing that was generated.
func (m *mediaFacts) fields() []any {
	if m == nil || !m.haveOutputs {
		return nil
	}
	out := []any{"media", m.kind}
	if m.kind == mediaVideo {
		out = append(out, "videos", m.outputs)
	} else {
		out = append(out, "images", m.outputs)
	}
	if m.requested > 0 {
		out = append(out, "requested_n", m.requested)
	}
	if m.size != "" {
		out = append(out, "size", m.size, "size_source", m.sizeSource)
	}
	if m.haveSeconds {
		out = append(out, "video_seconds", m.seconds)
	}
	if m.haveFrames {
		out = append(out, "video_frames", m.frames)
	}
	if m.upstream > 0 {
		out = append(out, "upstream_ms", m.upstream.Milliseconds())
	}
	if m.haveInferenceSeconds {
		out = append(out, "inference_time_s", m.inferenceSeconds)
	}
	return out
}
