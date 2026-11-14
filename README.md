# Handshake

Handshake is an agent gateway for the Valio Aimo Junction 2026 challenge: an external purchasing
agent looks up products, receives a signed fixed-price offer and places an order within limits
set by a human, who approves anything over the limit out of band.
The full specification is in [handshake-docs/README.md](handshake-docs/README.md).

## Connecting an assistant over MCP

`mcp_server/server.py` exposes the eight Handshake tools to any MCP-capable assistant (Claude
Desktop, Claude Code, ...) over stdio. It is a thin client of the gateway's HTTP API through
`agent/handshake_client.py`: offer signatures are verified before the assistant sees them,
orders carry an idempotency key, one trace ID covers the session, and supplier or approver free
text stays inside blocks marked UNTRUSTED. The assistant can never approve its own order.

Install (Python 3.12) and run:

```
pip install -e ".[agent,mcp]"
HANDSHAKE_BASE_URL=https://your-gateway.example HANDSHAKE_AGENT_KEY=hsk_... python -m mcp_server.server
```

| Variable | Meaning |
|---|---|
| `HANDSHAKE_BASE_URL` | Gateway base URL (default `http://127.0.0.1:8000`) |
| `HANDSHAKE_AGENT_KEY` | The agent's bearer key (required); issued per agent by the gateway operator |
| `SSL_CERT_FILE` | Optional CA bundle, e.g. behind a TLS-intercepting proxy; verification is never disabled |

Example client configuration (`mcpServers` entry, e.g. `claude_desktop_config.json` or a
project's `.mcp.json`):

```json
{
  "mcpServers": {
    "handshake": {
      "command": "/path/to/handshake/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/handshake",
      "env": {
        "HANDSHAKE_BASE_URL": "https://your-gateway.example",
        "HANDSHAKE_AGENT_KEY": "<agent key>"
      }
    }
  }
}
```

On Windows use `C:\\path\\to\\handshake\\.venv\\Scripts\\python.exe` as the command.

Tools:

| Tool | What it does |
|---|---|
| `get_mandate` | Read the agent's mandate: spend cap, approvers, allowed categories, allergen and delivery rules. Call first. |
| `search_products` | Search the catalogue with structured filters (category, claims, allergens, labels); at most 20 results, no prices. |
| `get_product` | One product's full detail; supplier text is returned in an UNTRUSTED block. |
| `request_offer` | Get a signed, fixed-price offer for products in order of preference; the signature is verified before it is returned. |
| `dry_run_order` | Check an offer against the mandate without ordering: accepted, needs_approval or rejected, with reasons. |
| `place_order` | Order a verified offer (idempotent); confirmed, pending_approval or rejected. |
| `get_order` | Current order status; returns immediately, poll no more often than every 5 seconds. |
| `get_receipt` | The signed receipt and event timeline of an order. |
