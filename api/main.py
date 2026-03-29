"""
PICon Demo Backend — FastAPI server.

Both Experience Mode (human-in-the-loop) and Agent Test Mode use picon.run().

Experience Mode works by routing picon's agent calls through a bridge endpoint
on this server. When picon asks a "question" to the agent, it hits our bridge,
which holds the question until the human responds via the browser.

Agent Test Mode simply calls picon.run() with the user's external API endpoint.
"""

import asyncio
import json
import logging
import multiprocessing
import os
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Optional

import redis
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("picon-api")

# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client: Optional[redis.Redis] = None

JOB_TTL = 86400  # 24 hours

# ---------------------------------------------------------------------------
# Usage / Budget tracking
# ---------------------------------------------------------------------------
# Hard cap per individual job (prevents a single runaway evaluation). 0 = off.
MAX_COST_PER_JOB = float(os.getenv("MAX_COST_PER_JOB", "3.0"))
# Daily per-IP budget — once an IP crosses this, new jobs are rejected. 0 = off.
MAX_COST_PER_IP_DAILY = float(os.getenv("MAX_COST_PER_IP_DAILY", "10.0"))
USAGE_TTL = 86400 * 7  # keep cost keys for 7 days

# In-memory cost map: job_id -> cumulative USD spent
job_costs: Dict[str, float] = {}


def _picon_worker(result_queue: multiprocessing.Queue, run_kwargs: dict):
    """
    Runs inside a subprocess. Patches picon.utils.completion so every LiteLLM
    call sends its cost back to the parent via result_queue, then calls picon.run().
    """
    import picon
    import picon.utils
    from litellm import completion as _original_completion, completion_cost

    def _patched_completion(*args, **kwargs):
        response = _original_completion(*args, **kwargs)
        try:
            cost = completion_cost(completion_response=response)
        except Exception:
            cost = 0.0
        result_queue.put(("cost", cost))
        return response

    picon.utils.completion = _patched_completion

    try:
        result = picon.run(**run_kwargs)
        result_queue.put(("result", result))
    except Exception as e:
        result_queue.put(("error", str(e)))


def _client_ip(request: Request) -> str:
    """Best-effort client IP — respects X-Forwarded-For (Railway / reverse proxy)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _ip_day_key(ip: str) -> str:
    return f"usage:ip:{ip}:{time.strftime('%Y-%m-%d')}"


def _get_ip_daily_cost(ip: str) -> float:
    try:
        r = get_redis()
        val = r.get(_ip_day_key(ip))
        return float(val) if val else 0.0
    except Exception:
        return 0.0


def _check_ip_budget(ip: str):
    """Raise HTTP 429 if the IP has already exceeded its daily budget."""
    if MAX_COST_PER_IP_DAILY <= 0:
        return
    spent = _get_ip_daily_cost(ip)
    if spent >= MAX_COST_PER_IP_DAILY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily usage limit reached (${spent:.4f} of ${MAX_COST_PER_IP_DAILY:.2f}). Try again tomorrow.",
        )


def _add_job_cost(job_id: str, cost: float) -> float:
    """Accumulate *cost* for a job; also update per-IP and global daily counters."""
    job_costs[job_id] = job_costs.get(job_id, 0.0) + cost
    job_total = job_costs[job_id]

    try:
        r = get_redis()
        # Per-job running total
        r.setex(f"cost:{job_id}", USAGE_TTL, job_total)
        # Per-IP daily total
        job = agent_jobs.get(job_id) or sessions.get(job_id, {})
        ip = job.get("client_ip", "unknown")
        ip_key = _ip_day_key(ip)
        ip_total = r.incrbyfloat(ip_key, cost)
        r.expire(ip_key, USAGE_TTL)
        # Global daily total
        day_key = f"usage:daily:{time.strftime('%Y-%m-%d')}"
        r.incrbyfloat(day_key, cost)
        r.expire(day_key, USAGE_TTL)
    except Exception:
        ip_total = 0.0

    return job_total


def _cancel_job_over_budget(job_id: str, scope: str, total: float, limit: float):
    """Kill the subprocess and mark the job cancelled."""
    job = agent_jobs.get(job_id)
    if not job or job.get("is_complete"):
        return
    msg = f"Budget exceeded ({scope}): ${total:.4f} > limit ${limit:.2f}"
    job["status"] = "cancelled"
    job["is_complete"] = True
    job["error"] = msg
    proc = job.get("process")
    if proc and proc.is_alive():
        proc.terminate()
    logger.warning("Job %s cancelled — %s", job_id, msg)

# Agent model configuration for picon pipeline
PICON_AGENT_MODELS = {
    "questioner_model": os.getenv("PICON_QUESTIONER_MODEL", "gpt-5"),
    "extractor_model": os.getenv("PICON_EXTRACTOR_MODEL", "gpt-5.1"),
    "web_search_model": os.getenv("PICON_WEB_SEARCH_MODEL", "gpt-5"),
    "evaluator_model": os.getenv("PICON_EVALUATOR_MODEL", "gemini/gemini-2.5-flash"),
}


def get_redis() -> redis.Redis:
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return redis_client


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        r = get_redis()
        r.ping()
        logger.info("Redis connected: %s", REDIS_URL)
    except Exception as e:
        logger.warning("Redis not available: %s", e)
    yield
    logger.info("Shutting down, %d active sessions", len(sessions))


app = FastAPI(title="PICon Demo API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Session store for Experience Mode bridge
# ---------------------------------------------------------------------------
# Each session holds queues for synchronizing picon <-> human
sessions: Dict[str, dict] = {}
# {
#   session_id: {
#     "question_queue": queue.Queue,  # picon puts question, browser reads
#     "response_queue": queue.Queue,  # browser puts response, picon reads
#     "task": asyncio.Task,           # background picon.run() task
#     "status": str,                  # "running", "complete", "error"
#     "progress": dict,               # latest progress info
#     "result": dict | None,          # final eval scores
#     "error": str | None,
#     "name": str,
#   }
# }

# Background jobs for Agent Test Mode
agent_jobs: Dict[str, dict] = {}


# ===========================================================================
# Pydantic models
# ===========================================================================

class ExperienceStartRequest(BaseModel):
    name: str
    num_turns: int = 50


class ExperienceRespondRequest(BaseModel):
    session_id: str
    response: str


class AgentStartRequest(BaseModel):
    name: str
    mode: str = "quick"  # "external" or "quick"
    model: str = ""
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    persona: Optional[str] = None
    num_turns: int = 50
    num_sessions: int = 2


# ===========================================================================
# Bridge endpoint — picon calls this as if it were an OpenAI-compatible agent
# ===========================================================================

@app.post("/bridge/{session_id}/v1/chat/completions")
async def bridge_chat_completions(session_id: str, request: Request):
    """
    OpenAI-compatible endpoint that picon.run() calls as the 'agent'.
    Extracts the latest question from the messages, sends it to the browser,
    and waits for the human to respond.
    """
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    body = await request.json()
    messages = body.get("messages", [])

    # Picon sends questions as the "user" role (picon is the interviewer).
    # Extract the last user message as the question to display.
    last_question = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_question = msg.get("content", "")
            break

    # Put the question in the queue for the browser to pick up
    session["question_queue"].put({
        "question": last_question,
        "messages": messages,
        "turn": session.get("turn_count", 0),
    })
    session["turn_count"] = session.get("turn_count", 0) + 1

    # Wait for the human to respond (blocking, but we're in a thread via run_in_executor)
    try:
        human_response = await asyncio.to_thread(
            session["response_queue"].get, timeout=600  # 10 min timeout per question
        )
    except queue.Empty:
        human_response = "I'd rather not answer that."

    # Return as OpenAI chat completion format
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "human",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": human_response,
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ===========================================================================
# Experience Mode — human-in-the-loop via bridge
# ===========================================================================

@app.post("/api/start")
async def experience_start(req: ExperienceStartRequest, request: Request):
    """Start Experience Mode: launches picon.run() with bridge as the agent endpoint."""
    ip = _client_ip(request)
    _check_ip_budget(ip)

    session_id = str(uuid.uuid4())

    # Determine the server's own URL for the bridge
    bridge_base = os.getenv("BRIDGE_BASE_URL", "http://localhost:8000")
    agent_api_base = f"{bridge_base}/bridge/{session_id}/v1"

    session = {
        "question_queue": queue.Queue(),
        "response_queue": queue.Queue(),
        "task": None,
        "status": "running",
        "progress": {"phase": "init", "current": 0, "total": req.num_turns},
        "result": None,
        "error": None,
        "name": req.name,
        "turn_count": -1,
        "client_ip": ip,
    }
    sessions[session_id] = session

    # Launch picon.run() in background
    task = asyncio.create_task(
        _run_experience_session(session_id, req.name, agent_api_base, req.num_turns)
    )
    session["task"] = task

    # Wait for the first message from picon (the instruction/welcome message)
    try:
        first_q = await asyncio.to_thread(
            session["question_queue"].get, timeout=60
        )
    except queue.Empty:
        error = session.get("error") or "Timeout waiting for first question"
        raise HTTPException(status_code=500, detail=error)

    # If picon failed immediately, __COMPLETE__ is the first message
    if first_q.get("question") == "__COMPLETE__":
        error = session.get("error") or "Interview failed to start"
        raise HTTPException(status_code=500, detail=error)

    return {
        "session_id": session_id,
        "first_question": first_q["question"],
        "progress": session["progress"],
    }


@app.post("/api/respond")
async def experience_respond(req: ExperienceRespondRequest):
    """Human sends a response; forward it to picon via the bridge queue."""
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Put human's response in the queue for picon to pick up
    session["response_queue"].put(req.response)

    # Wait for picon to process and produce the next question (or finish)
    try:
        next_q = await asyncio.to_thread(
            session["question_queue"].get, timeout=120  # 2 min for picon to process
        )
    except queue.Empty:
        # picon might have finished
        if session["status"] == "complete":
            return {
                "next_question": None,
                "is_complete": True,
                "progress": session["progress"],
            }
        return {
            "next_question": None,
            "is_complete": False,
            "progress": session["progress"],
            "error": "Timeout waiting for next question",
        }

    # Check if it's a completion signal
    if next_q.get("question") == "__COMPLETE__":
        return {
            "next_question": None,
            "is_complete": True,
            "progress": session["progress"],
        }

    session["progress"]["current"] = next_q.get("turn", 0)

    return {
        "next_question": next_q["question"],
        "is_complete": False,
        "progress": session["progress"],
    }


@app.get("/api/results/{session_id}")
async def experience_results(session_id: str):
    """Get evaluation results for a completed Experience Mode session."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session["status"] != "complete":
        raise HTTPException(status_code=400, detail="Interview not yet complete")

    result = session.get("result", {})

    # Cleanup
    sessions.pop(session_id, None)

    return {
        "session_id": session_id,
        "results": {"eval_scores": result},
    }


async def _run_experience_session(
    session_id: str, name: str, agent_api_base: str, num_turns: int
):
    """Background task: run picon.run() with the bridge as the agent."""
    import picon

    session = sessions[session_id]

    def _run_with_context(*args, **kwargs):
        _job_context.job_id = session_id
        try:
            return picon.run(*args, **kwargs)
        finally:
            _job_context.job_id = None

    try:
        result = await asyncio.to_thread(
            _run_with_context,
            persona="",  # no system prompt — the human IS the persona
            name=name,
            model="human",
            api_base=agent_api_base,
            num_turns=num_turns,
            num_sessions=1,  # single session for experience mode
            do_eval=True,
            output_dir="/tmp/picon_results",
            **PICON_AGENT_MODELS,
        )

        scores = result.eval_scores or {}
        session["result"] = {
            "ic": scores.get("internal_harmonic_mean"),
            "ec": scores.get("external_ec"),
            "rc": scores.get("intra_session_stability"),
        }
        session["status"] = "complete"

        # Signal completion to the browser
        session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})

        logger.info("Experience session complete for %s", name)

    except Exception as e:
        logger.exception("Experience session error for %s", name)
        session["status"] = "error"
        session["error"] = str(e)
        session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})


# ===========================================================================
# Agent Test Mode — background picon.run() with external endpoint
# ===========================================================================

@app.post("/api/agent/start")
async def agent_start(req: AgentStartRequest, request: Request):
    """Start a background picon.run() evaluation against an external agent."""
    if not req.name:
        raise HTTPException(status_code=400, detail="Agent name is required")
    if req.mode == "external" and not req.api_base:
        raise HTTPException(status_code=400, detail="API endpoint is required for external agent mode")
    if req.mode == "quick":
        if not req.model:
            raise HTTPException(status_code=400, detail="Model name is required for quick agent mode")
        if not req.persona:
            raise HTTPException(status_code=400, detail="Persona / system prompt is required for quick agent mode")

    ip = _client_ip(request)
    _check_ip_budget(ip)

    job_id = str(uuid.uuid4())

    job = {
        "status": "running",
        "name": req.name,
        "model": req.model,
        "current_session": 1,
        "total_sessions": req.num_sessions,
        "current_turn": 0,
        "total_turns": req.num_turns,
        "is_complete": False,
        "result": None,
        "error": None,
        "client_ip": ip,
    }
    agent_jobs[job_id] = job

    # Persist to Redis
    _update_job_redis(job_id, job)

    # Launch background task
    task = asyncio.create_task(
        _run_agent_evaluation(job_id, req)
    )
    job["task"] = task

    return {"session_id": job_id, "status": "running"}


@app.get("/api/agent/status/{job_id}")
async def agent_status(job_id: str):
    """Poll agent evaluation progress."""
    job = agent_jobs.get(job_id)
    if not job:
        # Try Redis
        data = _get_job_redis(job_id)
        if not data:
            raise HTTPException(status_code=404, detail="Job not found")
        return data

    return {
        "session_id": job_id,
        "status": job["status"],
        "current_session": job["current_session"],
        "total_sessions": job["total_sessions"],
        "current_turn": job["current_turn"],
        "total_turns": job["total_turns"],
        "is_complete": job["is_complete"],
        "error": job["error"],
        "cost_usd": round(job_costs.get(job_id, 0.0), 5),
    }


@app.get("/api/agent/results/{job_id}")
async def agent_results(job_id: str):
    """Get final agent evaluation results."""
    job = agent_jobs.get(job_id)
    if not job:
        data = _get_job_redis(job_id)
        if not data:
            raise HTTPException(status_code=404, detail="Job not found")
        if data.get("status") != "complete":
            raise HTTPException(status_code=400, detail="Not yet complete")
        return {"session_id": job_id, "name": data.get("name", "Agent"), "scores": data.get("scores", {})}

    if not job["is_complete"]:
        raise HTTPException(status_code=400, detail="Evaluation still in progress")
    if job["error"]:
        raise HTTPException(status_code=500, detail=job["error"])

    return {
        "session_id": job_id,
        "name": job["name"],
        "scores": job["result"],
    }


@app.get("/api/agent/logs/{job_id}")
async def agent_logs(job_id: str, since: int = 0):
    """Return captured picon logs for an agent job, starting from index `since`."""
    job = agent_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    logs = job.get("logs", [])
    return {"lines": logs[since:], "total": len(logs)}


@app.delete("/api/agent/cancel/{job_id}")
async def agent_cancel(job_id: str):
    """Cancel a running agent evaluation."""
    job = agent_jobs.get(job_id)
    if job:
        proc = job.get("process")
        if proc and proc.is_alive():
            proc.terminate()
        job["status"] = "cancelled"
        job["is_complete"] = True
    return {"status": "cancelled"}


async def _run_agent_evaluation(job_id: str, req: AgentStartRequest):
    """Background task: runs picon in a subprocess and drains its result queue."""
    job = agent_jobs[job_id]
    job["logs"] = []

    if req.mode == "external":
        run_kwargs = dict(
            name=req.name,
            api_base=req.api_base,
            num_turns=req.num_turns,
            num_sessions=req.num_sessions,
            do_eval=True,
            output_dir="/tmp/picon_results",
            **PICON_AGENT_MODELS,
        )
    else:
        run_kwargs = dict(
            persona=req.persona,
            name=req.name,
            model=req.model or "openai/custom",
            api_base=req.api_base or None,
            api_key=req.api_key or None,
            num_turns=req.num_turns,
            num_sessions=req.num_sessions,
            do_eval=True,
            output_dir="/tmp/picon_results",
            **PICON_AGENT_MODELS,
        )

    result_queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_picon_worker,
        args=(result_queue, run_kwargs),
        daemon=True,
    )
    job["process"] = proc
    proc.start()

    ip = job.get("client_ip", "unknown")

    try:
        while proc.is_alive() or not result_queue.empty():
            try:
                msg_type, payload = await asyncio.to_thread(result_queue.get, timeout=1)
            except queue.Empty:
                continue

            if msg_type == "cost":
                job_total = _add_job_cost(job_id, payload)
                logger.debug("Job %s: +$%.5f (total $%.5f)", job_id, payload, job_total)
                # Per-job cap
                if MAX_COST_PER_JOB > 0 and job_total > MAX_COST_PER_JOB:
                    _cancel_job_over_budget(job_id, "job", job_total, MAX_COST_PER_JOB)
                    return
                # Per-IP daily cap
                ip_total = _get_ip_daily_cost(ip)
                if MAX_COST_PER_IP_DAILY > 0 and ip_total > MAX_COST_PER_IP_DAILY:
                    _cancel_job_over_budget(job_id, f"IP {ip} daily", ip_total, MAX_COST_PER_IP_DAILY)
                    return

            elif msg_type == "result":
                result = payload
                if result.success:
                    scores = result.eval_scores or {}
                    job["result"] = {
                        "ic": scores.get("internal_harmonic_mean"),
                        "ec": scores.get("external_ec"),
                        "rc": scores.get("intra_session_stability"),
                        "internal_responsiveness": scores.get("internal_responsiveness"),
                        "internal_consistency": scores.get("internal_consistency"),
                        "external_coverage": scores.get("external_coverage"),
                        "external_non_refutation": scores.get("external_non_refutation_rate"),
                        "intra_session_stability": scores.get("intra_session_stability"),
                    }
                    job["status"] = "complete"
                    job["is_complete"] = True
                    _add_to_leaderboard(req.name, req.model, job["result"], req.num_turns)
                    logger.info("Agent eval complete for %s", req.name)
                else:
                    job["status"] = "failed"
                    job["is_complete"] = True
                    job["error"] = "Evaluation failed" + (" (AI detected)" if result.ai_detected else "")

            elif msg_type == "error":
                logger.error("Agent eval error for %s: %s", req.name, payload)
                job["status"] = "error"
                job["is_complete"] = True
                job["error"] = payload

    except asyncio.CancelledError:
        if proc.is_alive():
            proc.terminate()
        job["status"] = "cancelled"
        job["is_complete"] = True

    except Exception as e:
        logger.exception("Agent eval error for %s", req.name)
        if proc.is_alive():
            proc.terminate()
        job["status"] = "error"
        job["is_complete"] = True
        job["error"] = str(e)

    finally:
        proc.join(timeout=5)

    _update_job_redis(job_id, {
        "status": job["status"],
        "name": job["name"],
        "is_complete": True,
        "scores": job.get("result"),
        "error": job.get("error"),
        "cost_usd": round(job_costs.get(job_id, 0.0), 5),
    })


# ===========================================================================
# Leaderboard
# ===========================================================================

@app.get("/api/leaderboard")
async def get_leaderboard():
    """Return community leaderboard entries from Redis."""
    try:
        r = get_redis()
        raw = r.lrange("leaderboard:community", 0, -1)
        return {"entries": [json.loads(e) for e in raw]}
    except Exception:
        return {"entries": []}


def _add_to_leaderboard(name: str, model: str, scores: dict, turns: int = 50):
    """Add a completed evaluation to the community leaderboard in Redis."""
    try:
        r = get_redis()
        entry = {
            "name": name,
            "model": model,
            "type": "community",
            "arch": "Community",
            "turns": turns,
            "ic": scores.get("ic") or 0,
            "ec": scores.get("ec") or 0,
            "rc": scores.get("rc") or 0,
            "timestamp": time.time(),
        }
        r.rpush("leaderboard:community", json.dumps(entry))
    except Exception as e:
        logger.warning("Failed to add leaderboard entry: %s", e)


@app.delete("/api/leaderboard/latest")
async def delete_latest_leaderboard():
    """Remove the most recent community leaderboard entry."""
    try:
        r = get_redis()
        removed = r.rpop("leaderboard:community")
        if removed:
            return {"removed": json.loads(removed)}
        return {"removed": None, "message": "Leaderboard is empty"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# Redis helpers
# ===========================================================================

def _update_job_redis(job_id: str, data: dict):
    try:
        r = get_redis()
        r.setex(f"job:{job_id}", JOB_TTL, json.dumps(data))
    except Exception:
        pass


def _get_job_redis(job_id: str) -> Optional[dict]:
    try:
        r = get_redis()
        raw = r.get(f"job:{job_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ===========================================================================
# Usage summary
# ===========================================================================

@app.get("/api/usage")
async def get_usage():
    """Return daily cost totals broken down by day and by IP."""
    daily_global: Dict[str, float] = {}
    daily_by_ip: Dict[str, Dict[str, float]] = {}

    try:
        r = get_redis()
        for key in r.keys("usage:daily:*"):
            val = r.get(key)
            if val:
                daily_global[key.replace("usage:daily:", "")] = round(float(val), 5)
        for key in r.keys("usage:ip:*"):
            val = r.get(key)
            if val:
                # key format: usage:ip:{ip}:{date}
                parts = key.split(":", 3)  # ["usage", "ip", ip, date]
                if len(parts) == 4:
                    ip, date = parts[2], parts[3]
                    daily_by_ip.setdefault(ip, {})[date] = round(float(val), 5)
    except Exception:
        pass

    active = {
        jid: {
            "cost_usd": round(job_costs.get(jid, 0.0), 5),
            "client_ip": agent_jobs[jid].get("client_ip", "unknown"),
        }
        for jid in agent_jobs
        if not agent_jobs[jid].get("is_complete")
    }

    return {
        "limits": {
            "max_cost_per_job_usd": MAX_COST_PER_JOB,
            "max_cost_per_ip_daily_usd": MAX_COST_PER_IP_DAILY,
        },
        "daily_totals_usd": daily_global,
        "daily_by_ip_usd": daily_by_ip,
        "active_jobs": active,
    }


# ===========================================================================
# Health
# ===========================================================================

@app.get("/api/health")
async def health():
    redis_ok = False
    try:
        get_redis().ping()
        redis_ok = True
    except Exception:
        pass

    picon_ok = False
    picon_err = None
    try:
        import picon
        picon_ok = True
    except Exception as e:
        picon_err = str(e)

    env_keys = {
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "SERPER_API_KEY": bool(os.getenv("SERPER_API_KEY")),
        "BRIDGE_BASE_URL": os.getenv("BRIDGE_BASE_URL", "(not set)"),
    }

    return {
        "status": "ok",
        "redis": redis_ok,
        "picon": picon_ok,
        "picon_error": picon_err,
        "env_keys": env_keys,
    }
