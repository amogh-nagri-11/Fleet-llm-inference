import time
from dataclasses import asdict, dataclass, field
from typing import List, Optional


@dataclass
class WorkerRecord:
    """One entry in the dynamic worker registry — the pivot from static
    WORKER_URLS to machines that join/leave a pool at runtime (personal
    hardware: a Mac, a gaming PC, a laptop, each with different RAM/VRAM
    and different models pulled).

    Distinct from workers/ollama_client.py's WorkerStats: WorkerStats is
    live in-memory routing/circuit-breaker state owned by LoadBalancer,
    rebuilt from settings.WORKER_URLS on every gateway restart.
    WorkerRecord is the machine's *self-reported* hardware/capability
    profile, persisted in Redis (registry/store.py) so it survives a
    gateway restart and is visible across gateway replicas. Nothing reads
    this yet to make routing decisions — that's capability-aware routing,
    a later step. For now this is storage only.
    """
    worker_id: str          # stable across restarts — the agent persists this locally
    url: str                # this machine's Ollama endpoint, e.g. http://192.168.1.42:11434
    name: str                # human-friendly label, e.g. "Mac-mini" or "victus-wsl"
    ram_gb: float
    has_gpu: bool
    vram_gb: Optional[float] = None
    models: List[str] = field(default_factory=list)   # models currently pulled on this machine
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerRecord":
        return cls(**data)
