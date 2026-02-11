from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path
import subprocess
import json
import re
from typing import Dict, Optional
import tempfile
import time
import uuid
import os
from collections import deque

# ---- Prometheus imports ----
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = FastAPI(title="Platform API")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

INVENTORY_FILE = BASE_DIR / "ansible" / "inventory.yml"

DEFAULT_TARGETS = ["18.144.29.14", "50.18.130.21"]

GRAFANA_URL = "http://ac3183f05b9224744bea7533000a4006-1765183457.us-west-1.elb.amazonaws.com/login"

ACTIONS: Dict[str, Dict[str, str]] = {
    "ec2_ping": {
        "label": "Ping EC2 targets",
        "description": "Runs Ansible ping against EC2 targets.",
        "playbook": str(BASE_DIR / "ansible" / "ping.yml"),
        "limit": "platformapi",
    },
    "httpd_restart": {
        "label": "Restart HTTPD on EC2 targets",
        "description": "Restart HTTPD service on EC2 targets.",
        "playbook": str(BASE_DIR / "ansible" / "httpdrestart.yml"),
        "limit": "platformapi",
    },
    "zos_ping": {
        "label": "Ping z/OS host",
        "description": "Ping mainframe host.",
        "playbook": str(BASE_DIR / "ansible" / "mainframe" / "mainframe_ping.yaml"),
        "limit": "mainframe",
    },
    "zos_cpu_jobs": {
        "label": "Show z/OS CPU jobs",
        "description": "Display address space CPU usage.",
        "playbook": str(BASE_DIR / "ansible" / "mainframe" / "cpujobs.yaml"),
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

        # Prevent high-cardinality labels
        if path.startswith("/api/run/"):
            path = "/api/run/{action}"
        elif path.startswith("/api/runs/"):
            path = "/api/runs/{id}"

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

def is_valid_ipv4(addr: str) -> bool:
    try:
        parts = addr.split(".")
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)
    except Exception:
        return False


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

# ============================================================
# --------------------- CORE ROUTES --------------------------
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

# ============================================================
# ------------------- ACTION EXECUTION -----------------------
# ============================================================

def _run_ansible_playbook(*, playbook: str, limit: Optional[str] = None):
    if not INVENTORY_FILE.exists():
        return {"success": False, "error": "Inventory not found"}

    cmd = ["ansible-playbook", "-i", str(INVENTORY_FILE), playbook]
    if limit:
        cmd.extend(["--limit", limit])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "cmd": " ".join(cmd),
            "play_summary": parse_play_recap(proc.stdout),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/run/{action}")
async def api_run_action(action: str):
    meta = ACTIONS.get(action)
    if not meta:
        raise HTTPException(status_code=404, detail="unknown action")

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
        **result,
    }

    RUNS.appendleft(run_record)

    return run_record


@app.get("/api/actions")
async def api_actions():
    return {
        "grafana_url": GRAFANA_URL,
        "actions": [
            {"name": n, "label": m["label"], "description": m["description"]}
            for n, m in ACTIONS.items()
        ],
    }


@app.get("/api/runs")
async def api_runs():
    return {"runs": list(RUNS)}
