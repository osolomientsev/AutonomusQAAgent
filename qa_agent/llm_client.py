from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic
import ollama
import openai


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall]


class OllamaClient:
    def __init__(self, host: str, model: str, num_ctx: int = 8192):
        self.client = ollama.Client(host=host)
        self.model = model
        self.num_ctx = num_ctx

    def chat_with_tools(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        response = self.client.chat(
            model=self.model,
            messages=messages,
            tools=tools,
            options={"num_ctx": self.num_ctx},
        )
        message = response["message"]
        raw_calls = message.get("tool_calls") or []
        calls = [
            ToolCall(name=tc["function"]["name"], args=dict(tc["function"]["arguments"] or {}))
            for tc in raw_calls
        ]
        return ChatResult(content=message.get("content", "") or "", tool_calls=calls)

    def chat_json(self, messages: list[dict]) -> str:
        response = self.client.chat(
            model=self.model,
            messages=messages,
            format="json",
            options={"num_ctx": self.num_ctx},
        )
        return response["message"].get("content", "") or ""


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    converted = []
    for t in tools:
        fn = t["function"]
        converted.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """Translates the executor's shared OpenAI/Ollama-shaped message list
    (assistant messages carrying a "tool_calls" list, tool results as
    separate {"role": "tool", ...} entries) into Anthropic's Messages API
    shape (a top-level system string, plus tool_use/tool_result content
    blocks). This is the only place that needs to know about that shape —
    planner.py/executor.py stay provider-agnostic.
    """
    system = ""
    result: list[dict] = []
    pending_tool_ids: list[str] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            result.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system = msg.get("content", "") or ""
        elif role == "user":
            flush_tool_results()
            result.append({"role": "user", "content": msg.get("content", "") or ""})
        elif role == "assistant":
            flush_tool_results()
            content: list[dict] = []
            text = msg.get("content") or ""
            if text:
                content.append({"type": "text", "text": text})
            pending_tool_ids = []
            for i, tc in enumerate(msg.get("tool_calls") or []):
                tool_id = f"toolu_{len(result)}_{i}"
                pending_tool_ids.append(tool_id)
                content.append(
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tc["function"]["name"],
                        "input": tc["function"]["arguments"] or {},
                    }
                )
            if not content:
                content = [{"type": "text", "text": "(continuing)"}]
            result.append({"role": "assistant", "content": content})
        elif role == "tool":
            if pending_tool_ids:
                tool_id = pending_tool_ids.pop(0)
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(msg.get("content", "")),
                    }
                )
    flush_tool_results()
    return system, result


def _thinking_kwargs(model: str) -> dict:
    # Haiku 4.5 doesn't support adaptive thinking or the effort parameter at
    # all; every other current Claude model does, and explicitly disabling
    # thinking on Opus-tier models has a documented failure mode (tool calls
    # occasionally leak into visible text instead of tool_use blocks), so
    # thinking is on unless it can't be.
    if "haiku" in model:
        return {}
    # Effort defaults to "high" when omitted - expensive for what is mostly
    # routine per-step tool-call decisions (click this, fill that), not hard
    # reasoning. "medium" is Anthropic's own published middle ground for
    # routine/agentic work: holds accuracy close to default at a real cost
    # cut. Pinned constant (not varied per request) since changing effort
    # between requests invalidates the messages cache.
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}


def _cached_system(system: str) -> str | list[dict]:
    # Render order is tools -> system -> messages, so a breakpoint on the
    # last (only) system block caches the tool schemas together with it -
    # the static, per-case-frozen part of every request in this loop.
    if not system:
        return system
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


class ClaudeClient:
    """Anthropic Claude client matching OllamaClient's interface. `api_key`
    is an optional explicit override (e.g. entered in the web UI and kept
    only in the server process's memory - never written to RunConfig/
    report.json); when omitted, the SDK resolves credentials from the
    process's own environment (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
    `ant auth login` profile) as usual. `workspace_id` is only needed for an
    organization-level API key that isn't scoped to one workspace - the API
    then rejects requests with a 400 unless every request carries an
    `anthropic-workspace-id` header naming which workspace to use.
    """

    def __init__(self, model: str, api_key: str = "", workspace_id: str = "", max_tokens: int = 8192):
        default_headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        self.client = anthropic.Anthropic(api_key=api_key or None, default_headers=default_headers)
        self.model = model
        self.max_tokens = max_tokens

    def chat_with_tools(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        system, anthropic_messages = _to_anthropic_messages(messages)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_cached_system(system),
            messages=anthropic_messages,
            tools=_to_anthropic_tools(tools),
            # Every turn of this tool-calling loop resends the whole growing
            # conversation - system prompt, tool schemas, every prior turn -
            # so an uncached loop's cost grows roughly with the square of
            # turn count. The explicit breakpoint on the (static, per-case)
            # system+tools prefix guarantees that part is always a cheap
            # cache read from turn 2 onward; this top-level marker adds
            # automatic caching for the growing messages tail on top of it.
            cache_control={"type": "ephemeral"},
            **_thinking_kwargs(self.model),
        )
        content_text = []
        calls = []
        for block in response.content:
            if block.type == "text":
                content_text.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(name=block.name, args=dict(block.input or {})))
        return ChatResult(content="".join(content_text), tool_calls=calls)

    def chat_json(self, messages: list[dict]) -> str:
        system, anthropic_messages = _to_anthropic_messages(messages)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=_cached_system(system),
            messages=anthropic_messages,
            cache_control={"type": "ephemeral"},
            **_thinking_kwargs(self.model),
        )
        return "".join(b.text for b in response.content if b.type == "text")


def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """Translates the shared internal message shape into the OpenAI
    chat-completions format. Much closer to the internal shape than
    Anthropic's - same roles, same flat one-tool-result-per-message layout -
    the only gaps are per-call ids (OpenAI needs a "tool_call_id" on each
    tool-result message) and JSON-string- vs dict-encoded call arguments.
    """
    result: list[dict] = []
    pending_tool_ids: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role in ("system", "user"):
            result.append({"role": role, "content": msg.get("content", "") or ""})
        elif role == "assistant":
            entry: dict = {"role": "assistant", "content": msg.get("content") or None}
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                pending_tool_ids = []
                oa_calls = []
                for i, tc in enumerate(tool_calls):
                    call_id = f"call_{len(result)}_{i}"
                    pending_tool_ids.append(call_id)
                    oa_calls.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": json.dumps(tc["function"]["arguments"] or {}),
                            },
                        }
                    )
                entry["tool_calls"] = oa_calls
            result.append(entry)
        elif role == "tool":
            tool_call_id = pending_tool_ids.pop(0) if pending_tool_ids else ""
            result.append(
                {"role": "tool", "tool_call_id": tool_call_id, "content": str(msg.get("content", ""))}
            )
    return result


class OpenAIClient:
    """OpenAI-compatible chat-completions client - real OpenAI, or any
    endpoint that speaks the same wire format (Azure OpenAI, OpenRouter,
    Groq, Together, a local llama.cpp/vLLM/LM Studio server, ...) via a
    configurable base_url. Like ClaudeClient, `api_key` is an optional
    explicit override kept only in the server process's memory - never
    written to RunConfig/report.json; when omitted, the SDK resolves
    OPENAI_API_KEY from the process's own environment as usual. `base_url`
    itself is not a secret and is a normal (optional) RunConfig field.
    """

    def __init__(self, model: str, base_url: str = "", api_key: str = "", max_tokens: int = 8192):
        self.client = openai.OpenAI(base_url=base_url or None, api_key=api_key or None)
        self.model = model
        self.max_tokens = max_tokens

    def chat_with_tools(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=_to_openai_messages(messages),
            tools=tools,
        )
        message = response.choices[0].message
        calls = [
            ToolCall(name=tc.function.name, args=json.loads(tc.function.arguments or "{}"))
            for tc in (message.tool_calls or [])
        ]
        return ChatResult(content=message.content or "", tool_calls=calls)

    def chat_json(self, messages: list[dict]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=_to_openai_messages(messages),
        )
        return response.choices[0].message.content or ""
