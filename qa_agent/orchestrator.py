from __future__ import annotations

import datetime as dt
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .browser import BrowserSession
from .config import RunConfig
from .executor import run_test_case
from .llm_client import ClaudeClient, OllamaClient, OpenAIClient
from .models import StepLog, TestCase, TestResult
from .planner import generate_test_cases
from .report import write_report

EventCallback = Callable[[dict], None]

REVIEW_POLL_SECONDS = 1.0
REVIEW_TIMEOUT_SECONDS = 1800.0

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _url_slug(url: str) -> str:
    """A short, filesystem/URL-safe fragment of `url` (scheme stripped) for
    use in run ids, so `reports/` and the past-runs list are recognizable at
    a glance instead of being bare timestamps."""
    stripped = _SCHEME_RE.sub("", url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", stripped).strip("-").lower()
    return (slug[:50] or "run")


def make_run_id(url: str) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}__{_url_slug(url)}"


@dataclass
class ReviewGate:
    """Pauses a run after planning so a human can edit the proposed test
    cases before execution starts. `approval_event` is set once the reviewer
    responds (approve endpoint); `approved_cases` holds their edited list.
    """

    approval_event: threading.Event = field(default_factory=threading.Event)
    approved_cases: list[TestCase] | None = None


def _make_llm_client(cfg: RunConfig, api_key: str = "", workspace_id: str = ""):
    if cfg.provider == "claude":
        return ClaudeClient(model=cfg.model, api_key=api_key, workspace_id=workspace_id)
    if cfg.provider == "openai":
        return OpenAIClient(model=cfg.model, base_url=cfg.openai_base_url, api_key=api_key)
    return OllamaClient(host=cfg.ollama_host, model=cfg.model, num_ctx=cfg.num_ctx)


def _new_browser(cfg: RunConfig) -> BrowserSession:
    return BrowserSession(
        headless=cfg.headless,
        nav_timeout_ms=cfg.nav_timeout_ms,
        action_timeout_ms=cfg.action_timeout_ms,
        channel=cfg.channel,
    )


def _step_event(case_id: str, step_log: StepLog, run_id: str) -> dict:
    event = {"type": "step", "case_id": case_id, **asdict(step_log)}
    if step_log.screenshot_path:
        event["screenshot_url"] = f"/reports/{run_id}/screenshots/{os.path.basename(step_log.screenshot_path)}"
    return event


def _navigate_and_precondition(
    browser: BrowserSession,
    cfg: RunConfig,
    llm: OllamaClient,
    screenshots_dir: str,
    cancel_event: threading.Event | None,
    emit: EventCallback,
    run_id: str,
) -> tuple[bool, str, str, str, str | None]:
    """Navigates an already-entered browser to cfg.url and, if set, runs the
    precondition on it. Returns (ok, url, title, snapshot, error_message) —
    on failure only `ok` and `error_message` are meaningful.
    """
    nav_obs = browser.navigate(cfg.url)
    if not nav_obs.ok:
        return False, "", "", "", f"Failed to open page: {nav_obs.result_text}"

    emit({"type": "info", "message": "Page loaded."})

    # effective_start is what planning sees and what every reset_before case
    # navigates back to. It's cfg.url unless a precondition (e.g. a login)
    # ran and moved the page somewhere else worth starting from.
    effective_start_url = nav_obs.url
    effective_start_title = nav_obs.title
    effective_start_snapshot = nav_obs.snapshot

    if cfg.precondition_goal.strip():
        emit({"type": "precondition_start", "goal": cfg.precondition_goal})
        precondition_case = TestCase(
            id="precondition",
            title="Precondition",
            goal=cfg.precondition_goal,
            reset_before=False,
        )

        def on_precondition_step(step_log: StepLog) -> None:
            emit(_step_event("precondition", step_log, run_id))

        precondition_result = run_test_case(
            case=precondition_case,
            initial_url=nav_obs.url,
            initial_title=nav_obs.title,
            initial_snapshot=nav_obs.snapshot,
            browser=browser,
            llm=llm,
            max_steps=cfg.max_steps_per_case,
            # Preconditions are user-authored setup steps, not the agent's
            # own autonomous exploration, so they always auto-confirm
            # guardrail-flagged actions (a login button often matches the
            # "submit" keyword) regardless of the run's own setting.
            auto_confirm_destructive=True,
            screenshots_dir=screenshots_dir,
            on_step=on_precondition_step,
            system_prompt_template=cfg.executor_system_prompt,
            cancel_event=cancel_event,
        )
        emit(
            {
                "type": "precondition_end",
                "status": precondition_result.status,
                "summary": precondition_result.summary,
            }
        )
        effective_start_url = browser.page.url
        effective_start_title = browser.page.title()
        effective_start_snapshot = browser.snapshot_text()

    return True, effective_start_url, effective_start_title, effective_start_snapshot, None


def run_agent(
    cfg: RunConfig,
    on_event: EventCallback | None = None,
    run_id: str | None = None,
    cancel_event: threading.Event | None = None,
    review_gate: ReviewGate | None = None,
    predefined_cases: list[TestCase] | None = None,
    api_key: str = "",
    workspace_id: str = "",
) -> dict:
    def emit(event: dict) -> None:
        if on_event:
            on_event(event)

    run_id = run_id or make_run_id(cfg.url)
    run_dir = os.path.join(cfg.output_dir, run_id)
    screenshots_dir = os.path.join(run_dir, "screenshots")

    # api_key is a request-scoped override (e.g. typed into the web UI and
    # cached only in the server process's memory) - deliberately not a
    # RunConfig field, so it's never in `asdict(cfg)`/report.json.
    llm = _make_llm_client(cfg, api_key=api_key, workspace_id=workspace_id)

    emit({"type": "info", "message": f"Opening {cfg.url} ..."})

    results: list[TestResult] = []
    planning_browser = _new_browser(cfg)
    planning_browser.__enter__()
    try:
        ok, effective_start_url, effective_start_title, effective_start_snapshot, error = _navigate_and_precondition(
            planning_browser, cfg, llm, screenshots_dir, cancel_event, emit, run_id
        )
        if not ok:
            emit({"type": "error", "message": error})
            return {"run_id": run_id, "run_dir": run_dir, "results": [], "error": error}

        if predefined_cases is not None:
            cases = predefined_cases
            emit({"type": "cases_planned", "cases": [asdict(c) for c in cases]})
        else:
            emit({"type": "info", "message": "Asking the model to plan test cases..."})
            try:
                cases = generate_test_cases(
                    llm=llm,
                    url=effective_start_url,
                    title=effective_start_title,
                    snapshot=effective_start_snapshot,
                    max_cases=cfg.max_cases,
                    focus_hint=cfg.focus_hint,
                    system_prompt=cfg.planner_system_prompt,
                )
            except Exception as exc:
                message = f"Planning failed: {exc}"
                emit({"type": "error", "message": message})
                return {"run_id": run_id, "run_dir": run_dir, "results": [], "error": message}

            emit({"type": "cases_planned", "cases": [asdict(c) for c in cases]})

        cancelled_during_review = False
        if review_gate is not None:
            emit({"type": "awaiting_review", "cases": [asdict(c) for c in cases]})
            elapsed = 0.0
            while not review_gate.approval_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    cancelled_during_review = True
                    break
                if elapsed >= REVIEW_TIMEOUT_SECONDS:
                    cancelled_during_review = True
                    emit({"type": "info", "message": "Review timed out with no response; cancelling run."})
                    break
                time.sleep(REVIEW_POLL_SECONDS)
                elapsed += REVIEW_POLL_SECONDS
            if not cancelled_during_review:
                cases = review_gate.approved_cases or []
                emit({"type": "review_approved", "cases": [asdict(c) for c in cases]})

        if cfg.fresh_browser_per_case:
            # The planning browser has done its job; each case below gets its
            # own from here, so there's no reason to keep it around.
            planning_browser.__exit__(None, None, None)
            planning_browser = None

            if not cancelled_during_review:
                for case in cases:
                    if cancel_event is not None and cancel_event.is_set():
                        emit({"type": "info", "message": "Run cancelled; skipping remaining planned cases."})
                        break

                    emit({"type": "info", "message": f"Opening a fresh browser for '{case.title}'..."})
                    case_browser = _new_browser(cfg)
                    case_browser.__enter__()
                    try:
                        ok, init_url, init_title, init_snap, error = _navigate_and_precondition(
                            case_browser, cfg, llm, screenshots_dir, cancel_event, emit, run_id
                        )
                        # reset_before is moot here: a fresh browser is
                        # already a full reset regardless of the per-case flag.
                        emit({"type": "case_start", "case_id": case.id, "title": case.title, "reset": True})
                        if not ok:
                            emit({"type": "info", "message": error})
                            result = TestResult(case=case, status="error", summary=error or "Failed to open page.")
                        else:

                            def on_step(step_log: StepLog, case_id: str = case.id) -> None:
                                emit(_step_event(case_id, step_log, run_id))

                            result = run_test_case(
                                case=case,
                                initial_url=init_url,
                                initial_title=init_title,
                                initial_snapshot=init_snap,
                                browser=case_browser,
                                llm=llm,
                                max_steps=cfg.max_steps_per_case,
                                auto_confirm_destructive=cfg.auto_confirm_destructive,
                                screenshots_dir=screenshots_dir,
                                on_step=on_step,
                                focus_hint=cfg.focus_hint,
                                cancel_event=cancel_event,
                                system_prompt_template=cfg.executor_system_prompt,
                            )
                    finally:
                        case_browser.__exit__(None, None, None)

                    emit(
                        {
                            "type": "case_end",
                            "case_id": case.id,
                            "status": result.status,
                            "summary": result.summary,
                            "findings": [asdict(f) for f in result.findings],
                            "screenshot_url": f"/reports/{run_id}/screenshots/{case.id}.png"
                            if result.screenshot_path
                            else None,
                        }
                    )
                    results.append(result)
        else:
            # Captured once, right before any case can mutate anything: what
            # "reset to the initial state" restores to on every reset_before
            # case, including cookies/localStorage — not just the same URL.
            baseline_storage_state = planning_browser.capture_storage_state()

            if not cancelled_during_review:
                for case in cases:
                    if cancel_event is not None and cancel_event.is_set():
                        emit({"type": "info", "message": "Run cancelled; skipping remaining planned cases."})
                        break

                    if case.reset_before:
                        emit({"type": "info", "message": f"Resetting to {effective_start_url} before '{case.title}'..."})
                        planning_browser.restore_storage_state(baseline_storage_state)
                        reset_obs = planning_browser.navigate(effective_start_url)
                        if reset_obs.ok:
                            planning_browser.reset_local_storage(baseline_storage_state)
                            # reload so any script that reads localStorage at
                            # load time (e.g. an i18n framework) sees the
                            # restored/cleared values, not stale in-memory state
                            reset_obs = planning_browser.navigate(effective_start_url)
                        if not reset_obs.ok:
                            emit({"type": "info", "message": f"Reset navigation had an issue: {reset_obs.result_text}"})
                        init_url, init_title, init_snap = reset_obs.url, reset_obs.title, reset_obs.snapshot
                    else:
                        init_url = planning_browser.page.url
                        init_title = planning_browser.page.title()
                        init_snap = planning_browser.snapshot_text()

                    emit({"type": "case_start", "case_id": case.id, "title": case.title, "reset": case.reset_before})

                    def on_step(step_log: StepLog, case_id: str = case.id) -> None:
                        emit(_step_event(case_id, step_log, run_id))

                    result = run_test_case(
                        case=case,
                        initial_url=init_url,
                        initial_title=init_title,
                        initial_snapshot=init_snap,
                        browser=planning_browser,
                        llm=llm,
                        max_steps=cfg.max_steps_per_case,
                        auto_confirm_destructive=cfg.auto_confirm_destructive,
                        screenshots_dir=screenshots_dir,
                        on_step=on_step,
                        focus_hint=cfg.focus_hint,
                        cancel_event=cancel_event,
                        system_prompt_template=cfg.executor_system_prompt,
                    )
                    emit(
                        {
                            "type": "case_end",
                            "case_id": case.id,
                            "status": result.status,
                            "summary": result.summary,
                            "findings": [asdict(f) for f in result.findings],
                            "screenshot_url": f"/reports/{run_id}/screenshots/{case.id}.png"
                            if result.screenshot_path
                            else None,
                        }
                    )
                    results.append(result)

        md_path, json_path = write_report(cfg.url, cfg.model, results, run_dir, config=asdict(cfg))
    finally:
        if planning_browser is not None:
            planning_browser.__exit__(None, None, None)

    cancelled = cancelled_during_review or bool(cancel_event is not None and cancel_event.is_set())
    emit(
        {
            "type": "done",
            "run_id": run_id,
            "md_path": md_path,
            "json_path": json_path,
            "md_url": f"/reports/{run_id}/report.md",
            "json_url": f"/reports/{run_id}/report.json",
            "cancelled": cancelled,
        }
    )
    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "md_path": md_path,
        "json_path": json_path,
        "results": results,
    }
