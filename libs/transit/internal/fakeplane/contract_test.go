package fakeplane

import (
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// gatewayPlane is the gateway's plane client, relative to this package. The
// fake cannot import it (it is internal to another module), so the two are
// held together by reading its source.
const gatewayPlane = "../../../gateway/internal/plane"

// TestTheWireTypesMatchTheGatewaysClient fails when a JSON field exists on one
// side and not the other. A field the gateway gained and the fake never sends
// is how this harness drifted before: it kept running, against a contract the
// gateway no longer had.
func TestTheWireTypesMatchTheGatewaysClient(t *testing.T) {
	gateway := gatewayStructs(t)

	pairs := map[string]any{
		"Upstream":           Upstream{},
		"Rule":               Rule{},
		"Policy":             Policy{},
		"Endpoint":           Endpoint{},
		"Table":              Table{},
		"GatewayTenant":      GatewayTenant{},
		"TenantTable":        TenantTable{},
		"tunnelAuthResponse": Cluster{},
	}
	for name, fake := range pairs {
		want, ok := gateway[name]
		if !ok {
			t.Errorf("the gateway's plane package no longer declares %s; update the pairs in this test", name)
			continue
		}
		got := jsonFields(reflect.TypeOf(fake))
		if !reflect.DeepEqual(got, want) {
			t.Errorf("%s: fake fields %v, gateway fields %v", name, got, want)
		}
	}
}

// TestEveryRouteTheGatewayCallsIsServed fails when the gateway's plane client
// names a /v1 path this fake does not answer.
func TestEveryRouteTheGatewayCallsIsServed(t *testing.T) {
	h := New(Config{}).Handler()
	for _, path := range gatewayPaths(t) {
		method := http.MethodGet
		if strings.HasSuffix(path, "/tunnel-auth") {
			method = http.MethodPost
		}
		req := httptest.NewRequest(method, path, strings.NewReader(`{"token":""}`))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code == http.StatusNotFound || rec.Code == http.StatusMethodNotAllowed {
			t.Errorf("%s %s = %d: the gateway calls it and the fake does not serve it", method, path, rec.Code)
		}
	}
}

func gatewaySources(t *testing.T) (*token.FileSet, []*ast.File) {
	t.Helper()
	matches, err := filepath.Glob(filepath.Join(gatewayPlane, "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	if len(matches) == 0 {
		if _, err := os.Stat("../../../gateway"); err != nil {
			t.Skip("libs/gateway is not beside libs/transit in this checkout")
		}
		t.Fatalf("no Go files under %s", gatewayPlane)
	}
	fset := token.NewFileSet()
	var files []*ast.File
	for _, path := range matches {
		if strings.HasSuffix(path, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		files = append(files, file)
	}
	return fset, files
}

func gatewayStructs(t *testing.T) map[string][]string {
	t.Helper()
	_, files := gatewaySources(t)
	out := map[string][]string{}
	for _, file := range files {
		ast.Inspect(file, func(n ast.Node) bool {
			spec, ok := n.(*ast.TypeSpec)
			if !ok {
				return true
			}
			st, ok := spec.Type.(*ast.StructType)
			if !ok {
				return true
			}
			var names []string
			for _, field := range st.Fields.List {
				if field.Tag == nil {
					continue
				}
				raw, err := strconv.Unquote(field.Tag.Value)
				if err != nil {
					continue
				}
				if name := tagName(reflect.StructTag(raw)); name != "" {
					names = append(names, name)
				}
			}
			if len(names) > 0 {
				sort.Strings(names)
				out[spec.Name.Name] = names
			}
			return true
		})
	}
	return out
}

func gatewayPaths(t *testing.T) []string {
	t.Helper()
	_, files := gatewaySources(t)
	seen := map[string]bool{}
	for _, file := range files {
		ast.Inspect(file, func(n ast.Node) bool {
			lit, ok := n.(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				return true
			}
			value, err := strconv.Unquote(lit.Value)
			if err == nil && strings.HasPrefix(value, "/v1/") {
				seen[value] = true
			}
			return true
		})
	}
	if len(seen) == 0 {
		t.Fatal("found no /v1 paths in the gateway's plane client")
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

func jsonFields(typ reflect.Type) []string {
	var names []string
	for i := 0; i < typ.NumField(); i++ {
		if name := tagName(typ.Field(i).Tag); name != "" {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

func tagName(tag reflect.StructTag) string {
	name, _, _ := strings.Cut(tag.Get("json"), ",")
	if name == "-" {
		return ""
	}
	return name
}
