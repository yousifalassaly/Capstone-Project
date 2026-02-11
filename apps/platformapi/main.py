from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from pathlib import Path
import subprocess
import json
import re
from typing import Dict, List, Optional
import tempfile
import time
import uuid
import os
from collections import deque

app = FastAPI(title="Platform API")
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


INVENTORY_FILE = BASE_DIR / "ansible" / "inventory.yml"

# Default target hosts (can be overridden by /ansible/ping JSON body)
DEFAULT_TARGETS = ["18.144.29.14", "50.18.130.21"]

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
}

# In-memory run history for the demo (most recent first)
RUNS: "deque[Dict]" = deque(maxlen=50)


def is_valid_ipv4(addr: str) -> bool:
    if not isinstance(addr, str):
        return False
    parts = addr.split(".")
    if len(parts) != 4:
        return False
    try:
        for p in parts:
            i = int(p)
            if i < 0 or i > 255:
                return False
    except ValueError:
        return False
    return True


def parse_play_recap(output: str):
    """Parse the PLAY RECAP section from ansible-playbook stdout into a dict.

    Returns { host: {ok:int, changed:int, unreachable:int, failed:int, skipped:int, rescued:int, ignored:int}, ... }
    """
    summary = {}
    if not output:
        return summary
    m = re.search(r"PLAY RECAP \*+\n(.*?)(?:\n\n|\Z)", output, re.DOTALL)
    if not m:
        return summary
    body = m.group(1).strip()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        host = parts[0].strip()
        kvs = parts[1]
        stats = {}
        for kv in re.finditer(r"(\w+)=([0-9]+)", kvs):
            stats[kv.group(1)] = int(kv.group(2))
        summary[host] = stats
    return summary


@app.get("/", response_class=HTMLResponse)
async def get_login(request: Request) -> HTMLResponse:
    # Phase 1: no auth gating. Route straight to dashboard.
    return RedirectResponse(url=request.url_for("app_home"), status_code=302)


@app.post("/login", name="login", response_class=HTMLResponse)
async def do_login(request: Request, username: str = Form(...), password: str = Form(...)) -> HTMLResponse:
    # Kept for later phases (JWT). Not used in Phase 1.
    if username == "admin" and password == "admin":
        return RedirectResponse(url=request.url_for("app_home"), status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid credentials", "message": None, "url_for": request.url_for},
        status_code=401,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/app", name="app_home", response_class=HTMLResponse)
async def app_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Platform API",
            "health_url": request.url_for("health"),
            "endpoints": ["/", "/health", "/api/actions", "/api/runs", "/api/run/{action}"],
            "grafana_url": GRAFANA_URL,
            "actions": [
                {"name": name, "label": meta["label"], "description": meta["description"]}
                for name, meta in ACTIONS.items()
            ],
        },
    )


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


@app.get("/api/actions")
async def api_actions() -> Dict:
    return {
        "grafana_url": GRAFANA_URL,
        "actions": [{"name": n, "label": m["label"], "description": m["description"]} for n, m in ACTIONS.items()],
    }


@app.get("/api/runs")
async def api_runs() -> Dict:
    return {"runs": list(RUNS)}


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str) -> Dict:
    for r in RUNS:
        if r.get("id") == run_id:
            return r
    raise HTTPException(status_code=404, detail="run not found")


@app.post("/api/run/{action}")
async def api_run_action(action: str) -> Dict:
    meta = ACTIONS.get(action)
    if not meta:
        raise HTTPException(status_code=404, detail="unknown action")

    run_id = str(uuid.uuid4())
    started = time.time()

    run_record: Dict = {
        "id": run_id,
        "action": action,
        "label": meta["label"],
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "duration_s": None,
        "returncode": None,
        "success": None,
        "play_summary": None,
        "stdout": "",
        "stderr": "",
        "cmd": "",
        "error": None,
    }
    RUNS.appendleft(run_record)

    result = _run_ansible_playbook(playbook=meta["playbook"], limit=meta.get("limit"))

    finished = time.time()
    run_record.update(
        {
            "status": "success" if result.get("success") else "failed",
            "finished_at": finished,
            "duration_s": round(finished - started, 3),
            "returncode": result.get("returncode"),
            "success": result.get("success"),
            "play_summary": result.get("play_summary"),
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "cmd": result.get("cmd", ""),
            "error": result.get("error"),
        }
    )

    return run_record


@app.post("/ansible/ping")
async def ansible_ping(request: Request):
    """Legacy endpoint retained (used earlier)."""
    hosts = DEFAULT_TARGETS
    try:
        payload = await request.json()
    except Exception:
        payload = None

    if payload and isinstance(payload, dict) and "hosts" in payload:
        candidate = payload.get("hosts")
        if isinstance(candidate, list) and all(isinstance(h, str) for h in candidate):
            hosts = candidate

    valid_hosts = [h for h in hosts if is_valid_ipv4(h)]
    if not valid_hosts:
        return {"success": False, "error": "No valid IPv4 hosts provided"}

    playbook_path = BASE_DIR / "ansible" / "ping.yml"
    inventory_file = BASE_DIR / "ansible" / "inventory.yml"
    if not playbook_path.exists():
        return {"success": False, "error": f"Playbook not found at {playbook_path}"}

    tmp_inv = None
    try:
        if payload and isinstance(payload, dict) and "hosts" in payload:
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
                tmp_inv = f.name
                for h in valid_hosts:
                    f.write(h + "\n")
            inv_path = tmp_inv
        else:
            if not inventory_file.exists():
                return {"success": False, "error": f"Inventory not found at {inventory_file}"}
            inv_path = str(inventory_file)

        cmd = [
            "ansible-playbook",
            "-i",
            str(inv_path),
            str(playbook_path),
            "--user",
            "ec2-user",
            "--private-key",
            "/home/app/.ssh/id_rsa",
            "--ssh-extra-args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
    finally:
        if tmp_inv:
            try:
                Path(tmp_inv).unlink()
            except Exception:
                pass
