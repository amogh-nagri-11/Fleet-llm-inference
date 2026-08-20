# Testing the worker registry across two real machines

Verifies the dynamic worker registry (`registry/`, `POST/GET/DELETE
/api/v1/registry`) and `scripts/worker_agent.py` end-to-end: gateway on one
machine, worker agents registering in from both machines over the LAN.

This guide assumes the gateway runs on **Victus** (WSL) and the
**Mac** joins as a remote worker, with Victus also registering itself
locally. Swap the roles if you'd rather run the gateway on the Mac — the
steps are the same, just reverse which machine is "local" vs "remote."

## 0. Prerequisites

**Victus (gateway machine):**
- Redis running (`redis-server`, or `docker run -p 6379:6379 redis:7-alpine`)
- Postgres reachable per `.env`'s `MEMORY_DB_*` — the gateway's lifespan
  connects to it on startup and will fail to boot without it
- `pip install -r gateway/requirements.txt`
- Ollama running locally if you want real model detection (optional)

**Mac (remote worker):**
- `pip install httpx` (only real dependency `scripts/worker_agent.py` needs)
- Ollama running locally if you want real model detection (optional)
- Just needs `scripts/worker_agent.py` — doesn't need the rest of the repo,
  but easiest to just clone the repo and run it from there

## 1. Find both machines' LAN IPs

**On Victus, in Windows PowerShell (not inside WSL):**
```powershell
ipconfig
```
Look for the **IPv4 Address** under your active Wi-Fi/Ethernet adapter —
not the `vEthernet (WSL)` adapter. This is the address the Mac will need
to reach.

**On the Mac:**
```bash
ipconfig getifaddr en0   # Wi-Fi, typically
```

## 2. Make the gateway's port reachable from outside WSL

This is the step people usually get stuck on. WSL2 defaults to NAT
networking — nothing outside the Windows host can reach a port a WSL
process is listening on unless you do one of the following.

**Option A — WSL mirrored networking (Windows 11, WSL ≥ 2.0, recommended):**

Create/edit `%UserProfile%\.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
```
Then in PowerShell:
```powershell
wsl --shutdown
```
and restart your WSL terminal. WSL now shares the Windows host's network
interface directly — no port proxy needed.

**Option B — port proxy (older WSL / NAT mode):**

Every time WSL restarts, its internal IP can change, so re-run this
(elevated PowerShell) after each `wsl --shutdown`/restart:
```powershell
wsl hostname -I   # note the WSL internal IP it prints, e.g. 172.20.x.x

netsh interface portproxy add v4tov4 listenport=8000 listenaddress=0.0.0.0 connectport=8000 connectaddress=<WSL_INTERNAL_IP>
```

**Either way, open the Windows Firewall** (elevated PowerShell):
```powershell
New-NetFirewallRule -DisplayName "Fleet Gateway 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

## 3. Start the gateway on Victus (inside WSL)

```bash
redis-server &                          # if not already running
uvicorn gateway.main:app --host 0.0.0.0 --port 8000 --reload
```
`--host 0.0.0.0` matters — binding to `localhost` only accepts connections
from Victus itself, not from the Mac. (`gateway/main.py`'s `GATEWAY_HOST`
default is already `0.0.0.0`, so plain `uvicorn gateway.main:app` also
works if you're not overriding host/port.)

## 4. Verify locally on Victus first

```bash
curl http://localhost:8000/api/v1/health -H "x-api-key: dev-key"
```
Should return `{"status": "ok", ...}`. Don't move to cross-machine testing
until this works — it isolates "is the gateway even up" from "is the
network path working."

## 5. Verify reachability from the Mac

From the Mac, before running the agent:
```bash
curl http://<victus-lan-ip>:8000/api/v1/health -H "x-api-key: dev-key"
```
If this hangs or connection-refuses, it's a networking/firewall problem —
fix that before touching the agent script. (See Troubleshooting below.)

## 6. Register Victus itself as a worker (local agent)

In a new WSL terminal on Victus:
```bash
pip install httpx   # if this venv doesn't already have it
python scripts/worker_agent.py \
  --gateway-url http://localhost:8000/api/v1 \
  --api-key dev-key \
  --name victus
```
Leave it running — this is the heartbeat loop, not `--once`.

## 7. Register the Mac as a worker (remote agent)

On the Mac:
```bash
python scripts/worker_agent.py \
  --gateway-url http://<victus-lan-ip>:8000/api/v1 \
  --api-key dev-key \
  --name mac
```
If auto-detected `--worker-url` looks wrong in the startup log line, override
it explicitly with `--worker-url http://<mac-lan-ip>:11434`.

## 8. Confirm both machines show up

From either machine:
```bash
curl http://<victus-lan-ip>:8000/api/v1/registry -H "x-api-key: dev-key" | python -m json.tool
```
Expect two entries — `victus` and `mac` — each with real `ram_gb`,
`has_gpu`/`vram_gb`, and `models` (non-empty if Ollama was running
locally on that machine when it registered).

## 9. Confirm heartbeating

Wait past one `--interval` (default 30s), check `last_heartbeat` in the
listing again — it should have advanced. This is currently just a
timestamp refresh; nothing evicts a stale entry yet (that's the
not-yet-built presence-detection step).

## 10. Confirm graceful leave

Ctrl+C the Mac's agent process. Re-check the registry — the `mac` entry
should be gone (the agent's `DELETE /registry/{worker_id}` fired on
shutdown). Re-run the same `worker_agent.py` command again — it should
reuse the same `worker_id` (persisted at `~/.fleet/worker_id`) rather than
creating a new entry.

## 11. Run the automated test suite

On Victus (needs the full dev deps + Redis reachable):
```bash
pytest tests/test_registry.py
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `curl` from Mac hangs / times out | Firewall rule missing, or WSL still on NAT mode without a port proxy |
| `curl` from Mac gets connection refused instantly | Gateway bound to `localhost` instead of `0.0.0.0`, or wrong IP |
| Gateway won't start at all | Postgres unreachable — check `MEMORY_DB_HOST` in `.env` |
| Agent logs `401` on register | `--api-key` doesn't match the gateway's `API_KEY` (`.env`, default `dev-key`) |
| Agent's registered `models` is always `[]` | Ollama isn't running/reachable at `--ollama-url` on that machine — check the agent's own startup warning line |
| Port-proxy stops working after a reboot | WSL's internal IP changed — re-run the `netsh` command in step 2B, or switch to mirrored networking (2A) to stop needing this |
