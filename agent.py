import asyncio
import sys
import os
import json
import re
import subprocess
from pathlib import Path

from rich.console import Console
from rich.text import Text
from rich.rule import Rule
from rich.theme import Theme
from rich.prompt import Prompt
from rich import box
from rich.panel import Panel

from codepilot import (
    AsyncRuntime,
    on_ask_user,
    on_finish,
    on_permission_request,
    on_stream,
    on_tool_call,
    on_tool_result,
    on_user_message_injected,
    on_user_message_queued,
)


# ── Identity ───────────────────────────────────────────────────────────────────

APP_NAME    = "CodePilot"
APP_VERSION = "v0.9.3"

MODELS = [
    "deepseek-v4-pro",
    "deepseek-v4-flash",
]

SLASH_COMMANDS = {
    "/models":   "List & switch models",
    "/session":  "Show session metadata",
    "/sessions": "Browse & resume a session",
    "/reset":    "Clear current session",
    "/bash":     "Start an interactive bash sub-shell",
    "/shell":    "Start an interactive bash sub-shell",
    "/exit":     "Quit CodePilot",
    "/help":     "Show all commands",
}

# ── Paths ──────────────────────────────────────────────────────────────────────

INSTALL_DIR  = Path(__file__).resolve().parent
SESSION_DIR  = INSTALL_DIR / "sessions"
CONFIG_FILE  = INSTALL_DIR / "agent.yaml"
WORK_DIR     = Path.cwd()

# ── Theme ──────────────────────────────────────────────────────────────────────

THEME = Theme({
    "brand":      "bold #FF8533",
    "dim_orange": "dim #FF8533",
    "tool":       "bold #FF8533",
    "tool_result":"dim #555555",
    "finish":     "#66BB6A",
    "finish_icon":"bold #66BB6A",
    "permission": "bold #FFA726",
    "question":   "bold #4FC3F7",
    "answer":     "#4FC3F7",
    "queued":     "dim #FF8533",
    "injected":   "dim #FF8533",
    "muted":      "dim #555555",
    "error":      "bold #EF5350",
    "stream":     "#d4d4d4",
    "divider":    "#1e1e1e",
    "status_key": "dim #444444",
    "status_val": "#FF8533",
    "ready":      "dim #3a3a3a",
})

console = Console(theme=THEME, highlight=False)

# ── Orange gradient banner ─────────────────────────────────────────────────────

GRADIENT_STOPS = [
    "#C94400","#D95000","#E86000","#F47120",
    "#FF8533","#FF9A50","#FFAE70",
]

def _gradient_text(s: str) -> Text:
    t = Text()
    n = len(GRADIENT_STOPS)
    chars = list(s)
    for i, ch in enumerate(chars):
        stop = GRADIENT_STOPS[int(i / max(len(chars)-1, 1) * (n-1))]
        t.append(ch, style=f"bold {stop}")
    return t

# Pre-rendered pyfiglet 'slant' font — no runtime dependency needed.
_BANNER_ART = (
    "   ______          __     ____  _ __      __  \n"
    "  / ____/___  ____/ /__  / __ \\(_) /___  / /_\n"
    " / /   / __ \\/ __  / _ \\/ /_/ / / / __ \\/ __/\n"
    "/ /___/ /_/ / /_/ /  __/ ____/ / / /_/ / /_  \n"
    "\\____/\\____/\\__,_/\\___/_/   /_/_/\\____/\\__/  "
)

def print_banner():
    console.clear()
    lines = _BANNER_ART.splitlines()
    all_chars = [(li, ch) for li, line in enumerate(lines) for ch in line]
    printable = [(li, ch) for li, ch in all_chars if ch != " "]
    total = max(len(printable) - 1, 1)
    n = len(GRADIENT_STOPS)

    line_texts = [Text() for _ in lines]
    p_idx = 0
    for li, ch in all_chars:
        if ch == " ":
            line_texts[li].append(" ")
        else:
            stop = GRADIENT_STOPS[int(p_idx / total * (n - 1))]
            line_texts[li].append(ch, style=f"bold {stop}")
            p_idx += 1

    console.print()
    for t in line_texts:
        console.print(" ", t)
    console.print(
        f"  [status_key]version[/status_key]  [dim #FF8533]{APP_VERSION}[/dim #FF8533]  "
        f"[status_key]workspace[/status_key]  [status_val]{WORK_DIR}[/status_val]"
    )
    console.print()

# ── Session helpers ────────────────────────────────────────────────────────────

def list_sessions():
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for f in SESSION_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            msgs = len(data.get("messages", []))
            from datetime import datetime
            mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            sessions.append((f.stem, mtime, msgs, f))
        except Exception:
            continue
    return sorted(sessions, key=lambda s: s[3].stat().st_mtime, reverse=True)

def next_session_id():
    existing = list_sessions()
    nums = []
    for s in existing:
        sid = s[0]
        if sid.startswith("devtool") and sid[7:].isdigit():
            nums.append(int(sid[7:]))
    return f"devtool{(max(nums)+1) if nums else 100}"

def pick_session():
    """Pre-launch session picker. Returns session_id string."""
    sessions = list_sessions()
    if not sessions:
        return next_session_id()

    console.print()
    console.print(f"  [brand]{APP_NAME}[/brand]  [muted]sessions[/muted]")
    console.print()
    console.print("  [muted]0)[/muted]  [bold]New session[/bold]")
    for i, (sid, mtime, msgs, _) in enumerate(sessions[:10], 1):
        console.print(f"  [muted]{i})[/muted]  [status_val]{sid}[/status_val]  [muted]{mtime}  {msgs} msgs[/muted]")
    console.print()
    try:
        choice = Prompt.ask("  [brand]›[/brand]  Pick session", default="0", console=console).strip()
    except (KeyboardInterrupt, EOFError):
        console.print()
        sys.exit(0)

    if choice == "0" or choice == "":
        return next_session_id()
    try:
        idx = int(choice)
        if 1 <= idx <= len(sessions[:10]):
            return sessions[idx-1][0]
    except ValueError:
        pass
    return next_session_id()

def show_sessions_inline():
    """Show sessions during a running CLI session, return chosen id or None."""
    sessions = list_sessions()
    if not sessions:
        console.print("  [muted]No saved sessions.[/muted]")
        return None
    console.print()
    console.print("  [muted]0)[/muted]  [bold]Cancel[/bold]")
    for i, (sid, mtime, msgs, _) in enumerate(sessions[:10], 1):
        console.print(f"  [muted]{i})[/muted]  [status_val]{sid}[/status_val]  [muted]{mtime}  {msgs} msgs[/muted]")
    console.print()
    try:
        choice = Prompt.ask("  [brand]›[/brand]  Pick session", default="0", console=console).strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if choice == "0" or choice == "":
        return None
    try:
        idx = int(choice)
        if 1 <= idx <= len(sessions[:10]):
            return sessions[idx-1][0]
    except ValueError:
        pass
    return None

# ── Config patching ────────────────────────────────────────────────────────────

def patched_config() -> Path:
    """Copy agent.yaml to a temp file with ${WORK_DIR} substituted."""
    import tempfile
    content = CONFIG_FILE.read_text()
    content = content.replace("${WORK_DIR}", str(WORK_DIR))
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="codepilot_"
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)

# ── Spinner ────────────────────────────────────────────────────────────────────

FRAMES = ("⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏")
_spinner_task: asyncio.Task | None = None
_spinner_active = False

async def _spin_loop():
    i = 0
    while True:
        frame = FRAMES[i % len(FRAMES)]
        sys.stdout.write(f"\r\033[2m{frame} thinking…\033[0m")
        sys.stdout.flush()
        await asyncio.sleep(0.08)
        i += 1

def spinner_start():
    global _spinner_task, _spinner_active
    _spinner_active = True
    loop = asyncio.get_event_loop()
    _spinner_task = loop.create_task(_spin_loop())

def spinner_stop():
    global _spinner_task, _spinner_active
    if not _spinner_active:
        return
    _spinner_active = False
    if _spinner_task and not _spinner_task.done():
        _spinner_task.cancel()
    _spinner_task = None
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

# ── Helpers ────────────────────────────────────────────────────────────────────

async def ainput(prompt: str = "") -> str:
    return await asyncio.to_thread(input, prompt)

def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[muted]…truncated[/muted]"

def print_status(session_id: str, model: str):
    console.print(
        f"  [status_key]session[/status_key]  [status_val]{session_id}[/status_val]"
        f"  [muted]│[/muted]  "
        f"[status_key]model[/status_key]  [status_val]{model}[/status_val]"
    )

# ── Hooks ──────────────────────────────────────────────────────────────────────

def install_hooks(runtime: AsyncRuntime) -> None:

    @on_stream(runtime)
    def _on_stream(text: str, **_):
        spinner_stop()
        sys.stdout.write(text)
        sys.stdout.flush()

    @on_tool_call(runtime)
    def _on_tool_call(tool: str, args: dict, label: str = "", **_):
        spinner_stop()
        display = label or (json.dumps(args)[:100] if args else "")
        print()
        console.print(f"  [tool]⚙  {tool}[/tool]  [muted]{display}[/muted]")

    @on_tool_result(runtime)
    def _on_tool_result(tool: str, result: str, **_):
        preview = _truncate(result)
        # single clean line, no box
        first_line = preview.splitlines()[0][:120] if preview else ""
        console.print(f"  [tool_result]└─ {first_line}[/tool_result]")

    @on_ask_user(runtime)
    def _on_ask_user(question: str, **_):
        spinner_stop()
        print()
        console.print(f"  [question]?  {question}[/question]")
        return Prompt.ask("  [answer]›[/answer]", console=console).strip()

    @on_finish(runtime)
    def _on_finish(summary: str, **_):
        spinner_stop()
        print()
        # Compact ready signal — icon only, no verbose text
        console.print("  [finish_icon]✦[/finish_icon]  [ready]ready[/ready]")

    @on_permission_request(runtime)
    def _on_permission_request(tool: str, description: str, **_):
        spinner_stop()
        print()
        console.print(f"  [permission]⚠  {tool}[/permission]  [muted]{description}[/muted]")
        answer = Prompt.ask(
            "  [permission]Approve?[/permission] [muted]y/N[/muted]",
            console=console,
            default="N",
        )
        return answer.strip().lower() in {"y", "yes"}

    @on_user_message_queued(runtime)
    def _on_queued(message: str, **_):
        console.print(f"  [queued]↑ queued    {message}[/queued]")

    @on_user_message_injected(runtime)
    def _on_injected(message: str, **_):
        console.print(f"  [injected]↓ injected  {message}[/injected]")

# ── Inject listener ────────────────────────────────────────────────────────────

_inject_queue: asyncio.Queue = asyncio.Queue()
_inject_listener_task: asyncio.Task | None = None
_runtime_ref: AsyncRuntime | None = None
_agent_running = False

async def _inject_listener():
    """Background task: reads stdin for inject/abort while agent is running."""
    global _agent_running
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(0.05)
        if not _agent_running:
            continue
        # Non-blocking check — we can't use ainput here as it blocks
        # Instead we rely on the queue being fed by the ESC watcher
        try:
            msg = _inject_queue.get_nowait()
            if msg == "__ABORT__":
                if _runtime_ref:
                    _runtime_ref.abort()
                    print()
                    console.print("  [muted]✗  abort requested[/muted]")
            else:
                if _runtime_ref:
                    _runtime_ref.send_message(msg)
        except asyncio.QueueEmpty:
            pass

# ── Main loop ──────────────────────────────────────────────────────────────────

async def main():
    global _runtime_ref, _agent_running

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    # Pick session before printing banner
    session_id = pick_session()
    current_model = MODELS[0]

    print_banner()
    print_status(session_id, current_model)
    console.print()
    console.print(Rule(style="dim #1e1e1e"))
    console.print()
    console.print("  [muted]Type a task, or /help for commands[/muted]")
    console.print()

    config_path = patched_config()

    try:
        runtime = AsyncRuntime(
            str(config_path),
            session="file",
            session_id=session_id,
            session_dir=SESSION_DIR,
            stream=True,
        )
        _runtime_ref = runtime
        install_hooks(runtime)

        while True:
            # ── Prompt ────────────────────────────────────────────────────────
            try:
                console.print("[brand]›[/brand] ", end="")
                task = (await ainput("")).strip()
            except (KeyboardInterrupt, EOFError):
                print("\n  Goodbye.")
                return

            if not task:
                continue

            # ── Run shell command prefix ──────────────────────────────────────
            if task.startswith("!"):
                cmd = task[1:].strip()
                if not cmd:
                    console.print("  [error]No command provided after ![/error]")
                    continue
                console.print(f"  [brand]⚡ Running local command:[/brand] [muted]{cmd}[/muted]")
                console.print()
                try:
                    subprocess.run(cmd, shell=True)
                except Exception as exc:
                    console.print(f"  [error]Failed to run command: {exc}[/error]")
                console.print()
                continue

            # ── Built-in keywords ─────────────────────────────────────────────
            if task.lower() in {"quit", "exit"}:
                console.print("  [muted]Goodbye.[/muted]")
                return

            # ── Slash commands ────────────────────────────────────────────────
            if task.startswith("/"):
                cmd = task.split()[0].lower()

                if cmd == "/help":
                    console.print()
                    for c, desc in SLASH_COMMANDS.items():
                        console.print(f"  [brand]{c:<12}[/brand]  [muted]{desc}[/muted]")
                    console.print()

                elif cmd == "/models":
                    console.print()
                    for m in MODELS:
                        marker = "[brand]●[/brand]" if m == current_model else "[muted]○[/muted]"
                        console.print(f"  {marker}  {m}")
                    console.print()
                    try:
                        choice = Prompt.ask(
                            "  [brand]›[/brand]  Switch to",
                            default=current_model,
                            console=console,
                        ).strip()
                    except (KeyboardInterrupt, EOFError):
                        continue
                    if choice in MODELS:
                        current_model = choice
                        console.print(f"  [finish]✓  model → {current_model}[/finish]")
                        print_status(session_id, current_model)
                    else:
                        console.print(f"  [error]Unknown model: {choice}[/error]")
                    console.print()

                elif cmd == "/sessions":
                    chosen = show_sessions_inline()
                    if chosen and chosen != session_id:
                        session_id = chosen
                        runtime = AsyncRuntime(
                            str(config_path),
                            session="file",
                            session_id=session_id,
                            session_dir=SESSION_DIR,
                            stream=True,
                        )
                        _runtime_ref = runtime
                        install_hooks(runtime)
                        console.print()
                        console.print(f"  [finish]✓  resumed session {session_id}[/finish]")
                        print_status(session_id, current_model)
                    console.print()

                elif cmd == "/session":
                    console.print()
                    try:
                        meta = runtime.metadata()
                        if isinstance(meta, dict):
                            for k, v in meta.items():
                                console.print(f"  [status_key]{k:<16}[/status_key]  [muted]{v}[/muted]")
                        else:
                            console.print(f"  [muted]{meta}[/muted]")
                    except Exception:
                        pass
                    console.print(f"  [status_key]{'session_id':<16}[/status_key]  [status_val]{session_id}[/status_val]")
                    console.print(f"  [status_key]{'model':<16}[/status_key]  [status_val]{current_model}[/status_val]")
                    console.print(f"  [status_key]{'work_dir':<16}[/status_key]  [muted]{WORK_DIR}[/muted]")
                    console.print()

                elif cmd == "/reset":
                    await runtime.areset()
                    console.print("  [muted]✓  session cleared[/muted]")
                    console.print()

                elif cmd == "/exit":
                    console.print("  [muted]Goodbye.[/muted]")
                    return

                elif cmd in {"/bash", "/shell"}:
                    console.print()
                    console.print("  [brand]⚡ Entering interactive bash shell. Type 'exit' to return to CodePilot.[/brand]")
                    console.print()
                    try:
                        subprocess.run(["/bin/bash"])
                    except Exception as exc:
                        console.print(f"  [error]Failed to start shell: {exc}[/error]")
                    console.print()
                    console.print("  [brand]✓ Returned to CodePilot[/brand]")
                    console.print()

                else:
                    console.print(f"  [error]Unknown command: {cmd}[/error]  [muted]/help for list[/muted]")

                continue

            # ── Run task ──────────────────────────────────────────────────────
            console.print()
            spinner_start()
            _agent_running = True
            try:
                await runtime.run(task)
            except KeyboardInterrupt:
                runtime.abort()
                spinner_stop()
                console.print()
                console.print("  [muted]✗  aborted[/muted]")
            finally:
                spinner_stop()
                _agent_running = False

            console.print()
            console.print(Rule(style="dim #1e1e1e"))
            console.print()

    finally:
        try:
            config_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Goodbye.")
        sys.exit(0)