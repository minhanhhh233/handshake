"""Modal entry point: one ASGI web endpoint (document 02, sections 3 and 8).

Deploy:  modal deploy modal_app.py
Secret:  one Modal secret named "handshake" holding the variables in document 02, section 7.

Warm container: one container is kept running for the demo (no cold start on stage). The
scheduled function `scale_down_after_demo` sets the minimum back to 0 on Monday 16 November
2026 at 12:00 Europe/Helsinki (document 08, section 0).
"""

import os

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    # The `agent` extra (Anthropic SDK) runs the Claude buyer agent for the live demo.
    .pip_install_from_pyproject("pyproject.toml", optional_dependencies=["agent"])
    # CA for the hosted MariaDB's TLS certificate; DATABASE_URL carries ?ssl_ca=<this path>.
    .add_local_file("certs/globalsignrootca.pem", "/root/certs/globalsignrootca.pem")
    .add_local_python_source("handshake", ignore=["**/__pycache__/**", "**/*.pyc"])
    .add_local_python_source("agent", ignore=["**/__pycache__/**", "**/*.pyc"])
)

app = modal.App("handshake")

# Containers run in the UK, next to the hosted database (SkySQL eu-west-2, London), and requests
# enter Modal in eu-west (this sets the URL's region). Measured 2026-11-14: product search from
# about 2.6 s (us-east) to well under 1 s. Override with the environment variables if needed.
REGION = os.environ.get("HANDSHAKE_MODAL_REGION", "uk") or None
ROUTING_REGION = os.environ.get("HANDSHAKE_MODAL_ROUTING_REGION", "eu-west") or None


# "handshake-anthropic" holds ANTHROPIC_API_KEY and LIVE_DEMO_DAILY_BUDGET_USD (live demo only).
@app.function(image=image, min_containers=1,
              secrets=[modal.Secret.from_name("handshake"),
                       modal.Secret.from_name("handshake-anthropic")],
              region=REGION, routing_region=ROUTING_REGION)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from handshake.api import create_app

    return create_app()


@app.function(schedule=modal.Cron("0 12 16 11 *", timezone="Europe/Helsinki"))
def scale_down_after_demo():
    """After judging (Monday 16 Nov 2026, 12:00 Helsinki) stop keeping a container warm."""
    modal.Function.from_name("handshake", "web").update_autoscaler(min_containers=0)
