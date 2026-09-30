"""Keyword-based guardrail for destructive-looking actions.

This is deliberately simple (keyword/regex matching, not LLM judgement) so its
behavior is predictable and auditable. It flags actions whose target text or
tool name suggests an irreversible side effect (payment, deletion, logout,
form submission, confirmation dialogs), in English and Russian.
"""
from __future__ import annotations

import re
import sys

DESTRUCTIVE_PATTERN = re.compile(
    r"submit|pay\b|payment|checkout|purchase|buy now|delete|remove|deactivate|"
    r"cancel subscription|unsubscribe|log ?out|sign ?out|confirm|"
    r"оплат|удал|отправ|подтверд|выйти|отмен|купить|заказ",
    re.IGNORECASE,
)


def classify(tool_name: str, tool_args: dict, target_text: str = "") -> str | None:
    """Return a reason string if the action should be confirmed, else None."""
    haystack = f"{tool_name} {tool_args.get('description', '')} {target_text}"
    match = DESTRUCTIVE_PATTERN.search(haystack)
    if match:
        return f"matched destructive keyword '{match.group(0)}'"
    return None


def confirm_with_user(action_desc: str, reason: str, auto_confirm: bool) -> bool:
    if auto_confirm:
        print(f"[guardrail] auto-confirmed: {action_desc} ({reason})")
        return True

    if not sys.stdin.isatty():
        print(f"[guardrail] blocked (non-interactive, no auto-confirm): {action_desc} ({reason})")
        return False

    try:
        answer = input(
            f"\n[guardrail] Agent wants to: {action_desc}\n"
            f"  Reason flagged: {reason}\n"
            f"  Allow this action? [y/N] "
        ).strip().lower()
    except (EOFError, OSError):
        # isatty() can lie (e.g. some shells/terminal wrappers report a TTY
        # that has no actual input available) — fail safe rather than crash.
        print(f"[guardrail] blocked (stdin unavailable, no auto-confirm): {action_desc} ({reason})")
        return False
    return answer in ("y", "yes")
