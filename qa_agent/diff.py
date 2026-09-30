"""Compares two run reports (report.json payloads) to surface regressions.

Cases are matched by normalized title rather than id: ids like "tc1"/"tc2"
aren't stable across independently LLM-planned runs (a re-plan can propose
the same case in a different order, or phrase it slightly differently and
get a different id) — title is the more meaningful identity for a human
comparing two runs. Suite-based runs (same cases every time) match exactly.
"""
from __future__ import annotations


def _norm_title(title: str) -> str:
    return " ".join(title.strip().lower().split())


def diff_reports(report_a: dict, report_b: dict) -> dict:
    cases_a = {_norm_title(r["case"]["title"]): r for r in report_a.get("results", [])}
    cases_b = {_norm_title(r["case"]["title"]): r for r in report_b.get("results", [])}

    regressions = []
    fixed = []
    changed = []
    unchanged = []
    only_in_a = []
    only_in_b = []

    for key, ra in cases_a.items():
        rb = cases_b.get(key)
        if rb is None:
            only_in_a.append({"title": ra["case"]["title"], "status": ra["status"]})
            continue
        status_a, status_b = ra["status"], rb["status"]
        entry = {"title": ra["case"]["title"], "status_a": status_a, "status_b": status_b}
        if status_a == status_b:
            unchanged.append(entry)
        elif status_a == "pass" and status_b != "pass":
            regressions.append(entry)
        elif status_a != "pass" and status_b == "pass":
            fixed.append(entry)
        else:
            changed.append(entry)

    for key, rb in cases_b.items():
        if key not in cases_a:
            only_in_b.append({"title": rb["case"]["title"], "status": rb["status"]})

    return {
        "a_url": report_a.get("url"),
        "b_url": report_b.get("url"),
        "regressions": regressions,
        "fixed": fixed,
        "changed": changed,
        "unchanged": unchanged,
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
    }
