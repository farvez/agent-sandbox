"""Settings that can change without replacing the server.

The host's boot script fixes most environment variables, and changing it replaces the EC2
instance. Settings added later (snapshot storage, ...) come instead from one SSM String
parameter holding a JSON object of SANDBOX_* variables, read once at startup. Terraform
writes it; the in-place deploy restarts the API, which picks it up.

SANDBOX_RUNTIME_CONFIG: "ssm:<parameter name>", or "off". When unset, the server on AWS
(AWS_REGION set and a DynamoDB keystore) reads /agent-sandbox/runtime-config; elsewhere nothing
is read. Variables already in the environment win over the parameter.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger("agent-sandbox")

DEFAULT_PARAM = "/agent-sandbox/runtime-config"


def parameter_name(env=os.environ) -> Optional[str]:
    spec = env.get("SANDBOX_RUNTIME_CONFIG", "").strip()
    if spec == "off":
        return None
    if spec:
        kind, _, name = spec.partition(":")
        if kind != "ssm" or not name:
            raise RuntimeError(f"SANDBOX_RUNTIME_CONFIG must be 'ssm:<name>' or 'off', got {spec!r}.")
        return name
    if env.get("AWS_REGION") and env.get("SANDBOX_KEYSTORE", "").startswith("dynamodb:"):
        return DEFAULT_PARAM
    return None


def parse(raw: str) -> Dict[str, str]:
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("the runtime config must be a JSON object")
    return {str(k): str(v) for k, v in data.items() if str(k).startswith("SANDBOX_") and v is not None}


def apply(env=os.environ, fetch=None) -> Dict[str, str]:
    """Sets the parameter's variables that aren't already set. Returns what was applied."""
    name = parameter_name(env)
    if not name:
        return {}
    try:
        raw = (fetch or _fetch_ssm)(name, env.get("AWS_REGION"))
    except Exception as e:  # a missing or unreadable parameter must not stop the API
        logger.warning("Runtime config %s not loaded: %s", name, type(e).__name__)
        return {}
    if raw is None:
        return {}
    try:
        values = parse(raw)
    except ValueError as e:
        logger.warning("Runtime config %s ignored: %s", name, e)
        return {}
    applied = {k: v for k, v in values.items() if k not in env}
    env.update(applied)
    return applied


def _fetch_ssm(name: str, region: Optional[str]) -> Optional[str]:
    import boto3

    client = boto3.client("ssm", region_name=region)
    try:
        return client.get_parameter(Name=name)["Parameter"]["Value"]
    except client.exceptions.ParameterNotFound:
        return None
