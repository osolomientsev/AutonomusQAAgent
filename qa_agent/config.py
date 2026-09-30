from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RunConfig:
    url: str
    provider: str = "ollama"  # "ollama", "claude", or "openai"
    model: str = "hf.co/richardyoung/Qwen3-14B-abliterated-GGUF:Q4_K_M"
    ollama_host: str = "http://localhost:11434"
    openai_base_url: str = ""  # empty = https://api.openai.com/v1; or any OpenAI-compatible endpoint
    headless: bool = True
    channel: str = "chrome"
    max_cases: int = 6
    max_steps_per_case: int = 10
    num_ctx: int = 16384
    auto_confirm_destructive: bool = False
    focus_hint: str = ""
    precondition_goal: str = ""
    planner_system_prompt: str = ""
    executor_system_prompt: str = ""
    fresh_browser_per_case: bool = False
    suite_id: str = ""
    output_dir: str = "reports"
    nav_timeout_ms: int = 30_000
    action_timeout_ms: int = 10_000
