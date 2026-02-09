"""
FastAPI-based operations orchestrator for the capstone project.

This service exposes a minimal API to dispatch operational workflows using GitHub
Actions. It accepts three high-level actions: running the batch job, rerunning
it, and applying a corrective fix prior to a rerun. The orchestration logic
relies on environment variables to locate the target repository, select the
workflow, and authenticate with the GitHub API.
"""

from fastapi import FastAPI, HTTPException
import os
import requests

app = FastAPI(title="Ops Orchestrator")

# Read configuration from environment variables. These must be set in the
# Kubernetes deployment via the deployment YAML (see apps/orchestrator/deployment.yaml).
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY")
WORKFLOW_ID = os.getenv("WORKFLOW_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def trigger_workflow(action: str) -> None:
    """
    Dispatch a GitHub Actions workflow with the given action input.

    The repository, workflow ID, and token are derived from environment variables.
    Raises an HTTPException if the request fails.
    """
    if not GITHUB_REPOSITORY or not WORKFLOW_ID or not GITHUB_TOKEN:
        raise RuntimeError("Required environment variables are not set")

    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/"
        f"workflows/{WORKFLOW_ID}/dispatches"
    )
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    data = {"ref": "main", "inputs": {"action": action}}
    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to dispatch workflow: {resp.text}",
        )


@app.post("/batch/run")
def batch_run() -> dict[str, str]:
    """Trigger a fresh batch job run."""
    trigger_workflow("run")
    return {"status": "batch run started"}


@app.post("/batch/rerun")
def batch_rerun() -> dict[str, str]:
    """Trigger a rerun of the previous batch job."""
    trigger_workflow("rerun")
    return {"status": "batch rerun started"}


@app.post("/fix/apply")
def fix_apply() -> dict[str, str]:
    """Trigger a corrective fix before rerunning the batch job."""
    trigger_workflow("fix")
    return {"status": "fix applied"}
