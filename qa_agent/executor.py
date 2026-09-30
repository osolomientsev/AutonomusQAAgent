from __future__ import annotations

import json
import os
import threading
from typing import Callable

from . import guardrails
from .browser import BrowserSession
from .llm_client import OllamaClient
from .models import Finding, StepLog, TestCase, TestResult
from .tools import ACTION_TOOL_NAMES, TOOL_SCHEMAS

MAX_SNAPSHOT_CHARS = 8000

EXECUTOR_SYSTEM_PROMPT = """You are an autonomous QA testing agent operating a real web
browser through tools. You are executing exactly one test case at a time.

Test case: "{title}"
Goal: {goal}
{steps_section}{focus_section}

Rules:
- Only act on elements that appear in the most recent snapshot's [ref=eN] markers.
  Refs can change after the page updates, so always use the ref from the latest
  snapshot you were shown, not an earlier one.
- Take the smallest useful step at a time and observe the result before continuing.
- Some actions (payments, deletions, logout, form submission, confirmations) are
  guarded: they may be blocked or require human confirmation. If one is blocked,
  treat that as information, not a crash, and adapt or conclude the case.
- If you see a JavaScript/console error or a failed network request relevant to
  your goal, call report_finding before continuing.
- If an action opens a new tab/window (target="_blank" links, window.open, ...),
  it is inspected and then closed automatically — you stay on the original tab.
  Its url/title/snapshot are reported in the "new_tabs" field, not in the main
  "snapshot" (which still reflects the original tab). Use "new_tabs" to verify
  what the new tab showed; do not expect its content to appear in the main
  snapshot or wait for the main page itself to change because of it.
- Detecting a new tab has a little latency: if you clicked something that
  should plausibly open a new tab/window and "new_tabs" is empty on that same
  step, do NOT immediately conclude the case failed. Take one more step first
  (e.g. browser_wait for 1-2 seconds, or press nothing and just observe) —
  the new tab's info may land on the following step's "new_tabs" instead.
  Only treat it as a real failure if it's still missing after that.
- When you have enough evidence to decide the outcome (or you are stuck / the
  needed element does not exist), call finish_case with status pass, fail, or
  blocked, and a concise summary. Always finish with finish_case eventually.
"""


def _truncate(text: str, limit: int = MAX_SNAPSHOT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [snapshot truncated, {len(text) - limit} more chars]"


_COLLAPSED_SNAPSHOT = "[collapsed - superseded by a later snapshot; only the most recent snapshot's refs are valid, per the system prompt's own rule]"


def _collapse_older_snapshots(messages: list[dict]) -> None:
    """Replaces the (now-stale) full `snapshot` text in every earlier
    observation with a short placeholder, once a newer one is about to be
    appended. The agent is already instructed to only ever act on refs from
    the *latest* snapshot - older ones are pure context cost (and, on
    Claude, mostly-wasted prefix bytes) with no grounding value, while the
    result/errors/new_tabs fields around them still carry real history and
    are left untouched. Mutates in place; call once per turn, right before
    appending the new observation, so exactly one snapshot per case stays
    full at any time.
    """
    for msg in messages:
        if msg.get("role") not in ("user", "tool"):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        changed = False
        if payload.get("snapshot") and payload["snapshot"] != _COLLAPSED_SNAPSHOT:
            payload["snapshot"] = _COLLAPSED_SNAPSHOT
            changed = True
        for tab in payload.get("new_tabs") or []:
            if isinstance(tab, dict) and tab.get("snapshot") and tab["snapshot"] != _COLLAPSED_SNAPSHOT:
                tab["snapshot"] = _COLLAPSED_SNAPSHOT
                changed = True
        if changed:
            msg["content"] = json.dumps(payload, ensure_ascii=False)


def _observation_to_tool_content(result_text: str, obs) -> str:
    payload = {
        "result": result_text,
        "url": obs.url,
        "title": obs.title,
        "console_errors": obs.console_errors,
        "page_errors": obs.page_errors,
        "request_failures": obs.request_failures,
        "blocked_navigations": obs.blocked_navigations,
        "new_tabs": [
            {"url": t["url"], "title": t["title"], "snapshot": _truncate(t.get("snapshot", ""), 3000)}
            for t in obs.new_tabs
        ],
        "snapshot": _truncate(obs.snapshot),
    }
    return json.dumps(payload, ensure_ascii=False)


def run_test_case(
    case: TestCase,
    initial_url: str,
    initial_title: str,
    initial_snapshot: str,
    browser: BrowserSession,
    llm: OllamaClient,
    max_steps: int,
    auto_confirm_destructive: bool,
    screenshots_dir: str,
    on_step: Callable[[StepLog], None] | None = None,
    focus_hint: str = "",
    cancel_event: threading.Event | None = None,
    system_prompt_template: str = "",
) -> TestResult:
    focus_section = f"Tester's focus/guidance: {focus_hint.strip()}\n" if focus_hint.strip() else ""
    steps_section = ""
    if case.steps:
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(case.steps, start=1))
        steps_section = (
            "Planned steps (work through them in order as your plan; adapt if one "
            f"doesn't apply to what's actually on the page):\n{numbered}\n"
        )
    if system_prompt_template.strip():
        # Same reasoning as the planner: substitute directly rather than
        # str.format(), so an edited prompt need not escape literal braces.
        system_prompt = (
            system_prompt_template.replace("{title}", case.title)
            .replace("{goal}", case.goal)
            .replace("{focus_section}", focus_section)
            .replace("{steps_section}", steps_section)
        )
    else:
        system_prompt = EXECUTOR_SYSTEM_PROMPT.format(
            title=case.title, goal=case.goal, focus_section=focus_section, steps_section=steps_section
        )
    initial_payload = {
        "result": "Current page state before this test case.",
        "url": initial_url,
        "title": initial_title,
        "console_errors": [],
        "page_errors": [],
        "request_failures": [],
        "blocked_navigations": [],
        "new_tabs": [],
        "snapshot": _truncate(initial_snapshot),
    }
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(initial_payload, ensure_ascii=False)},
    ]

    steps: list[StepLog] = []
    findings: list[Finding] = []
    status = "incomplete"
    summary = "Max steps reached without the agent calling finish_case."
    os.makedirs(screenshots_dir, exist_ok=True)

    def log_step(step_log: StepLog) -> None:
        steps.append(step_log)
        if on_step:
            on_step(step_log)

    for step_num in range(1, max_steps + 1):
        if cancel_event is not None and cancel_event.is_set():
            status = "cancelled"
            summary = "Cancelled by user before this case finished."
            break

        result = llm.chat_with_tools(messages, TOOL_SCHEMAS)

        if not result.tool_calls:
            messages.append({"role": "assistant", "content": result.content})
            messages.append(
                {
                    "role": "user",
                    "content": "You must use one of the provided tools to act, observe, "
                    "or finish_case. Please call a tool now.",
                }
            )
            continue

        messages.append(
            {
                "role": "assistant",
                "content": result.content,
                "tool_calls": [
                    {"function": {"name": tc.name, "arguments": tc.args}} for tc in result.tool_calls
                ],
            }
        )

        finished = False
        for tc in result.tool_calls:
            if tc.name == "finish_case":
                status = tc.args.get("status", "incomplete")
                summary = tc.args.get("summary", "")
                messages.append({"role": "tool", "content": "Case finished."})
                finished = True
                continue

            if tc.name == "report_finding":
                findings.append(
                    Finding(
                        severity=tc.args.get("severity", "info"),
                        message=tc.args.get("message", ""),
                    )
                )
                messages.append({"role": "tool", "content": "Finding recorded."})
                log_step(StepLog(step=step_num, tool_name=tc.name, tool_args=tc.args, result_summary="recorded"))
                continue

            if tc.name not in ACTION_TOOL_NAMES:
                messages.append({"role": "tool", "content": f"Unknown tool '{tc.name}'."})
                continue

            reason = guardrails.classify(tc.name, tc.args, target_text=tc.args.get("description", ""))
            if reason:
                action_desc = f"{tc.name}({tc.args})"
                allowed = guardrails.confirm_with_user(action_desc, reason, auto_confirm_destructive)
                if not allowed:
                    content = f"blocked by guardrail: {reason}"
                    messages.append({"role": "tool", "content": content})
                    log_step(
                        StepLog(
                            step=step_num,
                            tool_name=tc.name,
                            tool_args=tc.args,
                            result_summary=content,
                            guardrail_blocked=True,
                        )
                    )
                    continue

            obs = browser.act(tc.name, tc.args)
            _collapse_older_snapshots(messages)
            messages.append({"role": "tool", "content": _observation_to_tool_content(obs.result_text, obs)})
            # Captured immediately after this action, not at the end of the
            # case: by then a later action (or the next case's reset) may
            # have already changed the page, making a single end-of-case
            # screenshot misleading about what the page looked like *here*.
            step_screenshot_path = os.path.join(screenshots_dir, f"{case.id}_step{step_num}_{len(steps) + 1}.png")
            browser.screenshot(step_screenshot_path)
            log_step(
                StepLog(
                    step=step_num,
                    tool_name=tc.name,
                    tool_args=tc.args,
                    result_summary=obs.result_text,
                    screenshot_path=step_screenshot_path,
                )
            )

        if finished:
            break

    screenshot_path = None
    if browser.page is not None:
        os.makedirs(screenshots_dir, exist_ok=True)
        screenshot_path = os.path.join(screenshots_dir, f"{case.id}.png")
        browser.screenshot(screenshot_path)

    return TestResult(
        case=case,
        status=status,
        summary=summary,
        steps=steps,
        findings=findings,
        screenshot_path=screenshot_path,
    )
