package api

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
)

func TestAManifestIsReadByNamespaceAndNameAndAPublicArtifactByNameAlone(t *testing.T) {
	var paths []string
	c, _ := serveJSON(t, func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.EscapedPath())
		_, _ = w.Write([]byte(`{"apiVersion":"plane.wingsmith.dev/v1alpha1","kind":"Workload"}`))
	})
	m, err := c.Manifest(context.Background(), "workloads", "search", "qwen")
	if err != nil || m["kind"] != "Workload" {
		t.Fatalf("got %v, %v", m, err)
	}
	if _, err := c.Manifest(context.Background(), "artifacts", "", "gpt-oss"); err != nil {
		t.Fatal(err)
	}
	want := []string{"/v1/manifests/workloads/search/qwen", "/v1/manifests/artifacts/gpt-oss"}
	if len(paths) != 2 || paths[0] != want[0] || paths[1] != want[1] {
		t.Errorf("paths %v, want %v", paths, want)
	}
}

// The embedded schema is what apply and diff will read; it has to name every kind
// the CLI knows and carry the markers the OpenAPI document drops.
func TestTheEmbeddedManifestSchemaCoversEveryKindAndCarriesItsMarkers(t *testing.T) {
	var doc struct {
		Title string                     `json:"title"`
		Defs  map[string]json.RawMessage `json:"$defs"`
	}
	if err := json.Unmarshal(ManifestSchema, &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Title != ManifestAPIVersion {
		t.Errorf("title %q", doc.Title)
	}
	for kind := range ManifestKinds {
		if _, ok := doc.Defs[kind+"Manifest"]; !ok {
			t.Errorf("no %sManifest in the schema", kind)
		}
	}
	var spec struct {
		Properties map[string]map[string]any `json:"properties"`
	}
	if err := json.Unmarshal(doc.Defs["WorkloadManifestSpec"], &spec); err != nil {
		t.Fatal(err)
	}
	if spec.Properties["gang_size"]["x-immutable"] != true || spec.Properties["priority_class"]["x-defaulted"] != true {
		t.Errorf("markers missing: %v", spec.Properties)
	}
}
