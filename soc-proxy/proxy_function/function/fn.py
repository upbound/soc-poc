"""The SOC proxy composition function: adapts proxy.compose to the SDK."""

import grpc
from crossplane.function import logging, request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1
from crossplane.function.proto.v1 import run_function_pb2_grpc as grpcv1

from function import config, proxy


def lookup(api_version: str, kind: str) -> dict | None:
    """Find the generated proxy constants for an XR's group and kind."""
    group = api_version.rsplit("/", 1)[0] if "/" in api_version else ""
    return config.PROXIES.get(f"{group}/{kind}")


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
        cfg = lookup(xr.get("apiVersion", ""), xr.get("kind", ""))
        if cfg is None:
            response.fatal(rsp, f"this proxy was not generated for {xr.get('apiVersion')} {xr.get('kind')}")
            return rsp

        observed = {k: resource.struct_to_dict(v.resource) for k, v in req.observed.resources.items()}
        required = {k: request.get_required_resources(req, k) for k in req.required_resources}

        plan = proxy.compose(xr, observed, required, cfg)

        # Declare requirements on every call; Crossplane treats them as
        # satisfied once they stop changing.
        for name, selector in plan.requirements.items():
            response.require_resources(rsp, name, **selector)

        if plan.fatal:
            response.fatal(rsp, plan.fatal)
            return rsp

        for key, body in plan.desired.items():
            resource.update(rsp.desired.resources[key], body)
            rsp.desired.resources[key].ready = fnv1.READY_TRUE if plan.ready.get(key) else fnv1.READY_FALSE

        if plan.status is not None:
            resource.update(rsp.desired.composite, {"status": plan.status})

        response.set_conditions(rsp, *[
            resource.Condition(typ=c.typ, status=c.status, reason=c.reason, message=c.message or None)
            for c in plan.conditions
        ])
        for w in plan.warnings:
            response.warning(rsp, w)

        return rsp
