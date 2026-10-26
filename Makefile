# OpenSandbox on Kubernetes. The kind-* targets run it on a local cluster shaped like
# techx-xora-nonprod-eks (k8s/kind.yaml); render, install, smoke, forward and check work on any
# cluster through KUBE_CONTEXT and VALUES.
-include .env
export

OSB_VERSION := 1.1.1
RUN_DIR := .run
CHART := charts/opensandbox

KIND_CLUSTER := osb-nonprod
KUBE_CONTEXT ?= kind-$(KIND_CLUSTER)
VALUES ?= k8s/values-kind.yaml
KUBECTL := kubectl --context $(KUBE_CONTEXT)
CALICO_VERSION := v3.33.0
# Where forward holds the server for a client (8090 is xora-sandbox's), and where smoke reaches
# it while forward runs.
PORT := 8081
SMOKE_PORT := 8082

.PHONY: deps render install smoke forward check lint charts \
	kind-up kind-cluster kind-install kind-load kind-down

deps:
	@helm dependency build $(CHART) >&2

# The manifests the cluster gets, for review: make render VALUES=k8s/values-eks.yaml
render: deps
	@cat k8s/manifests.yaml
	@echo ---
	@helm template opensandbox $(CHART) -n opensandbox-system -f k8s/values.yaml -f $(VALUES)

# Needs the opensandbox-api-key Secret in opensandbox-system (README, "API key").
install: deps
	$(KUBECTL) apply -f k8s/manifests.yaml
	helm upgrade --install opensandbox $(CHART) --kube-context $(KUBE_CONTEXT) \
		-n opensandbox-system -f k8s/values.yaml -f $(VALUES) --wait --timeout 10m

# The platform smoke test, through a port-forward to the server.
smoke:
	@mkdir -p $(RUN_DIR)
	@$(KUBECTL) -n opensandbox-system port-forward svc/opensandbox-server $(SMOKE_PORT):80 \
		> $(RUN_DIR)/port-forward.log 2>&1 & pf=$$!; \
	for i in $$(seq 1 20); do curl -sf -m 2 http://127.0.0.1:$(SMOKE_PORT)/health >/dev/null && break; sleep 1; done; \
	OPENSANDBOX_DOMAIN=127.0.0.1:$(SMOKE_PORT) uv run -q --no-project --python 3.11 \
		--with opensandbox==$(OSB_VERSION) python smoke/platform_smoke.py; rc=$$?; \
	kill $$pf; exit $$rc

# The server for a client on this machine (xora-sandbox): OPENSANDBOX_DOMAIN=127.0.0.1:$(PORT).
forward:
	$(KUBECTL) -n opensandbox-system port-forward svc/opensandbox-server $(PORT):80

# What the server's and controller's service accounts may read, and where their pods run.
check:
	@for sa in opensandbox-server opensandbox-controller-manager; do \
		for ns in opensandbox opensandbox-system kube-system; do \
			printf '%-32s get secrets -n %-20s %s\n' $$sa $$ns \
				"$$($(KUBECTL) auth can-i get secrets -n $$ns --as=system:serviceaccount:opensandbox-system:$$sa)"; \
		done; \
	done
	@$(KUBECTL) get pods -A -o wide --field-selector=metadata.namespace!=kube-system \
		| awk 'NR==1 || /opensandbox/ {print $$1, $$2, $$4, $$8}' | column -t

lint:
	uvx ruff check smoke && uvx ruff format --check smoke

# Rebuild charts/ from the release tag and k8s/chart.patch (a new release: change OSB_VERSION,
# fix the patch if it no longer applies, and review the diff of charts/).
charts:
	@rm -rf $(RUN_DIR)/OpenSandbox $(RUN_DIR)/charts
	@mkdir -p $(RUN_DIR)
	git clone -q --depth 1 --branch release-$(OSB_VERSION) https://github.com/opensandbox-group/OpenSandbox.git $(RUN_DIR)/OpenSandbox
	cp -R $(RUN_DIR)/OpenSandbox/manifests/charts $(RUN_DIR)/charts
	patch -s -d $(RUN_DIR) -p1 < k8s/chart.patch
	rm -rf charts && mv $(RUN_DIR)/charts charts

kind-up: kind-cluster kind-install smoke check

kind-cluster:
	@kind get clusters 2>/dev/null | grep -qx $(KIND_CLUSTER) || kind create cluster --config k8s/kind.yaml
	$(KUBECTL) apply -f https://raw.githubusercontent.com/projectcalico/calico/$(CALICO_VERSION)/manifests/calico.yaml
	$(KUBECTL) wait --for=condition=Ready nodes --all --timeout=300s

# On kind the API key Secret comes from .env.
kind-install:
	@test -n "$(OPENSANDBOX_SERVER_API_KEY)" || (echo "OPENSANDBOX_SERVER_API_KEY is empty: copy .env.example to .env" && exit 1)
	$(KUBECTL) apply -f k8s/manifests.yaml
	@$(KUBECTL) -n opensandbox-system create secret generic opensandbox-api-key \
		--from-literal=api-key="$(OPENSANDBOX_SERVER_API_KEY)" --dry-run=client -o yaml | $(KUBECTL) apply -f -
	$(MAKE) install KUBE_CONTEXT=kind-$(KIND_CLUSTER) VALUES=k8s/values-kind.yaml

# A local image (a client's sandbox image) onto the cluster's nodes.
kind-load:
	@test -n "$(IMAGE)" || (echo "IMAGE is empty: make kind-load IMAGE=<name:tag>" && exit 1)
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)

kind-down:
	kind delete cluster --name $(KIND_CLUSTER)
