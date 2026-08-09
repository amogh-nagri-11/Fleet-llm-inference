from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, List
from router.load_balancer import load_balancer
from config import settings
import uuid 
from workers.worker_pool import worker_pool 
from router.autoscaler import autoscaler

router = APIRouter()


# ── Request Models ────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    stream: bool = False

class Message(BaseModel):
    role: str   # "user" | "assistant" | "system"
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    model: Optional[str] = None


# ── Auth helper ───────────────────────────────────────────

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


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

    model = req.model or settings.DEFAULT_MODEL

    try:
        worker = load_balancer.pick_worker()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        result = await worker.generate(model=model, prompt=req.prompt)
        load_balancer.record_success(worker.stats.url)
        return result
    except RuntimeError as e:
        load_balancer.record_failure(worker.stats.url)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/chat")
async def chat(req: ChatRequest, x_api_key: Optional[str] = Header(None)):
    verify_api_key(x_api_key)

    model = req.model or settings.DEFAULT_MODEL

    try:
        worker = load_balancer.pick_worker()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        messages = [m.model_dump() for m in req.messages]
        result = await worker.chat(model=model, messages=messages)
        load_balancer.record_success(worker.stats.url)
        return result
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
    
    request_id = str(uuid.uuid4()) 
    model = req.model or settings.DEFAULT_MODEL 

    await worker_pool.enqueue(request_id, {
        "model": model, 
        "prompt": req.prompt, 
    })

    result = await worker_pool.get_result(request_id) 
    if not result: 
        raise HTTPException(status_code=504, detail="Request timed out in queue") 
    
    return {"request_id": request_id, **result} 

@router.get("/queue/depth") 
async def queue_depth(x_api_key: Optional[str] = Header(None)): 
    verify_api_key(x_api_key) 
    depth = await worker_pool.queue_depth() 
    return {"queue_depth": depth}