"""Deploy an image to the Lightsail container service, with its settings read at deploy time.

    python deploy/lightsail_deploy.py <image-uri>

Settings come from two places, read fresh on every deploy (so changing one never needs a code change):
  - Secrets Manager, secret SECRET_ID (default olist/prod): passwords and keys, as JSON key/values;
  - Parameter Store, every parameter under PARAMETER_PATH (default /olist/prod/): plain settings, one
    parameter per variable (e.g. /olist/prod/DEMO_LOGIN -> DEMO_LOGIN).
They become the containers' environment variables. Values are never printed: only their names.

Two containers run from the same image: "api" (the website and API, behind the public HTTPS endpoint)
and "scheduler" (background jobs). Lightsail switches traffic to the new deployment only when its health
check passes, otherwise the running deployment stays; this script waits and fails if it didn't.
"""

from __future__ import annotations

import json
import os
import sys
import time

import boto3

SERVICE = os.environ.get("LIGHTSAIL_SERVICE", "olist")
SECRET_ID = os.environ.get("SECRET_ID", "olist/prod")
PARAMETER_PATH = os.environ.get("PARAMETER_PATH", "/olist/prod/")
HEALTH_PATH = "/api/health"
WAIT_S = int(os.environ.get("DEPLOY_WAIT_S", 900))


def settings() -> dict[str, str]:
    """Plain settings first, then secrets (a name in both: the secret wins)."""
    env: dict[str, str] = {}
    ssm = boto3.client("ssm")
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=PARAMETER_PATH, WithDecryption=True):
        for p in page["Parameters"]:
            env[p["Name"][len(PARAMETER_PATH):]] = p["Value"]
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=SECRET_ID)["SecretString"]
    env.update({k: str(v) for k, v in json.loads(secret).items()})
    return env


def deploy(image: str) -> None:
    env = settings()
    print(f"settings: {len(env)} ({', '.join(sorted(env))})")   # names only, never values
    containers = {
        "api": {"image": image, "environment": env, "ports": {"8000": "HTTP"}},
        "scheduler": {"image": image, "environment": env, "command": ["python", "-m", "ai.scheduler", "run"]},
    }
    endpoint = {"containerName": "api", "containerPort": 8000,
                "healthCheck": {"path": HEALTH_PATH, "successCodes": "200-299", "intervalSeconds": 10,
                                "timeoutSeconds": 5, "healthyThreshold": 2, "unhealthyThreshold": 3}}
    ls = boto3.client("lightsail")
    service = ls.create_container_service_deployment(serviceName=SERVICE, containers=containers,
                                                     publicEndpoint=endpoint)["containerService"]
    version = service["nextDeployment"]["version"]
    print(f"deployment {version}: {image}")

    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        time.sleep(15)
        svc = ls.get_container_services(serviceName=SERVICE)["containerServices"][0]
        current, nxt = svc.get("currentDeployment") or {}, svc.get("nextDeployment") or {}
        if current.get("version") == version and current.get("state") == "ACTIVE":
            print(f"deployment {version} is live at {svc['url']}")
            return
        if nxt.get("version") == version and nxt.get("state") == "FAILED" or \
                current.get("version") == version and current.get("state") == "FAILED":
            sys.exit(f"deployment {version} FAILED its health check; the previous deployment keeps running. "
                     "See the Lightsail console > olist > Deployments and Logs.")
        print(f"  waiting: service {svc['state']}, deployment {nxt.get('state') or current.get('state')}")
    sys.exit(f"deployment {version} did not become active within {WAIT_S}s")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    deploy(sys.argv[1])
