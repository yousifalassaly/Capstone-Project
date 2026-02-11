from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path
import subprocess
import re
from typing import Dict, Optional
import time
import uuid
import os
from collections import deque

# ---- Prometheus ----
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---- Scheduler ----
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

# ============================================================
# ------------------ APP INIT -------------------------------
# ============================================================

app = FastAPI(title="Platform API")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

INVENTORY_FILE = BASE_DIR / "ansible" / "inventory.yml"

# Phase 1 UI: fixed set of allowed actions (no free-form command execution)
GRAFANA_URL = "http://ac3183f05b9224744bea7533000a4006-1765183457.us-west-1.elb.amazonaws.com/login"  

ACTIONS: Dict[str, Dict[str, str]] = {
    "ec2_ping": {
        "label": "Ping EC2 targets",
        "description": "Runs Ansible ping against the EC2 targets in inventory.yml (group: platformapi).",
        "playbook": str(BASE_DIR / "ansible" / "ping.yml"),
        "limit": "platformapi",
    },
    "httpd_restart": {
        "label": "Restart HTTPD on EC2 targets",
        "description": "Runs Ansible playbook to restart HTTPD service on the EC2 targets in inventory.yml (group: platformapi).",
        "playbook": str(BASE_DIR / "ansible" / "httpdrestart.yml"),
        "limit": "platformapi",
    },
    "zos_ping": {
        "label": "Ping z/OS host",
        "description": "Runs IBM z/OS ping playbook against the mainframe host.",
        "playbook": str(BASE_DIR / "ansible" / "mainframe" / "mainframe_ping.yaml"),
        "limit": "mainframe",
    },
    "zos_cpu_jobs": {
        "label": "Show z/OS CPU jobs",
        "description": "Runs a z/OS operator command playbook to display address space CPU usage.",
        "playbook": str(BASE_DIR / "ansible" / "mainframe" / "cpujobs.yaml"),
        "limit": "zos",
    },
    "zos_submit_jcl": {
        "label": "Submit JCL to z/OS",
        "description": "Runs a z/OS playbook to submit JCL.",
        "playbook": str(BASE_DIR / "ansible" / "mainframe" / "submit_jcl.yaml"),
        "limit": "zos",
    },
}

RUNS: "deque[Dict]" = deque(maxlen=50)

# ============================================================
# ----------------- PROMETHEUS METRICS -----------------------
# ============================================================

HTTP_REQUESTS_TOTAL = Counter(
    "platformapi_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

HTTP_REQUEST_DURATION = Histogram(
    "platformapi_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)

ACTION_RUNS_TOTAL = Counter(
    "platformapi_action_runs_total",
    "Total action runs",
    ["action", "status"],
)

ACTION_RUN_DURATION = Histogram(
    "platformapi_action_run_duration_seconds",
    "Duration of action runs",
    ["action"],
)

IN_PROGRESS_RUNS = Gauge(
    "platformapi_action_runs_in_progress",
    "Number of action runs currently executing",
)

ZOS_UP = Gauge(
    "platformapi_zos_up",
    "Whether the z/OS host is reachable (1 = up, 0 = down)",
)


# ============================================================
# ---------------- HTTP METRICS MIDDLEWARE -------------------
# ============================================================

@app.middleware("http")
async def prometheus_http_middleware(request: Request, call_next):
    start_time = time.time()
    response = None

    try:
        response = await call_next(request)
        return response
    finally:
        duration = time.time() - start_time
        path = request.url.path

        if path.startswith("/api/run/"):
            path = "/api/run/{action}"
        elif path.startswith("/api/schedule/"):
            path = "/api/schedule/{action}"

        status_code = response.status_code if response else 500

        HTTP_REQUESTS_TOTAL.labels(
            method=request.method,
            path=path,
            status=status_code,
        ).inc()

        HTTP_REQUEST_DURATION.labels(
            method=request.method,
            path=path,
        ).observe(duration)

# ============================================================
# --------------------- UTILITIES ----------------------------
# ============================================================

def parse_play_recap(output: str):
    summary = {}
    if not output:
        return summary

    m = re.search(r"PLAY RECAP \*+\n(.*?)(?:\n\n|\Z)", output, re.DOTALL)
    if not m:
        return summary

    for line in m.group(1).strip().splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        host = parts[0].strip()
        stats = {
            kv.group(1): int(kv.group(2))
            for kv in re.finditer(r"(\w+)=([0-9]+)", parts[1])
        }
        summary[host] = stats

    return summary


def _run_ansible_playbook(*, playbook: str, limit: Optional[str] = None) -> Dict:
    if not INVENTORY_FILE.exists():
        return {"success": False, "error": f"Inventory not found at {INVENTORY_FILE}"}

    cmd = ["ansible-playbook", "-i", str(INVENTORY_FILE), playbook]
    if limit:
        cmd.extend(["--limit", limit])

    # If the playbook lives under ansible/mainframe, tell Ansible to use that config.
    # We use the container path /app/ansible/mainframe/ansible.cfg as requested.
    env = None
    try:
        pb_path = Path(playbook)
        mainframe_dir = BASE_DIR / "ansible" / "mainframe"
        # Use string containment to avoid resolve() failures on missing files
        if str(mainframe_dir) in str(pb_path.parent):
            env = os.environ.copy()
            env["ANSIBLE_CONFIG"] = "/app/ansible/mainframe/ansible.cfg"
    except Exception:
        # If anything goes wrong detecting, continue without special env.
        env = None

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cmd": " ".join(cmd),
            "play_summary": parse_play_recap(proc.stdout),
        }
    except FileNotFoundError:
        return {"success": False, "error": "ansible-playbook binary not found in PATH."}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Ansible command timed out"}





# ============================================================
# ------------------ ACTION EXECUTION ------------------------
# ============================================================

def execute_action(action: str, scheduled: bool = False):
    meta = ACTIONS.get(action)
    if not meta:
        return None

    run_id = str(uuid.uuid4())
    started = time.time()

    IN_PROGRESS_RUNS.inc()

    result = _run_ansible_playbook(
        playbook=meta["playbook"],
        limit=meta.get("limit"),
    )

    finished = time.time()
    duration = finished - started
    success = bool(result.get("success"))

    # Set availability for zos_ping
    if action == "zos_ping":
        ZOS_UP.set(1 if success else 0)

    ACTION_RUN_DURATION.labels(action=action).observe(duration)
    ACTION_RUNS_TOTAL.labels(
        action=action,
        status="success" if success else "failed",
    ).inc()

    IN_PROGRESS_RUNS.dec()

    run_record = {
        "id": run_id,
        "action": action,
        "status": "success" if success else "failed",
        "duration_s": round(duration, 3),
        "started_at": started,
        "scheduled": scheduled,
        **result,
    }

    RUNS.appendleft(run_record)
    return run_record

# ============================================================
# --------------------- SCHEDULER ----------------------------
# ============================================================

scheduler = BackgroundScheduler()

@app.on_event("startup")
def start_scheduler():
    scheduler.start()

    # Default: zos_ping every 10 minutes
    scheduler.add_job(
        func=execute_action,
        trigger=IntervalTrigger(minutes=10),
        id="zos_ping_interval",
        replace_existing=True,
        args=["zos_ping", True],
        max_instances=1,
    )

@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler.shutdown()

# ============================================================
# ------------------------ ROUTES ----------------------------
# ============================================================

@app.get("/")
async def root(request: Request):
    return RedirectResponse(url=request.url_for("app_home"), status_code=302)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/app", name="app_home", response_class=HTMLResponse)
async def app_home(request: Request):
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Platform API",
            "grafana_url": GRAFANA_URL,
            "actions": [
                {"name": n, "label": m["label"], "description": m["description"]}
                for n, m in ACTIONS.items()
            ],
        },
    )

@app.post("/api/run/{action}")
async def api_run_action(action: str):
    result = execute_action(action, scheduled=False)
    if not result:
        raise HTTPException(status_code=404, detail="unknown action")
    return result

@app.get("/api/runs")
async def api_runs():
    return {"runs": list(RUNS)}

@app.get("/api/schedules")
async def list_schedules():
    return {
        "jobs": [
            {
                "id": job.id,
                "next_run_time": str(job.next_run_time),
            }
            for job in scheduler.get_jobs()
        ]
    }

@app.post("/api/schedule/{action}")
async def set_schedule(action: str, minutes: int):
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="unknown action")

    job_id = f"{action}_interval"

    scheduler.add_job(
        func=execute_action,
        trigger=IntervalTrigger(minutes=minutes),
        id=job_id,
        replace_existing=True,
        args=[action, True],
        max_instances=1,
    )

    return {"status": "scheduled", "action": action, "minutes": minutes}

@app.delete("/api/schedule/{action}")
async def remove_schedule(action: str):
    job_id = f"{action}_interval"
    scheduler.remove_job(job_id)
    return {"status": "removed", "action": action}
