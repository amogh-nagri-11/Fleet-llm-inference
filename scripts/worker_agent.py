#!/usr/bin/env python3
"""
Fleet worker agent — runs on a personal machine (Mac, gaming PC, old
laptop, WSL box) that's contributing its local Ollama to the pool.

Reads local hardware specs (RAM, GPU/VRAM) and currently-pulled Ollama
models, then registers with the Fleet gateway's dynamic worker registry
(POST /api/v1/registry/register) and keeps re-registering on an interval
so the gateway can tell this machine is still around. Deliberately
standalone: stdlib + httpx only, no imports from gateway/config/registry
-- this script has to run on a bare personal machine that hasn't
necessarily cloned the rest of the Fleet repo.

The registry endpoints don't feed routing yet (that's a later step) --
today this just makes a machine's specs and model list visible at
GET /api/v1/registry.

Examples
--------
    # Simplest case: gateway and Ollama both on defaults, LAN IP auto-detected
    python scripts/worker_agent.py --gateway-url http://192.168.1.10:8000/api/v1 --api-key dev-key

    # Named machine, custom heartbeat interval
    python scripts/worker_agent.py --gateway-url http://192.168.1.10:8000/api/v1 \
        --api-key dev-key --name mac-mini --interval 20

    # WSL: Ollama is reachable to the agent at localhost, but the gateway
    # (running elsewhere) needs a different address to reach it -- WSL2's
    # own IP usually isn't directly reachable from outside, so give the
    # Windows host's LAN IP + a forwarded port explicitly.
    python scripts/worker_agent.py --gateway-url http://192.168.1.10:8000/api/v1 \
        --api-key dev-key --name victus-wsl --worker-url http://192.168.1.20:11434

    # One-shot registration (no heartbeat loop) -- useful to sanity check
    # the endpoint before leaving the agent running
    python scripts/worker_agent.py --gateway-url http://192.168.1.10:8000/api/v1 \
        --api-key dev-key --once
"""
import argparse
import asyncio
import json
import platform
import signal
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

DEFAULT_STATE_DIR = Path.home() / ".fleet"


# ── Local hardware / capability detection ───────────────────────

def detect_ram_gb() -> float:
    system = platform.system()
    if system == "Linux":
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 ** 2), 1)
        raise RuntimeError("MemTotal not found in /proc/meminfo")

    if system == "Darwin":
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
        return round(int(out.strip()) / (1024 ** 3), 1)

    if system == "Windows":
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return round(stat.ullTotalPhys / (1024 ** 3), 1)

    raise RuntimeError(f"Don't know how to detect RAM on platform {system!r}")


def detect_gpu() -> Tuple[bool, Optional[float]]:
    """Best-effort. Returns (has_gpu, vram_gb). NVIDIA via nvidia-smi
    covers Linux/WSL/Windows gaming PCs. Apple Silicon always has a GPU
    but shares unified memory with system RAM rather than dedicated
    VRAM, so there's no single honest number to report there --
    has_gpu=True, vram_gb=None rather than guessing."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        mib = int(out.strip().splitlines()[0])
        return True, round(mib / 1024, 1)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass

    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return True, None

    return False, None


def detect_lan_ip() -> str:
    """No packets actually sent -- connecting a UDP socket just makes the
    kernel pick which local interface/IP would be used for that route,
    which is what a remote gateway would need to reach this machine."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_or_create_worker_id(state_dir: Path, override: Optional[str]) -> str:
    if override:
        return override

    id_file = state_dir / "worker_id"
    if id_file.exists():
        existing = id_file.read_text().strip()
        if existing:
            return existing

    worker_id = f"{socket.gethostname().lower()}-{uuid.uuid4().hex[:6]}"
    state_dir.mkdir(parents=True, exist_ok=True)
    id_file.write_text(worker_id)
    return worker_id


async def detect_local_models(client: httpx.AsyncClient, ollama_url: str) -> List[str]:
    resp = await client.get(f"{ollama_url.rstrip('/')}/api/tags", timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return [m["name"] for m in data.get("models", []) if m.get("name")]


def default_worker_url(ollama_url: str) -> str:
    """What the gateway should use to reach this machine's Ollama --
    different from --ollama-url, which is how *this agent* reaches
    Ollama (usually localhost). Reuses --ollama-url's port against the
    auto-detected LAN IP."""
    port = 11434
    if ":" in ollama_url.rsplit("/", 1)[-1]:
        try:
            port = int(ollama_url.rsplit(":", 1)[-1].split("/")[0])
        except ValueError:
            pass
    return f"http://{detect_lan_ip()}:{port}"


# ── Registration ─────────────────────────────────────────────

async def build_payload(args, client: httpx.AsyncClient) -> dict:
    ram_gb = args.ram_gb if args.ram_gb is not None else detect_ram_gb()

    if args.no_gpu:
        has_gpu, vram_gb = False, None
    elif args.vram_gb is not None:
        has_gpu, vram_gb = True, args.vram_gb
    else:
        has_gpu, vram_gb = detect_gpu()

    try:
        models = await detect_local_models(client, args.ollama_url)
    except Exception as e:
        print(f"[WorkerAgent] warning: couldn't reach Ollama at {args.ollama_url} to list models: {e}")
        models = []

    return {
        "worker_id": args.worker_id,
        "url": args.worker_url,
        "name": args.name,
        "ram_gb": ram_gb,
        "has_gpu": has_gpu,
        "vram_gb": vram_gb,
        "models": models,
    }


async def register(client: httpx.AsyncClient, gateway_url: str, api_key: str, payload: dict) -> bool:
    try:
        resp = await client.post(
            f"{gateway_url.rstrip('/')}/registry/register",
            json=payload,
            headers={"x-api-key": api_key},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        print(f"[WorkerAgent] event=registered worker_id={body['worker_id']} url={body['url']} "
              f"models={body['models']}")
        return True
    except httpx.HTTPStatusError as e:
        print(f"[WorkerAgent] event=register_failed status={e.response.status_code} body={e.response.text}")
        return False
    except httpx.HTTPError as e:
        print(f"[WorkerAgent] event=register_failed error={e}")
        return False


async def deregister(client: httpx.AsyncClient, gateway_url: str, api_key: str, worker_id: str) -> None:
    try:
        resp = await client.delete(
            f"{gateway_url.rstrip('/')}/registry/{worker_id}",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"[WorkerAgent] event=deregistered worker_id={worker_id}")
        else:
            print(f"[WorkerAgent] event=deregister_failed status={resp.status_code}")
    except httpx.HTTPError as e:
        print(f"[WorkerAgent] event=deregister_failed error={e}")


# ── Main loop ────────────────────────────────────────────────

async def run(args) -> None:
    state_dir = Path(args.state_dir)
    args.worker_id = get_or_create_worker_id(state_dir, args.worker_id)
    args.name = args.name or socket.gethostname()

    async with httpx.AsyncClient() as client:
        if not args.worker_url:
            args.worker_url = default_worker_url(args.ollama_url)
            print(f"[WorkerAgent] --worker-url not given, auto-detected {args.worker_url} "
                  f"(override with --worker-url if the gateway can't reach this)")

        payload = await build_payload(args, client)
        print(f"[WorkerAgent] worker_id={args.worker_id} name={args.name} "
              f"ram_gb={payload['ram_gb']} has_gpu={payload['has_gpu']} vram_gb={payload['vram_gb']}")

        ok = await register(client, args.gateway_url, args.api_key, payload)
        if args.once:
            sys.exit(0 if ok else 1)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass  # signal handlers aren't available on some platforms (e.g. native Windows)

        print(f"[WorkerAgent] heartbeating every {args.interval}s -- Ctrl+C to stop")
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    payload = await build_payload(args, client)
                    await register(client, args.gateway_url, args.api_key, payload)
        finally:
            if not args.no_deregister_on_exit:
                await deregister(client, args.gateway_url, args.api_key, args.worker_id)


def main():
    p = argparse.ArgumentParser(
        description="Fleet worker agent -- registers this machine's Ollama into the pool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--gateway-url", required=True, help="e.g. http://192.168.1.10:8000/api/v1")
    p.add_argument("--api-key", required=True)
    p.add_argument("--ollama-url", default="http://localhost:11434",
                    help="how THIS agent reaches Ollama locally (default: %(default)s)")
    p.add_argument("--worker-url", default=None,
                    help="how the GATEWAY should reach this machine's Ollama; "
                         "auto-detected from the local LAN IP if omitted")
    p.add_argument("--name", default=None, help="human-friendly label (default: hostname)")
    p.add_argument("--worker-id", default=None,
                    help="stable id for this machine; auto-generated and persisted under "
                         "--state-dir on first run if omitted")
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR),
                    help="where the generated worker-id is persisted (default: %(default)s)")
    p.add_argument("--interval", type=float, default=30.0, help="heartbeat interval in seconds")
    p.add_argument("--ram-gb", type=float, default=None, help="override auto-detected RAM")
    p.add_argument("--vram-gb", type=float, default=None, help="override auto-detected VRAM, implies a GPU")
    p.add_argument("--no-gpu", action="store_true", help="force has_gpu=false, skip GPU detection")
    p.add_argument("--once", action="store_true", help="register once and exit, no heartbeat loop")
    p.add_argument("--no-deregister-on-exit", action="store_true",
                    help="don't call DELETE /registry on Ctrl+C -- leave the entry for the "
                         "gateway's (not-yet-built) missed-heartbeat eviction to clean up instead")
    args = p.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
