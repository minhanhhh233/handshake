# Rehearsal sheet

Gateway: `https://minhanhhh233--handshake-web.eu-west.modal.run` (hosted DB, demo mode, one warm container until Sun 12:00).
Run from the repository root in **PowerShell**. Demo order follows document 09, section 6.

## 0. Setup (once per terminal)

```powershell
cd C:\Users\minha\Documents\handshake
$B     = "https://minhanhhh233--handshake-web.eu-west.modal.run"
$keys  = Get-Content keys\demo_agents.hosted.json -Raw | ConvertFrom-Json
$admin = ((Get-Content .env | Select-String '^ADMIN_KEY=').Line -split '=', 2)[1]
$py    = ".venv\Scripts\python"
$env:HANDSHAKE_BASE_URL  = $B
$env:HANDSHAKE_AGENT_KEY = $keys.nordlys.key
$env:SSL_CERT_FILE       = "$PWD\keys\ca_bundle_windows.pem"   # this laptop only (TLS interception)
Invoke-RestMethod "$B/v1/health"                                # expect status ok, database ok
```

## 1. Reset (before every run-through)

```powershell
Invoke-RestMethod -Method Post "$B/v1/admin/reset" -Headers @{ "X-Admin-Key" = $admin }
```

## 2. Open the console (right screen; QR codes for approvals)

```powershell
Start-Process "$B/console?key=$admin"
```

## 3. Main scenario: Claude agent, 1,000 L of lactose-free milk

```powershell
& $py agent\claude_agent.py "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our requirements based on price and availability." --base-url $B --key $keys.nordlys.key --verbose
```

When it says it needs Maria Virtanen's approval: scan the QR on the console with the phone, tap **Approve order**. The agent then reports `confirmed`. Note the `off_...` and `ord_...` IDs it prints.
Cheaper rehearsal: add `--model claude-sonnet-5-5` (about half the cost of the default `claude-opus-5-5`, about $0.16 per run with caching).

## 4. Signature check with a tampered copy (use the offer ID from step 3)

```powershell
$offer = "off_..."   # from step 3
(Invoke-RestMethod "$B/v1/offers/$offer" -Headers @{ Authorization = "Bearer $($keys.nordlys.key)" }).jws | Set-Content "$env:TEMP\offer.jws" -NoNewline
& $py agent\verify_offer.py --base-url $B --jws-file "$env:TEMP\offer.jws"
```

Expect "Signature valid", then "Signature invalid" for the copy with one price digit changed.

## 5. Receipt

Open the receipt link the agent printed in step 3 (`human_url`), or the Valio Aimo view: `Start-Process "$B/receipts/ord_...?key=$admin"`.

## 6. Anonymous search (no key: same products, no prices, no stock)

```powershell
Invoke-RestMethod "$B/v1/products?category=10400&claims=FREE_FROM_LACTOSE&limit=3" | ConvertTo-Json -Depth 6
```

## 7. Allergy guard (Koivula: refused, "Lactose-free is not milk-free.")

```powershell
& $py agent\scripted.py --scenario allergy_guard --base-url $B --key $keys.koivula.key
```

## 8. Split on real stock (469363 first: 384 + 616)

```powershell
& $py agent\scripted.py --scenario milk_split --base-url $B --key $keys.nordlys.key --delivery-date 2026-11-16
```

It needs approval (1574.48 EUR): approve from the console QR. It uses up 469363's 384 units: **reset (step 1) before repeating it.**

## 9. Order through MCP with Claude Code (off-the-shelf assistant)

Register once (stores the hosted key in Claude Code's local config for this project):

```powershell
claude mcp add handshake -e HANDSHAKE_BASE_URL=$B -e HANDSHAKE_AGENT_KEY=$($keys.nordlys.key) -e SSL_CERT_FILE=$env:SSL_CERT_FILE -- "$PWD\.venv\Scripts\python.exe" "$PWD\mcp_server\server.py"
```

Then start `claude` in a new terminal and type: *"Using the handshake tools, order 1,000 litres of lactose-free milk for delivery on Monday 16 November. Read the mandate first and run a dry run before ordering."* Approve from the console QR. Remove afterwards with `claude mcp remove handshake`.

## Fallbacks (document 09, section 6)

| Problem | Do |
|---|---|
| Model slow or unavailable | `& $py agent\scripted.py --scenario milk_order --base-url $B --key $keys.nordlys.key` (same calls, no model) |
| Phone cannot open the approval page | Click "Open approval page" on the console |
| `no_candidates` for Monday | Upstream cut-off for Monday is Sun 15 Nov 17:00 (Q-11); after that use `--delivery-date` with a later date only if upstream offers one |
| Gateway cold or odd | `Invoke-RestMethod "$B/v1/health"`; redeploy is `& $py scripts\modal_cli.py deploy modal_app.py` (document 02, section 8) |
