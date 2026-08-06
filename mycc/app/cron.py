"""
Cron Scheduler — 定时任务独立线程 + 子 agent 隔离执行。

Architecture:
  cron_scheduler_loop (daemon, 1s poll)
    → cron_queue (thread-safe)
    → cron_queue_processor (daemon, 0.2s poll)
    → spawn_subagent(client, MODEL, job.prompt)
    → result printed to terminal, stored for later retrieval

Key: cron 触发时启动子 agent，不污染主对话的 session_history。
子 agent 有自己的 messages=[]，干净上下文。主 agent_loop 完全不受干扰。
"""

import json
import random
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

WORKDIR = Path.cwd()
DURABLE_PATH = WORKDIR / ".scheduled_tasks.json"


@dataclass
class CronJob:
    id: str
    cron: str        # 5-field: "0 9 * * *"
    prompt: str      # message injected to subagent when fired
    recurring: bool  # True = recurring, False = one-shot
    durable: bool    # True = persist to .scheduled_tasks.json


scheduled_jobs: dict[str, CronJob] = {}
cron_queue: list[CronJob] = []
cron_lock = threading.Lock()
_last_fired: dict[str, str] = {}  # job_id → "YYYY-MM-DD HH:MM"

# Set by init_cron()
_client = None
_MODEL = ""

# Store recent cron results for the user to check
cron_results: list[dict] = []  # [{job_id, prompt, summary, time}]
MAX_RESULTS = 50


# ── Cron Field Matching ───────────────────────────────────

def _cron_field_matches(field: str, value: int) -> bool:
    """Match a single cron field against a value.
    Supports: *, */N, N, N-M, N,M,..."""
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(f.strip(), value)
                   for f in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    """Check if a 5-field cron expression matches the given datetime.
    Standard cron semantics: DOM and DOW use OR when both constrained."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7  # Python Monday=0 → cron Sunday=0

    m = _cron_field_matches(minute, dt.minute)
    h = _cron_field_matches(hour, dt.hour)
    dom_ok = _cron_field_matches(dom, dt.day)
    month_ok = _cron_field_matches(month, dt.month)
    dow_ok = _cron_field_matches(dow, dow_val)

    if not (m and h and month_ok):
        return False
    dom_unconstrained = dom == "*"
    dow_unconstrained = dow == "*"
    if dom_unconstrained and dow_unconstrained:
        return True
    if dom_unconstrained:
        return dow_ok
    if dow_unconstrained:
        return dom_ok
    return dom_ok or dow_ok


# ── Validation ────────────────────────────────────────────

def _validate_cron_field(field: str, lo: int, hi: int):
    """Validate a single cron field. Returns error string or None."""
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit():
            return f"Invalid step: {field}"
        step = int(step_str)
        if step <= 0:
            return f"Step must be > 0: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        if a > b:
            return f"Range start > end: {field}"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str):
    """Validate a 5-field cron expression. Returns error string or None."""
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for i, (field, (lo, hi), name) in enumerate(zip(fields, bounds, names)):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


# ── Persistence ───────────────────────────────────────────

def save_durable_jobs():
    """Persist durable jobs to .scheduled_tasks.json."""
    durable = [asdict(j) for j in scheduled_jobs.values() if j.durable]
    DURABLE_PATH.write_text(json.dumps(durable, indent=2))


def load_durable_jobs():
    """Load durable jobs from disk on startup."""
    if not DURABLE_PATH.exists():
        return
    try:
        jobs = json.loads(DURABLE_PATH.read_text())
        for j in jobs:
            job = CronJob(**j)
            err = validate_cron(job.cron)
            if err:
                print(f"  \033[31m[cron] skip invalid {job.id}: {err}\033[0m")
                continue
            scheduled_jobs[job.id] = job
        valid = [j for j in jobs if j["id"] in scheduled_jobs]
        if valid:
            print(f"  \033[35m[cron] loaded {len(valid)} durable job(s)\033[0m")
    except Exception:
        pass


# ── Job Management ────────────────────────────────────────

def schedule_job(cron: str, prompt: str, recurring: bool = True,
                 durable: bool = True):
    """Register a new cron job. Returns CronJob or error string."""
    err = validate_cron(cron)
    if err:
        return err
    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        cron=cron, prompt=prompt,
        recurring=recurring, durable=durable,
    )
    with cron_lock:
        scheduled_jobs[job.id] = job
    if durable:
        save_durable_jobs()
    print(f"  \033[35m[cron register] {job.id} '{cron}' → {prompt[:40]}\033[0m")
    return job


def cancel_job(job_id: str) -> str:
    """Cancel a cron job by ID."""
    with cron_lock:
        job = scheduled_jobs.pop(job_id, None)
    if not job:
        return f"Job {job_id} not found"
    if job.durable:
        save_durable_jobs()
    print(f"  \033[31m[cron cancel] {job_id}\033[0m")
    return f"Cancelled {job_id}"


# ── Scheduler Thread ──────────────────────────────────────

def cron_scheduler_loop():
    """Independent daemon thread: poll every 1s, fire matching jobs.
    Individual job errors are caught — one bad job won't kill the scheduler."""
    while True:
        time.sleep(1)
        now = datetime.now()
        minute_marker = now.strftime("%Y-%m-%d %H:%M")
        with cron_lock:
            for job in list(scheduled_jobs.values()):
                try:
                    if cron_matches(job.cron, now):
                        if _last_fired.get(job.id) != minute_marker:
                            cron_queue.append(job)
                            _last_fired[job.id] = minute_marker
                            print(f"  \033[35m[cron fire] {job.id} → "
                                  f"{job.prompt[:50]}\033[0m")
                        if not job.recurring:
                            scheduled_jobs.pop(job.id, None)
                            if job.durable:
                                save_durable_jobs()
                except Exception as e:
                    print(f"  \033[31m[cron error] {job.id}: {e}\033[0m")


# ── Queue Processor (Plan A: subagent isolation) ──────────

def _run_cron_subagent(job: CronJob):
    """Execute a cron job in a subagent (clean context, no main-session pollution)."""
    # Lazy import to avoid circular dependency at module level
    from subagent import spawn_subagent

    prompt = (
        f"[Cron job: {job.id}]\n"
        f"{job.prompt}\n\n"
        f"(This is a scheduled task. Complete it and return a summary.)"
    )
    print(f"\n  \033[35m[cron:subagent] {job.prompt[:60]}\033[0m")
    try:
        result = spawn_subagent(_client, _MODEL, prompt)
    except Exception as e:
        result = f"Subagent failed: {e}"

    summary = result[:300] if len(result) > 300 else result
    print(f"  \033[35m[cron:done] {job.id}: {summary}\033[0m")

    # Store result for later retrieval
    cron_results.append({
        "job_id": job.id,
        "prompt": job.prompt,
        "summary": result,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    if len(cron_results) > MAX_RESULTS:
        cron_results.pop(0)


def cron_queue_processor():
    """Poll the queue and spawn subagents for fired cron jobs.
    Each subagent runs in its own thread — non-blocking to the queue."""
    while True:
        time.sleep(0.2)
        job = None
        with cron_lock:
            if cron_queue:
                job = cron_queue.pop(0)
        if job:
            t = threading.Thread(target=_run_cron_subagent, args=(job,),
                                 daemon=True)
            t.start()


# ── Init ──────────────────────────────────────────────────

def init_cron(client, model: str):
    """Start cron scheduler and queue processor threads.
    Called once from agent_loop after client is created."""
    global _client, _MODEL
    _client = client
    _MODEL = model
    load_durable_jobs()
    threading.Thread(target=cron_scheduler_loop, daemon=True).start()
    threading.Thread(target=cron_queue_processor, daemon=True).start()
    print("  \033[35m[cron] scheduler started\033[0m")


# ── Tool Implementations ──────────────────────────────────

def run_schedule_cron(cron: str, prompt: str,
                      recurring: bool = True, durable: bool = True) -> str:
    """Tool: schedule a new cron job."""
    result = schedule_job(cron, prompt, recurring, durable)
    if isinstance(result, str):
        return f"Error: {result}"
    tag = "recurring" if result.recurring else "one-shot"
    store = "durable" if result.durable else "session-only"
    return (f"Scheduled {result.id}: '{result.cron}' → {result.prompt} "
            f"[{tag}, {store}]")


def run_list_crons() -> str:
    """Tool: list all registered cron jobs."""
    with cron_lock:
        jobs = list(scheduled_jobs.values())
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        tag = "recurring" if j.recurring else "one-shot"
        store = "durable" if j.durable else "session"
        lines.append(f"  {j.id}: '{j.cron}' → {j.prompt[:50]} "
                     f"[{tag}, {store}]")
    return "\n".join(lines)


def run_cancel_cron(job_id: str) -> str:
    """Tool: cancel a cron job by ID."""
    return cancel_job(job_id)


def run_cron_results() -> str:
    """Tool: show recent cron execution results."""
    if not cron_results:
        return "No cron results yet."
    lines = []
    for r in cron_results[-20:]:
        summary = r["summary"][:120]
        lines.append(f"  [{r['time']}] {r['job_id']}: {summary}")
    return "\n".join(lines)
