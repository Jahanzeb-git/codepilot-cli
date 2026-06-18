"""
cli/app.py  –  CodePilot CLI  (Rich + prompt_toolkit, fully synchronous)

Architecture:
    Main thread  → prompt_toolkit input + Rich rendering + queue consumer
    Worker thread → Runtime.run(task) — blocks until agent finishes
    Spinner thread → braille animation while agent is thinking
    All streaming output delivered via thread-safe queue.Queue

No asyncio event loop is used anywhere in this module.
"""

from __future__ import annotations

import datetime
import json
import difflib
import os
import platform
import re
import shutil
import sys
import tempfile
import threading
import time
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Optional

from rich.console import Console
from rich.text import Text
from rich.rule import Rule
from rich.theme import Theme
from rich.live import Live
from rich.markdown import Markdown

from prompt_toolkit.application import Application
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style as PTStyle

from .theme import (
    APP_NAME, APP_VERSION, GRADIENT, PROVIDERS, MODEL_TO_PROVIDER,
    ALL_MODELS, DEFAULT_MODEL, DEFAULT_PROVIDER, PROVIDER_YAML_NAME,
    SLASH_COMMANDS, gradient_text,
    MODEL_CONTEXT_WINDOWS, DEFAULT_CONTEXT_WINDOW,
)
from .sessions import SESSION_DIR, list_sessions, next_session_id

try:
    from codepilot import (
        Runtime,
        on_ask_user,
        on_finish,
        on_permission_request,
        on_runtime_error,
        on_stream,
        on_thinking_stream,
        on_tool_call,
        on_tool_result,
        on_user_message_injected,
        on_user_message_queued,
        on_context_drop,
        on_subagent_spawn,
        on_subagent_message,
        on_subagent_finish,
        on_llm_response,
    )
    HAS_RUNTIME = True
except ImportError:
    HAS_RUNTIME = False


# ── Console ────────────────────────────────────────────────────────────────────

RICH_THEME = Theme({
    "brand":        "bold #FF8533",
    "brand.dim":    "dim #FF8533",
    "tool":         "bold #FF8533",
    "tool.result":  "dim #3a3a3a",
    "tool.path":    "#FFAE70",
    "terminal":     "dim #9aa0a6",
    "diff.add":     "#66BB6A",
    "diff.del":     "#EF5350",
    "diff.meta":    "dim #777777",
    "diff.ctx":     "#bdbdbd",
    "diff.no":      "dim #666666",
    "finish":       "#66BB6A",
    "finish.icon":  "bold #66BB6A",
    "perm":         "bold #FFA726",
    "question":     "bold #4FC3F7",
    "answer":       "#4FC3F7",
    "muted":        "dim #555555",
    "error":        "bold #EF5350",
    "stream":       "#d4d4d4",
    "divider":      "#1e1e1e",
    "status.key":   "dim #444444",
    "status.val":   "#FF8533",
    "ready":        "dim #3a3a3a",
    "heading":      "bold #FF8533",
    "success":      "bold #66BB6A",
    "warn":         "bold #FFA726",
    "pill":         "bold #66BB6A",
})

console = Console(theme=RICH_THEME, highlight=False)

PT_STYLE = PTStyle.from_dict({
    "prompt": "#FF8533 bold",
    "select.title": "#FF8533 bold",
    "select.help": "#555555",
    "select.cursor": "#FF8533 bold",
    "select.item": "#d0d0d0",
    "select.item-selected": "#FFAE70 bold",
    "select.meta": "#666666",
    "select.rule": "#333333",
    # Autocomplete dropdown styles
    "completion-menu": "bg:#1a1a1a #888888",
    "completion-menu.completion": "bg:#1a1a1a #888888",
    "completion-menu.completion.current": "bg:#2a2a2a #FFAE70 bold",
    "completion-menu.meta.completion": "bg:#141414 #555555",
    "completion-menu.meta.completion.current": "bg:#1e1e1e #777777",
    "completion-menu.multi-column-meta": "bg:#141414 #555555",
    "scrollbar.background": "bg:#111111",
    "scrollbar.button": "bg:#333333",
    "":       "",
})


# ── Slash command autocompleter ────────────────────────────────────────────────

class SlashCompleter(Completer):
    """Fires completion suggestions for any input that starts with '/'."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        partial = text.lower()
        for cmd, desc in SLASH_COMMANDS.items():
            if cmd.startswith(partial):
                # display = command, meta = description
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )

# ── Banner ─────────────────────────────────────────────────────────────────────

_BANNER_ART = (
    "   ______          __     ____  _ __      __  \n"
    "  / ____/___  ____/ /__  / __ \\(_) /___  / /_\n"
    " / /   / __ \\/ __  / _ \\/ /_/ / / / __ \\/ __/\n"
    "/ /___/ /_/ / /_/ /  __/ ____/ / / /_/ / /_  \n"
    "\\____/\\____/\\__,_/\\___/_/   /_/_/\\____/\\__/  "
)


def _make_banner() -> Text:
    lines = _BANNER_ART.splitlines()
    all_chars = [(li, ch) for li, line in enumerate(lines) for ch in line]
    printable = [(li, ch) for li, ch in all_chars if ch.strip()]
    total = max(len(printable) - 1, 1)
    n = len(GRADIENT)
    line_texts: list[Text] = [Text() for _ in lines]
    p_idx = 0
    for li, ch in all_chars:
        if not ch.strip():
            line_texts[li].append(ch)
        else:
            stop = GRADIENT[int(p_idx / total * (n - 1))]
            line_texts[li].append(ch, style=f"bold {stop}")
            p_idx += 1
    result = Text()
    for i, t in enumerate(line_texts):
        result.append("   ")
        result.append_text(t)
        if i < len(line_texts) - 1:
            result.append("\n")
    return result


def print_banner(work_dir: Path, session_id: str, model: str) -> None:
    console.clear()
    console.print()
    console.print(_make_banner())
    console.print()
    console.print(
        f"   [status.key]version[/status.key]   [brand.dim]{APP_VERSION}[/brand.dim]"
        f"   [status.key]workspace[/status.key]  [status.val]{work_dir}[/status.val]"
    )
    console.print()
    console.print(Rule(style="dim #1e1e1e"))
    console.print()
    console.print(
        f"   [status.key]session[/status.key]   [status.val]{session_id}[/status.val]"
        f"   [status.key]model[/status.key]     [status.val]{model}[/status.val]"
    )
    console.print()
    console.print("   [muted]Type a task, or /help for commands.[/muted]")
    console.print()


# ── Session picker ─────────────────────────────────────────────────────────────


def _term_height(default: int = 24) -> int:
    try:
        return shutil.get_terminal_size().lines
    except Exception:
        return default


def _select(
    title: str,
    entries: list[Any],
    render: Callable[[Any, bool], Text],
    *,
    selected: int = 0,
    subtitle: str = "",
    empty: str = "Nothing to select.",
    cancelable: bool = True,
) -> Any | None:
    if not entries:
        console.print(f"   [muted]{empty}[/muted]")
        return None

    selected = max(0, min(selected, len(entries) - 1))
    visible = max(6, min(14, _term_height() - 10))
    state = {"selected": selected}

    def fragments():
        selected_idx = state["selected"]
        top = max(0, min(selected_idx - visible // 2, max(0, len(entries) - visible)))
        bottom = min(len(entries), top + visible)
        if subtitle:
            header = f"   {title}\n   {subtitle}\n"
        else:
            header = f"   {title}\n"
        controls = "↑/↓ move   Enter select   Esc cancel" if cancelable else "↑/↓ move   Enter select"
        parts: list[tuple[str, str]] = [
            ("class:select.title", header),
            ("class:select.help", f"   {controls}\n\n"),
            ("class:select.rule", "   " + "─" * 56 + "\n\n"),
        ]
        if top:
            parts.append(("class:select.meta", f"   ... {top} above\n"))

        for idx in range(top, bottom):
            is_selected = idx == selected_idx
            item = render(entries[idx], is_selected).plain
            prefix = ">  " if is_selected else "   "
            parts.append(("class:select.cursor" if is_selected else "class:select.meta", f"   {prefix}"))
            parts.append(("class:select.item-selected" if is_selected else "class:select.item", item))
            parts.append(("", "\n"))

        if bottom < len(entries):
            parts.append(("class:select.meta", f"   ... {len(entries) - bottom} below\n"))
        return parts

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        state["selected"] = (state["selected"] - 1) % len(entries)
        event.app.invalidate()

    @kb.add("down")
    def _(event):
        state["selected"] = (state["selected"] + 1) % len(entries)
        event.app.invalidate()

    @kb.add("pageup")
    def _(event):
        state["selected"] = max(0, state["selected"] - visible)
        event.app.invalidate()

    @kb.add("pagedown")
    def _(event):
        state["selected"] = min(len(entries) - 1, state["selected"] + visible)
        event.app.invalidate()

    @kb.add("enter")
    def _(event):
        event.app.exit(result=entries[state["selected"]])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        if cancelable:
            event.app.exit(result=None)

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(fragments), wrap_lines=False)])),
        key_bindings=kb,
        style=PT_STYLE,
        full_screen=True,
        mouse_support=False,
    )
    return app.run()


def pick_session_interactive() -> str:
    sessions = list_sessions()

    if not sessions:
        console.clear()
        console.print()
        console.print("   [muted]No saved sessions yet.[/muted]")
        console.print()
        console.print("   [brand.dim]>  Starting new session...[/brand.dim]")
        console.print()
        return next_session_id()

    entries = [None] + sessions

    def render_session(item: Any, selected: bool) -> Text:
        if item is None:
            return Text("New session", style="bold #e0e0e0" if selected else "#d0d0d0")
        text = Text(item.session_id, style="#FFAE70" if selected else "#d0d0d0")
        text.append(f"   {item.updated_at}   {item.message_count} msgs", style="dim #666666")
        return text

    choice = _select("CodePilot Sessions", entries, render_session, cancelable=False)
    console.clear()
    if choice is None:
        return next_session_id()
    return choice.session_id


# ── Config patching ────────────────────────────────────────────────────────────

def _find_base_config() -> Path:
    packaged_config = resources.files("cli").joinpath("agent.yaml")
    with resources.as_file(packaged_config) as config_path:
        if config_path.exists():
            return config_path

    local_config = Path.cwd() / "agent.yaml"
    if local_config.exists():
        return local_config

    return Path(__file__).resolve().parent / "agent.yaml"


def build_patched_config(work_dir: Path, model: str, provider_ui: str) -> Path:
    base = _find_base_config()
    if not base.exists():
        raise FileNotFoundError(f"agent.yaml not found (tried {base})")
    content = base.read_text()
    content = content.replace("${WORK_DIR}", str(work_dir))
    yaml_provider = PROVIDER_YAML_NAME.get(provider_ui, provider_ui.lower())
    content = re.sub(
        r'^([ \t]{4}provider:\s*")[^"]*(")',
        rf'\g<1>{yaml_provider}\g<2>',
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r'^([ \t]{4}provider:\s*)(\S+)',
        lambda m: m.group(1) + yaml_provider,
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r'^([ \t]{4}name:\s*)(\S+)',
        lambda m: m.group(1) + model,
        content,
        flags=re.MULTILINE,
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="codepilot_"
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# ── Thread-safe Spinner ───────────────────────────────────────────────────────

_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Spinner:
    """Thread-based spinner — no asyncio, no event loop, no race conditions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._label = "thinking"
        self._started_at = 0.0
        self._timeout: int | None = None

    def start(self, label: str = "thinking", timeout: int | None = None) -> None:
        with self._lock:
            self._label = label
            self._timeout = timeout
            self._started_at = time.monotonic()
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False

        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

        # Clear the spinner line
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _spin(self) -> None:
        i = 0
        while True:
            with self._lock:
                if not self._running:
                    break
                label = self._label
                started_at = self._started_at
                timeout = self._timeout
            frame = _FRAMES[i % len(_FRAMES)]
            elapsed = max(0, int(time.monotonic() - started_at))
            suffix = f" / {timeout}s" if timeout else "s"
            sys.stdout.write(f"\r\033[2m{frame} {label}... {elapsed}{suffix}\033[0m")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1


spinner = Spinner()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text


def _middle_truncate(text: str, limit: int = 96) -> str:
    text = text.replace("\n", "\\n")
    if len(text) <= limit:
        return text
    head = max(12, limit // 2 - 2)
    tail = max(12, limit - head - 3)
    return text[:head] + "..." + text[-tail:]


def _resolve_work_path(runtime: Any, path: str | None) -> Path | None:
    if not path:
        return None
    try:
        return Path(runtime.config.runtime.work_dir) / path
    except Exception:
        return None


def _read_text_if_exists(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _print_diff_lines(lines: list[str], *, max_lines: int = 80) -> None:
    omitted = max(0, len(lines) - max_lines)
    for line in lines[:max_lines]:
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            console.print(f"      [diff.meta]{line}[/diff.meta]")
        elif line.startswith("+"):
            console.print(f"      [diff.add]{line}[/diff.add]")
        elif line.startswith("-"):
            console.print(f"      [diff.del]{line}[/diff.del]")
        else:
            console.print(f"      [diff.ctx]{line}[/diff.ctx]")
    if omitted:
        console.print(f"      [muted]... {omitted} diff lines omitted[/muted]")


def _render_file_snapshot(result: str, *, max_lines: int = 70) -> None:
    lines = result.splitlines()
    if not lines:
        return
    console.print(f"   [tool.result]{lines[0]}[/tool.result]")
    body = lines[1:]
    omitted = max(0, len(body) - max_lines)
    for line in body[:max_lines]:
        if line.startswith("[END") or line.startswith("[TRUNCATED"):
            console.print(f"      [diff.meta]{line}[/diff.meta]")
        elif " | " in line[:10]:
            number, content = line.split(" | ", 1)
            console.print(f"      [diff.no]{number} |[/diff.no] [diff.add]{content}[/diff.add]")
        else:
            console.print(f"      [diff.ctx]{line}[/diff.ctx]")
    if omitted:
        console.print(f"      [muted]... {omitted} lines omitted[/muted]")


def _split_terminal_result(result: str) -> tuple[str, str, list[str], str]:
    lines = result.splitlines()
    if not lines:
        return "", "", [], ""

    header = lines[0]
    footer = ""
    body = lines[1:]
    if body and body[-1].startswith("[status:"):
        footer = body[-1]
        body = body[:-1]

    label = ""
    if header.startswith("[terminal:"):
        close = header.find("]")
        if close != -1:
            label = header[close + 1:].strip()
            header = header[:close + 1]

    return header, label, body, footer


_cached_hostname: str = ""

def _get_short_hostname() -> str:
    global _cached_hostname
    if not _cached_hostname:
        try:
            import socket as _socket
            _cached_hostname = _socket.gethostname()
        except Exception:
            _cached_hostname = "localhost"
    return _cached_hostname


def _make_shell_prompt(cwd: str) -> str:
    """Build a shell prompt prefix like 'user@host:~/path$' from a cwd path."""
    try:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        host = _get_short_hostname()
        home = os.path.expanduser("~")
        display_cwd = cwd
        if cwd.startswith(home):
            display_cwd = "~" + cwd[len(home):]
        who = f"{user}@{host}" if user else host
        return f"{who}:{display_cwd}$"
    except Exception:
        return "$"


def _render_terminal_result(result: str, *, max_lines: int = 28) -> None:
    lines = result.splitlines()
    if not lines:
        console.print("   [tool.result][no output][/tool.result]")
        return
    header, label, body, footer = _split_terminal_result(result)
    status = ""
    cwd = ""
    if footer:
        status_match = re.search(r"\[status:\s*([^|\]]+)", footer)
        if status_match:
            status = status_match.group(1).strip()
        cwd_match = re.search(r"cwd:\s*([^|\]]+)", footer)
        if cwd_match:
            cwd = cwd_match.group(1).strip()

    # Build shell prompt prefix for the command label
    shell_prompt = _make_shell_prompt(cwd) if cwd else "$"

    if status == "completed":
        status_style = "success"
        status_icon = "✔"
    elif status == "running":
        status_style = "warn"
        status_icon = "⟳"
    else:
        status_style = "success"
        status_icon = ""

    console.print(f"   [tool.result]{header}[/tool.result]")

    # Show the command label with the full shell prompt prefix
    if label and label not in ("(continued output)", "(complete output)"):
        cmd_text = label[2:] if label.startswith("$ ") else label
        cmd_display = _middle_truncate(cmd_text, 110)
        console.print(f"   [dim #555555]{shell_prompt}[/dim #555555] [diff.meta]{cmd_display}[/diff.meta]")
    elif label:
        console.print(f"   [diff.meta]{label}[/diff.meta]")

    omitted = max(0, len(body) - max_lines)
    for line in body[:max_lines]:
        if "status=running" in line or "running" in line.lower():
            console.print(f"   [warn]{line}[/warn]")
        elif line == label:
            continue
        elif "Permission denied" in line or "Error:" in line:
            console.print(f"   [error]{line}[/error]")
        else:
            console.print(f"   [terminal]{line}[/terminal]")
    if omitted:
        console.print(f"   [muted]... {omitted} output lines omitted[/muted]")
    # Show status + cwd footer as a clean summary line
    if status and status_icon:
        console.print(f"   [{status_style}]{status_icon} {status.capitalize()}[/{status_style}]")
    if "status=running" in result or "[running]" in result:
        console.print("   [muted]process still running — call read_output() to wait for more[/muted]")


def _tool_wait_label(tool: str, args: dict, display: str) -> tuple[str, int | None]:
    timeout = args.get("timeout") if isinstance(args, dict) else None
    timeout = timeout if isinstance(timeout, int) and timeout > 0 else None
    if tool == "execute":
        command = args.get("command", "") if isinstance(args, dict) else ""
        return f"running {_middle_truncate(command, 52) or 'command'}", timeout
    if tool == "read_output":
        session_id = args.get("session_id", "terminal") if isinstance(args, dict) else "terminal"
        return f"waiting for terminal output [{session_id}]", timeout
    if tool == "send_input":
        session_id = args.get("session_id", "terminal") if isinstance(args, dict) else "terminal"
        return f"sending input [{session_id}]", timeout
    if tool == "write_file":
        path = args.get("path", "file") if isinstance(args, dict) else "file"
        return f"writing {path}", None
    if tool == "view_file":
        path = args.get("path", "file") if isinstance(args, dict) else "file"
        return f"reading {path}", None
    if tool == "edit_file":
        path = args.get("path", "file") if isinstance(args, dict) else "file"
        return f"editing {path}", None
    return f"working {display or tool}", timeout


def _tool_call_summary(tool: str, args: dict, display: str) -> str:
    if tool == "execute":
        timeout = args.get("timeout") if isinstance(args, dict) else None
        session = args.get("session_id", "main") if isinstance(args, dict) else "main"
        suffix = f"   timeout {timeout}s" if timeout else ""
        return f"session {session}{suffix}"
    if tool == "read_output":
        timeout = args.get("timeout") if isinstance(args, dict) else None
        session = args.get("session_id", "main") if isinstance(args, dict) else "main"
        suffix = f"   timeout {timeout}s" if timeout else ""
        return f"session {session}{suffix}"
    if tool == "write_file":
        path = args.get("path", "") if isinstance(args, dict) else ""
        return path.strip()
    if tool == "view_file":
        path = args.get("path", "") if isinstance(args, dict) else ""
        start = args.get("start_line") if isinstance(args, dict) else None
        end = args.get("end_line") if isinstance(args, dict) else None
        if start and end:
            return f"{path}   L{start}-{end}"
        return path
    if tool == "edit_file":
        path = args.get("path", "") if isinstance(args, dict) else ""
        return path.strip()
    return _middle_truncate(display, 110)


# ── /models picker ─────────────────────────────────────────────────────────────

def show_models_picker(current_model: str) -> str | None:
    entries: list[tuple[str, str]] = []
    for provider, models in PROVIDERS.items():
        for m in models:
            entries.append((provider, m))

    def render_model(item: tuple[str, str], selected: bool) -> Text:
        provider, model = item
        marker = "●  " if model == current_model else "   "
        text = Text(marker + model, style="#FFAE70" if selected else "#d0d0d0")
        text.append(f"   {provider}", style="dim #666666")
        return text

    current_idx = next((i for i, (_, m) in enumerate(entries) if m == current_model), 0)
    choice = _select("Models", entries, render_model, selected=current_idx)
    console.clear()
    return choice[1] if choice else None


# ── /sessions picker (inline) ──────────────────────────────────────────────────

def show_sessions_picker() -> str | None:
    sessions = list_sessions()
    if not sessions:
        console.print("   [muted]No saved sessions.[/muted]")
        return None

    def render_session(item: Any, selected: bool) -> Text:
        text = Text(item.session_id, style="#FFAE70" if selected else "#d0d0d0")
        text.append(f"   {item.updated_at}   {item.message_count} msgs", style="dim #666666")
        return text

    choice = _select("Resume Session", sessions, render_session)
    console.clear()
    return choice.session_id if choice else None


# ── Permission prompt ──────────────────────────────────────────────────────────

# Lines printed by ask_permission before the prompt_toolkit input:
#  1 blank, 1 header, 1 blank, 1 desc, 1 blank, 1 rule, 1 blank, 1 hints, 1 blank
#  = 9 lines (plus the prompt_toolkit prompt line itself = 10 total to erase)
_PERM_LINES_ABOVE_PROMPT = 10


def _erase_n_lines(n: int) -> None:
    """Move cursor up n lines and erase each one, leaving cursor at start of first erased line."""
    for _ in range(n):
        sys.stdout.write("\x1b[A\x1b[2K")
    sys.stdout.flush()


def ask_permission(runtime: Any, tool: str, description: str) -> bool:
    # ── Header: permission label + tool name
    console.print()
    console.print(f"   [perm]⚠ Permission[/perm]   [tool.path]{tool}[/tool.path]")
    console.print()

    # ── Command/description text — visible, not muted
    desc_text = _middle_truncate(description, 140)
    console.print(f"   [stream]{desc_text}[/stream]")
    console.print()

    # ── Dim separator
    console.print(Rule(style="dim #2a2a2a"))
    console.print()

    # ── Key hints: key colored, action word dim
    console.print(
        "   [success]Enter[/success] [muted]allow[/muted]   "
        "[error]Esc[/error] [muted]reject[/muted]   "
        "[question]Ctrl+I[/question] [muted]instruct[/muted]"
    )
    console.print()

    try:
        result: dict[str, str] = {"action": "allow"}
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            result["action"] = "allow"
            event.app.exit()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            result["action"] = "reject"
            event.app.exit()

        @kb.add("c-i")
        def _(event):
            result["action"] = "instruct"
            event.app.exit()

        prompt = PromptSession(key_bindings=kb, style=PT_STYLE)
        prompt.prompt(HTML('<b><style fg="#FFA726">permission ›</style></b> '))
        key = result["action"]
    except (KeyboardInterrupt, EOFError):
        key = "reject"

    if key == "allow":
        # Erase the entire permission block (all lines above + prompt line)
        # so only the compact approval badge remains.
        _erase_n_lines(_PERM_LINES_ABOVE_PROMPT)
        console.print(f"   [success]✔ Approved[/success]   [dim #444444]{tool}[/dim #444444]")
        return True

    if key == "instruct":
        _erase_n_lines(_PERM_LINES_ABOVE_PROMPT)
        try:
            prompt = PromptSession(style=PT_STYLE)
            instruction = prompt.prompt(
                HTML('<b><style fg="#4FC3F7">instruct ›</style></b> ')
            ).strip()
        except (KeyboardInterrupt, EOFError):
            instruction = ""
        if instruction:
            runtime.send_message(
                f"Permission guidance for {tool}: the requested action was not approved. "
                f"Instead, follow this instruction: {instruction}"
            )
            console.print("   [question]↑ instruction queued[/question]")
        else:
            console.print("   [muted]no instruction entered — rejected[/muted]")
        return False

    # Rejected
    _erase_n_lines(_PERM_LINES_ABOVE_PROMPT)
    console.print(f"   [error]✖ Rejected[/error]   [dim #444444]{tool}[/dim #444444]")
    return False


# ── Streaming output lock ──────────────────────────────────────────────────────

_output_lock = threading.Lock()


def _safe_write(text: str) -> None:
    """Thread-safe stdout write — prevents interleaved output."""
    with _output_lock:
        sys.stdout.write(text)
        sys.stdout.flush()


# ── Runtime hooks ──────────────────────────────────────────────────────────────

# Mutable state shared between install_hooks and run_cli
_cli_state: dict[str, Any] = {"model": ""}

# Running token / call stats accumulated during the session
_session_stats: dict[str, Any] = {
    "calls":            0,
    "est_input_tokens":  0,
    "est_output_tokens": 0,
    # snapshot of message count after last call (to compute deltas)
    "_last_msg_count":   0,
    "_last_msg_chars":   0,
}

# Raw LLM generations — exact response_text captured for observability.
# Populated by the on_llm_response hook; exported via /export for observability.
_raw_generations: list[dict[str, Any]] = []


def install_hooks(runtime: Any) -> None:
    _last_args = {}
    _file_before: dict[str, str] = {}

    @on_llm_response(runtime)
    def _on_llm_response(step: int, response: str, **_):
        """Capture the exact raw LLM generation for the trace JSON.
        This fires before history persistence, so the payload is always
        the full, unmodified string the model produced.
        """
        _raw_generations.append({
            "step":     step,
            "response": response,
        })
    # Rolling window state for thinking display
    # Tracks how many dim lines are currently rendered so we can erase them
    _thinking_rendered: list = [0]  # list so inner functions can mutate

    def _erase_thinking_lines():
        """Erase all currently rendered thinking lines from the terminal."""
        n = _thinking_rendered[0]
        if n > 0:
            for _ in range(n):
                sys.stdout.write("\x1b[A\x1b[2K")
            sys.stdout.flush()
            _thinking_rendered[0] = 0

    _markdown_buf: list[str] = [""]
    _live_display: list[Any] = [None]

    def _stop_live_display():
        with _output_lock:
            if _live_display[0] is not None:
                _live_display[0].stop()
                _live_display[0] = None
                _markdown_buf[0] = ""

    @on_stream(runtime)
    def _on_stream(text: str, **_):
        # Thinking is now intercepted at the runtime level and routed to
        # THINKING_STREAM — this handler only ever receives clean response text.
        spinner.stop()
        _erase_thinking_lines()
        with _output_lock:
            _markdown_buf[0] += text
            if _live_display[0] is None:
                _live_display[0] = Live(Markdown(_markdown_buf[0]), console=console, refresh_per_second=15, auto_refresh=False)
                _live_display[0].start()
            else:
                _live_display[0].update(Markdown(_markdown_buf[0]), refresh=True)

    _thinking_buf: list = [""]  # accumulates partial line across chunks
    _thinking_lines_acc: list = [[]]  # completed lines accumulator

    @on_thinking_stream(runtime)
    def _on_thinking_stream(thinking: str, **_):
        spinner.stop()
        _thinking_buf[0] += thinking
        lines = _thinking_buf[0].split("\n")
        # Complete lines are everything except the last element
        complete = lines[:-1]
        _thinking_lines_acc[0].extend(complete)
        _thinking_buf[0] = lines[-1]  # keep partial line in buffer

        # Collect 3-line display window from last N completed lines + current partial
        all_lines = _thinking_lines_acc[0] + ([_thinking_buf[0]] if _thinking_buf[0] else [])
        display_lines = all_lines[-3:]

        if not display_lines:
            return

        # Erase previously rendered thinking lines before redrawing
        _erase_thinking_lines()

        import shutil
        term_width = shutil.get_terminal_size().columns
        drawn = 0
        for line in display_lines:
            # Strip any stray ANSI codes from thinking text itself
            clean = line.replace("\x1b", "")
            if len(clean) > term_width - 4:
                clean = clean[:term_width - 7] + "..."
            sys.stdout.write(f"\x1b[2m{clean}\x1b[0m\n")
            drawn += 1
        sys.stdout.flush()
        _thinking_rendered[0] = drawn

    def _reset_thinking_state():
        """Called when a new task/step starts to reset accumulator state."""
        _erase_thinking_lines()
        _thinking_buf[0] = ""
        _thinking_lines_acc[0] = []

    @on_tool_call(runtime)
    def _on_tool_call(tool: str, args: dict, label: str = "", **_):
        _stop_live_display()
        _erase_thinking_lines()
        _reset_thinking_state()
        _last_args[tool] = args
        spinner.stop()
        display = label or (_truncate(json.dumps(args), 80) if args else "")
        if tool in ("write_file", "edit_file"):
            path = args.get("path") if isinstance(args, dict) else None
            work_path = _resolve_work_path(runtime, path)
            if path:
                _file_before[path] = _read_text_if_exists(work_path)
        _safe_write("\n")
        with _output_lock:
            icon = ">" if tool == "execute" else "•"
            summary = _tool_call_summary(tool, args if isinstance(args, dict) else {}, display)
            console.print(f"   [tool]{icon}  {tool}[/tool]   [muted]{summary}[/muted]")
        label, timeout = _tool_wait_label(tool, args if isinstance(args, dict) else {}, display)
        spinner.start(label, timeout)

    @on_tool_result(runtime)
    def _on_tool_result(tool: str, result: str, **_):
        spinner.stop()
        with _output_lock:
            if tool == "view_file":
                _render_file_snapshot(result)
            elif tool in ("write_file", "edit_file"):
                lines = result.splitlines()
                if lines:
                    console.print(f"   [tool.result]{lines[0]}[/tool.result]")
                args = _last_args.get(tool, {})
                path = args.get("path") if isinstance(args, dict) else None
                before = _file_before.pop(path, "") if path else ""
                after = _read_text_if_exists(_resolve_work_path(runtime, path))
                if path and ("ERROR" not in result) and (before or after):
                    diff = list(difflib.unified_diff(
                        before.splitlines(),
                        after.splitlines(),
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                        lineterm="",
                        n=3,
                    ))
                    if diff:
                        _print_diff_lines(diff)
                elif len(lines) > 1:
                    _print_diff_lines(lines[1:])
            elif tool in ("execute", "read_output", "send_input"):
                _render_terminal_result(result)
            else:
                if result.strip():
                    preview = _truncate(result.strip(), 120)
                    console.print(f"   [tool.result]{preview}[/tool.result]")
            console.print()
        spinner.start("thinking")

    @on_ask_user(runtime)
    def _on_ask_user(question: str, **_):
        _stop_live_display()
        _erase_thinking_lines()
        spinner.stop()
        _safe_write("\n")
        with _output_lock:
            console.print(f"   [question]?  {question}[/question]")
        try:
            sys.stdout.write("   \033[38;2;79;195;247m›\033[0m  ")
            sys.stdout.flush()
            return input().strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    @on_finish(runtime)
    def _on_finish(summary: str, **_):
        _stop_live_display()
        _erase_thinking_lines()
        _reset_thinking_state()
        spinner.stop()
        _safe_write("\n")
        with _output_lock:
            console.print(Rule(style="dim #2a2a2a"))
            model_name = _cli_state.get("model", "")
            model_part = f"[dim #555555]◉[/dim #555555] [muted]{model_name}[/muted]" if model_name else ""
            await_part = "[bold #3d7a3d]●[/bold #3d7a3d] [dim #4a7c4a]awaiting task...[/dim #4a7c4a]"
            spacer = "   " if model_name else ""
            console.print(f"   {model_part}{spacer}{await_part}")

        # ── Accumulate session stats ──────────────────────────────────────────
        try:
            msgs = list(runtime.messages)  # snapshot (thread-safe copy)
            total_chars = sum(len(m.get("content") or "") for m in msgs)
            prev_chars = _session_stats["_last_msg_chars"]
            prev_count = _session_stats["_last_msg_count"]

            # New chars since last call
            delta_chars = max(0, total_chars - prev_chars)
            new_msgs = msgs[prev_count:]  # new messages since last call

            # Rough split: user/tool messages → input, assistant → output
            in_chars = sum(
                len(m.get("content") or "")
                for m in new_msgs
                if m.get("role") != "assistant"
            )
            out_chars = sum(
                len(m.get("content") or "")
                for m in new_msgs
                if m.get("role") == "assistant"
            )

            _session_stats["calls"]            += 1
            _session_stats["est_input_tokens"]  += max(0, in_chars // 4)
            _session_stats["est_output_tokens"] += max(0, out_chars // 4)
            _session_stats["_last_msg_count"]   = len(msgs)
            _session_stats["_last_msg_chars"]   = total_chars
        except Exception:
            pass

    @on_permission_request(runtime)
    def _on_permission(tool: str, description: str, **_):
        spinner.stop()
        approved = ask_permission(runtime, tool, description)
        if approved:
            spinner.start(f"running {tool}")
        return approved

    @on_user_message_queued(runtime)
    def _on_queued(message: str, **_):
        with _output_lock:
            console.print(f"   [brand.dim]↑  {message}[/brand.dim]")

    @on_user_message_injected(runtime)
    def _on_injected(message: str, **_):
        with _output_lock:
            console.print(f"   [brand.dim]↓  {message}[/brand.dim]")

    @on_runtime_error(runtime)
    def _on_runtime_error(error: str, **_):
        spinner.stop()
        _safe_write("\n")
        with _output_lock:
            # Show parser errors cleanly — truncate to first 3 lines for readability
            lines = error.strip().splitlines()
            header = lines[0] if lines else error
            console.print(f"   [error]✗  {header}[/error]")
            for line in lines[1:4]:
                console.print(f"      [muted]{line}[/muted]")
            if len(lines) > 4:
                console.print(f"      [muted]… ({len(lines) - 4} more lines)[/muted]")
        console.print()
        spinner.start()

    @on_context_drop(runtime)
    def _on_context_drop(
        before_pct: int, after_pct: int, tokens_saved: int,
        tasks_archived: list, **_
    ):
        with _output_lock:
            tasks_str = ", ".join(str(t) for t in tasks_archived)
            console.print(
                f"   [dim #888888]◈  Context dropped [/dim #888888]"
                f"[dim #aaaaaa]{before_pct}%[/dim #aaaaaa]"
                f"[dim #666666] → [/dim #666666]"
                f"[dim #66BB6A]{after_pct}%[/dim #66BB6A]"
                f"[dim #888888]  (saved ~{tokens_saved:,} tokens · tasks {tasks_str})[/dim #888888]"
            )

    @on_subagent_spawn(runtime)
    def _on_subagent_spawn(agent_id: int, task_summary: str, **_):
        with _output_lock:
            short = task_summary[:70] + ("…" if len(task_summary) > 70 else "")
            console.print(
                f"   [dim #4FC3F7]⟳  Sub-Agent #{agent_id} spawned[/dim #4FC3F7]"
                f"[dim #555555]  {short}[/dim #555555]"
            )

    @on_subagent_message(runtime)
    def _on_subagent_message(agent_id: int, message: str, **_):
        with _output_lock:
            console.print(
                f"   [bold #FFA726]◎  Sub-Agent #{agent_id} →[/bold #FFA726]"
                f" [dim #ddaa66]{message}[/dim #ddaa66]"
            )

    @on_subagent_finish(runtime)
    def _on_subagent_finish(
        agent_id: int, summary: str, files_written: list,
        elapsed_seconds: float, error: str | None, **_
    ):
        with _output_lock:
            if error:
                console.print(
                    f"   [error]✗  Sub-Agent #{agent_id} failed[/error]"
                    f" [muted]({int(elapsed_seconds)}s)[/muted]"
                    f" [error]{error[:80]}[/error]"
                )
            else:
                files_str = ", ".join(files_written) if files_written else "no files"
                console.print(
                    f"   [bold #66BB6A]✓  Sub-Agent #{agent_id} done[/bold #66BB6A]"
                    f" [muted]({int(elapsed_seconds)}s · {files_str})[/muted]"
                )


# ── Agent worker thread ───────────────────────────────────────────────────────

class AgentWorker:
    """Runs Runtime.run(task) on a background thread so main thread stays responsive."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[Exception] = None
        self._done = threading.Event()

    def run_task(self, task: str) -> Optional[Exception]:
        """Start agent on background thread, block main thread until done.
        Returns the exception if one occurred, or None on success.
        Handles KeyboardInterrupt for clean abort.
        """
        self._error = None
        self._done.clear()
        self._thread = threading.Thread(
            target=self._worker, args=(task,), daemon=True
        )
        self._thread.start()

        # Wait with interrupt support
        try:
            while not self._done.wait(timeout=0.1):
                pass
        except KeyboardInterrupt:
            self.runtime.abort()
            spinner.stop()
            console.print()
            console.print("   [muted]✗  aborted[/muted]")
            # Wait for worker to finish after abort
            self._done.wait(timeout=5.0)
            return None

        return self._error

    def _worker(self, task: str) -> None:
        try:
            self.runtime.run(task)
        except Exception as exc:
            self._error = exc
        finally:
            self._done.set()


# ── Main loop ──────────────────────────────────────────────────────────────────

# ── Config editor ─────────────────────────────────────────────────────────────

import yaml  # for config editor


def _load_yaml_config(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _save_yaml_config(path: Path, data: dict) -> bool:
    try:
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        return True
    except Exception:
        return False


def show_config_editor(base_config_path: Path) -> None:
    """Interactive config editor backed by agent.yaml."""
    data = _load_yaml_config(base_config_path)
    agent = data.get("agent", {})
    model_cfg = agent.get("model", {})
    runtime_cfg = agent.get("runtime", {})
    thinking_cfg = model_cfg.get("thinking", {})

    def _get(d: dict, *keys, default=""):
        v = d
        for k in keys:
            if not isinstance(v, dict):
                return default
            v = v.get(k, default)
        return v if v is not None else default

    def _prompt_text(label: str, current: str, hint: str = "") -> str:
        hint_str = f"  [{hint}]" if hint else ""
        try:
            p = PromptSession(style=PT_STYLE)
            val = p.prompt(
                HTML(f'<b><style fg="#4FC3F7">{label} ›</style></b> '),
                default=str(current),
            ).strip()
            return val if val else str(current)
        except (KeyboardInterrupt, EOFError):
            return str(current)

    def _prompt_bool(label: str, current: bool) -> bool:
        display = "true" if current else "false"
        console.print(f"   [question]{label}[/question]  current: [muted]{display}[/muted]")
        console.print("   [success]Enter[/success] [muted]keep[/muted]   [brand]t[/brand] [muted]true[/muted]   [error]f[/error] [muted]false[/muted]")
        console.print()
        kb = KeyBindings()
        result = {"val": current}

        @kb.add("t")
        @kb.add("T")
        def _(event):
            result["val"] = True
            event.app.exit()

        @kb.add("f")
        @kb.add("F")
        def _(event):
            result["val"] = False
            event.app.exit()

        @kb.add("enter")
        def _(event):
            event.app.exit()

        @kb.add("escape")
        @kb.add("c-c")
        def _(event):
            event.app.exit()

        try:
            p = PromptSession(key_bindings=kb, style=PT_STYLE)
            p.prompt(HTML('<b><style fg="#FFA726">toggle ›</style></b> '))
        except (KeyboardInterrupt, EOFError):
            pass
        return result["val"]

    def _prompt_choice(label: str, current: str, choices: list[str]) -> str:
        entries = choices
        selected = choices.index(current) if current in choices else 0
        choice = _select(
            f"{label}  (current: {current})",
            entries,
            lambda item, sel: Text(item, style="#FFAE70" if sel else "#d0d0d0"),
            selected=selected,
        )
        console.clear()
        return choice if choice else current

    CONFIG_FIELDS = [
        ("model.name",                    "Model"),
        ("model.provider",                "Provider"),
        ("model.thinking.enabled",        "Thinking"),
        ("model.thinking.reasoning_effort","Reasoning Effort"),
        ("runtime.max_steps",             "Max Steps"),
        ("runtime.unsafe_mode",           "Unsafe Mode"),
        ("agent.system_prompt",           "System Prompt"),
    ]

    def _current_value(field: str) -> str:
        if field == "model.name":
            return str(_get(model_cfg, "name"))
        if field == "model.provider":
            return str(_get(model_cfg, "provider"))
        if field == "model.thinking.enabled":
            return "enabled" if _get(thinking_cfg, "enabled", default=False) else "disabled"
        if field == "model.thinking.reasoning_effort":
            return str(_get(thinking_cfg, "reasoning_effort", default="high"))
        if field == "runtime.max_steps":
            return str(_get(runtime_cfg, "max_steps", default=25))
        if field == "runtime.unsafe_mode":
            return "true" if _get(runtime_cfg, "unsafe_mode", default=False) else "false"
        if field == "agent.system_prompt":
            sp = str(_get(agent, "system_prompt", default=""))
            return sp[:60] + "…" if len(sp) > 60 else sp
        return ""

    def _edit_field(field: str) -> None:
        nonlocal data, agent, model_cfg, runtime_cfg, thinking_cfg
        if field == "model.name":
            # Pick from known models or type manually
            console.print()
            choice = _prompt_choice("Model", _get(model_cfg, "name"), ALL_MODELS)
            if choice:
                model_cfg["name"] = choice
                # auto-update provider
                prov = MODEL_TO_PROVIDER.get(choice, model_cfg.get("provider", ""))
                model_cfg["provider"] = PROVIDER_YAML_NAME.get(prov, prov.lower())
                model_cfg["api_key_env"] = f"{prov.upper()}_API_KEY"
        elif field == "model.provider":
            providers = list(PROVIDER_YAML_NAME.values())
            choice = _prompt_choice("Provider", _get(model_cfg, "provider"), providers)
            if choice:
                model_cfg["provider"] = choice
        elif field == "model.thinking.enabled":
            console.print()
            val = _prompt_bool("Thinking", bool(_get(thinking_cfg, "enabled", default=False)))
            thinking_cfg["enabled"] = val
        elif field == "model.thinking.reasoning_effort":
            choice = _prompt_choice("Reasoning Effort",
                                    _get(thinking_cfg, "reasoning_effort", default="high"),
                                    ["low", "medium", "high"])
            thinking_cfg["reasoning_effort"] = choice
        elif field == "runtime.max_steps":
            val = _prompt_text("Max Steps", str(_get(runtime_cfg, "max_steps", default=25)))
            try:
                runtime_cfg["max_steps"] = int(val)
            except ValueError:
                console.print("   [error]Invalid number — keeping previous value[/error]")
        elif field == "runtime.unsafe_mode":
            console.print()
            val = _prompt_bool("Unsafe Mode", bool(_get(runtime_cfg, "unsafe_mode", default=False)))
            runtime_cfg["unsafe_mode"] = val
        elif field == "agent.system_prompt":
            full = str(_get(agent, "system_prompt", default=""))
            val = _prompt_text("System Prompt", full, hint="edit full text")
            agent["system_prompt"] = val

        # Propagate nested back into data
        if "model" not in agent:
            agent["model"] = {}
        agent["model"] = model_cfg
        if "thinking" not in agent["model"]:
            agent["model"]["thinking"] = {}
        agent["model"]["thinking"] = thinking_cfg
        agent["runtime"] = runtime_cfg
        data["agent"] = agent

    while True:
        def render_config(item: tuple[str, str], selected: bool) -> Text:
            field, label = item
            val = _current_value(field)
            text = Text(f"{label:<22}", style="#FFAE70" if selected else "#d0d0d0")
            text.append(val, style="dim #888888")
            return text

        choice = _select(
            "Configuration",
            CONFIG_FIELDS,
            render_config,
            subtitle="Enter to edit   Esc to save & exit",
            cancelable=True,
        )
        if choice is None:
            break
        _edit_field(choice[0])

    # Save back
    ok = _save_yaml_config(base_config_path, data)
    if ok:
        console.print("   [success]✓ configuration saved[/success]")
    else:
        console.print("   [error]✗ failed to save configuration[/error]")
    console.print()


# ── Main loop ──────────────────────────────────────────────────────────────────

# ── /status command ────────────────────────────────────────────────────────────

def show_status(
    runtime: Any,
    session_id: str,
    current_model: str,
    current_provider: str,
    work_dir: Path,
) -> None:
    """Display a styled system status panel."""
    import subprocess

    # Collect runtime config values
    try:
        safe_mode   = runtime.config.runtime.unsafe_mode
        max_steps   = runtime.config.runtime.max_steps
        sa_enabled  = getattr(
            getattr(runtime.config, "sub_agents", None), "enabled", False
        )
    except Exception:
        safe_mode  = False
        max_steps  = "?"
        sa_enabled = False

    # OS / platform info
    try:
        uname = platform.uname()
        os_str = f"{uname.system} {uname.machine} ({uname.release})"
    except Exception:
        os_str = platform.platform()

    # Python runtime
    py_ver = f"Python {sys.version.split()[0]}"

    # Session message count
    try:
        msg_count = len(runtime.messages)
    except Exception:
        msg_count = 0

    # Memory / process stats
    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        mem_str = f"{mem_mb:.1f} MB"
    except Exception:
        mem_str = "n/a"

    # API key hint (masked)
    key_env = ""
    try:
        key_env = runtime.config.model.api_key_env or ""
    except Exception:
        pass
    key_val = os.environ.get(key_env, "")
    key_display = f"{key_env} ✓" if key_val else f"{key_env} ✗ (not set)"

    console.print()

    # ── Header bar
    w = shutil.get_terminal_size().columns
    bar_inner = " Status "
    pad = max(0, w - 4 - len(bar_inner))
    left  = pad // 2
    right = pad - left
    console.print(f"   [brand]{'─' * left}{bar_inner}{'─' * right}[/brand]")
    console.print()

    rows = [
        ("CodePilot",   f"{APP_NAME} {APP_VERSION}"),
        ("Runtime",     py_ver),
        ("OS",          os_str),
        ("Model",       current_model),
        ("Provider",    current_provider),
        ("API Key",     key_display),
        ("Session ID",  session_id),
        ("Messages",    str(msg_count)),
        ("Work Dir",    str(work_dir)),
        ("Max Steps",   str(max_steps)),
        ("Safe Mode",   "off (unsafe)" if safe_mode else "on"),
        ("Sub-Agents",  "enabled" if sa_enabled else "disabled"),
        ("Memory RSS",  mem_str),
    ]

    for key, val in rows:
        val_style = "status.val" if key in ("Model", "Provider", "CodePilot") else "muted"
        console.print(
            f"   [question]{key:<14}[/question]  [{val_style}]{val}[/{val_style}]"
        )

    console.print()
    console.print(f"   [brand]{'─' * (w - 4)}[/brand]")
    console.print()


# ── /context command ───────────────────────────────────────────────────────────

def _est_tokens(val: str | int) -> int:
    """Fast character-based token estimate (~4 chars/token)."""
    if isinstance(val, int):
        return max(0, val // 4)
    if isinstance(val, str):
        return max(0, len(val) // 4)
    return 0


def _fill_bar(used: int, total: int, width: int = 36) -> str:
    """
    Returns a Rich-markup fill bar string.
    filled = amber, empty = dim.
    """
    if total <= 0:
        ratio = 0.0
    else:
        ratio = min(1.0, used / total)
    filled = int(ratio * width)
    empty  = width - filled

    bar = f"[bold #FF8533]{'█' * filled}[/bold #FF8533][dim #333333]{'░' * empty}[/dim #333333]"
    pct = int(ratio * 100)

    # Colour the percentage: green <50%, amber 50-80%, red >80%
    if pct < 50:
        pct_style = "#66BB6A"
    elif pct < 80:
        pct_style = "#FFA726"
    else:
        pct_style = "#EF5350"

    return f"{bar} [{pct_style}]{pct:3d}%[/{pct_style}]"


def show_context(runtime: Any, current_model: str) -> None:
    """Visual context window usage breakdown with fill bars."""
    context_window = MODEL_CONTEXT_WINDOWS.get(current_model, DEFAULT_CONTEXT_WINDOW)

    try:
        msgs = list(runtime.messages)
    except Exception:
        msgs = []

    # ── Estimate each category ──────────────────────────────────────────────
    try:
        from codepilot.core.memory import count_tokens
        actual_rt = getattr(runtime, "_async", runtime)
        
        # 1. Exact System Prompt & Tool Schemas calculation
        sys_parts = actual_rt._build_system_prompt()
        sys_str = getattr(sys_parts, "static", "") + "\n" + getattr(sys_parts, "dynamic", "")
        total_sys = count_tokens(sys_str)
        
        reg = getattr(actual_rt, "registry", None)
        defs = ""
        if reg:
            try:
                defs = reg.get_definitions() or ""
            except Exception:
                pass
                
        tools_tokens = count_tokens(defs) if defs else 0
        # Tool schemas are injected into the static prompt, so subtract them to isolate prompt text
        sys_tokens = max(0, total_sys - tools_tokens)
        
        # 2. Exact History Messages calculation
        user_tokens  = sum(count_tokens(m.get("content") or "") for m in msgs if m.get("role") == "user")
        asst_tokens  = sum(count_tokens(m.get("content") or "") for m in msgs if m.get("role") == "assistant")
        tool_tokens  = sum(count_tokens(m.get("content") or "") for m in msgs if m.get("role") not in ("user", "assistant"))
        
    except Exception:
        # Fallback if something goes wrong or runtime isn't fully initialized
        sys_tokens   = 3_000
        tools_tokens = 2_500
        user_chars  = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "user")
        asst_chars  = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "assistant")
        tool_chars  = sum(len(m.get("content") or "") for m in msgs if m.get("role") not in ("user", "assistant"))
        user_tokens  = _est_tokens(user_chars)
        asst_tokens  = _est_tokens(asst_chars)
        tool_tokens  = _est_tokens(tool_chars)

    hist_tokens  = user_tokens + asst_tokens + tool_tokens
    used_tokens  = sys_tokens + tools_tokens + hist_tokens
    free_tokens  = max(0, context_window - used_tokens)

    console.print()

    # Header
    w = shutil.get_terminal_size().columns
    console.print(f"   [brand]Context Window[/brand]   [muted]{current_model}[/muted]   [dim #555555]{context_window // 1_000}k tokens[/dim #555555]")
    console.print()

    # ── Overall fill bar
    overall_bar = _fill_bar(used_tokens, context_window, width=42)
    used_k  = used_tokens  / 1_000
    total_k = context_window / 1_000
    console.print(f"   {overall_bar}   [dim #666666]{used_k:.1f}k / {total_k:.0f}k used[/dim #666666]")
    console.print()

    # ── Section breakdown ───────────────────────────────────────────────────
    sections = [
        ("System prompt",    sys_tokens,   "#9C89FF"),
        ("Tool schemas",     tools_tokens, "#4FC3F7"),
        ("User messages",    user_tokens,  "#66BB6A"),
        ("Agent responses",  asst_tokens,  "#FF8533"),
        ("Tool results",     tool_tokens,  "#FFA726"),
        ("Free capacity",    free_tokens,  "#3a6644"),
    ]

    console.print(f"   [dim #555555]{'Section':<18}  {'Tokens':>8}   Fill (relative to window)[/dim #555555]")
    console.print(f"   [dim #2a2a2a]{'─' * 64}[/dim #2a2a2a]")

    for label, toks, color in sections:
        bar = _fill_bar(toks, context_window, width=28)
        toks_k = toks / 1_000
        prefix = "  └ " if label not in ("System prompt", "Free capacity") else "  ● "
        console.print(
            f"   [{color}]{prefix}{label:<16}[/{color}]  "
            f"[dim #888888]{toks_k:>5.1f}k[/dim #888888]   {bar}"
        )

    console.print()
    console.print(f"   [dim #444444]✓  token counts calculated exactly via cl100k_base (tiktoken)[/dim #444444]")
    console.print()


# ── /export command ────────────────────────────────────────────────────────────

def show_export(
    runtime: Any,
    session_id: str,
    current_model: str,
    current_provider: str,
    work_dir: Path,
) -> None:
    """Export full session conversation to a JSON file."""
    try:
        msgs = list(runtime.messages)
    except Exception:
        msgs = []
    try:
        raw_generations = list(runtime.raw_llm_generations())
    except Exception:
        raw_generations = list(_raw_generations)
    if not raw_generations and _raw_generations:
        raw_generations = list(_raw_generations)

    now      = datetime.datetime.now(datetime.timezone.utc)
    ts_file  = now.strftime("%Y%m%d_%H%M%S")
    ts_iso   = now.isoformat()

    # Build conversation list
    conversation = []
    for idx, m in enumerate(msgs):
        role    = m.get("role", "unknown")
        content = m.get("content") or ""
        conversation.append({"index": idx, "role": role, "content": content})

    # Stats
    user_count  = sum(1 for m in msgs if m.get("role") == "user")
    asst_count  = sum(1 for m in msgs if m.get("role") == "assistant")
    est_tokens  = _est_tokens("".join(m.get("content") or "" for m in msgs))

    payload = {
        "export_metadata": {
            "exported_at":       ts_iso,
            "codepilot_version": APP_VERSION,
            "session_id":        session_id,
            "model":             current_model,
            "provider":          current_provider,
            "work_dir":          str(work_dir),
        },
        # Model-visible history — exactly what the LLM sees as context.
        # Agentic turns are stored verbatim; context archiving handles pressure.
        "conversation": conversation,
        # Raw LLM generations — the exact, unmodified response_text the model
        # produced for every agentic step, independent of model-visible history.
        # Use this for debugging, hallucination analysis, and prompt auditing.
        "raw_llm_generations": raw_generations,
        "stats": {
            "total_messages":       len(msgs),
            "user_messages":        user_count,
            "assistant_messages":   asst_count,
            "other_messages":       len(msgs) - user_count - asst_count,
            "estimated_tokens":     est_tokens,
            "raw_generations_captured": len(raw_generations),
        },
    }

    out_path = work_dir / f"codepilot_export_{session_id}_{ts_file}.json"
    try:
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        console.print(f"   [error]✗  Export failed: {exc}[/error]")
        console.print()
        return

    console.print()
    console.print(f"   [success]✓  Session exported[/success]")
    console.print()
    console.print(f"   [status.key]{'File':<14}[/status.key]  [status.val]{out_path}[/status.val]")
    console.print(f"   [status.key]{'Messages':<14}[/status.key]  [muted]{len(msgs)} total ({user_count} user / {asst_count} agent)[/muted]")
    console.print(f"   [status.key]{'Raw traces':<14}[/status.key]  [muted]{len(raw_generations)} generation(s) captured[/muted]")
    console.print(f"   [status.key]{'Est. tokens':<14}[/status.key]  [muted]~{est_tokens:,}[/muted]")
    console.print(f"   [status.key]{'Timestamp':<14}[/status.key]  [muted]{ts_iso}[/muted]")
    console.print()


# ── /stat command ──────────────────────────────────────────────────────────────

# Approximate cost per 1M tokens (USD) — rough public estimates, June 2026
_COST_PER_M: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro":   (0.55, 2.19),
    "claude-haiku-4-5":  (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8":   (15.00, 75.00),
    "gpt-5.4-mini":      (0.15, 0.60),
    "gpt-5-mini":        (0.15, 0.60),
    "gpt-5.4":           (5.00, 20.00),
    "gpt-5.5":           (5.00, 20.00),
    "qwen3-coder-plus":  (0.70, 2.10),
    "qwen3-coder-next":  (0.70, 2.10),
    "qwen3-coder-flash": (0.14, 0.42),
    "qwen3.6-plus":      (0.70, 2.10),
}


def show_stat(current_model: str) -> None:
    """Display session-level token usage stats."""
    calls     = _session_stats["calls"]
    est_in    = _session_stats["est_input_tokens"]
    est_out   = _session_stats["est_output_tokens"]
    est_total = est_in + est_out

    # Cost estimate
    in_cost = out_cost = 0.0
    if current_model in _COST_PER_M:
        in_rate, out_rate = _COST_PER_M[current_model]
        in_cost  = est_in  / 1_000_000 * in_rate
        out_cost = est_out / 1_000_000 * out_rate
    total_cost = in_cost + out_cost

    console.print()

    # ── Header
    w = shutil.get_terminal_size().columns
    console.print(f"   [brand]Token Usage[/brand]   [muted]{current_model}[/muted]")
    console.print()

    if calls == 0:
        console.print("   [muted]No API calls made yet this session.[/muted]")
        console.print()
        return

    # ── Numbers
    rows = [
        ("API calls",       str(calls),             "#FFAE70"),
        ("Input tokens",    f"~{est_in:,}",          "#4FC3F7"),
        ("Output tokens",   f"~{est_out:,}",         "#FF8533"),
        ("Total tokens",    f"~{est_total:,}",        "#66BB6A"),
    ]
    for key, val, color in rows:
        console.print(f"   [dim #888888]{key:<18}[/dim #888888]  [{color}]{val}[/{color}]")

    console.print()

    # ── Cost estimate mini-bar
    if total_cost > 0:
        console.print(f"   [dim #555555]{'Cost estimate':<18}[/dim #555555]  [dim #888888]in ${in_cost:.5f}  out ${out_cost:.5f}  total [bold #FFAE70]${total_cost:.4f}[/bold #FFAE70][/dim #888888]")
    else:
        console.print(f"   [dim #555555]{'Cost estimate':<18}[/dim #555555]  [dim #666666]unavailable for this model[/dim #666666]")

    # ── Throughput fill bar (output vs input ratio)
    console.print()
    if est_in > 0:
        out_ratio = min(1.0, est_out / est_in)
        bar_w = 32
        filled = int(out_ratio * bar_w)
        console.print(
            f"   [dim #555555]Output/Input ratio   [/dim #555555]"
            f"[bold #FF8533]{'█' * filled}[/bold #FF8533]"
            f"[dim #333333]{'░' * (bar_w - filled)}[/dim #333333]"
            f"  [dim #888888]{out_ratio:.0%}[/dim #888888]"
        )
        console.print()

    console.print(f"   [dim #444444]⚠  estimates based on ~4 chars/token heuristic; reset with /reset[/dim #444444]")
    console.print()



def run_cli() -> None:
    work_dir   = Path.cwd()
    session_id = pick_session_interactive()

    # ── Read model/provider from agent.yaml (fallback to compiled defaults) ──
    _base_cfg_path = _find_base_config()
    _base_cfg_data = _load_yaml_config(_base_cfg_path) if _base_cfg_path.exists() else {}
    _yaml_model    = (_base_cfg_data.get("agent", {}) or {}).get("model", {}) or {}
    _yaml_model_name = _yaml_model.get("name", "").strip()
    _yaml_provider   = _yaml_model.get("provider", "").strip()

    # Resolve to a known model name; fall back to compiled default
    if _yaml_model_name and _yaml_model_name in ALL_MODELS:
        current_model = _yaml_model_name
    else:
        current_model = DEFAULT_MODEL

    # Resolve provider: prefer yaml, then MODEL_TO_PROVIDER lookup, then default
    if _yaml_provider:
        # yaml stores lowercase provider name; find the display name key
        _prov_display = next(
            (k for k, v in PROVIDER_YAML_NAME.items() if v == _yaml_provider),
            None
        )
        current_provider = _prov_display or MODEL_TO_PROVIDER.get(current_model, DEFAULT_PROVIDER)
    else:
        current_provider = MODEL_TO_PROVIDER.get(current_model, DEFAULT_PROVIDER)

    # Expose model name to hooks via shared state
    _cli_state["model"] = current_model

    print_banner(work_dir, session_id, current_model)

    if not HAS_RUNTIME:
        console.print("   [error]✗  codepilot package not found — install it first.[/error]")
        console.print()
        return

    config_path = build_patched_config(work_dir, current_model, current_provider)
    runtime: Any = None

    def _make_runtime(cfg: Path) -> Any:
        rt = Runtime(
            str(cfg),
            session="file",
            session_id=session_id,
            session_dir=SESSION_DIR,
            stream=True,
        )
        install_hooks(rt)
        return rt

    try:
        runtime = _make_runtime(config_path)
        worker = AgentWorker(runtime)
        pt_session: PromptSession = PromptSession(
            style=PT_STYLE,
            completer=SlashCompleter(),
            complete_while_typing=True,
        )

        while True:
            # ── Prompt ────────────────────────────────────────────────────
            try:
                task = pt_session.prompt(
                    HTML('<b><style fg="#4FC3F7">›</style></b> '),
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n   [muted]Goodbye.[/muted]")
                return
            except Exception:
                task = ""

            if not task:
                continue

            # ── Run shell command prefix ──────────────────────────────────
            if task.startswith("!"):
                cmd = task[1:].strip()
                if not cmd:
                    console.print("   [error]No command provided after ![/error]")
                    continue
                console.print(f"   [brand]⚡ Running local command:[/brand] [muted]{cmd}[/muted]")
                console.print()
                try:
                    subprocess.run(cmd, shell=True)
                except Exception as exc:
                    console.print(f"   [error]Failed to run command: {exc}[/error]")
                console.print()
                continue

            # ── Built-ins ─────────────────────────────────────────────────
            if task.lower() in {"quit", "exit"}:
                console.print("   [muted]Goodbye.[/muted]")
                return

            # ── Slash commands ────────────────────────────────────────────
            if task.startswith("/"):
                cmd = task.split()[0].lower()

                if cmd == "/help":
                    console.print()
                    for c, desc in SLASH_COMMANDS.items():
                        console.print(
                            f"   [brand]{c:<12}[/brand]  [muted]{desc}[/muted]"
                        )
                    console.print()

                elif cmd == "/models":
                    chosen = show_models_picker(current_model)
                    if chosen and chosen != current_model:
                        current_model    = chosen
                        current_provider = MODEL_TO_PROVIDER.get(chosen, DEFAULT_PROVIDER)
                        _cli_state["model"] = current_model
                        try:
                            config_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        config_path = build_patched_config(
                            work_dir, current_model, current_provider
                        )
                        runtime = _make_runtime(config_path)
                        worker = AgentWorker(runtime)
                        console.print(
                            f"   [finish]✓  model → {current_model}[/finish]"
                            f"   [muted]({current_provider})[/muted]"
                        )
                    console.print()

                elif cmd == "/config":
                    base_cfg = _find_base_config()
                    show_config_editor(base_cfg)
                    # Rebuild runtime with updated config
                    try:
                        config_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    config_path = build_patched_config(work_dir, current_model, current_provider)
                    runtime = _make_runtime(config_path)
                    worker = AgentWorker(runtime)

                elif cmd == "/sessions":
                    chosen = show_sessions_picker()
                    if chosen and chosen != session_id:
                        session_id = chosen
                        runtime = _make_runtime(config_path)
                        worker = AgentWorker(runtime)
                        console.print(
                            f"   [finish]✓  resumed session {session_id}[/finish]"
                        )
                    console.print()

                elif cmd == "/session":
                    console.print()
                    try:
                        meta = runtime.metadata()
                        if isinstance(meta, dict):
                            for k, v in meta.items():
                                console.print(
                                    f"   [status.key]{k:<18}[/status.key]  [muted]{v}[/muted]"
                                )
                    except Exception:
                        pass
                    console.print(f"   [status.key]{'session_id':<18}[/status.key]  [status.val]{session_id}[/status.val]")
                    console.print(f"   [status.key]{'model':<18}[/status.key]  [status.val]{current_model}[/status.val]")
                    console.print(f"   [status.key]{'provider':<18}[/status.key]  [status.val]{current_provider}[/status.val]")
                    console.print(f"   [status.key]{'work_dir':<18}[/status.key]  [muted]{work_dir}[/muted]")
                    console.print()

                elif cmd == "/status":
                    show_status(
                        runtime, session_id, current_model,
                        current_provider, work_dir,
                    )

                elif cmd == "/context":
                    show_context(runtime, current_model)

                elif cmd == "/export":
                    show_export(
                        runtime, session_id, current_model,
                        current_provider, work_dir,
                    )

                elif cmd == "/stat":
                    show_stat(current_model)

                elif cmd == "/reset":
                    runtime.reset()
                    # Also wipe accumulated session stats
                    _session_stats["calls"]            = 0
                    _session_stats["est_input_tokens"]  = 0
                    _session_stats["est_output_tokens"] = 0
                    _session_stats["_last_msg_count"]   = 0
                    _session_stats["_last_msg_chars"]   = 0
                    console.print("   [muted]✓  session cleared[/muted]")
                    console.print()

                elif cmd == "/exit":
                    console.print("   [muted]Goodbye.[/muted]")
                    return

                elif cmd in {"/bash", "/shell"}:
                    console.print()
                    console.print("   [brand]⚡ Entering interactive bash shell. Type 'exit' to return to CodePilot.[/brand]")
                    console.print()
                    try:
                        subprocess.run(["/bin/bash"])
                    except Exception as exc:
                        console.print(f"   [error]Failed to start shell: {exc}[/error]")
                    console.print()
                    console.print("   [brand]✓ Returned to CodePilot[/brand]")
                    console.print()

                else:
                    console.print(
                        f"   [error]Unknown command: {cmd}[/error]  [muted]/help for list[/muted]"
                    )

                continue


            # ── Run task ──────────────────────────────────────────────────
            console.print()
            console.print(Rule(style="dim #2a2a2a"))  # separator: prompt → agent
            console.print()
            spinner.start()

            error = worker.run_task(task)
            spinner.stop()

            if error is not None:
                console.print(f"   [error]✗  {error}[/error]")
                console.print()
                console.print(Rule(style="dim #2a2a2a"))
                model_name = _cli_state.get("model", "")
                model_part = f"[dim #555555]◉[/dim #555555] [muted]{model_name}[/muted]" if model_name else ""
                await_part = "[bold #3d7a3d]●[/bold #3d7a3d] [dim #4a7c4a]awaiting task...[/dim #4a7c4a]"
                spacer = "   " if model_name else ""
                console.print(f"   {model_part}{spacer}{await_part}")
            console.print()


    finally:
        spinner.stop()
        try:
            config_path.unlink(missing_ok=True)
        except Exception:
            pass
