from __future__ import annotations

import argparse
import sys

from qa_agent.config import RunConfig
from qa_agent.orchestrator import run_agent


def parse_args() -> RunConfig:
    parser = argparse.ArgumentParser(description="Autonomous QA agent: Playwright + local Ollama LLM")
    parser.add_argument("--url", required=True, help="URL of the page to test")
    parser.add_argument("--model", default=RunConfig.model, help="Ollama model name")
    parser.add_argument("--ollama-host", default=RunConfig.ollama_host)
    parser.add_argument("--headed", action="store_true", help="Show the browser window (default: headless)")
    parser.add_argument(
        "--channel",
        default=RunConfig.channel,
        choices=["chrome", "msedge", "chromium"],
        help="Browser to drive: system Chrome/Edge (default: chrome) or Playwright's own bundled chromium",
    )
    parser.add_argument("--max-cases", type=int, default=RunConfig.max_cases)
    parser.add_argument("--max-steps", type=int, default=RunConfig.max_steps_per_case)
    parser.add_argument("--num-ctx", type=int, default=RunConfig.num_ctx)
    parser.add_argument(
        "--auto-confirm-destructive",
        action="store_true",
        help="Automatically approve guardrail-flagged (destructive-looking) actions instead of prompting",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="Optional note telling the agent what to focus testing on, e.g. 'the checkout form'",
    )
    parser.add_argument(
        "--precondition",
        default="",
        help="Optional one-time setup goal run before planning, e.g. "
        "'log in with username demo and password demo123'. Always auto-confirms "
        "guardrail-flagged actions (it's a step you authored yourself).",
    )
    parser.add_argument(
        "--planner-prompt-file",
        default="",
        help="Path to a text file overriding the planner's system prompt (default: built-in). "
        "May contain a literal '{max_cases}' placeholder.",
    )
    parser.add_argument(
        "--executor-prompt-file",
        default="",
        help="Path to a text file overriding the executor's system prompt (default: built-in). "
        "May contain literal '{title}', '{goal}', '{focus_section}' placeholders.",
    )
    parser.add_argument(
        "--fresh-browser-per-case",
        action="store_true",
        help="Close and relaunch the browser before every test case instead of just "
        "re-navigating in the same session/context. Slower, but rules out any leakage "
        "between cases (cache, extensions state, ...) beyond cookies/localStorage, "
        "which reset_before already handles. If a --precondition is set, it re-runs "
        "for every case's fresh browser, not just once.",
    )
    parser.add_argument("--output", default=RunConfig.output_dir)
    args = parser.parse_args()

    planner_prompt = _read_prompt_file(args.planner_prompt_file)
    executor_prompt = _read_prompt_file(args.executor_prompt_file)

    return RunConfig(
        url=args.url,
        model=args.model,
        ollama_host=args.ollama_host,
        headless=not args.headed,
        channel=args.channel,
        max_cases=args.max_cases,
        max_steps_per_case=args.max_steps,
        num_ctx=args.num_ctx,
        auto_confirm_destructive=args.auto_confirm_destructive,
        focus_hint=args.focus,
        precondition_goal=args.precondition,
        planner_system_prompt=planner_prompt,
        executor_system_prompt=executor_prompt,
        fresh_browser_per_case=args.fresh_browser_per_case,
        output_dir=args.output,
    )


def _read_prompt_file(path: str) -> str:
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def print_event(event: dict) -> None:
    etype = event["type"]
    if etype == "info":
        print(event["message"])
    elif etype == "error":
        print(event["message"], file=sys.stderr)
    elif etype == "precondition_start":
        print(f"Running precondition: {event['goal']}")
    elif etype == "precondition_end":
        print(f"  -> precondition {event['status']}: {event['summary']}")
    elif etype == "cases_planned":
        cases = event["cases"]
        print(f"Planned {len(cases)} test case(s):")
        for c in cases:
            print(f"  - [{c['id']}] {c['title']}")
    elif etype == "case_start":
        print(f"\nRunning [{event['case_id']}] {event['title']} ...")
    elif etype == "case_end":
        print(f"  -> {event['status']}: {event['summary']}")
    elif etype == "done":
        print(f"\nReport written to:\n  {event['md_path']}\n  {event['json_path']}")


def main() -> int:
    cfg = parse_args()
    result = run_agent(cfg, on_event=print_event)
    return 1 if "error" in result else 0


if __name__ == "__main__":
    raise SystemExit(main())
