package serving

import (
	"bytes"
	"errors"
	"io"
	"mime"
	"mime/multipart"
	"net/textproto"
	"strings"
)

// errNoBoundary is a multipart content type that names no boundary. There is
// nothing to read and nothing to guess.
var errNoBoundary = errors.New("multipart content type has no boundary")

// maxModelFieldBytes caps the `model` form field. A routing key is a model id,
// not a payload, and this is read before anything about the caller is known.
const maxModelFieldBytes = 4 << 10

// multipartContentType reports whether a body carries its fields as parts
// rather than as JSON, and the boundary that separates them.
//
// This is the whole of the content-type decision. Everything else in this hop
// works on a model string, so the only thing that varies between an
// OpenAI-JSON request and an OpenAI-multipart one is where that string is read
// from and how it is written back.
//
// A multipart type with no boundary is still multipart. It is unreadable, but
// reporting it as bad JSON would tell the caller to fix the wrong thing.
func multipartContentType(contentType string) (boundary string, ok bool) {
	mediaType, params, err := mime.ParseMediaType(contentType)
	if err != nil {
		return "", false
	}
	if !strings.HasPrefix(mediaType, "multipart/") {
		return "", false
	}
	return params["boundary"], true
}

// modelFromMultipart reads the `model` form field out of a buffered multipart
// body.
//
// File parts are skipped without being read. The body is already in memory
// under the body cap, and an audio upload is most of it — walking to the
// field that names the model must not cost a second copy of the recording.
func modelFromMultipart(body []byte, boundary string) (string, error) {
	if boundary == "" {
		return "", errNoBoundary
	}
	reader := multipart.NewReader(bytes.NewReader(body), boundary)
	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			return "", nil
		}
		if err != nil {
			return "", err
		}
		if part.FormName() != "model" || part.FileName() != "" {
			_ = part.Close()
			continue
		}
		value, err := io.ReadAll(io.LimitReader(part, maxModelFieldBytes))
		_ = part.Close()
		if err != nil {
			return "", err
		}
		return string(value), nil
	}
}

// withModelMultipart returns the body with the `model` part replaced by the
// name the engine answers to, and the Content-Type that describes it.
//
// Every other part is copied through with its own headers intact, so a file
// part keeps its filename and its content type. The boundary changes because
// the writer picks its own, which is why the caller must take the returned
// content type as well as the returned body.
//
// This costs a copy of the upload, and it is only paid when an upstream's
// served name actually differs from what the caller sent. The alternative —
// forwarding the body untouched — routes to a name the engine does not know
// and turns a served-name mismatch into a 404 from inside the cluster, which
// is the one failure this hop exists to prevent.
func withModelMultipart(body []byte, boundary string, model string) (rewritten []byte, contentType string, err error) {
	reader := multipart.NewReader(bytes.NewReader(body), boundary)
	var out bytes.Buffer
	writer := multipart.NewWriter(&out)

	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, "", err
		}

		header := textproto.MIMEHeader{}
		for name, values := range part.Header {
			header[name] = append([]string(nil), values...)
		}

		destination, err := writer.CreatePart(header)
		if err != nil {
			_ = part.Close()
			return nil, "", err
		}
		if part.FormName() == "model" && part.FileName() == "" {
			_, err = io.WriteString(destination, model)
		} else {
			_, err = io.Copy(destination, part)
		}
		_ = part.Close()
		if err != nil {
			return nil, "", err
		}
	}

	if err := writer.Close(); err != nil {
		return nil, "", err
	}
	return out.Bytes(), writer.FormDataContentType(), nil
}
