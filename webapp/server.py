from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ollama
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qa_agent import suites as suites_module
from qa_agent.config import RunConfig
from qa_agent.diff import diff_reports
from qa_agent.executor import EXECUTOR_SYSTEM_PROMPT
from qa_agent.models import TestCase
from qa_agent.orchestrator import ReviewGate, make_run_id, run_agent
from qa_agent.planner import PLANNER_SYSTEM_PROMPT

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
REPORTS_DIR = BASE_DIR / RunConfig.output_dir
SUITES_DIR = BASE_DIR / suites_module.SUITES_DIR
# The slug suffix is optional so runs from before it was added still match.
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}(__[a-z0-9-]{1,50})?$")
SUITE_ID_RE = re.compile(r"^[a-z0-9-]{1,50}__\d{8}-\d{6}$")
_RUN_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")

app = FastAPI(title="Autonomous QA Agent")


ACTIVE_STATUSES = ("running", "stopping", "awaiting_review")


@dataclass
class RunHandle:
    queue: "queue.Queue[dict]" = field(default_factory=queue.Queue)
    status: str = "running"
    cancel_event: threading.Event = field(default_factory=threading.Event)
    review_gate: ReviewGate | None = None


RUNS: dict[str, RunHandle] = {}


CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]

# In-memory only, per hosted provider ("claude"/"openai") - never written to
# disk, never included in any report or API response. Lets the web UI cache
# a key typed once for the rest of this server process's life without ever
# putting it in RunConfig/report.json (which would leak it into reports.md
# on disk and back to the browser via rerunPastRun's config reuse).
SESSION_API_KEYS: dict[str, str] = {}
_ENV_VAR_BY_PROVIDER = {"claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "openai": ("OPENAI_API_KEY",)}

# Same in-memory-only rationale as SESSION_API_KEYS. Only relevant for Claude:
# an organization-level API key that isn't scoped to one workspace makes the
# API reject every request with a 400 unless it carries an
# anthropic-workspace-id header naming which workspace to use.
SESSION_CLAUDE_WORKSPACE_ID: dict[str, str] = {}


class RunRequest(BaseModel):
    url: str
    provider: str = RunConfig.provider
    model: str = RunConfig.model
    ollama_host: str = RunConfig.ollama_host
    openai_base_url: str = RunConfig.openai_base_url
    api_key: str = ""
    workspace_id: str = ""
    headless: bool = RunConfig.headless
    channel: str = RunConfig.channel
    max_cases: int = RunConfig.max_cases
    max_steps: int = RunConfig.max_steps_per_case
    num_ctx: int = RunConfig.num_ctx
    auto_confirm_destructive: bool = RunConfig.auto_confirm_destructive
    focus_hint: str = ""
    precondition_goal: str = ""
    planner_system_prompt: str = ""
    executor_system_prompt: str = ""
    fresh_browser_per_case: bool = False
    review: bool = False
    suite_id: str = ""


class CasePayload(BaseModel):
    id: str | None = None
    title: str
    goal: str
    steps: list[str] = []
    reset_before: bool = True


class ApproveRequest(BaseModel):
    cases: list[CasePayload]


def _cases_from_payload(raw_cases: list[CasePayload]) -> list[TestCase]:
    cases = []
    for i, item in enumerate(raw_cases):
        title = item.title.strip()
        goal = item.goal.strip()
        if not title or not goal:
            continue
        cid = (item.id or "").strip() or f"tc{i + 1}"
        steps = [s.strip() for s in item.steps if s.strip()]
        cases.append(TestCase(id=cid, title=title, goal=goal, steps=steps, reset_before=item.reset_before))
    return cases


def _execute(
    cfg: RunConfig,
    run_id: str,
    handle: RunHandle,
    predefined_cases: list[TestCase] | None,
    api_key: str = "",
    workspace_id: str = "",
) -> None:
    def on_event(event: dict) -> None:
        # status must be updated before the event is queued, otherwise a fast
        # client could see the event (e.g. via SSE) and call /approve before
        # the status flip below has happened, racing the guard in approve_run.
        if event.get("type") == "awaiting_review":
            handle.status = "awaiting_review"
        handle.queue.put(event)

    try:
        run_agent(
            cfg,
            on_event=on_event,
            run_id=run_id,
            cancel_event=handle.cancel_event,
            review_gate=handle.review_gate,
            predefined_cases=predefined_cases,
            api_key=api_key,
            workspace_id=workspace_id,
        )
        handle.status = "cancelled" if handle.cancel_event.is_set() else "done"
    except Exception as exc:
        handle.queue.put({"type": "error", "message": str(exc)})
        handle.status = "error"


@app.post("/api/run")
def start_run(req: RunRequest) -> dict:
    predefined_cases = None
    if req.suite_id:
        try:
            suite = suites_module.load_suite(req.suite_id, suites_dir=str(SUITES_DIR))
        except OSError:
            raise HTTPException(status_code=404, detail="Unknown suite_id")
        predefined_cases = suite.cases

    run_id = make_run_id(req.url)
    cfg = RunConfig(
        url=req.url,
        provider=req.provider,
        model=req.model,
        openai_base_url=req.openai_base_url,
        ollama_host=req.ollama_host,
        headless=req.headless,
        channel=req.channel,
        max_cases=req.max_cases,
        max_steps_per_case=req.max_steps,
        num_ctx=req.num_ctx,
        auto_confirm_destructive=req.auto_confirm_destructive,
        focus_hint=req.focus_hint,
        precondition_goal=req.precondition_goal,
        planner_system_prompt=req.planner_system_prompt,
        executor_system_prompt=req.executor_system_prompt,
        fresh_browser_per_case=req.fresh_browser_per_case,
        suite_id=req.suite_id,
        output_dir=str(REPORTS_DIR),
    )

    submitted_key = req.api_key.strip()
    if submitted_key:
        SESSION_API_KEYS[req.provider] = submitted_key
    resolved_key = submitted_key or SESSION_API_KEYS.get(req.provider, "")

    submitted_workspace_id = req.workspace_id.strip()
    if submitted_workspace_id:
        SESSION_CLAUDE_WORKSPACE_ID["id"] = submitted_workspace_id
    resolved_workspace_id = submitted_workspace_id or SESSION_CLAUDE_WORKSPACE_ID.get("id", "")

    handle = RunHandle(review_gate=ReviewGate() if req.review else None)
    RUNS[run_id] = handle
    thread = threading.Thread(
        target=_execute,
        args=(cfg, run_id, handle, predefined_cases, resolved_key, resolved_workspace_id),
        daemon=True,
    )
    thread.start()
    return {"run_id": run_id}


@app.get("/api/credentials")
def get_credentials_status() -> dict:
    """Whether an API key is currently available per hosted provider - never
    the key itself. `session_set` reflects SESSION_API_KEYS (this server
    process's memory only); `env_set` reflects the process's own environment,
    which the SDKs fall back to when no session key is set.
    """
    status = {
        provider: {
            "session_set": bool(SESSION_API_KEYS.get(provider)),
            "env_set": any(os.environ.get(var) for var in env_vars),
        }
        for provider, env_vars in _ENV_VAR_BY_PROVIDER.items()
    }
    status["claude"]["workspace_id_set"] = bool(SESSION_CLAUDE_WORKSPACE_ID.get("id"))
    return status


@app.delete("/api/credentials/{provider}")
def clear_credentials(provider: str) -> dict:
    if provider not in _ENV_VAR_BY_PROVIDER:
        raise HTTPException(status_code=400, detail="Unknown provider")
    SESSION_API_KEYS.pop(provider, None)
    if provider == "claude":
        SESSION_CLAUDE_WORKSPACE_ID.pop("id", None)
    return {"cleared": provider}


@app.post("/api/run/{run_id}/approve")
def approve_run(run_id: str, req: ApproveRequest) -> dict:
    handle = RUNS.get(run_id)
    if handle is None or handle.review_gate is None:
        raise HTTPException(status_code=404, detail="Unknown run_id, or run has no pending review")
    if handle.status != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"Run is not awaiting review (status={handle.status})")
    cases = _cases_from_payload(req.cases)
    handle.review_gate.approved_cases = cases
    handle.status = "running"
    handle.review_gate.approval_event.set()
    return {"status": "running", "cases": [asdict(c) for c in cases]}


@app.post("/api/run/{run_id}/stop")
def stop_run(run_id: str) -> dict:
    handle = RUNS.get(run_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")
    if handle.status not in ACTIVE_STATUSES:
        return {"status": handle.status, "message": "run is not active"}
    handle.cancel_event.set()
    handle.status = "stopping"
    return {"status": "stopping"}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run_id")
    handle = RUNS.get(run_id)
    if handle is not None and handle.status in ACTIVE_STATUSES:
        raise HTTPException(status_code=409, detail="Run is still active; stop it first")
    run_dir = REPORTS_DIR / run_id
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="Unknown run")
    shutil.rmtree(run_dir)
    RUNS.pop(run_id, None)
    return {"deleted": run_id}


@app.get("/api/run/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    handle = RUNS.get(run_id)
    if handle is None:
        raise HTTPException(status_code=404, detail="Unknown run_id")

    async def gen():
        while True:
            event = await asyncio.to_thread(handle.queue.get)
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event.get("type") in ("done", "error"):
                break

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/prompts")
def get_default_prompts() -> dict:
    return {"planner": PLANNER_SYSTEM_PROMPT, "executor": EXECUTOR_SYSTEM_PROMPT}


@app.get("/api/models")
def list_models(provider: str = "ollama") -> dict:
    if provider == "claude":
        return {"models": CLAUDE_MODELS, "default": "claude-opus-5"}
    if provider == "openai":
        # No curated list: model catalogs vary per OpenAI-compatible endpoint
        # (real OpenAI, Azure, OpenRouter, Groq, local servers, ...), so the
        # UI takes free-text model input for this provider instead.
        return {"models": [], "default": ""}
    try:
        resp = ollama.Client(host=RunConfig.ollama_host).list()
        names = [m["model"] for m in resp.get("models", [])]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Ollama: {exc}")
    return {"models": names, "default": RunConfig.model}


def _load_report(entry: Path) -> dict | None:
    report_json = entry / "report.json"
    if not report_json.is_file():
        return None
    try:
        with open(report_json, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _run_summary(entry: Path, data: dict) -> dict:
    status_counts: dict[str, int] = {}
    for r in data.get("results", []):
        status = r.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    created_at = None
    m = _RUN_TS_RE.match(entry.name)
    if m:
        created_at = f"{m.group(1)}-{m.group(2)}-{m.group(3)}T{m.group(4)}:{m.group(5)}:{m.group(6)}"
    return {
        "run_id": entry.name,
        "json_url": f"/reports/{entry.name}/report.json",
        "url": data.get("url"),
        "created_at": created_at,
        "case_count": len(data.get("results", [])),
        "status_counts": status_counts,
    }


@app.get("/api/runs")
def list_runs() -> dict:
    runs = []
    if REPORTS_DIR.is_dir():
        for entry in sorted(REPORTS_DIR.iterdir(), reverse=True):
            data = _load_report(entry)
            if data is not None:
                runs.append(_run_summary(entry, data))
    return {"runs": runs}


@app.get("/api/runs/diff")
def get_runs_diff(a: str, b: str) -> dict:
    for run_id in (a, b):
        if not RUN_ID_RE.match(run_id):
            raise HTTPException(status_code=400, detail=f"Invalid run_id: {run_id}")
    path_a = REPORTS_DIR / a / "report.json"
    path_b = REPORTS_DIR / b / "report.json"
    for run_id, path in ((a, path_a), (b, path_b)):
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    with open(path_a, "r", encoding="utf-8") as f:
        report_a = json.load(f)
    with open(path_b, "r", encoding="utf-8") as f:
        report_b = json.load(f)
    result = diff_reports(report_a, report_b)
    result["a_run_id"] = a
    result["b_run_id"] = b
    return result


class SaveSuiteRequest(BaseModel):
    name: str
    url: str
    cases: list[CasePayload]


@app.post("/api/suites")
def create_suite(req: SaveSuiteRequest) -> dict:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Suite name is required")
    cases = _cases_from_payload(req.cases)
    if not cases:
        raise HTTPException(status_code=400, detail="Cannot save a suite with no cases")
    suite = suites_module.save_suite(name, req.url, cases, suites_dir=str(SUITES_DIR))
    return {"id": suite.id, "name": suite.name, "url": suite.url, "case_count": len(suite.cases)}


@app.put("/api/suites/{suite_id}")
def update_suite(suite_id: str, req: SaveSuiteRequest) -> dict:
    if not SUITE_ID_RE.match(suite_id):
        raise HTTPException(status_code=400, detail="Invalid suite_id")
    path = SUITES_DIR / f"{suite_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown suite")
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Suite name is required")
    cases = _cases_from_payload(req.cases)
    if not cases:
        raise HTTPException(status_code=400, detail="Cannot save a suite with no cases")
    suite = suites_module.update_suite(suite_id, name, req.url, cases, suites_dir=str(SUITES_DIR))
    return {"id": suite.id, "name": suite.name, "url": suite.url, "case_count": len(suite.cases)}


@app.get("/api/suites")
def get_suites() -> dict:
    return {"suites": suites_module.list_suites(suites_dir=str(SUITES_DIR))}


@app.get("/api/suites/{suite_id}")
def get_suite(suite_id: str) -> dict:
    if not SUITE_ID_RE.match(suite_id):
        raise HTTPException(status_code=400, detail="Invalid suite_id")
    try:
        suite = suites_module.load_suite(suite_id, suites_dir=str(SUITES_DIR))
    except OSError:
        raise HTTPException(status_code=404, detail="Unknown suite")
    return {"id": suite.id, "name": suite.name, "url": suite.url, "cases": [asdict(c) for c in suite.cases]}


@app.delete("/api/suites/{suite_id}")
def remove_suite(suite_id: str) -> dict:
    if not SUITE_ID_RE.match(suite_id):
        raise HTTPException(status_code=400, detail="Invalid suite_id")
    path = SUITES_DIR / f"{suite_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown suite")
    suites_module.delete_suite(suite_id, suites_dir=str(SUITES_DIR))
    return {"deleted": suite_id}


@app.get("/api/suites/{suite_id}/runs")
def get_suite_runs(suite_id: str) -> dict:
    if not SUITE_ID_RE.match(suite_id):
        raise HTTPException(status_code=400, detail="Invalid suite_id")
    runs = []
    if REPORTS_DIR.is_dir():
        for entry in sorted(REPORTS_DIR.iterdir(), reverse=True):
            data = _load_report(entry)
            if data is None:
                continue
            if (data.get("config") or {}).get("suite_id") == suite_id:
                runs.append(_run_summary(entry, data))
    return {"runs": runs}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that never lets the browser cache a response. This app's
    UI (index.html/app.js/style.css) is actively edited while the server
    keeps running - StaticFiles already re-reads the file from disk on every
    request, but browsers still happily reuse a stale cached copy of app.js
    on a normal reload (only a hard refresh bypasses that), which has caused
    real confusion (stale JS behavior against fresh HTML). Not applied to
    /reports, where caching write-once screenshots/reports is harmless.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


os.makedirs(REPORTS_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=str(REPORTS_DIR)), name="reports")
app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
