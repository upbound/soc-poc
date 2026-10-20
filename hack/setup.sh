#!/usr/bin/env bash
# Create the two control planes and wire them together:
#   soc-poc-network   provider: the Network service (configuration-aws-network, unchanged)
#   soc-poc-database  consumer: the Database service plus the generated Network proxy
# Idempotent. Kubeconfigs and tokens go to _output/live, never to ~/.kube/config.
source "$(dirname "$0")/lib.sh"
: "${AWS_ROLE_ARN:?set AWS_ROLE_ARN to an IAM role that trusts Upbound OIDC for your control planes (see README)}"

CROSSPLANE_CHANNEL="${CROSSPLANE_CHANNEL:-Stable}"

step "Group ${ORG}/${SPACE}/${GROUP} and control planes"
up ctx "${ORG}/${SPACE}" -f - > "${WORK}/space-root.kubeconfig"
up group get "${GROUP}" --kubeconfig "${WORK}/space-root.kubeconfig" >/dev/null 2>&1 \
  || up group create "${GROUP}" --kubeconfig "${WORK}/space-root.kubeconfig"
for cp in "${NET_CP}" "${DB_CP}"; do
  up ctp get "${cp}" -g "${GROUP}" --kubeconfig "${WORK}/space-root.kubeconfig" >/dev/null 2>&1 \
    || up ctp create "${cp}" -g "${GROUP}" --crossplane-channel "${CROSSPLANE_CHANNEL}" \
      --kubeconfig "${WORK}/space-root.kubeconfig"
done
kubeconfig_for "" space
for cp in "${NET_CP}" "${DB_CP}"; do
  wait_for 900 "control plane ${cp} Ready" \
    cond_is kc_space "controlplane.spaces.upbound.io/${cp}" Ready True
done
kubeconfig_for "${NET_CP}" network
kubeconfig_for "${DB_CP}" database
for kc in kc_net kc_db; do
  "${kc}" get deploy -n crossplane-system crossplane -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
done

if [[ -n "${PULL_TOKEN_FILE}" ]]; then
  step "Pull access to ${REGISTRY}/soc-poc-* on both control planes"
  user=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["accessId"])' "${PULL_TOKEN_FILE}")
  pass=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "${PULL_TOKEN_FILE}")
  for kc in kc_net kc_db; do
    "${kc}" -n crossplane-system create secret docker-registry soc-poc-pull \
      --docker-server=xpkg.upbound.io --docker-username="${user}" --docker-password="${pass}" \
      --dry-run=client -o yaml | "${kc}" apply -f -
    "${kc}" apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1beta1
kind: ImageConfig
metadata:
  name: soc-poc-pull
spec:
  matchImages:
  - type: Prefix
    prefix: ${REGISTRY}/soc-poc-
  registry:
    authentication:
      pullSecretRef:
        name: soc-poc-pull
EOF
  done
fi

step "Network CP: the Network service, and the database CP's tenant namespace ${REMOTE_NS}"
kc_net apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: configuration-aws-network
spec:
  package: ${NETWORK_PKG}
---
apiVersion: v1
kind: Namespace
metadata:
  name: ${REMOTE_NS}
EOF
wait_for 900 "configuration-aws-network healthy" \
  cond_is kc_net configuration.pkg.crossplane.io/configuration-aws-network Healthy True
wait_for 600 "aws ProviderConfig CRD" kc_net get crd providerconfigs.aws.m.upbound.io
kc_net apply -f - <<EOF
apiVersion: aws.m.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
  namespace: ${REMOTE_NS}
spec:
  credentials:
    source: Upbound
    upbound:
      webIdentity:
        roleARN: ${AWS_ROLE_ARN}
EOF

step "Database CP: the Database service (pulls in the Network proxy and proxy-base)"
kc_db apply -f - <<EOF
apiVersion: pkg.crossplane.io/v1
kind: Configuration
metadata:
  name: soc-poc-database-service
spec:
  package: ${REGISTRY}/soc-poc-database-service:${VERSION}
---
apiVersion: v1
kind: Namespace
metadata:
  name: team-a
EOF
wait_for 900 "soc-poc-database-service healthy" \
  cond_is kc_db configuration.pkg.crossplane.io/soc-poc-database-service Healthy True
wait_for 600 "aws ProviderConfig CRD" kc_db get crd providerconfigs.aws.m.upbound.io
kc_db apply -f - <<EOF
apiVersion: aws.m.upbound.io/v1beta1
kind: ProviderConfig
metadata:
  name: default
  namespace: team-a
spec:
  credentials:
    source: Upbound
    upbound:
      webIdentity:
        roleARN: ${AWS_ROLE_ARN}
EOF

step "Identity: robot ${ROBOT} in team ${TEAM}, editor on group ${GROUP}"
up team get "${TEAM}" >/dev/null 2>&1 || up team create "${TEAM}"
up robot get "${ROBOT}" >/dev/null 2>&1 || up robot create "${ROBOT}" --description "soc-poc: database CP calling the network CP"
up robot team list "${ROBOT}" 2>/dev/null | grep -q "${TEAM}" || up robot team join "${TEAM}" "${ROBOT}"
team_id=$(up team get "${TEAM}" --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
kc_space apply -f - <<EOF
apiVersion: authorization.spaces.upbound.io/v1alpha1
kind: ObjectRoleBinding
metadata:
  name: ${TEAM}
  namespace: ${GROUP}
spec:
  object:
    apiGroup: core
    resource: namespaces
    name: ${GROUP}
  subjects:
  - kind: UpboundTeam
    name: ${team_id}
    role: editor
EOF
if [[ ! -s "${WORK}/robot-token.json" ]]; then
  up robot token create "${ROBOT}" "soc-poc-$(date +%s)" -f "${WORK}/robot-token.json"
fi

step "Database CP: ProviderConfig and ProxyConfig that reach the network CP"
# The kubeconfig keeps the `up organization token` exec user; provider-kubernetes'
# UpboundTokens identity swaps it for the robot token at runtime.
up ctx "${ORG}/${SPACE}/${GROUP}/${NET_CP}" -f - \
  | yq '(.users[].user.exec.env) = [{"name": "ORGANIZATION", "value": "'"${ORG}"'"}]' > "${WORK}/network-cp-for-provider.kubeconfig"
kc_db -n crossplane-system create secret generic network-service-cp \
  --from-file=kubeconfig="${WORK}/network-cp-for-provider.kubeconfig" --dry-run=client -o yaml | kc_db apply -f -
kc_db -n crossplane-system create secret generic network-service-cp-token \
  --from-literal=token="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["token"])' "${WORK}/robot-token.json")" \
  --dry-run=client -o yaml | kc_db apply -f -
wait_for 600 "provider-kubernetes CRDs" kc_db get crd clusterproviderconfigs.kubernetes.m.crossplane.io
kc_db apply -f - <<EOF
apiVersion: kubernetes.m.crossplane.io/v1alpha1
kind: ClusterProviderConfig
metadata:
  name: network-service-cp
spec:
  credentials:
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: network-service-cp
      key: kubeconfig
  identity:
    type: UpboundTokens
    source: Secret
    secretRef:
      namespace: crossplane-system
      name: network-service-cp-token
      key: token
EOF
kc_db apply -f "${ROOT}/proxy-base/examples/proxyconfigs/networks.yaml"
kc_db get proxyconfigs.soc.upbound.io

bold "Setup done. Kubeconfigs: ${WORK}/network.kubeconfig, ${WORK}/database.kubeconfig"
