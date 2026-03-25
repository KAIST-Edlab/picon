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
    model: str = ""
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    persona: str
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
async def experience_start(req: ExperienceStartRequest):
    """Start Experience Mode: launches picon.run() with bridge as the agent endpoint."""
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

    try:
        result = await asyncio.to_thread(
            picon.run,
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
            "rc": None,  # single session, no retest consistency
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
async def agent_start(req: AgentStartRequest):
    """Start a background picon.run() evaluation against an external agent."""
    if not req.name:
        raise HTTPException(status_code=400, detail="Agent name is required")
    if not req.persona:
        raise HTTPException(status_code=400, detail="Persona / system prompt is required")

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
        task = job.get("task")
        if task and not task.done():
            task.cancel()
        job["status"] = "cancelled"
        job["is_complete"] = True
    return {"status": "cancelled"}


async def _run_agent_evaluation(job_id: str, req: AgentStartRequest):
    """Background task: run picon.run() against an external agent endpoint."""
    import picon

    job = agent_jobs[job_id]
    job["logs"] = []  # list of log line strings

    # Capture picon's logging output into job["logs"]
    class _JobLogHandler(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
                job["logs"].append(msg)
            except Exception:
                pass

    log_handler = _JobLogHandler()
    log_handler.setLevel(logging.INFO)
    log_handler.setFormatter(logging.Formatter("%(message)s"))

    # Attach to root logger so we catch all picon submodule logs
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)

    try:
        result = await asyncio.to_thread(
            picon.run,
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

        if result.success:
            scores = result.eval_scores or {}
            job["result"] = {
                "ic": scores.get("internal_harmonic_mean"),
                "ec": scores.get("external_ec"),
                "rc": scores.get("inter_session_stability"),
                "internal_responsiveness": scores.get("internal_responsiveness"),
                "internal_consistency": scores.get("internal_consistency"),
                "external_coverage": scores.get("external_coverage"),
                "external_non_refutation": scores.get("external_non_refutation_rate"),
                "intra_session_stability": scores.get("intra_session_stability"),
            }
            job["status"] = "complete"
            job["is_complete"] = True

            # Save to leaderboard in Redis
            _add_to_leaderboard(req.name, req.model, job["result"], req.num_turns)

            logger.info("Agent eval complete for %s: %s", req.name, job["result"])
        else:
            job["status"] = "failed"
            job["is_complete"] = True
            job["error"] = "Evaluation failed" + (" (AI detected)" if result.ai_detected else "")

    except asyncio.CancelledError:
        job["status"] = "cancelled"
        job["is_complete"] = True

    except Exception as e:
        logger.exception("Agent eval error for %s", req.name)
        job["status"] = "error"
        job["is_complete"] = True
        job["error"] = str(e)

    finally:
        root_logger.removeHandler(log_handler)

    # Persist final state
    _update_job_redis(job_id, {
        "status": job["status"],
        "name": job["name"],
        "is_complete": True,
        "scores": job.get("result"),
        "error": job.get("error"),
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
