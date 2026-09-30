"""Tool schemas shared between the executor's tool-calling loop and the browser
action dispatcher. Refs (e.g. "e3") come from the ``[ref=eN]`` markers in the
AI-mode ARIA snapshot the model is shown after every step.
"""
from __future__ import annotations

REF_DESC = (
    "Element reference from the most recent snapshot's [ref=eN] marker, e.g. 'e3'."
)

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element on the page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": REF_DESC},
                    "description": {
                        "type": "string",
                        "description": "Short human description of the target, e.g. 'Submit button'.",
                    },
                },
                "required": ["ref", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Fill a text input, textarea, or contenteditable element with text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": REF_DESC},
                    "text": {"type": "string", "description": "Text to type into the field."},
                    "description": {
                        "type": "string",
                        "description": "Short human description of the target field.",
                    },
                },
                "required": ["ref", "text", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select_option",
            "description": "Choose one or more options in a <select> dropdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": REF_DESC},
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option value(s) or visible label(s) to select.",
                    },
                    "description": {"type": "string"},
                },
                "required": ["ref", "values", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_check",
            "description": "Set a checkbox or radio button to a checked/unchecked state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": REF_DESC},
                    "checked": {"type": "boolean"},
                    "description": {"type": "string"},
                },
                "required": ["ref", "checked", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_hover",
            "description": "Hover the mouse over an element (e.g. to reveal a submenu).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": REF_DESC},
                    "description": {"type": "string"},
                },
                "required": ["ref", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "Press a single keyboard key (e.g. 'Enter', 'Escape', 'Tab').",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "Wait for a short period (seconds) for async page updates to settle. Max 5 seconds.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_go_back",
            "description": "Navigate back to the previous page in history (same-origin only).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_finding",
            "description": "Record a bug or anomaly observed on the page without ending the test case.",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["info", "minor", "major", "critical"],
                    },
                    "message": {"type": "string"},
                },
                "required": ["severity", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_case",
            "description": "End the current test case with a final verdict. Always call this when the goal has been verified or definitively disproven, or you are stuck.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["pass", "fail", "blocked"]},
                    "summary": {
                        "type": "string",
                        "description": "One or two sentences explaining the verdict and what was observed.",
                    },
                },
                "required": ["status", "summary"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}
ACTION_TOOL_NAMES = TOOL_NAMES - {"report_finding", "finish_case"}
