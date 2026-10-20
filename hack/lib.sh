# Shared settings and helpers for the soc-poc scripts. Source it; don't run it.
# shellcheck shell=bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Your Upbound organization: the control planes, robot and team are created there.
ORG="${ORG:?set ORG to your Upbound organization, e.g. make setup ORG=acme}"
SPACE="${SPACE:-upbound-gcp-us-central-1}"
GROUP="${GROUP:-soc-poc}"
NET_CP="${NET_CP:-soc-poc-network}"
DB_CP="${DB_CP:-soc-poc-database}"
VERSION="${VERSION:-v0.1.0}"
# Where the soc-poc packages are pulled from. The published ones are public.
REGISTRY="${REGISTRY:-xpkg.upbound.io/solutions}"
NETWORK_PKG="${NETWORK_PKG:-xpkg.upbound.io/upbound/configuration-aws-network:v2.2.0}"
NETWORK_XRD_URL="${NETWORK_XRD_URL:-https://raw.githubusercontent.com/upbound/configuration-aws-network/v2.2.0/apis/networks/definition.yaml}"
# The IAM role the AWS providers assume with Upbound web identity (setup only; see README).
AWS_ROLE_ARN="${AWS_ROLE_ARN:-}"
REMOTE_NS="${REMOTE_NS:-ctp-database}"   # the database CP's namespace on the network CP
TEAM="${TEAM:-soc-poc-consumers}"
ROBOT="${ROBOT:-soc-poc-database}"
# Optional: a docker-registry token (robot token JSON from `up robot token create`)
# for pulling the private soc-poc packages. Unset means the repositories are public.
PULL_TOKEN_FILE="${PULL_TOKEN_FILE:-}"

WORK="${WORK:-${ROOT}/_output/live}"
mkdir -p "${WORK}"

kc_net() { kubectl --kubeconfig "${WORK}/network.kubeconfig" "$@"; }
kc_db() { kubectl --kubeconfig "${WORK}/database.kubeconfig" "$@"; }
kc_space() { kubectl --kubeconfig "${WORK}/space.kubeconfig" "$@"; }

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok() { printf '\033[32m  ok\033[0m %s\n' "$*"; }
fail() { printf '\033[31m  FAIL\033[0m %s\n' "$*"; FAILED=$((${FAILED:-0} + 1)); }

# wait_for <timeout-seconds> <description> <command...>: poll until the command succeeds.
wait_for() {
  local timeout="$1" what="$2"; shift 2
  local start=$SECONDS
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS - start > timeout )); then
      echo "  timed out after ${timeout}s waiting for ${what}" >&2
      return 1
    fi
    sleep 10
  done
}

# cond_is <kc-fn> <resource> <condition> <status> [kubectl args...]: one-shot check.
# Polled with wait_for instead of `kubectl wait`, whose filtered watch the Spaces
# API gateway rejects.
cond_is() {
  local kc=$1 res=$2 cond=$3 want=$4; shift 4
  [[ "$("${kc}" get "${res}" "$@" -o "jsonpath={.status.conditions[?(@.type==\"${cond}\")].status}" 2>/dev/null)" == "${want}" ]]
}

# kubeconfigs <ctp>: write a kubeconfig for a control plane (or the group, with no argument).
kubeconfig_for() {
  local path="${ORG}/${SPACE}/${GROUP}${1:+/$1}"
  up ctx "${path}" -f - > "${WORK}/${2}.kubeconfig"
}

# remote_name <local-ns> <local-name>: the remote XR name the proxy derives.
remote_name() {
  printf 'network-%s' "$(printf '%s/%s' "$1" "$2" | shasum -a 256 | cut -c1-10)"
}
