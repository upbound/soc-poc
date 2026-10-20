# soc-poc: split Network and Database onto two control planes, and prove the contract.
#   make unit lint           host-side tests (in a python:3.13 container, nothing installed locally)
#   make gen check           generate the Network proxy from the upstream XRD, and check it is byte-identical
#   make push                maintainers: build, test and push proxy-base -> network-proxy -> database-service
#   make setup verify demo   live: create and wire the two control planes, run C1-C9, walk through it
#                            (needs ORG=<your Upbound org>, and AWS_ROLE_ARN for setup)
#   make teardown            delete the AWS resources (DELETE_CONTROL_PLANES=1 also removes the CPs)

VERSION ?= v0.1.0
PUBLIC ?=
PUSH := up project push --tag $(VERSION) $(if $(PUBLIC),--public)
# The published, public soc-poc packages. `make push` writes to the repositories
# named in each upbound.yaml; change those too if you publish your own copy.
REGISTRY ?= xpkg.upbound.io/solutions
NETWORK_XRD_URL ?= https://raw.githubusercontent.com/upbound/configuration-aws-network/v2.2.0/apis/networks/definition.yaml
PROXY_OUT := _output/network-proxy

PY := docker run --rm -v $(CURDIR):/src -w /src python:3.13-slim sh -c
PIP := pip install -q --root-user-action=ignore --disable-pip-version-check
GEN := cd soc-proxy && python -m soc_proxy_gen

.PHONY: unit lint gen check test push setup verify demo teardown

unit:
	$(PY) '$(PIP) crossplane-function-sdk-python==0.15.1 pyyaml==6.0.2 && cd soc-proxy && python -m unittest discover -s tests -t .'

lint:
	$(PY) '$(PIP) ruff==0.16.9 && ruff check .'

gen:
	$(PY) '$(PIP) pyyaml==6.0.2 && $(GEN) --xrd $(NETWORK_XRD_URL) --example ../examples/network-a.yaml \
	  --name soc-poc-network-proxy --repository $(REGISTRY)/soc-poc-network-proxy \
	  --proxy-base $(REGISTRY)/soc-poc-proxy-base --proxy-base-version $(VERSION) --out ../$(PROXY_OUT)'

check:
	$(PY) '$(PIP) pyyaml==6.0.2 && $(GEN) --check --xrd $(NETWORK_XRD_URL) --out ../$(PROXY_OUT)'

# Each project resolves the one before it from the registry, so test and push in order.
push: unit lint gen check
	cd proxy-base && up test run 'tests/*' && up project build && $(PUSH)
	cd $(PROXY_OUT) && up test run 'tests/*' && up project build && $(PUSH)
	cd database-service && up test run 'tests/*' && up project build && $(PUSH)

test: unit lint check
	cd proxy-base && up test run 'tests/*'
	cd $(PROXY_OUT) && up test run 'tests/*'
	cd database-service && up test run 'tests/*'

setup:
	ORG=$(ORG) REGISTRY=$(REGISTRY) VERSION=$(VERSION) hack/setup.sh

verify:
	ORG=$(ORG) REGISTRY=$(REGISTRY) VERSION=$(VERSION) hack/verify.sh

demo:
	ORG=$(ORG) REGISTRY=$(REGISTRY) VERSION=$(VERSION) hack/demo.sh

teardown:
	ORG=$(ORG) hack/teardown.sh
