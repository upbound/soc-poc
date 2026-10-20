"""SQLInstance: an RDS database placed in a local Network.

The Network is resolved the standard way: a required resource in the XR's own
namespace, selected by name (networkRef) or labels (networkSelector), read
through its status. The database never looks anything up in AWS. On a consumer
of the Network service that Network is a proxy XR, which this function cannot
tell apart from a local one.
"""

import base64

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1
from models.io.upbound.m.aws.ec2.securitygroup import v1beta1 as sgv1beta1
from models.io.upbound.m.aws.ec2.securitygroupingressrule import v1beta1 as sgrv1beta1
from models.io.upbound.m.aws.rds.instance import v1beta1 as instancev1beta1
from models.io.upbound.m.aws.rds.subnetgroup import v1beta1 as subnetgroupv1beta1

NETWORK_API = "aws.platform.upbound.io/v1alpha1"
NETWORK_REQ = "network"
PORTS = {"postgres": 5432, "mariadb": 3306}
DEFAULT_CIDR = "192.168.0.0/16"

# Composition keys. The first two match configuration-aws-database.
SUBNET_GROUP = "compositeSQLInstanceDbSubnetGroup"
INSTANCE = "RDSInstanceSmall"
SECURITY_GROUP = "securityGroup"
INGRESS = "securityGroupIngressRule"
SECRET = "connectionSecret"
USAGES = {
    "usageNetworkByInstance": ("rds.aws.m.upbound.io/v1beta1", "Instance"),
    "usageNetworkBySecurityGroup": ("ec2.aws.m.upbound.io/v1beta1", "SecurityGroup"),
    "usageNetworkBySubnetGroup": ("rds.aws.m.upbound.io/v1beta1", "SubnetGroup"),
}


def dig(obj, *path, default=None):
    """Walk nested dicts, returning default when any step is missing."""
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return default
        obj = obj[p]
    return obj


def is_ready(obj: dict) -> bool:
    """True if the object reports Ready=True."""
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in dig(obj, "status", "conditions", default=[]) or [])


def resolve_network(networks: list[dict]) -> dict | None:
    """Pick the first Ready Network by name, and read what the database needs."""
    for net in sorted(networks, key=lambda n: n["metadata"]["name"]):
        subnets = dig(net, "status", "privateSubnetIds") or dig(net, "status", "subnetIds") or []
        vpc = dig(net, "status", "vpcId")
        if not is_ready(net) or not vpc or not subnets:
            continue
        return {
            "network": net["metadata"]["name"],
            "vpcId": vpc,
            "subnetIds": list(subnets),
            "cidrBlock": dig(net, "spec", "parameters", "vpcCidrBlock", default=DEFAULT_CIDR),
        }
    return None


def b64(s: str | bytes) -> str:
    """Base64 for Secret data."""
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


class FunctionRunner(grpcv1.FunctionRunnerService):
    """A FunctionRunner handles gRPC RunFunctionRequests."""

    def __init__(self):
        """Create a new FunctionRunner."""
        self.log = logging.get_logger()

    async def RunFunction(
        self, req: fnv1.RunFunctionRequest, _: grpc.aio.ServicerContext
    ) -> fnv1.RunFunctionResponse:
        """Run the function."""
        rsp = response.to(req)

        xr = resource.struct_to_dict(req.observed.composite.resource)
        name, ns = xr["metadata"]["name"], xr["metadata"]["namespace"]
        params = xr["spec"]["parameters"]
        stored = xr.get("status") or {}

        # Resolve the Network in our own namespace, by name or by labels.
        if dig(params, "networkRef", "name"):
            wanted = params["networkRef"]["name"]
            response.require_resources(rsp, NETWORK_REQ, NETWORK_API, "Network", match_name=wanted, namespace=ns)
        else:
            labels = dig(params, "networkSelector", "matchLabels", default={}) or {}
            wanted = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            response.require_resources(rsp, NETWORK_REQ, NETWORK_API, "Network", match_labels=labels, namespace=ns)

        if NETWORK_REQ not in req.required_resources:
            return rsp  # Crossplane fetches the Network and calls again.

        net = resolve_network(request.get_required_resources(req, NETWORK_REQ))
        if net is None and stored.get("vpcId") and stored.get("subnetIds"):
            # Sticky: once placed, a database never moves or loses its network
            # because the Network is temporarily unavailable.
            net = {"network": stored.get("network"), "vpcId": stored["vpcId"],
                   "subnetIds": stored["subnetIds"], "cidrBlock": stored.get("cidrBlock", DEFAULT_CIDR)}
        if net is None:
            response.set_conditions(rsp, resource.Condition(
                typ="NetworkResolved", status="False", reason="WaitingForNetwork",
                message=f"no Ready Network {wanted!r} with a VPC and subnets in namespace {ns!r}"))
            resource.update(rsp.desired.composite, {"status": {"network": wanted}})
            # Nothing is composed yet, which Crossplane would otherwise call Ready.
            rsp.desired.composite.ready = fnv1.READY_FALSE
            return rsp

        response.set_conditions(rsp, resource.Condition(
            typ="NetworkResolved", status="True", reason="Resolved", message=f"Network {net['network']}"))

        self.compose_aws(rsp, xr, net)
        self.compose_usages(rsp, name, net["network"])
        status = {"network": net["network"], "vpcId": net["vpcId"], "subnetIds": net["subnetIds"]}
        status.update(self.compose_connection(req, rsp, name))
        if SECURITY_GROUP in req.observed.resources:
            sg = resource.struct_to_dict(req.observed.resources[SECURITY_GROUP].resource)
            if sg_id := dig(sg, "status", "atProvider", "id"):
                status["securityGroupId"] = sg_id

        resource.update(rsp.desired.composite, {"status": status})
        return rsp

    @staticmethod
    def compose_aws(rsp: fnv1.RunFunctionResponse, xr: dict, net: dict) -> None:
        """The subnet group, security group and RDS instance, all in the Network's VPC."""
        name, ns = xr["metadata"]["name"], xr["metadata"]["namespace"]
        params = xr["spec"]["parameters"]
        region = params["region"]
        pc_ref = {"kind": "ProviderConfig", "name": params.get("providerConfigName", "default")}
        policies = params.get("managementPolicies") or ["*"]
        mgmt = {} if policies == ["*"] else {"managementPolicies": policies}
        port = PORTS[params["engine"]]

        resource.update(rsp.desired.resources[SUBNET_GROUP], subnetgroupv1beta1.SubnetGroup(
            spec=subnetgroupv1beta1.Spec(
                providerConfigRef=subnetgroupv1beta1.ProviderConfigRef(**pc_ref),
                forProvider=subnetgroupv1beta1.ForProvider(
                    region=region,
                    description=f"{ns}/{name} in Network {net['network']}",
                    subnetIds=net["subnetIds"],
                ),
                **mgmt,
            ),
        ))
        resource.update(rsp.desired.resources[SECURITY_GROUP], sgv1beta1.SecurityGroup(
            spec=sgv1beta1.Spec(
                providerConfigRef=sgv1beta1.ProviderConfigRef(**pc_ref),
                forProvider=sgv1beta1.ForProvider(
                    region=region,
                    description=f"{params['engine']} access to {ns}/{name}",
                    vpcId=net["vpcId"],
                ),
                **mgmt,
            ),
        ))
        resource.update(rsp.desired.resources[INGRESS], sgrv1beta1.SecurityGroupIngressRule(
            spec=sgrv1beta1.Spec(
                providerConfigRef=sgrv1beta1.ProviderConfigRef(**pc_ref),
                forProvider=sgrv1beta1.ForProvider(
                    region=region,
                    description=f"{params['engine']} from the VPC",
                    cidrIpv4=net["cidrBlock"],
                    ipProtocol="tcp",
                    fromPort=port,
                    toPort=port,
                    securityGroupIdSelector=sgrv1beta1.SecurityGroupIdSelector(matchControllerRef=True),
                ),
                **mgmt,
            ),
        ))
        resource.update(rsp.desired.resources[INSTANCE], instancev1beta1.Instance(
            spec=instancev1beta1.Spec(
                providerConfigRef=instancev1beta1.ProviderConfigRef(**pc_ref),
                forProvider=instancev1beta1.ForProvider(
                    region=region,
                    engine=params["engine"],
                    engineVersion=params["engineVersion"],
                    allocatedStorage=params["storageGB"],
                    instanceClass="db.t3.micro",
                    dbName="upbound",
                    username="masteruser",
                    publiclyAccessible=False,
                    skipFinalSnapshot=True,
                    applyImmediately=True,
                    autoGeneratePassword=bool(params.get("autoGeneratePassword", False)),
                    passwordSecretRef=instancev1beta1.PasswordSecretRef(
                        name=params["passwordSecretRef"]["name"], key=params["passwordSecretRef"]["key"]),
                    dbSubnetGroupNameSelector=instancev1beta1.DbSubnetGroupNameSelector(matchControllerRef=True),
                    vpcSecurityGroupIdSelector=instancev1beta1.VpcSecurityGroupIdSelector(matchControllerRef=True),
                ),
                writeConnectionSecretToRef=instancev1beta1.WriteConnectionSecretToRef(name=f"{name}-rds"),
                **mgmt,
            ),
        ))

    @staticmethod
    def compose_usages(rsp: fnv1.RunFunctionResponse, name: str, network: str) -> None:
        """The Network may not go away while anything in its VPC still exists."""
        for key, (api, kind) in USAGES.items():
            resource.update(rsp.desired.resources[key], {
                "apiVersion": "protection.crossplane.io/v1beta1",
                "kind": "Usage",
                "metadata": {"name": f"{name}-network-{kind.lower()}"},
                "spec": {
                    "replayDeletion": True,
                    "of": {"apiVersion": NETWORK_API, "kind": "Network", "resourceRef": {"name": network}},
                    "by": {"apiVersion": api, "kind": kind, "resourceSelector": {"matchControllerRef": True}},
                },
            })
            rsp.desired.resources[key].ready = fnv1.READY_TRUE

    @staticmethod
    def compose_connection(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse, name: str) -> dict:
        """Compose `<xr>-sql` from the RDS instance, and return status fields.

        Crossplane v2 XRs have no connection secret, so the Secret is composed:
        that lists it in this XR's resourceRefs, where a proxy can find it.
        """
        status: dict = {}
        data = None
        if INSTANCE in req.observed.resources:
            observed = req.observed.resources[INSTANCE]
            at = dig(resource.struct_to_dict(observed.resource), "status", "atProvider", default={}) or {}
            for k in ("address", "endpoint"):
                if at.get(k):
                    status[k] = at[k]
            if at.get("port"):
                status["port"] = int(at["port"])
            details = dict(observed.connection_details)
            if details.get("password"):
                data = {k: b64(v) for k, v in details.items()}
                if at.get("address"):
                    data.setdefault("host", b64(at["address"]))
        if data is None and SECRET in req.observed.resources:
            # Keep the last good Secret rather than drop it while details refresh.
            data = resource.struct_to_dict(req.observed.resources[SECRET].resource).get("data")
        if data:
            resource.update(rsp.desired.resources[SECRET], {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": f"{name}-sql"},
                "type": "connection.crossplane.io/v1alpha1",
                "data": data,
            })
            rsp.desired.resources[SECRET].ready = fnv1.READY_TRUE
            status["connectionSecret"] = f"{name}-sql"
        return status
