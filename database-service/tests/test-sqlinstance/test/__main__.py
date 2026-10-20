"""CompositionTests for the SQLInstance service, printed as YAML.

The Network a SQLInstance uses is a local XR the function selects through
required resources (extraResources here). On the database control plane that
Network is a proxy XR, but the function cannot tell, and these tests do not care.
"""

import yaml
from models.io.k8s.apimachinery.pkg.apis.meta import v1 as k8s
from models.io.upbound.dev.meta.compositiontest import v1alpha1 as compositiontest
from models.io.upbound.m.aws.ec2.securitygroup import v1beta1 as sgv1beta1
from models.io.upbound.m.aws.ec2.securitygroupingressrule import v1beta1 as sgrv1beta1
from models.io.upbound.m.aws.rds.instance import v1beta1 as instancev1beta1
from models.io.upbound.m.aws.rds.subnetgroup import v1beta1 as subnetgroupv1beta1

NS = "team-a"
API = "aws.platform.upbound.io/v1alpha1"
COMPOSITION = "apis/sqlinstances/composition.yaml"
XRD = "apis/sqlinstances/definition.yaml"
VPC = "vpc-0a1b2c3d"
PRIVATE = ["subnet-priv-a", "subnet-priv-b"]
PC_REF = {"kind": "ProviderConfig", "name": "tenant-aws"}


def network(name="net-a", ready="True", status=True, labels=None):
    """A Network XR as the database CP sees it: local, typed, with status."""
    net = {
        "apiVersion": API,
        "kind": "Network",
        "metadata": {"name": name, "namespace": NS, "labels": labels if labels is not None else {"network": "a"}},
        "spec": {"parameters": {"id": name, "region": "us-west-2", "vpcCidrBlock": "192.168.0.0/16"}},
        "status": {"conditions": [{"type": "Ready", "status": ready, "reason": "Available",
                                   "lastTransitionTime": "2026-01-01T00:00:00Z"}]},
    }
    if status:
        net["status"].update({
            "vpcId": VPC,
            "subnetIds": ["subnet-pub-a", "subnet-pub-b", *PRIVATE],
            "publicSubnetIds": ["subnet-pub-a", "subnet-pub-b"],
            "privateSubnetIds": PRIVATE,
        })
    return net


def sqlinstance(name="db-a", engine="postgres", status=None, **network_choice):
    """A SQLInstance XR. Pass networkSelector=... or networkRef=..."""
    xr = {
        "apiVersion": API,
        "kind": "SQLInstance",
        "metadata": {"name": name, "namespace": NS},
        "spec": {"parameters": {
            "region": "us-west-2",
            "engine": engine,
            "engineVersion": "16.4" if engine == "postgres" else "10.11.14",
            "storageGB": 20,
            "passwordSecretRef": {"name": f"{name}-password", "key": "password"},
            "providerConfigName": PC_REF["name"],
            **network_choice,
        }},
    }
    if status:
        xr["status"] = status
    return xr


def dump(model):
    return model.model_dump(by_alias=True, exclude_unset=True)


def expected_mrs(xr_name="db-a", engine="postgres", port=5432):
    """The AWS resources a resolved SQLInstance composes."""
    subnet_group = subnetgroupv1beta1.SubnetGroup(
        apiVersion="rds.aws.m.upbound.io/v1beta1",
        kind="SubnetGroup",
        metadata=k8s.ObjectMeta(namespace=NS),
        spec=subnetgroupv1beta1.Spec(
            providerConfigRef=subnetgroupv1beta1.ProviderConfigRef(**PC_REF),
            forProvider=subnetgroupv1beta1.ForProvider(region="us-west-2", subnetIds=PRIVATE),
        ),
    )
    security_group = sgv1beta1.SecurityGroup(
        apiVersion="ec2.aws.m.upbound.io/v1beta1",
        kind="SecurityGroup",
        metadata=k8s.ObjectMeta(namespace=NS),
        spec=sgv1beta1.Spec(
            providerConfigRef=sgv1beta1.ProviderConfigRef(**PC_REF),
            forProvider=sgv1beta1.ForProvider(region="us-west-2", vpcId=VPC),
        ),
    )
    ingress = sgrv1beta1.SecurityGroupIngressRule(
        apiVersion="ec2.aws.m.upbound.io/v1beta1",
        kind="SecurityGroupIngressRule",
        metadata=k8s.ObjectMeta(namespace=NS),
        spec=sgrv1beta1.Spec(
            providerConfigRef=sgrv1beta1.ProviderConfigRef(**PC_REF),
            forProvider=sgrv1beta1.ForProvider(
                region="us-west-2",
                cidrIpv4="192.168.0.0/16",
                ipProtocol="tcp",
                fromPort=port,
                toPort=port,
                securityGroupIdSelector=sgrv1beta1.SecurityGroupIdSelector(matchControllerRef=True),
            ),
        ),
    )
    instance = instancev1beta1.Instance(
        apiVersion="rds.aws.m.upbound.io/v1beta1",
        kind="Instance",
        metadata=k8s.ObjectMeta(namespace=NS),
        spec=instancev1beta1.Spec(
            providerConfigRef=instancev1beta1.ProviderConfigRef(**PC_REF),
            forProvider=instancev1beta1.ForProvider(
                region="us-west-2",
                engine=engine,
                engineVersion="16.4" if engine == "postgres" else "10.11.14",
                allocatedStorage=20,
                instanceClass="db.t3.micro",
                dbName="upbound",
                username="masteruser",
                publiclyAccessible=False,
                skipFinalSnapshot=True,
                applyImmediately=True,
                autoGeneratePassword=False,
                passwordSecretRef=instancev1beta1.PasswordSecretRef(name=f"{xr_name}-password", key="password"),
                dbSubnetGroupNameSelector=instancev1beta1.DbSubnetGroupNameSelector(matchControllerRef=True),
                vpcSecurityGroupIdSelector=instancev1beta1.VpcSecurityGroupIdSelector(matchControllerRef=True),
            ),
            writeConnectionSecretToRef=instancev1beta1.WriteConnectionSecretToRef(name=f"{xr_name}-rds"),
        ),
    )
    return [dump(subnet_group), dump(security_group), dump(ingress), dump(instance)]


def expected_usages(xr_name="db-a", network_name="net-a"):
    """Usages that keep the Network until every AWS resource in its VPC is gone."""
    return [
        {
            "apiVersion": "protection.crossplane.io/v1beta1",
            "kind": "Usage",
            "metadata": {"name": f"{xr_name}-network-{kind.lower()}", "namespace": NS},
            "spec": {
                "replayDeletion": True,
                "of": {"apiVersion": API, "kind": "Network", "resourceRef": {"name": network_name}},
                "by": {"apiVersion": api, "kind": kind, "resourceSelector": {"matchControllerRef": True}},
            },
        }
        for api, kind in (
            ("rds.aws.m.upbound.io/v1beta1", "Instance"),
            ("ec2.aws.m.upbound.io/v1beta1", "SecurityGroup"),
            ("rds.aws.m.upbound.io/v1beta1", "SubnetGroup"),
        )
    ]


def observed(key, api, kind, at_provider):
    """An observed composed resource, keyed by its composition resource name."""
    return {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": f"db-a-{key.lower()}", "namespace": NS,
                     "annotations": {"crossplane.io/composition-resource-name": key}},
        "status": {"atProvider": at_provider},
    }


def composite(name="db-a", **status):
    return {"apiVersion": API, "kind": "SQLInstance", "metadata": {"name": name, "namespace": NS}, "status": status}


def case(name, xr, assert_resources, extra=None, observed=None):
    return compositiontest.CompositionTest(
        metadata=k8s.ObjectMeta(name=name),
        spec=compositiontest.Spec(
            compositionPath=COMPOSITION,
            xrdPath=XRD,
            xr=xr,
            extraResources=extra or [],
            observedResources=observed or [],
            assertResources=assert_resources,
            timeoutSeconds=120,
            validate=False,
        ),
    )


RESOLVED = {"network": "net-a", "vpcId": VPC, "subnetIds": PRIVATE}

tests = [
    # Selected by label, the Network's status and CIDR wire every AWS resource.
    case(
        "wires-network-by-selector",
        sqlinstance(networkSelector={"matchLabels": {"network": "a"}}),
        [*expected_mrs(), *expected_usages(), composite(**RESOLVED)],
        extra=[network(), network(name="net-b", labels={"network": "b"})],
    ),
    # Selected by name; mariadb opens 3306 instead of 5432.
    case(
        "wires-network-by-ref",
        sqlinstance(engine="mariadb", networkRef={"name": "net-a"}),
        [*expected_mrs(engine="mariadb", port=3306), *expected_usages(), composite(**RESOLVED)],
        extra=[network()],
    ),
    # Once resolved, the network stays put even if the Network is unavailable.
    case(
        "keeps-resolved-network",
        sqlinstance(networkRef={"name": "net-a"}, status=RESOLVED),
        [*expected_mrs(), *expected_usages(), composite(**RESOLVED)],
        extra=[],
    ),
    # A Network that is not Ready yet is not used: nothing is composed, and
    # the composite reports which Network it is waiting for.
    case(
        "waits-for-network",
        sqlinstance(networkRef={"name": "net-a"}),
        [{
            **composite(network="net-a"),
            # resourceRefs is compared as a whole list, so a surplus resource fails.
            "spec": {"crossplane": {"resourceRefs": []}},
            "status": {
                "network": "net-a",
                "conditions": [
                    {"type": "Responsive"},
                    {"type": "NetworkResolved", "status": "False", "reason": "WaitingForNetwork"},
                    {"type": "Synced", "status": "True"},
                    {"type": "Ready", "status": "False"},
                ],
            },
        }],
        extra=[network(ready="False", status=False)],
    ),
    # Outputs of the observed AWS resources reach the composite, and the last
    # good connection Secret is kept while the RDS details refresh.
    case(
        "reports-observed-outputs",
        sqlinstance(networkRef={"name": "net-a"}),
        [
            *expected_mrs(),
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "db-a-sql", "namespace": NS},
             "type": "connection.crossplane.io/v1alpha1", "data": {"password": "cGFzcw=="}},
            composite(**RESOLVED, securityGroupId="sg-0123", address="db-a.rds.example",
                      endpoint="db-a.rds.example:5432", port=5432, connectionSecret="db-a-sql"),
        ],
        extra=[network()],
        observed=[
            observed("securityGroup", "ec2.aws.m.upbound.io/v1beta1", "SecurityGroup", {"id": "sg-0123"}),
            observed("RDSInstanceSmall", "rds.aws.m.upbound.io/v1beta1", "Instance", {
                "address": "db-a.rds.example", "endpoint": "db-a.rds.example:5432", "port": 5432}),
            {"apiVersion": "v1", "kind": "Secret", "type": "connection.crossplane.io/v1alpha1",
             "metadata": {"name": "db-a-sql", "namespace": NS,
                          "annotations": {"crossplane.io/composition-resource-name": "connectionSecret"}},
             "data": {"password": "cGFzcw=="}},
        ],
    ),
]

print(yaml.dump({"items": [t.model_dump(by_alias=True, exclude_none=True) for t in tests]}))
