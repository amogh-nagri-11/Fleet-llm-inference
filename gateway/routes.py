from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List
from router.load_balancer import load_balancer
from config import settings
import uuid
from workers.worker_pool import worker_pool
from router.autoscaler import autoscaler
from context.manager import context_manager
from context.models import ContextType

router = APIRouter()


# ── Request Models ────────────────────────────────────────

class AgentMetadata(BaseModel):
    """Identity fields that let Fleet associate a request with an agent
    workflow. All optional — a plain API caller doesn't need any of this.
    See REDESIGN.md §5."""
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    request_id: Optional[str] = None
    parent_request_id: Optional[str] = None

class GenerateRequest(AgentMetadata):
    prompt: str
    model: Optional[str] = None
    stream: bool = False

class Message(BaseModel):
    role: str   # "user" | "assistant" | "system"
    content: str

class ChatRequest(AgentMetadata):
    messages: List[Message]
    model: Optional[str] = None


# ── Auth helper ───────────────────────────────────────────

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ── Agent metadata helper ───────────────────────────────────
# Fleet only needs enough identity to understand which requests belong to
# which agent workflow (REDESIGN.md §5/§6) — it does not interpret agent
# reasoning. request_id is generated here if the caller doesn't supply one,
# so every request is traceable even from plain (non-agent) callers.

def resolve_agent_metadata(req: AgentMetadata, endpoint: str) -> dict:
    meta = {"request_id": req.request_id or str(uuid.uuid4())}
    if req.agent_id is not None:
        meta["agent_id"] = req.agent_id
    if req.workflow_id is not None:
        meta["workflow_id"] = req.workflow_id
    if req.parent_request_id is not None:
        meta["parent_request_id"] = req.parent_request_id

    fields = " ".join(f"{k}={v}" for k, v in meta.items())
    print(f"[Fleet] event=received endpoint={endpoint} {fields}")
    return meta


# ── Context-aware routing helper (Phase 9) ──────────────────
# REDESIGN.md §24/§41: worker selection should consider how much context a
# request needs. Only kicks in for agent-aware callers (workflow_id
# present) — plain callers get identical behavior to every phase before
# this one (pick_worker() with no context_tokens = no filtering).

def record_context_and_get_tokens(meta: dict, content: str) -> Optional[int]:
    workflow_id = meta.get("workflow_id")
    if workflow_id is None:
        return None
    context_manager.record(
        content, ContextType.CONVERSATION, workflow_id=workflow_id, agent_id=meta.get("agent_id")
    )
    return context_manager.total_tokens(workflow_id)


# ── Routes ────────────────────────────────────────────────

@router.get("/health")
async def health():
    await load_balancer.health_check_all()
    depth = await worker_pool.queue_depth()
    return {
        "status": "ok",
        "queue_depth": depth, 
        "worker_count": autoscaler._running_worker_count(),
        "workers": load_balancer.get_worker_stats()
    }


@router.post("/generate")
async def generate(req: GenerateRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    meta = resolve_agent_metadata(req, "/generate")
    model = req.model or settings.DEFAULT_MODEL
    context_tokens = record_context_and_get_tokens(meta, req.prompt)

    try:
        worker = load_balancer.pick_worker(context_tokens=context_tokens)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        result = await worker.generate(model=model, prompt=req.prompt)
        load_balancer.record_success(worker.stats.url)
        return {**meta, **result}
    except RuntimeError as e:
        load_balancer.record_failure(worker.stats.url)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/chat")
async def chat(req: ChatRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    meta = resolve_agent_metadata(req, "/chat")
    model = req.model or settings.DEFAULT_MODEL
    context_tokens = record_context_and_get_tokens(
        meta, "\n".join(m.content for m in req.messages)
    )

    try:
        worker = load_balancer.pick_worker(context_tokens=context_tokens)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        messages = [m.model_dump() for m in req.messages]
        result = await worker.chat(model=model, messages=messages)
        load_balancer.record_success(worker.stats.url)
        return {**meta, **result}
    except RuntimeError as e:
        load_balancer.record_failure(worker.stats.url)
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/workers")
async def workers(x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)
    await load_balancer.health_check_all()
    return load_balancer.get_worker_stats()

@router.post("/queued/generate")
async def queued_generate(req: GenerateRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    meta = resolve_agent_metadata(req, "/queued/generate")
    request_id = meta["request_id"]
    model = req.model or settings.DEFAULT_MODEL
    context_tokens = record_context_and_get_tokens(meta, req.prompt)

    await worker_pool.enqueue(request_id, {
        "model": model,
        "prompt": req.prompt,
        "agent_id": req.agent_id,
        "workflow_id": req.workflow_id,
        "parent_request_id": req.parent_request_id,
        "context_tokens": context_tokens,
    })

    result = await worker_pool.get_result(request_id)
    if not result:
        raise HTTPException(status_code=504, detail="Request timed out in queue")

    return {**meta, **result}

@router.get("/queue/depth") 
async def queue_depth(x_api_key: Optional[str] = Header(None)): 
    verify_api_key(x_api_key) 
    depth = await worker_pool.queue_depth() 
    return {"queue_depth": depth}