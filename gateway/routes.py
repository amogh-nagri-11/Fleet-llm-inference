from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List
from router.load_balancer import load_balancer
from config import settings
import uuid
from workers.worker_pool import worker_pool
from router.autoscaler import autoscaler
from context.manager import context_manager
from context.models import ContextType, estimate_tokens
from gateway.metrics import AGENT_REQUEST_COUNT, AGENT_WORKFLOW_FAILURES, CONTEXT_TOKENS

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

    AGENT_REQUEST_COUNT.labels(
        endpoint=endpoint, has_agent_id=str(req.agent_id is not None)
    ).inc()

    fields = " ".join(f"{k}={v}" for k, v in meta.items())
    print(f"[Fleet] event=received endpoint={endpoint} {fields}")
    return meta


def record_workflow_failure(meta: dict, endpoint: str) -> None:
    """fleet_agent_workflow_failures_total only counts agent-aware
    requests — a plain caller getting a 503 isn't a workflow failure,
    there's no workflow to have failed."""
    if meta.get("workflow_id") is not None:
        AGENT_WORKFLOW_FAILURES.labels(endpoint=endpoint).inc()


# ── Context-aware routing helper (Phase 9) ──────────────────
# REDESIGN.md §24/§41: worker selection should consider how much context a
# request needs. Only kicks in for agent-aware callers (workflow_id
# present) — plain callers get identical behavior to every phase before
# this one (pick_worker() with no context_tokens = no filtering).
#
# Recording is split from measuring on purpose. A prior version recorded
# the request into context_manager before checking whether any worker
# could actually take it — a rejected (oversized) request still
# permanently added its content to the workflow's context, and since
# ContextStore never expires anything, that workflow was stuck rejecting
# every future request forever, with no way to recover short of
# restarting the gateway. get_prospective_context_tokens() only *measures*
# what the total would become; record_context() is called by the route
# only after pick_worker() has actually succeeded, so a rejected request
# never touches the store.

def get_prospective_context_tokens(meta: dict, content: str) -> Optional[int]:
    workflow_id = meta.get("workflow_id")
    if workflow_id is None:
        return None
    tokens = context_manager.total_tokens(workflow_id) + estimate_tokens(content)
    CONTEXT_TOKENS.observe(tokens)
    return tokens


def record_context(meta: dict, content: str) -> None:
    workflow_id = meta.get("workflow_id")
    if workflow_id is None:
        return
    context_manager.record(
        content, ContextType.CONVERSATION, workflow_id=workflow_id, agent_id=meta.get("agent_id")
    )


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
    context_tokens = get_prospective_context_tokens(meta, req.prompt)

    try:
        worker = load_balancer.pick_worker(context_tokens=context_tokens)
    except RuntimeError as e:
        record_workflow_failure(meta, "/generate")
        raise HTTPException(status_code=503, detail=str(e))

    record_context(meta, req.prompt)

    try:
        result = await worker.generate(model=model, prompt=req.prompt)
        load_balancer.record_success(worker.stats.url)
        return {**meta, **result}
    except RuntimeError as e:
        load_balancer.record_failure(worker.stats.url)
        record_workflow_failure(meta, "/generate")
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/chat")
async def chat(req: ChatRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    meta = resolve_agent_metadata(req, "/chat")
    model = req.model or settings.DEFAULT_MODEL
    chat_content = "\n".join(m.content for m in req.messages)
    context_tokens = get_prospective_context_tokens(meta, chat_content)

    try:
        worker = load_balancer.pick_worker(context_tokens=context_tokens)
    except RuntimeError as e:
        record_workflow_failure(meta, "/chat")
        raise HTTPException(status_code=503, detail=str(e))

    record_context(meta, chat_content)

    try:
        messages = [m.model_dump() for m in req.messages]
        result = await worker.chat(model=model, messages=messages)
        load_balancer.record_success(worker.stats.url)
        return {**meta, **result}
    except RuntimeError as e:
        load_balancer.record_failure(worker.stats.url)
        record_workflow_failure(meta, "/chat")
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
    # Not recorded here — the queue is processed asynchronously, possibly
    # much later, so "does a worker have capacity" can only be answered
    # for real at actual dispatch time. worker_pool.py records only after
    # pick_worker() succeeds there, same rule as the synchronous routes.
    context_tokens = get_prospective_context_tokens(meta, req.prompt)

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
        record_workflow_failure(meta, "/queued/generate")
        raise HTTPException(status_code=504, detail="Request timed out in queue")
    if "error" in result:
        # worker_pool.py's own failure path — currently still returns 200
        # with an error body rather than a non-2xx status (a separate,
        # pre-existing behavior this metrics-only phase isn't changing),
        # but it's still a real failure worth counting accurately.
        record_workflow_failure(meta, "/queued/generate")

    return {**meta, **result}

@router.get("/queue/depth") 
async def queue_depth(x_api_key: Optional[str] = Header(None)): 
    verify_api_key(x_api_key) 
    depth = await worker_pool.queue_depth() 
    return {"queue_depth": depth}