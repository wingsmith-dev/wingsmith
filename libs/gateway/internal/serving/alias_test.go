package serving

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hangar-dev/hangar/libs/gateway/internal/metrics"
	"github.com/hangar-dev/hangar/libs/gateway/internal/routing"
)

// aliased binds one route under its canonical name and under every alias, which
// is exactly what routing's table does. The Route's own Name stays the
// canonical, because that is the only name anything past resolution ever sees.
func aliased(route routing.Route, aliases ...string) map[string]routing.Route {
	routes := map[string]routing.Route{route.Name: route}
	for _, alias := range aliases {
		routes[alias] = route
	}
	return routes
}

func TestAnAliasRoutesToTheSameUpstreamAsTheCanonical(t *testing.T) {
	server, _ := upstreamServer(t)
	route := proxyRoute(t, "ep_1", "reasoning-model", "dep_1", "ap", server.URL)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: aliased(route, "org/reasoning-model")}})

	rec := post(t, handler, `{"model":"org/reasoning-model","messages":[]}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

func TestAnAliasCannotWalkPastTheDenyRuleWrittenAgainstTheModelItPointsAt(t *testing.T) {
	// Resolution runs before policy for exactly this reason. If a term were
	// judged against the caller's own string, publishing an alias would be a way
	// to sell an endpoint without the terms it was sold under.
	server, _ := upstreamServer(t)
	pinned := routeTo(t, server.URL, nil)
	pinned.Policy = referencePolicy()
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: aliased(pinned, "chat-legacy")}})

	for _, model := range []string{"chat", "chat-legacy"} {
		rec := post(t, handler, `{"model":"`+model+`","temperature":0.7}`)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d, want the endpoint's own terms enforced", model, rec.Code)
		}
	}
}

func TestATermThatAdmitsTheCanonicalAdmitsItsAliasesToo(t *testing.T) {
	// The other direction of the same property: a policy is a fact about the
	// endpoint, so every name that reaches it is judged identically.
	server, _ := upstreamServer(t)
	pinned := routeTo(t, server.URL, nil)
	pinned.Policy = referencePolicy()
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: aliased(pinned, "chat-legacy")}})

	for _, model := range []string{"chat", "chat-legacy"} {
		rec := post(t, handler, `{"model":"`+model+`","temperature":1}`)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: status = %d, body = %s", model, rec.Code, rec.Body.String())
		}
	}
}

func TestUsageAndMetricsAreKeyedOnTheCanonicalWhateverTheCallerTyped(t *testing.T) {
	// This is what makes a rename cheap. A series split by the name a caller
	// happened to use would make an alias a second model in every dashboard.
	server, _ := upstreamServer(t)
	recorder := metrics.New()
	route := proxyRoute(t, "ep_1", "reasoning-model", "dep_1", "ap", server.URL)
	handler := New(Config{Zone: ZoneInternal, Metrics: recorder, Router: stubResolver{routes: aliased(route, "org/reasoning-model")}})

	post(t, handler, `{"model":"org/reasoning-model"}`)

	match := map[string]string{"hangar_endpoint": "ep_1", "endpoint_name": "reasoning-model"}
	if got := requests(t, recorder, match); got != 1 {
		t.Fatalf("%v = %v, want 1 (series present: %v)", match, got, allSeries(t, recorder))
	}
	if got := requests(t, recorder, map[string]string{"endpoint_name": "org/reasoning-model"}); got != 0 {
		t.Fatalf("the alias reached a metric label: %v", allSeries(t, recorder))
	}
}

func TestTheResponseEchoesTheStringTheCallerSent(t *testing.T) {
	// The reported bug: a client that reads `model` off a response and reuses it
	// got back a name the same endpoint refuses. Nothing rewrites the outbound
	// body (there is no SSE frame rewriter here and this change does not add
	// one), so the property is bought by not rewriting the inbound one either
	// when the upstream declared no served name of its own.
	server, _ := upstreamServer(t)
	route := proxyRoute(t, "ep_1", "reasoning-model", "dep_1", "ap", server.URL)
	handler := New(Config{Zone: ZoneInternal, Router: stubResolver{routes: aliased(route, "org/reasoning-model")}})

	rec := post(t, handler, `{"model":"org/reasoning-model","messages":[]}`)
	var echoed struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &echoed); err != nil {
		t.Fatalf("decode: %v (body %s)", err, rec.Body.String())
	}
	if echoed.Model != "org/reasoning-model" {
		t.Fatalf("model = %q, want the caller's own string back", echoed.Model)
	}
}

func TestTheCatalogNeverListsAnAlias(t *testing.T) {
	route := routing.NewRoute("reasoning-model", "weighted")
	handler := New(Config{Zone: ZoneInternal, Router: canonicalsOnly{routes: aliased(route, "org/reasoning-model"), canonical: []string{"reasoning-model"}}})

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/models", nil))

	var list modelList
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(list.Data) != 1 || list.Data[0].ID != "reasoning-model" {
		t.Fatalf("catalog = %+v, want only the canonical", list.Data)
	}
	if strings.Contains(rec.Body.String(), "org/reasoning-model") {
		t.Fatalf("an alias reached a client's picker: %s", rec.Body.String())
	}
}

// canonicalsOnly resolves every key alike but names only the canonicals, which
// is the contract routing.Router keeps.
type canonicalsOnly struct {
	routes    map[string]routing.Route
	canonical []string
}

func (c canonicalsOnly) Resolve(name string) (routing.Route, error) {
	route, ok := c.routes[name]
	if !ok {
		return routing.Route{}, routing.ErrUnknownEndpoint
	}
	return route, nil
}

func (c canonicalsOnly) Names() []string                  { return c.canonical }
func (c canonicalsOnly) NamesForTenant(_ string) []string { return c.canonical }
func (c canonicalsOnly) Revision() string                 { return "1" }
