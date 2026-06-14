#!/usr/bin/env python3
"""
test_tools.py — Manual tool tester for CodePilot CLI

Lets you call every registered tool interactively WITHOUT the LLM.
This exercises the exact same code paths the agent uses.

Usage:
    cd ~/codepilot-docker
    python3 test_tools.py

You'll get a menu to pick a tool, enter args, and see results.
"""

import sys
import os
import time
from pathlib import Path

# Ensure local codepilot is importable
CODEPILOT_ROOT = Path.home() / "codepilot"
sys.path.insert(0, str(CODEPILOT_ROOT))

from codepilot import (
    Runtime,
    on_stream,
    on_tool_call,
    on_tool_result,
    on_permission_request,
    on_finish,
)

# ── Config ─────────────────────────────────────────────────────────────────

AGENT_YAML = str(Path(__file__).resolve().parent / "agent.yaml")
WORK_DIR = str(Path.cwd())

# ── Colors ─────────────────────────────────────────────────────────────────

C_RESET  = "\033[0m"
C_BOLD   = "\033[1m"
C_DIM    = "\033[2m"
C_ORANGE = "\033[38;2;255;133;51m"
C_GREEN  = "\033[38;2;102;187;106m"
C_RED    = "\033[38;2;239;83;80m"
C_CYAN   = "\033[38;2;79;195;247m"
C_GRAY   = "\033[38;2;100;100;100m"


def pr(color, text):
    print(f"{color}{text}{C_RESET}")


def header(text):
    print()
    pr(C_ORANGE + C_BOLD, f"  ═══ {text} ═══")
    print()


def result_box(text):
    pr(C_GREEN, f"  ✓ Result:")
    for line in str(text).splitlines():
        pr(C_DIM, f"    {line}")
    print()


def error_box(text):
    pr(C_RED, f"  ✗ Error: {text}")
    print()


# ── Build Runtime (no LLM needed for direct tool calls) ────────────────────

def build_runtime():
    """Create a Runtime instance with hooks for visibility."""
    # Patch WORK_DIR into the yaml
    import tempfile, re
    base = Path(AGENT_YAML)
    content = base.read_text()
    content = content.replace("${WORK_DIR}", WORK_DIR)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, prefix="cptest_")
    tmp.write(content)
    tmp.close()

    rt = Runtime(
        tmp.name,
        session="file",
        session_id="tool_test",
        stream=False,
    )

    # Install visibility hooks
    @on_tool_call(rt)
    def _tc(tool, args, label="", **_):
        pr(C_CYAN, f"  ⚙  {tool}  {label or ''}")

    @on_tool_result(rt)
    def _tr(tool, result, **_):
        lines = str(result).splitlines()
        if len(lines) > 30:
            lines = lines[:30] + [f"... ({len(lines)-30} more lines)"]
        for line in lines:
            pr(C_DIM, f"  │ {line}")

    @on_permission_request(rt)
    def _perm(tool, description, **_):
        pr(C_ORANGE, f"  ⚠  Permission: {tool} — {description}")
        ans = input(f"  {C_ORANGE}▸  Approve? [Y/n]: {C_RESET}").strip().lower()
        return ans in ("", "y", "yes")

    return rt


# ── Tool testers ──────────────────────────────────────────────────────────

def test_execute(rt):
    header("execute()")
    pr(C_GRAY, "  Runs a shell command in the terminal session.")
    pr(C_GRAY, "  Args: session_id (default: main), command, timeout (default: 10)")
    print()
    
    session_id = input(f"  {C_CYAN}session_id [main]: {C_RESET}").strip() or "main"
    command = input(f"  {C_CYAN}command: {C_RESET}").strip()
    if not command:
        pr(C_RED, "  No command given.")
        return
    timeout = input(f"  {C_CYAN}timeout [10]: {C_RESET}").strip()
    timeout = int(timeout) if timeout else 10

    try:
        result = rt._async._terminal_manager.execute(session_id, command, timeout)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_send_input(rt):
    header("send_input()")
    pr(C_GRAY, "  Sends raw input to an interactive process.")
    pr(C_GRAY, "  Remember: include \\n for Enter, \\x03 for Ctrl+C, etc.")
    print()

    session_id = input(f"  {C_CYAN}session_id [main]: {C_RESET}").strip() or "main"
    raw_text = input(f"  {C_CYAN}text (use \\n for newline): {C_RESET}")
    if not raw_text:
        pr(C_RED, "  No text given.")
        return
    
    # Process escape sequences
    text = raw_text.encode().decode('unicode_escape')
    timeout = input(f"  {C_CYAN}timeout [5]: {C_RESET}").strip()
    timeout = int(timeout) if timeout else 5

    try:
        result = rt._async._terminal_manager.send_input(session_id, text, timeout)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_read_output(rt):
    header("read_output()")
    pr(C_GRAY, "  Reads latest output from a terminal session.")
    print()

    session_id = input(f"  {C_CYAN}session_id [main]: {C_RESET}").strip() or "main"
    timeout = input(f"  {C_CYAN}timeout [3]: {C_RESET}").strip()
    timeout = int(timeout) if timeout else 3

    try:
        result = rt._async._terminal_manager.read_output(session_id, timeout)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_read_file(rt):
    header("read_file()")
    pr(C_GRAY, "  Reads file content with line numbers.")
    print()

    path = input(f"  {C_CYAN}path (relative to work_dir): {C_RESET}").strip()
    if not path:
        pr(C_RED, "  No path given.")
        return
    start = input(f"  {C_CYAN}start_line [none]: {C_RESET}").strip()
    end = input(f"  {C_CYAN}end_line [none]: {C_RESET}").strip()

    kwargs = {}
    if start:
        kwargs["start_line"] = int(start)
    if end:
        kwargs["end_line"] = int(end)

    try:
        fs = rt._async._fs_tools
        result = fs.read_file(path, **kwargs)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_write_file(rt):
    header("write_file()")
    pr(C_GRAY, "  Writes content to a file. Uses payload queue internally.")
    pr(C_GRAY, "  Modes: w (create/overwrite), a (append), edit, insert, multi_edit, search_replace")
    print()

    path = input(f"  {C_CYAN}path (relative to work_dir): {C_RESET}").strip()
    if not path:
        pr(C_RED, "  No path given.")
        return
    mode = input(f"  {C_CYAN}mode [w]: {C_RESET}").strip() or "w"

    kwargs = {"mode": mode}

    if mode == "edit":
        sl = input(f"  {C_CYAN}start_line: {C_RESET}").strip()
        el = input(f"  {C_CYAN}end_line: {C_RESET}").strip()
        if sl:
            kwargs["start_line"] = int(sl)
        if el:
            kwargs["end_line"] = int(el)
    elif mode == "insert":
        al = input(f"  {C_CYAN}after_line: {C_RESET}").strip()
        if al:
            kwargs["after_line"] = int(al)
    elif mode == "multi_edit":
        raw = input(f"  {C_CYAN}edits e.g. 1-2,5-5: {C_RESET}").strip()
        edits = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            start, end = item.split("-", 1)
            edits.append((int(start), int(end)))
        kwargs["edits"] = edits

    payloads = []
    payload_count = len(kwargs.get("edits", [])) if mode == "multi_edit" else 1

    for idx in range(payload_count):
        print()
        if mode == "search_replace":
            pr(C_GRAY, "  Use blocks like: <<<<<<< SEARCH / ======= / >>>>>>> REPLACE")
        label = f"payload {idx + 1}/{payload_count}" if payload_count > 1 else "file content"
        pr(C_CYAN, f"  Enter {label} (type END on a line by itself to finish):")
        lines = []
        while True:
            line = input("  │ ")
            if line.strip() == "END":
                break
            lines.append(line)
        payloads.append("\n".join(lines))

    # Inject content into the payload queue (simulating what the runtime does)
    from codepilot.core.block_parser import CodeBlock
    rt._async._payload_queue = [
        CodeBlock(
            language="python", content=content, index=idx,
            filename=path, start_pos=0, end_pos=0,
        )
        for idx, content in enumerate(payloads)
    ]

    try:
        fs = rt._async._fs_tools
        result = fs.write_file(path, **kwargs)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_find(rt):
    header("find()")
    pr(C_GRAY, "  Searches for files/directories matching a pattern.")
    print()

    pattern = input(f"  {C_CYAN}pattern: {C_RESET}").strip()
    if not pattern:
        pr(C_RED, "  No pattern given.")
        return

    try:
        result = rt._async._search_tools.find(pattern)
        result_box(result)
    except Exception as e:
        error_box(e)


def test_ask_user(rt):
    header("ask_user()")
    pr(C_GRAY, "  Simulates the agent asking the user a question.")
    print()

    question = input(f"  {C_CYAN}question: {C_RESET}").strip() or "What is your name?"

    try:
        result = rt._async._interaction_tools.ask_user(question)
        result_box(result)
    except Exception as e:
        error_box(e)


# ── Interactive menu ──────────────────────────────────────────────────────

TOOLS = {
    "1": ("execute",          test_execute),
    "2": ("send_input",       test_send_input),
    "3": ("read_output",      test_read_output),
    "4": ("read_file",        test_read_file),
    "5": ("write_file",       test_write_file),
    "6": ("find",             test_find),
    "7": ("ask_user",         test_ask_user),
}


def main():
    print()
    pr(C_ORANGE + C_BOLD, "  ╔══════════════════════════════════════════════╗")
    pr(C_ORANGE + C_BOLD, "  ║    CodePilot — Manual Tool Tester            ║")
    pr(C_ORANGE + C_BOLD, "  ╚══════════════════════════════════════════════╝")
    print()
    pr(C_DIM, f"  work_dir: {WORK_DIR}")
    pr(C_DIM, f"  agent.yaml: {AGENT_YAML}")
    print()

    pr(C_CYAN, "  Building runtime...")
    rt = build_runtime()
    pr(C_GREEN, "  ✓ Runtime ready. Default terminal session started.")
    print()

    while True:
        pr(C_ORANGE + C_BOLD, "  ┌─ Pick a tool ─────────────────────────────────")
        for k, (name, _) in TOOLS.items():
            pr(C_GRAY, f"  │  {k}.  {name}")
        pr(C_GRAY, f"  │  q.  quit")
        pr(C_ORANGE + C_BOLD, "  └────────────────────────────────────────────────")
        print()

        choice = input(f"  {C_ORANGE}›{C_RESET}  ").strip().lower()
        if choice in ("q", "quit", "exit"):
            pr(C_DIM, "  Goodbye.")
            break
        
        if choice in TOOLS:
            try:
                TOOLS[choice][1](rt)
            except KeyboardInterrupt:
                print()
                pr(C_DIM, "  (interrupted)")
            except Exception as e:
                error_box(e)
        else:
            pr(C_RED, f"  Unknown choice: {choice}")
        print()


if __name__ == "__main__":
    main()
