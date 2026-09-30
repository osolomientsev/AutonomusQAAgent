"""Saved, reusable sets of test cases ("suites").

A suite is a plain JSON file under `suites/`, mirroring how `report.py`
persists run reports — no database needed for a local single-user tool.
Running from a suite skips LLM planning entirely (see
`orchestrator.run_agent`'s `predefined_cases`), making repeat runs against
the same page fast, cheap, and deterministic.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import asdict, dataclass, field

from .models import TestCase

SUITES_DIR = "suites"

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


@dataclass
class Suite:
    id: str
    name: str
    url: str
    cases: list[TestCase] = field(default_factory=list)
    created_at: str = ""


def _slug(text: str) -> str:
    return (_SLUG_RE.sub("-", text).strip("-").lower())[:50] or "suite"


def _suite_path(suite_id: str, suites_dir: str = SUITES_DIR) -> str:
    return os.path.join(suites_dir, f"{suite_id}.json")


def save_suite(name: str, url: str, cases: list[TestCase], suites_dir: str = SUITES_DIR) -> Suite:
    os.makedirs(suites_dir, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suite_id = f"{_slug(name)}__{timestamp}"
    suite = Suite(id=suite_id, name=name, url=url, cases=cases, created_at=dt.datetime.now().isoformat())
    with open(_suite_path(suite_id, suites_dir), "w", encoding="utf-8") as f:
        json.dump(asdict(suite), f, ensure_ascii=False, indent=2)
    return suite


def list_suites(suites_dir: str = SUITES_DIR) -> list[dict]:
    if not os.path.isdir(suites_dir):
        return []
    summaries = []
    for entry in sorted(os.listdir(suites_dir), reverse=True):
        if not entry.endswith(".json"):
            continue
        path = os.path.join(suites_dir, entry)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        summaries.append(
            {
                "id": data.get("id", entry[: -len(".json")]),
                "name": data.get("name", ""),
                "url": data.get("url", ""),
                "case_count": len(data.get("cases", [])),
                "created_at": data.get("created_at", ""),
            }
        )
    return summaries


def load_suite(suite_id: str, suites_dir: str = SUITES_DIR) -> Suite:
    path = _suite_path(suite_id, suites_dir)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    cases = [TestCase(**c) for c in data.get("cases", [])]
    return Suite(id=data["id"], name=data["name"], url=data["url"], cases=cases, created_at=data.get("created_at", ""))


def update_suite(suite_id: str, name: str, url: str, cases: list[TestCase], suites_dir: str = SUITES_DIR) -> Suite:
    """Overwrites an existing suite's cases/name/url in place, keeping the
    same id (and thus the same past-runs linkage) and original created_at.
    """
    existing = load_suite(suite_id, suites_dir)
    suite = Suite(id=suite_id, name=name, url=url, cases=cases, created_at=existing.created_at)
    with open(_suite_path(suite_id, suites_dir), "w", encoding="utf-8") as f:
        json.dump(asdict(suite), f, ensure_ascii=False, indent=2)
    return suite


def delete_suite(suite_id: str, suites_dir: str = SUITES_DIR) -> None:
    path = _suite_path(suite_id, suites_dir)
    if os.path.isfile(path):
        os.remove(path)
