from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FindingSeverity = Literal["info", "minor", "major", "critical"]
CaseStatus = Literal["pass", "fail", "blocked", "incomplete", "error", "cancelled"]


@dataclass
class TestCase:
    id: str
    title: str
    goal: str
    steps: list[str] = field(default_factory=list)
    reset_before: bool = True


@dataclass
class Finding:
    severity: FindingSeverity
    message: str


@dataclass
class StepLog:
    step: int
    tool_name: str
    tool_args: dict
    result_summary: str
    guardrail_blocked: bool = False
    screenshot_path: str | None = None


@dataclass
class TestResult:
    case: TestCase
    status: CaseStatus
    summary: str
    steps: list[StepLog] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    screenshot_path: str | None = None
