"""Run the Modal CLI on a machine whose TLS is intercepted by a locally trusted CA.

The Modal client trusts only certifi; this points certifi at keys/ca_bundle_windows.pem
(certifi + the Windows trust store), so TLS stays verified. Elsewhere, use `modal` directly.

    .venv/Scripts/python scripts/modal_cli.py deploy modal_app.py
"""

import sys
from pathlib import Path

import certifi

BUNDLE = Path(__file__).resolve().parent.parent / "keys" / "ca_bundle_windows.pem"
if BUNDLE.is_file():
    certifi.where = lambda: str(BUNDLE)  # type: ignore[assignment]

from modal.__main__ import main  # noqa: E402

sys.argv[0] = "modal"
main()
