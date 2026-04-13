"""
PICon Demo Backend — FastAPI server.

Both Experience Mode (human-in-the-loop) and Agent Test Mode use picon.run().

Experience Mode works by routing picon's agent calls through a bridge endpoint
on this server. When picon asks a "question" to the agent, it hits our bridge,
which holds the question until the human responds via the browser.

Agent Test Mode simply calls picon.run() with the user's external API endpoint.
"""

import asyncio
import logging
import multiprocessing
import os
import sys
import queue
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("picon-api")

# ---------------------------------------------------------------------------
# SQLite for persistent storage (leaderboard + completed results)
# ---------------------------------------------------------------------------
SQLITE_PATH = os.getenv("SQLITE_PATH", "/tmp/picon_data.db")
_sqlite_lock = threading.Lock()


def _init_db():
    """Create tables if they don't exist."""
    with _sqlite_lock:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leaderboard (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'community',
                arch TEXT NOT NULL DEFAULT 'Community',
                turns INTEGER NOT NULL DEFAULT 50,
                ic REAL NOT NULL DEFAULT 0,
                ec REAL NOT NULL DEFAULT 0,
                rc REAL NOT NULL DEFAULT 0,
                timestamp REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS completed_jobs (
                job_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                ic REAL,
                ec REAL,
                rc REAL,
                internal_responsiveness REAL,
                internal_consistency REAL,
                inter_session_stability REAL,
                intra_session_stability REAL,
                error TEXT,
                cost_usd REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                ip TEXT NOT NULL,
                cost REAL NOT NULL,
                date TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cost_log_ip_date ON cost_log (ip, date)"
        )
        conn.commit()
        conn.close()

# ---------------------------------------------------------------------------
# Concurrency control
# ---------------------------------------------------------------------------
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "60"))
# Soft deadline: at this point we log a warning and STOP counting fresh cost toward
# the user, but we keep reading messages from the subprocess so an almost-finished
# picon.run() can still deliver its result. Crucial for slow user backends (e.g. a
# single Ollama instance serving 4+ concurrent interrogation sessions).
JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", "3600"))  # 60 minutes soft deadline
# Hard deadline: after this, we really terminate the subprocess. Must be >= JOB_TIMEOUT.
JOB_HARD_TIMEOUT = int(os.getenv("JOB_HARD_TIMEOUT", str(JOB_TIMEOUT + 1800)))  # +30 min grace
# Experience Mode: if browser stops polling for this many seconds, consider abandoned
EXPERIENCE_ABANDON_TIMEOUT = int(os.getenv("EXPERIENCE_ABANDON_TIMEOUT", "120"))  # 2 minutes
_job_semaphore: Optional[asyncio.Semaphore] = None
_queued_job_ids: list = []  # ordered list of waiting job IDs for position tracking
_queue_lock: Optional[asyncio.Lock] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _job_semaphore
    if _job_semaphore is None:
        _job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    return _job_semaphore


def _get_queue_lock() -> asyncio.Lock:
    global _queue_lock
    if _queue_lock is None:
        _queue_lock = asyncio.Lock()
    return _queue_lock


# ---------------------------------------------------------------------------
# API key pool — round-robin across multiple OpenAI keys
# ---------------------------------------------------------------------------
def _load_key_pool() -> list:
    """Load OpenAI keys from OPENAI_API_KEYS (comma-separated) or fall back to OPENAI_API_KEY."""
    multi = os.getenv("OPENAI_API_KEYS", "")
    if multi:
        keys = [k.strip() for k in multi.split(",") if k.strip()]
        if keys:
            return keys
    single = os.getenv("OPENAI_API_KEY", "")
    return [single] if single else []


_openai_key_pool: list = _load_key_pool()
_key_assignment_lock = threading.Lock()
# Track how many active jobs each key index has
_key_active_counts: Dict[int, int] = {i: 0 for i in range(len(_openai_key_pool))}


def _acquire_key() -> tuple:
    """Pick the least-loaded key. Returns (key_index, key_string)."""
    if not _openai_key_pool:
        return (-1, os.getenv("OPENAI_API_KEY", ""))
    with _key_assignment_lock:
        # Pick the key with fewest active jobs
        idx = min(_key_active_counts, key=_key_active_counts.get)
        _key_active_counts[idx] = _key_active_counts.get(idx, 0) + 1
        logger.info("Assigned key pool[%d] (%d active on this key)", idx, _key_active_counts[idx])
        return (idx, _openai_key_pool[idx])


def _release_key(idx: int):
    """Release a key slot back to the pool."""
    if idx < 0:
        return
    with _key_assignment_lock:
        _key_active_counts[idx] = max(0, _key_active_counts.get(idx, 1) - 1)
        logger.info("Released key pool[%d] (%d active on this key)", idx, _key_active_counts[idx])

# picon uses the first 10 turns for pre-screening questions;
# the user-facing "num_turns" includes those, so subtract before calling picon.run().
# picon's max_turns already includes the 10 predefined get-to-know questions,
# so we pass the user's requested turns directly (no subtraction needed).
# The repeat phase (10 more turns) runs on top of max_turns automatically.

# ---------------------------------------------------------------------------
# Usage / Budget tracking  (SQLite-backed, survives restarts)
# ---------------------------------------------------------------------------
# Hard cap per individual job (prevents a single runaway evaluation). 0 = off.
MAX_COST_PER_JOB = float(os.getenv("MAX_COST_PER_JOB", "3.0"))
# Daily per-IP budget — once an IP crosses this, new jobs are rejected. 0 = off.
MAX_COST_PER_IP_DAILY = float(os.getenv("MAX_COST_PER_IP_DAILY", "10.0"))

# In-memory cache for per-job totals (fast path; rebuilt from SQLite on restart isn't
# needed because per-job cost only matters while the job is running).
_cost_lock = threading.Lock()
job_costs: Dict[str, float] = {}  # job_id -> cumulative USD


class _QueueLogStream:
    """File-like object that forwards writes to a multiprocessing.Queue as ('log', line) tuples."""

    def __init__(self, q: multiprocessing.Queue):
        self._q = q
        self._buf = ""

    def write(self, s: str):
        if not s:
            return
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try:
                    self._q.put(("log", line))
                except Exception:
                    pass

    def flush(self):
        if self._buf.strip():
            try:
                self._q.put(("log", self._buf))
            except Exception:
                pass
        self._buf = ""


def _picon_worker(result_queue: multiprocessing.Queue, run_kwargs: dict, openai_key: str = "", extra_env: Optional[dict] = None):
    """
    Runs inside a subprocess. Sets the assigned OpenAI key in the subprocess
    environment, then calls picon.run(). Used by both Agent Test and Experience modes.

    Captures stdout, stderr, and root logger output and forwards them as
    ('log', line) messages so the parent can expose them via /api/agent/logs.
    """
    if openai_key:
        os.environ["OPENAI_API_KEY"] = openai_key
    # Apply user-supplied env vars (already sanitized by the parent process).
    if extra_env:
        for k, v in extra_env.items():
            os.environ[k] = str(v)

    # Install log capture BEFORE importing picon so its module-level logger setup is intercepted.
    import logging as _logging
    stream = _QueueLogStream(result_queue)
    sys.stdout = stream
    sys.stderr = stream
    root = _logging.getLogger()
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(_logging.INFO)

    import picon

    try:
        result = picon.run(**run_kwargs)
        result_queue.put(("result", result))
    except Exception as e:
        import traceback
        result_queue.put(("log", f"ERROR: {e}"))
        result_queue.put(("log", traceback.format_exc()))
        result_queue.put(("error", str(e)))
    finally:
        try:
            stream.flush()
        except Exception:
            pass


def _client_ip(request: Request) -> str:
    """Best-effort client IP — respects X-Forwarded-For (Railway / reverse proxy)."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _get_ip_daily_cost(ip: str) -> float:
    """Read today's cumulative cost for an IP from SQLite."""
    today = time.strftime("%Y-%m-%d")
    try:
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            row = conn.execute(
                "SELECT COALESCE(SUM(cost), 0) FROM cost_log WHERE ip = ? AND date = ?",
                (ip, today),
            ).fetchone()
            conn.close()
            return float(row[0]) if row else 0.0
    except Exception as e:
        logger.warning("_get_ip_daily_cost failed for %s: %s", ip, e)
        return 0.0


async def _check_ip_budget(ip: str):
    """Raise HTTP 429 if the IP has already exceeded its daily budget."""
    if MAX_COST_PER_IP_DAILY <= 0:
        return
    spent = await asyncio.to_thread(_get_ip_daily_cost, ip)
    if spent >= MAX_COST_PER_IP_DAILY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily usage limit reached (${spent:.4f} of ${MAX_COST_PER_IP_DAILY:.2f}). Try again tomorrow.",
        )


def _add_job_cost(job_id: str, cost: float) -> float:
    """Accumulate cost for a job. Writes to in-memory cache + SQLite."""
    with _cost_lock:
        job_costs[job_id] = job_costs.get(job_id, 0.0) + cost
        job_total = job_costs[job_id]

    # Persist to SQLite
    job = agent_jobs.get(job_id) or sessions.get(job_id, {})
    ip = job.get("client_ip", "unknown")
    today = time.strftime("%Y-%m-%d")
    try:
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.execute(
                "INSERT INTO cost_log (job_id, ip, cost, date, created_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, ip, cost, today, time.time()),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("_add_job_cost SQLite write failed for job %s: %s", job_id, e)

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


# ---------------------------------------------------------------------------
# Completed job archive (SQLite-backed, survives restarts)
# ---------------------------------------------------------------------------

def _archive_job(job_id: str, data: dict):
    """Persist a completed job to SQLite."""
    scores = data.get("scores") or {}
    try:
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.execute(
                """INSERT OR REPLACE INTO completed_jobs
                   (job_id, name, model, status, ic, ec, rc,
                    internal_responsiveness, internal_consistency,
                    inter_session_stability, intra_session_stability,
                    error, cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, data.get("name", ""), data.get("model", ""),
                 data.get("status", "unknown"),
                 scores.get("ic"), scores.get("ec"), scores.get("rc"),
                 scores.get("internal_responsiveness"),
                 scores.get("internal_consistency"),
                 scores.get("inter_session_stability"),
                 scores.get("intra_session_stability"),
                 data.get("error"), data.get("cost_usd", 0.0), time.time()),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Failed to archive job %s: %s", job_id, e)


def _get_archived_job(job_id: str) -> Optional[dict]:
    """Retrieve a completed job from SQLite."""
    try:
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM completed_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            conn.close()
            if not row:
                return None
            row = dict(row)
        scores = {
            "ic": row["ic"], "ec": row["ec"], "rc": row["rc"],
            "internal_responsiveness": row["internal_responsiveness"],
            "internal_consistency": row["internal_consistency"],
            "inter_session_stability": row["inter_session_stability"],
            "intra_session_stability": row["intra_session_stability"],
        }
        return {
            "session_id": row["job_id"],
            "status": row["status"],
            "name": row["name"],
            "is_complete": True,
            "scores": scores,
            "error": row["error"],
            "cost_usd": row["cost_usd"],
        }
    except Exception as e:
        logger.warning("Failed to retrieve archived job %s: %s", job_id, e)
        return None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Expand default thread pool so Experience Mode bridge doesn't starve
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=300))

    _init_db()
    logger.info("SQLite leaderboard initialized at %s", SQLITE_PATH)
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
    api_version: Optional[str] = None  # for Azure-style providers
    extra_env: Optional[dict] = None   # arbitrary env vars for litellm (e.g. AWS_*, AZURE_*)
    persona: Optional[str] = None
    num_turns: int = 50
    num_sessions: int = 2


# Env var keys the user is NOT allowed to set — would collide with picon's
# evaluator auth or leak server-internal config.
_EXTRA_ENV_DENYLIST = {
    # Picon evaluator / search keys (server-owned)
    "OPENAI_API_KEY", "OPENAI_API_KEYS",
    "GEMINI_API_KEY",
    "SERPER_API_KEY", "TAVILY_API_KEY",
    # Server config
    "BRIDGE_BASE_URL", "MAX_CONCURRENT_JOBS",
    "MAX_COST_PER_IP_DAILY", "MAX_COST_PER_JOB",
    "REDIS_URL", "SQLITE_PATH",
    # Runtime / shell (prevent hijacking)
    "PATH", "HOME", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "OPENBLAS_NUM_THREADS",
}
_EXTRA_ENV_DENY_PREFIXES = ("PICON_",)


def _sanitize_extra_env(raw: Optional[dict]) -> dict:
    """Filter user-supplied env vars: reject denylisted keys, coerce to strings.

    Raises HTTPException(400) if a denied key is present.
    """
    if not raw:
        return {}
    clean: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k:
            continue
        key = k.strip()
        up = key.upper()
        if up in _EXTRA_ENV_DENYLIST or any(up.startswith(p) for p in _EXTRA_ENV_DENY_PREFIXES):
            raise HTTPException(
                status_code=400,
                detail=f"Env var '{key}' is not allowed (reserved by server).",
            )
        clean[key] = "" if v is None else str(v)
    return clean


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

    # Wait for the human to respond (abandon detection is handled by the drain loop)
    try:
        human_response = await asyncio.to_thread(
            session["response_queue"].get, timeout=300  # 5 min max per question
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
    """Start Experience Mode: launches picon.run() with bridge as the agent endpoint.

    If all concurrency slots are full the session enters 'queued' status and the
    response returns immediately so the browser can poll /api/experience/status.
    """
    ip = _client_ip(request)
    await _check_ip_budget(ip)

    session_id = str(uuid.uuid4())

    # Determine the server's own URL for the bridge
    bridge_base = os.getenv("BRIDGE_BASE_URL", "http://localhost:8000")
    agent_api_base = f"{bridge_base}/bridge/{session_id}/v1"

    session = {
        "question_queue": queue.Queue(),
        "response_queue": queue.Queue(),
        "task": None,
        "status": "queued",
        "progress": {"phase": "init", "current": 0, "total": None},
        "result": None,
        "error": None,
        "name": req.name,
        "turn_count": -1,
        "client_ip": ip,
        "key_idx": -1,
        "last_poll_time": time.time(),
        "started_at": time.time(),
    }
    sessions[session_id] = session

    # Subtract pre-screening turns so picon gets pure interview turn count
    picon_turns = max(req.num_turns, 15)
    session["picon_turns"] = picon_turns

    # Launch background task (will wait for semaphore if at capacity)
    task = asyncio.create_task(
        _run_experience_session(session_id, req.name, agent_api_base, picon_turns)
    )
    session["task"] = task

    # If a slot is available, wait briefly for the first question so the UX
    # stays snappy. Otherwise return immediately with queued status.
    sem = _get_semaphore()
    if sem._value > 0:
        # Slot likely available — wait for the first question
        try:
            first_q = await asyncio.to_thread(
                session["question_queue"].get, timeout=60
            )
        except queue.Empty:
            error = session.get("error") or "Timeout waiting for first question"
            raise HTTPException(status_code=500, detail=error)

        if first_q.get("question") == "__COMPLETE__":
            error = session.get("error") or "Interview failed to start"
            raise HTTPException(status_code=500, detail=error)

        return {
            "session_id": session_id,
            "status": "running",
            "first_question": first_q["question"],
            "progress": session["progress"],
        }

    # No slots — return queued status, frontend will poll
    return {
        "session_id": session_id,
        "status": "queued",
        "first_question": None,
        "progress": session["progress"],
    }


@app.get("/api/experience/status/{session_id}")
async def experience_status(session_id: str):
    """Poll experience session status — used when session was queued at start."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session["last_poll_time"] = time.time()
    queue_position = None
    if session["status"] == "queued" and session_id in _queued_job_ids:
        queue_position = _queued_job_ids.index(session_id) + 1

    resp = {
        "session_id": session_id,
        "status": session["status"],
        "queue_position": queue_position,
        "max_concurrent": MAX_CONCURRENT_JOBS,
        "progress": session["progress"],
        "first_question": None,
    }

    # If it transitioned to running, try to grab the first question
    if session["status"] == "running":
        try:
            first_q = session["question_queue"].get_nowait()
            if first_q.get("question") == "__COMPLETE__":
                resp["status"] = session["status"]
                resp["error"] = session.get("error") or "Interview failed to start"
            else:
                resp["first_question"] = first_q["question"]
        except queue.Empty:
            pass  # not ready yet, keep polling

    if session["status"] == "error":
        resp["error"] = session.get("error")

    return resp


@app.post("/api/respond")
async def experience_respond(req: ExperienceRespondRequest):
    """Human sends a response; forward it to picon via the bridge queue."""
    session = sessions.get(req.session_id)
    if session:
        session["last_poll_time"] = time.time()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Put human's response in the queue for picon to pick up
    session["response_queue"].put(req.response)

    # Wait for picon to process and produce the next question (or finish).
    # Under heavy load LLM calls can be slow, so we retry with a long total deadline
    # rather than giving up after a single timeout.
    next_q = None
    deadline = time.time() + 600  # 10 min total patience
    while time.time() < deadline:
        try:
            next_q = await asyncio.to_thread(
                session["question_queue"].get, timeout=10  # check every 10s
            )
            break
        except queue.Empty:
            # Check if picon finished or errored while we were waiting
            if session["status"] in ("complete", "error", "cancelled"):
                return {
                    "next_question": None,
                    "is_complete": True,
                    "progress": session["progress"],
                }
            continue  # keep waiting

    if next_q is None:
        # Timed out after full deadline — something is stuck
        return {
            "next_question": None,
            "is_complete": True,
            "progress": session["progress"],
            "error": "Timed out waiting for next question",
        }

    # Check if it's a completion signal
    if next_q.get("question") == "__COMPLETE__":
        return {
            "next_question": None,
            "is_complete": True,
            "progress": session["progress"],
        }

    # next_q["turn"]: -1=instruction, 0=first question, 1=second, ...
    # Display as 1-based, skipping instruction
    turn = next_q.get("turn", 0)
    session["progress"]["current"] = turn + 1

    # Update phase based on turn count vs picon_turns (stored at session start).
    # Warmup (predefined get-to-know): turns 0–9
    # Main interrogation: turns 10 – (picon_turns-1)
    # Repeat (retest): turns >= picon_turns
    # Confirmation questions are interleaved but we estimate phase from the count.
    picon_turns = session.get("picon_turns", 30)
    if turn < 10:
        session["progress"]["phase"] = "predefined"
    elif turn < picon_turns:
        session["progress"]["phase"] = "main"
    else:
        session["progress"]["phase"] = "repeat"

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

    if session["status"] == "complete" and session.get("result"):
        result = session.get("result", {})
        # Cleanup
        sessions.pop(session_id, None)
        return {
            "session_id": session_id,
            "status": "complete",
            "results": {"eval_scores": result},
        }

    if session["status"] in ("error", "cancelled"):
        error = session.get("error", "Unknown error")
        sessions.pop(session_id, None)
        return {
            "session_id": session_id,
            "status": "error",
            "error": error,
            "results": {"eval_scores": {}},
        }

    # Still running (eval in progress)
    return {
        "session_id": session_id,
        "status": "pending",
        "results": None,
    }


async def _run_experience_session(
    session_id: str, name: str, agent_api_base: str, num_turns: int
):
    """Background task: waits for a semaphore slot, then runs picon.run() in a subprocess.

    picon communicates with the browser via HTTP calls to the bridge endpoint,
    so running in a subprocess is safe — no shared queues cross the process boundary.
    """
    session = sessions[session_id]
    qlock = _get_queue_lock()

    # --- Queue until a slot opens ---
    sem = _get_semaphore()
    async with qlock:
        _queued_job_ids.append(session_id)
    logger.info("Experience %s queued (position %d)", session_id, len(_queued_job_ids))

    try:
        await sem.acquire()
    except asyncio.CancelledError:
        async with qlock:
            if session_id in _queued_job_ids:
                _queued_job_ids.remove(session_id)
        session["status"] = "cancelled"
        session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})
        return

    key_idx = -1
    proc = None
    try:
        async with qlock:
            if session_id in _queued_job_ids:
                _queued_job_ids.remove(session_id)
        session["status"] = "running"

        # Acquire an API key from the pool
        key_idx, openai_key = _acquire_key()
        session["key_idx"] = key_idx
        logger.info("Experience %s started (slots: %d/%d used, key pool[%d])",
                     session_id, MAX_CONCURRENT_JOBS - sem._value, MAX_CONCURRENT_JOBS, key_idx)

        run_kwargs = dict(
            persona="",  # no system prompt — the human IS the persona
            name=name,
            model="human",
            api_base=agent_api_base,
            num_turns=num_turns,
            num_sessions=1,
            do_eval=True,
            output_dir=f"/tmp/picon_results/{session_id}",
            **PICON_AGENT_MODELS,
        )

        result_queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_picon_worker,
            args=(result_queue, run_kwargs, openai_key),
            daemon=True,
        )
        session["process"] = proc
        proc.start()

        # Drain results from the subprocess
        job_start = session.get("started_at", time.time())
        while proc.is_alive() or not result_queue.empty():
            # Hard job timeout — only terminate after the hard deadline. We let the
            # subprocess keep running past the soft deadline in case it's almost done.
            if time.time() - job_start > JOB_HARD_TIMEOUT:
                logger.warning("Experience %s HARD timeout after %ds", session_id, JOB_HARD_TIMEOUT)
                if proc.is_alive():
                    proc.terminate()
                session["status"] = "error"
                session["error"] = "Session timed out"
                session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})
                break
            # Check browser abandon
            last_poll = session.get("last_poll_time", time.time())
            if session["status"] == "running" and time.time() - last_poll > EXPERIENCE_ABANDON_TIMEOUT:
                logger.warning("Experience %s abandoned (no browser activity for %ds)", session_id, EXPERIENCE_ABANDON_TIMEOUT)
                if proc.is_alive():
                    proc.terminate()
                session["status"] = "error"
                session["error"] = "Session abandoned"
                session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})
                break

            try:
                msg_type, payload = await asyncio.to_thread(result_queue.get, timeout=1)
            except queue.Empty:
                continue

            if msg_type == "log":
                # Experience mode shows the chat UI instead of a log stream; drop picon logs.
                continue

            if msg_type == "result":
                result = payload
                scores = result.eval_scores or {}
                logger.info("Experience eval_scores keys for %s: %s", name, list(scores.keys()))
                logger.info("Experience eval_scores for %s: %s", name, scores)
                session["result"] = {
                    "ic": scores.get("internal_harmonic_mean"),
                    "ec": scores.get("external_ec"),
                    "rc": scores.get("intra_session_stability"),
                }
                session["status"] = "complete"
                session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})
                logger.info("Experience session complete for %s: %s", name, session["result"])

            elif msg_type == "cost":
                _add_job_cost(session_id, payload)

            elif msg_type == "error":
                logger.error("Experience session error for %s: %s", name, payload)
                session["status"] = "error"
                session["error"] = payload
                session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})

    except asyncio.CancelledError:
        if proc and proc.is_alive():
            proc.terminate()
        session["status"] = "cancelled"
        session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})

    except Exception as e:
        logger.exception("Experience session error for %s", name)
        if proc and proc.is_alive():
            proc.terminate()
        session["status"] = "error"
        session["error"] = str(e)
        session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})

    finally:
        if proc:
            proc.join(timeout=5)
        _release_key(key_idx)
        sem.release()
        logger.info("Experience %s released slot + key pool[%d]", session_id, key_idx)


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
        # Prevent agent-inference leeching: without at least some form of
        # user-provided credentials, litellm would silently fall back to the
        # server's env (e.g. OPENAI_API_KEY), running the user's agent on our
        # dime. Require api_key, api_base, or a non-empty extra_env.
        has_creds = bool(req.api_key) or bool(req.api_base) or bool(req.extra_env)
        if not has_creds:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Provide credentials for your agent model: an API key, an API Base URL "
                    "(for local/self-hosted models), or env vars (Advanced — for providers "
                    "like Bedrock that authenticate via env vars)."
                ),
            )
        # Extra safety: if the model is a first-party cloud provider that we
        # have server-side keys for, litellm could fall back to our keys even
        # when user supplied extra_env. Require an explicit api_key for these.
        leech_prefixes = ("openai/", "gemini/")
        if not req.api_key and req.model.lower().startswith(leech_prefixes):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model '{req.model}' requires an explicit API key (to prevent "
                    "accidental use of the server's key)."
                ),
            )
        # Validate and filter user-supplied env vars (raises 400 on denylisted keys).
        req.extra_env = _sanitize_extra_env(req.extra_env)

    ip = _client_ip(request)
    await _check_ip_budget(ip)

    job_id = str(uuid.uuid4())

    job = {
        "status": "queued",
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
        "started_at": time.time(),
    }
    agent_jobs[job_id] = job

    # Launch background task (will wait for semaphore if at capacity)
    task = asyncio.create_task(
        _run_agent_evaluation(job_id, req)
    )
    job["task"] = task

    return {"session_id": job_id, "status": "queued"}


@app.get("/api/agent/status/{job_id}")
async def agent_status(job_id: str):
    """Poll agent evaluation progress."""
    job = agent_jobs.get(job_id)
    if not job:
        # Try archived completed jobs
        data = _get_archived_job(job_id)
        if not data:
            raise HTTPException(status_code=404, detail="Job not found")
        return data

    queue_position = None
    if job["status"] == "queued" and job_id in _queued_job_ids:
        queue_position = _queued_job_ids.index(job_id) + 1

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
        "queue_position": queue_position,
        "max_concurrent": MAX_CONCURRENT_JOBS,
    }


@app.get("/api/agent/results/{job_id}")
async def agent_results(job_id: str):
    """Get final agent evaluation results."""
    job = agent_jobs.get(job_id)
    if not job:
        data = _get_archived_job(job_id)
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
    """Cancel a running agent evaluation.

    The background task's own try/finally handles semaphore and key release
    when the asyncio.Task is cancelled, so we just cancel the task here.
    """
    job = agent_jobs.get(job_id)
    if job:
        task = job.get("task")
        if task and not task.done():
            task.cancel()
        else:
            # Task already finished — just mark it
            proc = job.get("process")
            if proc and proc.is_alive():
                proc.terminate()
            job["status"] = "cancelled"
            job["is_complete"] = True
    return {"status": "cancelled"}


async def _run_agent_evaluation(job_id: str, req: AgentStartRequest):
    """Background task: waits for a semaphore slot, then runs picon in a subprocess."""
    job = agent_jobs[job_id]
    job["logs"] = []
    qlock = _get_queue_lock()

    # --- Queue until a slot opens ---
    sem = _get_semaphore()
    async with qlock:
        _queued_job_ids.append(job_id)
    logger.info("Job %s queued (position %d)", job_id, len(_queued_job_ids))

    try:
        await sem.acquire()
    except asyncio.CancelledError:
        async with qlock:
            if job_id in _queued_job_ids:
                _queued_job_ids.remove(job_id)
        job["status"] = "cancelled"
        job["is_complete"] = True
        return

    # Everything below must release sem (and key once acquired) on any exit path.
    key_idx = -1
    proc = None
    try:
        # Slot acquired — remove from queue, mark running
        async with qlock:
            if job_id in _queued_job_ids:
                _queued_job_ids.remove(job_id)
        job["status"] = "running"

        # Acquire an API key from the pool
        key_idx, openai_key = _acquire_key()
        job["key_idx"] = key_idx
        logger.info("Job %s started (slots: %d/%d used, key pool[%d])", job_id,
                     MAX_CONCURRENT_JOBS - sem._value, MAX_CONCURRENT_JOBS, key_idx)

        picon_turns = max(req.num_turns, 15)

        if req.mode == "external":
            run_kwargs = dict(
                persona="",  # external agents have persona baked into their endpoint
                name=req.name,
                api_base=req.api_base,
                num_turns=picon_turns,
                num_sessions=req.num_sessions,
                do_eval=True,
                output_dir=f"/tmp/picon_results/{job_id}",
                **PICON_AGENT_MODELS,
            )
        else:
            run_kwargs = dict(
                persona=req.persona,
                name=req.name,
                model=req.model or "openai/custom",
                api_base=req.api_base or None,
                api_key=req.api_key or None,
                num_turns=picon_turns,
                num_sessions=req.num_sessions,
                do_eval=True,
                output_dir=f"/tmp/picon_results/{job_id}",
                **PICON_AGENT_MODELS,
            )
            if req.api_version:
                run_kwargs["api_version"] = req.api_version

        result_queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_picon_worker,
            args=(result_queue, run_kwargs, openai_key, req.extra_env if req.mode == "quick" else None),
            daemon=True,
        )
        job["process"] = proc
        proc.start()

        ip = job.get("client_ip", "unknown")
        job_start = job.get("started_at", time.time())
        soft_timeout_warned = False

        while proc.is_alive() or not result_queue.empty():
            elapsed = time.time() - job_start
            # Hard deadline: really kill the subprocess and give up.
            if elapsed > JOB_HARD_TIMEOUT:
                logger.warning("Job %s HARD timeout after %ds — terminating", job_id, int(elapsed))
                if proc.is_alive():
                    proc.terminate()
                job["status"] = "error"
                job["is_complete"] = True
                job["error"] = f"Job timed out after {JOB_HARD_TIMEOUT}s"
                break
            # Soft deadline: warn once, keep reading. Slow user backends (e.g. Ollama
            # under concurrent load) routinely go past this point and still deliver
            # a valid result — we must not throw that work away.
            if elapsed > JOB_TIMEOUT and not soft_timeout_warned:
                logger.warning(
                    "Job %s past soft deadline (%ds) — letting subprocess finish up to %ds",
                    job_id, JOB_TIMEOUT, JOB_HARD_TIMEOUT,
                )
                job["logs"].append(
                    f"[server] This job has been running for {int(elapsed)}s. Still waiting "
                    f"for the subprocess to finish (grace period until {JOB_HARD_TIMEOUT}s)."
                )
                soft_timeout_warned = True
            try:
                msg_type, payload = await asyncio.to_thread(result_queue.get, timeout=1)
            except queue.Empty:
                continue

            if msg_type == "log":
                job["logs"].append(payload)
                if len(job["logs"]) > 5000:
                    # Trim to bound memory for very long runs
                    job["logs"] = job["logs"][-4000:]

            elif msg_type == "cost":
                job_total = _add_job_cost(job_id, payload)
                logger.debug("Job %s: +$%.5f (total $%.5f)", job_id, payload, job_total)
                # Per-job cap
                if MAX_COST_PER_JOB > 0 and job_total > MAX_COST_PER_JOB:
                    _cancel_job_over_budget(job_id, "job", job_total, MAX_COST_PER_JOB)
                    return
                # Per-IP daily cap
                ip_total = await asyncio.to_thread(_get_ip_daily_cost, ip)
                if MAX_COST_PER_IP_DAILY > 0 and ip_total > MAX_COST_PER_IP_DAILY:
                    _cancel_job_over_budget(job_id, f"IP {ip} daily", ip_total, MAX_COST_PER_IP_DAILY)
                    return

            elif msg_type == "result":
                result = payload
                if result.success:
                    scores = result.eval_scores or {}
                    logger.info("Agent eval_scores keys for %s: %s", req.name, list(scores.keys()))
                    logger.info("Agent eval_scores for %s: %s", req.name, scores)
                    job["result"] = {
                        "ic": scores.get("internal_harmonic_mean"),
                        "ec": scores.get("external_ec"),
                        "rc": scores.get("intra_session_stability"),
                        "internal_responsiveness": scores.get("internal_responsiveness"),
                        "internal_consistency": scores.get("internal_consistency"),
                        "inter_session_stability": scores.get("inter_session_stability"),
                        "intra_session_stability": scores.get("intra_session_stability"),
                    }
                    job["status"] = "complete"
                    job["is_complete"] = True
                    # Result is kept private to the session. The frontend stores
                    # completed runs in per-browser localStorage so the user sees
                    # their own history without anyone else's. Public leaderboard
                    # submission requires an explicit POST to /api/leaderboard/submit.
                    logger.info("Agent eval complete for %s: %s", req.name, job["result"])
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
        if proc and proc.is_alive():
            proc.terminate()
        job["status"] = "cancelled"
        job["is_complete"] = True

    except Exception as e:
        logger.exception("Agent eval error for %s", req.name)
        if proc and proc.is_alive():
            proc.terminate()
        job["status"] = "error"
        job["is_complete"] = True
        job["error"] = str(e)

    finally:
        if proc:
            proc.join(timeout=5)
        _release_key(key_idx)
        sem.release()
        logger.info("Job %s released slot + key pool[%d] (slots: %d/%d used)", job_id,
                     key_idx, MAX_CONCURRENT_JOBS - sem._value, MAX_CONCURRENT_JOBS)

    _archive_job(job_id, {
        "session_id": job_id,
        "status": job["status"],
        "name": job["name"],
        "model": job.get("model", ""),
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
    """Return community leaderboard entries from SQLite."""
    def _fetch():
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT name, model, type, arch, turns, ic, ec, rc, timestamp "
                "FROM leaderboard ORDER BY id ASC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
    try:
        entries = await asyncio.to_thread(_fetch)
        return {"entries": entries}
    except Exception as e:
        logger.warning("get_leaderboard failed: %s", e)
        return {"entries": []}


def _add_to_leaderboard(name: str, model: str, scores: dict, turns: int = 50):
    """Add a completed evaluation to the community leaderboard in SQLite."""
    try:
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.execute(
                "INSERT INTO leaderboard (name, model, type, arch, turns, ic, ec, rc, timestamp) "
                "VALUES (?, ?, 'community', 'Community', ?, ?, ?, ?, ?)",
                (name, model, turns,
                 scores.get("ic") or 0, scores.get("ec") or 0, scores.get("rc") or 0,
                 time.time()),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("Failed to add leaderboard entry: %s", e)


@app.post("/api/leaderboard/submit")
async def submit_to_leaderboard(job_id: str):
    """
    Explicitly publish a completed agent job's result to the public leaderboard.

    Agent results are private to the session by default. The user can choose to
    publish by calling this endpoint with their own job_id after results are in.
    """
    job = agent_jobs.get(job_id)
    if not job:
        # Also look up from completed_jobs table (result archive)
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("is_complete") or job.get("status") != "complete":
        raise HTTPException(status_code=400, detail="Job is not in 'complete' state")
    result = job.get("result") or {}
    if not result:
        raise HTTPException(status_code=400, detail="No result to publish")
    name = job.get("name") or "Agent"
    model = job.get("model") or ""
    turns = job.get("total_turns") or 50
    await asyncio.to_thread(_add_to_leaderboard, name, model, result, turns)
    return {"status": "published", "name": name}


@app.delete("/api/leaderboard/latest")
async def delete_latest_leaderboard():
    """Remove the most recent community leaderboard entry."""
    def _pop():
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, model, type, arch, turns, ic, ec, rc, timestamp "
                "FROM leaderboard ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                conn.execute("DELETE FROM leaderboard WHERE id = ?", (row["id"],))
                conn.commit()
                conn.close()
                return dict(row)
            conn.close()
            return None
    try:
        removed = await asyncio.to_thread(_pop)
        if removed:
            return {"removed": removed}
        return {"removed": None, "message": "Leaderboard is empty"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# Usage summary
# ===========================================================================

@app.get("/api/usage")
async def get_usage():
    """Return daily cost totals broken down by day and by IP."""
    def _fetch_usage():
        with _sqlite_lock:
            conn = sqlite3.connect(SQLITE_PATH)
            # Daily global totals
            _daily = {}
            for row in conn.execute(
                "SELECT date, ROUND(SUM(cost), 5) FROM cost_log GROUP BY date ORDER BY date"
            ):
                _daily[row[0]] = row[1]
            # Daily by-IP totals
            _by_ip: Dict[str, Dict[str, float]] = {}
            for row in conn.execute(
                "SELECT ip, date, ROUND(SUM(cost), 5) FROM cost_log GROUP BY ip, date ORDER BY date"
            ):
                _by_ip.setdefault(row[0], {})[row[1]] = row[2]
            conn.close()
            return _daily, _by_ip

    try:
        daily_global, daily_by_ip = await asyncio.to_thread(_fetch_usage)
    except Exception as e:
        logger.warning("get_usage failed: %s", e)
        daily_global, daily_by_ip = {}, {}

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


@app.get("/api/admin/sessions")
async def admin_sessions():
    """Dump all in-memory experience sessions and agent jobs with their results."""
    exp = []
    for sid, s in sessions.items():
        exp.append({
            "session_id": sid,
            "name": s.get("name"),
            "status": s.get("status"),
            "turn_count": s.get("turn_count", 0),
            "result": s.get("result"),
            "error": s.get("error"),
        })
    agent = []
    for jid, j in agent_jobs.items():
        agent.append({
            "job_id": jid,
            "name": j.get("name"),
            "model": j.get("model"),
            "status": j.get("status"),
            "is_complete": j.get("is_complete"),
            "result": j.get("result"),
            "error": j.get("error"),
        })
    return {"experience_sessions": exp, "agent_jobs": agent}


# ===========================================================================
# Health
# ===========================================================================

@app.get("/api/queue")
async def queue_info():
    """Return current job queue status."""
    sem = _get_semaphore()
    running = MAX_CONCURRENT_JOBS - sem._value
    with _key_assignment_lock:
        key_usage = {f"key_{i}": count for i, count in _key_active_counts.items()}
    return {
        "max_concurrent": MAX_CONCURRENT_JOBS,
        "running": running,
        "queued": len(_queued_job_ids),
        "available_slots": sem._value,
        "key_pool_size": len(_openai_key_pool),
        "key_usage": key_usage,
    }


@app.post("/api/admin/cleanup")
async def admin_cleanup():
    """Force-cancel stale sessions/jobs and release their slots.

    Cancels: Experience sessions with no browser poll for EXPERIENCE_ABANDON_TIMEOUT,
    and any job older than JOB_HARD_TIMEOUT (the soft deadline is not grounds for
    forcible cancellation — the subprocess may still be delivering useful work).
    """
    cleaned = {"experience": [], "agent": []}
    now = time.time()

    # Clean stale experience sessions
    for sid, session in list(sessions.items()):
        if session.get("status") in ("complete", "error", "cancelled"):
            continue
        last_poll = session.get("last_poll_time", now)
        started = session.get("started_at", now)
        abandoned = now - last_poll > EXPERIENCE_ABANDON_TIMEOUT
        timed_out = now - started > JOB_HARD_TIMEOUT
        if abandoned or timed_out:
            task = session.get("task")
            if task and not task.done():
                task.cancel()
            proc = session.get("process")
            if proc and proc.is_alive():
                proc.terminate()
            session["status"] = "cancelled"
            session["question_queue"].put({"question": "__COMPLETE__", "turn": -1})
            cleaned["experience"].append(sid[:8])

    # Clean stale agent jobs
    for jid, job in list(agent_jobs.items()):
        if job.get("is_complete"):
            continue
        started = job.get("started_at", now)
        if now - started > JOB_HARD_TIMEOUT:
            task = job.get("task")
            if task and not task.done():
                task.cancel()
            proc = job.get("process")
            if proc and proc.is_alive():
                proc.terminate()
            job["status"] = "cancelled"
            job["is_complete"] = True
            cleaned["agent"].append(jid[:8])

    sem = _get_semaphore()
    return {
        "cleaned": cleaned,
        "slots_after": {"running": MAX_CONCURRENT_JOBS - sem._value, "available": sem._value},
    }


@app.get("/api/health")
async def health():
    picon_ok = False
    picon_err = None
    try:
        import picon
        picon_ok = True
    except Exception as e:
        picon_err = str(e)

    db_ok = False
    try:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        pass

    env_keys = {
        "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
        "OPENAI_API_KEY": bool(os.getenv("OPENAI_API_KEY")),
        "SERPER_API_KEY": bool(os.getenv("SERPER_API_KEY")),
        "BRIDGE_BASE_URL": os.getenv("BRIDGE_BASE_URL", "(not set)"),
    }

    return {
        "status": "ok",
        "sqlite": db_ok,
        "picon": picon_ok,
        "picon_error": picon_err,
        "env_keys": env_keys,
    }


# ===========================================================================
# Static file serving (for local / Docker testing — not used on Railway)
# ===========================================================================
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(_static_dir, "index.html"))

    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")
