"""
cli/theme.py  –  CodePilot visual constants
"""

from __future__ import annotations

# ── App identity ───────────────────────────────────────────────────────────────
APP_NAME    = "CodePilot"
APP_VERSION = "v0.9.9"

# ── Orange gradient stops (dark → bright) ─────────────────────────────────────
GRADIENT = [
    "#C94400",
    "#D95000",
    "#E86000",
    "#F47120",
    "#FF8533",
    "#FF9A50",
    "#FFAE70",
]

# ── Providers and their models ─────────────────────────────────────────────────
PROVIDERS: dict[str, list[str]] = {
    "OpenAI": [
        "gpt-5.4-mini",
        "gpt-5-mini",
        "gpt-5.3-codex",
        "gpt-5.4",
        "gpt-5.5",
    ],
    "Anthropic": [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
    ],
    "Deepseek": [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ],
    "Alibaba": [
        "qwen3-coder-plus",
        "qwen3-coder-next",
        "qwen3-coder-flash",
        "qwen3.6-plus",
    ],
}

# provider name keyed by model name for quick lookup
MODEL_TO_PROVIDER: dict[str, str] = {
    model: provider
    for provider, models in PROVIDERS.items()
    for model in models
}

# flat list for iteration
ALL_MODELS: list[str] = [m for models in PROVIDERS.values() for m in models]

DEFAULT_MODEL    = "deepseek-v4-flash"
DEFAULT_PROVIDER = "Deepseek"

# ── Provider → yaml provider string ───────────────────────────────────────────
PROVIDER_YAML_NAME: dict[str, str] = {
    "OpenAI":    "openai",
    "Anthropic": "anthropic",
    "Deepseek":  "deepseek",
    "Alibaba":   "alibaba",
}

# ── Model context window sizes (tokens) ───────────────────────────────────────
# Used by /context command to render fill bars.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-5.4-mini":    128_000,
    "gpt-5-mini":      128_000,
    "gpt-5.3-codex":   200_000,
    "gpt-5.4":         128_000,
    "gpt-5.5":         200_000,
    # Anthropic
    "claude-haiku-4-5":  200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-opus-4-8":   200_000,
    # Deepseek
    "deepseek-v4-pro":   64_000,
    "deepseek-v4-flash": 64_000,
    # Alibaba
    "qwen3-coder-plus":  131_072,
    "qwen3-coder-next":  131_072,
    "qwen3-coder-flash": 131_072,
    "qwen3.6-plus":      131_072,
}

DEFAULT_CONTEXT_WINDOW = 128_000  # fallback for unknown models

# ── Slash commands ─────────────────────────────────────────────────────────────
SLASH_COMMANDS: dict[str, str] = {
    "/status":   "Show runtime & system status",
    "/context":  "Visual context window usage breakdown",
    "/export":   "Export full session to JSON",
    "/stat":     "Model token usage statistics",
    "/models":   "List & switch models",
    "/config":   "Edit configuration (agent.yaml)",
    "/session":  "Show session metadata",
    "/sessions": "Browse & resume sessions",
    "/reset":    "Clear current session",
    "/bash":     "Start an interactive bash sub-shell",
    "/shell":    "Start an interactive bash sub-shell",
    "/help":     "Show all commands",
    "/exit":     "Quit CodePilot",
}


def gradient_text(text: str) -> str:
    """Return Rich markup string with per-char orange gradient colouring."""
    stops = GRADIENT
    n = len(stops)
    chars = list(text)
    out = []
    for i, ch in enumerate(chars):
        colour = stops[int(i / max(len(chars) - 1, 1) * (n - 1))]
        out.append(f"[bold {colour}]{ch}[/bold {colour}]")
    return "".join(out)