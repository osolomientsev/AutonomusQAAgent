from __future__ import annotations

import json
import os
from dataclasses import asdict

from .models import TestResult

STATUS_EMOJI = {
    "pass": "PASS",
    "fail": "FAIL",
    "blocked": "BLOCKED",
    "incomplete": "INCOMPLETE",
    "error": "ERROR",
    "cancelled": "CANCELLED",
}


def write_report(
    url: str,
    model: str,
    results: list[TestResult],
    run_dir: str,
    config: dict | None = None,
) -> tuple[str, str]:
    os.makedirs(run_dir, exist_ok=True)
    md_path = os.path.join(run_dir, "report.md")
    json_path = os.path.join(run_dir, "report.json")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "url": url,
                "model": model,
                "config": config,
                "results": [asdict(r) for r in results],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    lines = [f"# QA Report: {url}", "", f"Model: `{model}`", "", "## Summary", "", "| Case | Status | Title |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.case.id} | {STATUS_EMOJI.get(r.status, r.status)} | {r.case.title} |")
    lines.append("")

    for r in results:
        lines.append(f"## {r.case.id}: {r.case.title}")
        lines.append("")
        lines.append(f"**Goal:** {r.case.goal}")
        lines.append("")
        if r.case.steps:
            lines.append("**Planned steps:**")
            lines.append("")
            for i, step in enumerate(r.case.steps, start=1):
                lines.append(f"{i}. {step}")
            lines.append("")
        reset_note ="Yes (re-navigated to the start URL)" if r.case.reset_before else "No (continued from previous case)"
        lines.append(f"**Reset to initial state:** {reset_note}")
        lines.append("")
        lines.append(f"**Status:** {STATUS_EMOJI.get(r.status, r.status)}")
        lines.append("")
        lines.append(f"**Summary:** {r.summary}")
        lines.append("")
        if r.findings:
            lines.append("**Findings:**")
            for finding in r.findings:
                lines.append(f"- [{finding.severity}] {finding.message}")
            lines.append("")
        if r.steps:
            lines.append("<details><summary>Execution log</summary>")
            lines.append("")
            for s in r.steps:
                flag = " (blocked by guardrail)" if s.guardrail_blocked else ""
                lines.append(f"{s.step}. `{s.tool_name}({s.tool_args})` -> {s.result_summary}{flag}")
                if s.screenshot_path:
                    rel = os.path.relpath(s.screenshot_path, run_dir)
                    lines.append(f"   ![step {s.step} screenshot]({rel})")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        if r.screenshot_path:
            rel = os.path.relpath(r.screenshot_path, run_dir)
            lines.append("**Final state:**")
            lines.append("")
            lines.append(f"![screenshot]({rel})")
            lines.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return md_path, json_path
