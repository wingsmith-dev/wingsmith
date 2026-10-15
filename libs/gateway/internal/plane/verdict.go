package plane

import (
	"encoding/json"
	"io"
)

// verdictUnknownToken is the plane's verdict on a token that names no cluster.
const verdictUnknownToken = "unknown_token"

// verdictOf reads the verdict from the plane's error body, or empty when there
// is none.
func verdictOf(body io.Reader) string {
	var decoded struct {
		Detail struct {
			Verdict string `json:"verdict"`
		} `json:"detail"`
	}
	if err := json.NewDecoder(io.LimitReader(body, 64<<10)).Decode(&decoded); err != nil {
		return ""
	}
	return decoded.Detail.Verdict
}
