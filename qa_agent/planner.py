from __future__ import annotations

import json

from .llm_client import OllamaClient
from .models import TestCase

PLANNER_SYSTEM_PROMPT = """You are a QA test planner. You are given an ARIA
accessibility snapshot of a web page (element refs like [ref=e3] are internal
ids, ignore them for planning purposes) plus its URL and title.

Propose a set of concrete, independently-runnable manual test cases that
another agent will execute one at a time on this exact page, using only
elements that are actually present in the snapshot. Favor functional checks
over cosmetic ones: forms and their validation, buttons and the state changes
they cause, links, search, filters, toggles, menus. Do not invent elements
that are not in the snapshot. Do not propose destructive/irreversible test
cases (real payments, account deletion) as the primary case, but you may
include verifying that such actions require confirmation.

If the tester gives you a focus/guidance note below, prioritize test cases
related to it over generic ones, while still only using elements actually
present in the snapshot.

For each test case, also break it down into a "steps" array: 2 to 6 short,
ordered, imperative instructions describing what to do (e.g. "Click the
'Sign up' link", "Fill the email field with an invalid address", "Submit
and check for a validation message"). These steps will be shown to a human
for review/editing and then given to the executing agent as its plan.

Respond with ONLY a single JSON object (no prose, no markdown fences) shaped
exactly like this example, with a "test_cases" array containing 3 to {max_cases}
entries:
{{"test_cases": [
  {{"id": "tc1", "title": "short title", "goal": "one or two sentences describing what to do and what the expected outcome is", "steps": ["step one", "step two", "step three"]}},
  {{"id": "tc2", "title": "short title", "goal": "...", "steps": ["..."]}}
]}}
"""


def generate_test_cases(
    llm: OllamaClient,
    url: str,
    title: str,
    snapshot: str,
    max_cases: int,
    focus_hint: str = "",
    system_prompt: str = "",
) -> list[TestCase]:
    if system_prompt.strip():
        # A user-edited prompt shouldn't have to worry about escaping the
        # literal { } in the JSON example the way str.format() would require,
        # so substitute the one placeholder it's expected to contain directly.
        system = system_prompt.replace("{max_cases}", str(max_cases))
    else:
        system = PLANNER_SYSTEM_PROMPT.format(max_cases=max_cases)
    user = f"URL: {url}\nTitle: {title}\n\nARIA snapshot:\n{snapshot}"
    if focus_hint.strip():
        user += f"\n\nTester's focus/guidance: {focus_hint.strip()}"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    raw = llm.chat_json(messages)
    cases = _parse_cases(raw)
    if cases is not None:
        return cases[:max_cases]

    # one retry, telling the model what went wrong
    messages.append({"role": "assistant", "content": raw})
    messages.append(
        {
            "role": "user",
            "content": "That was not a valid JSON array of the required shape. "
            "Respond again with ONLY a valid JSON array, no other text.",
        }
    )
    raw_retry = llm.chat_json(messages)
    cases = _parse_cases(raw_retry)
    if cases is None:
        raise ValueError(
            f"Planner failed to produce valid JSON test cases after retry. Last output: {raw_retry!r}"
        )
    return cases[:max_cases]


def _parse_cases(raw: str) -> list[TestCase] | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict):
        for key in ("test_cases", "cases", "testCases"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            # a single test case object, not wrapped in a list
            data = [data] if {"title", "goal"} & data.keys() else None

    if not isinstance(data, list):
        return None
    cases = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None
        cid = str(item.get("id") or f"tc{i + 1}")
        title = str(item.get("title") or "").strip()
        goal = str(item.get("goal") or "").strip()
        if not title or not goal:
            return None
        raw_steps = item.get("steps")
        steps = [str(s).strip() for s in raw_steps if str(s).strip()] if isinstance(raw_steps, list) else []
        cases.append(TestCase(id=cid, title=title, goal=goal, steps=steps))
    return cases if cases else None
