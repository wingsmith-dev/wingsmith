BIN            := $(CURDIR)/bin
CONTROLLER_GEN := $(BIN)/controller-gen
CONTROLLER_TOOLS_VERSION ?= v0.19.0
IMG            ?= ghcr.io/hangar-dev/operator:dev
VERSION        ?= dev

.PHONY: all
all: generate manifests build test

$(CONTROLLER_GEN):
	GOBIN=$(BIN) go install sigs.k8s.io/controller-tools/cmd/controller-gen@$(CONTROLLER_TOOLS_VERSION)

.PHONY: generate
generate: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) object:headerFile="" paths=./api/...

.PHONY: manifests
manifests: $(CONTROLLER_GEN)
	$(CONTROLLER_GEN) crd paths=./api/... output:crd:artifacts:config=deploy/crd
	$(CONTROLLER_GEN) rbac:roleName=hangar-operator paths=./... output:rbac:artifacts:config=deploy/rbac

.PHONY: verify-manifests
verify-manifests: generate manifests
	@git diff --exit-code -- api deploy/crd deploy/rbac || \
		{ echo "generated code, CRDs or RBAC are stale: run 'make generate manifests' and commit the result" >&2; exit 1; }
	@untracked="$$(git ls-files --others --exclude-standard -- api deploy/crd deploy/rbac)"; \
		if [ -n "$$untracked" ]; then echo "untracked generated files: $$untracked" >&2; exit 1; fi

.PHONY: build
build:
	go build -ldflags "-X main.version=$(VERSION)" -o $(BIN)/operator ./cmd/operator

.PHONY: vet
vet:
	go vet ./...
	go vet -tags e2e ./internal/reconcile/
	go vet -tags interop ./internal/interop/

.PHONY: test
test:
	go test -race ./...

.PHONY: docker-build
docker-build:
	docker build --build-arg VERSION=$(VERSION) -t $(IMG) .

.PHONY: kind-load
kind-load: docker-build
	kind load docker-image $(IMG) --name hangar

.PHONY: deploy
deploy:
	kubectl apply -k deploy

.PHONY: undeploy
undeploy:
	kubectl delete -k deploy --ignore-not-found

.PHONY: clean
clean:
	rm -rf $(BIN)
