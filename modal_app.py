"""Modal entry point: one ASGI web endpoint (document 02, sections 3 and 8).

Deploy:  modal deploy modal_app.py
Secret:  one Modal secret named "handshake" holding the variables in document 02, section 7.
"""

import modal

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_pyproject("pyproject.toml")
    .add_local_python_source("handshake", ignore=["**/__pycache__/**", "**/*.pyc"])
)

app = modal.App("handshake")


@app.function(image=image, secrets=[modal.Secret.from_name("handshake")], min_containers=0)
@modal.concurrent(max_inputs=20)
@modal.asgi_app()
def web():
    from handshake.api import create_app

    return create_app()
