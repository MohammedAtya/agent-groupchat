#!/usr/bin/env python3
"""A pretend agent CLI for the tests: answers instantly, costs nothing.

Usage: fake_agent.py claude|codex <the real CLI's arguments...>
It prints the same kind of JSON event stream as the real CLI, so the app's real
adapter code parses it. Controlled by environment variables:
  FAKE_LOG    a folder: one JSON file per run {"style", "argv", "cwd"}, named so they sort by
              start time (one file each, because agents run at the same time and appends to
              one shared file can interleave on Windows)
  FAKE_TIMES  a folder: one JSON file per run with its start and end time (for overlap tests)
  FAKE_RULES  JSON list of {"style"?, "if"?, "reply"?, "write"?: {path: text}, "exit"?: code,
              "sleep"?: seconds};
              the first rule whose style matches and whose "if" text is in its arguments
              (prompt or system prompt) wins.
"""
import json
import os
import sys
import time
import uuid

started = time.time()
sys.stdout.reconfigure(encoding="utf-8")  # like the real CLIs (Windows defaults to cp1252)

style, argv = sys.argv[1], sys.argv[2:]
if argv == ["--version"]:  # the Test button
    print(f"fake {style} 1.0")
    sys.exit(0)
prompt = argv[-1] if argv else ""  # (gemini: --prompt is last too)
stdin_text = None
if (style == "claude" and argv and argv[-1] != "--" and "--" not in argv) or \
        (style == "codex" and argv[-1:] == ["-"]) or (style == "gemini" and argv[-1:] == [" "]):
    stdin_text = sys.stdin.read()  # a long prompt came on standard input
    prompt = stdin_text + (argv[-1] if style == "gemini" else "")
if style == "aider":  # the message is in a file
    with open(argv[argv.index("--message-file") + 1], encoding="utf-8") as f:
        prompt = f.read()
RUN = f"{time.time_ns():020d}-{os.getpid()}.json"


def record(folder, data):
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, RUN + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(folder, RUN))


if os.environ.get("FAKE_LOG"):
    record(os.environ["FAKE_LOG"], {"style": style, "argv": argv, "cwd": os.getcwd(), "stdin": stdin_text})

rule = {}
for r in json.loads(os.environ.get("FAKE_RULES") or "[]"):
    if r.get("style", style) == style and r.get("if", "") in "\n".join(argv + [prompt]):
        rule = r
        break
if rule.get("sleep"):
    time.sleep(rule["sleep"])
if os.environ.get("FAKE_TIMES"):
    record(os.environ["FAKE_TIMES"], {"argv": argv, "start": started, "end": time.time()})
reply = rule.get("reply", f"fake {style} reply")
writes = rule.get("write") or {}
for path, text in writes.items():
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


if rule.get("commit"):  # like an agent that commits its own changes (git add -A + commit)
    import subprocess
    subprocess.run(["git", "add", "-A"], capture_output=True)
    subprocess.run(["git", "-c", "user.name=fake", "-c", "user.email=fake@x", "commit", "-qm", "fake"],
                   capture_output=True)


def emit(ev):
    print(json.dumps(ev), flush=True)


if style == "gemini":
    sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else \
        argv[argv.index("--resume") + 1]
    emit({"type": "init", "session_id": sid, "model": "fake"})
    if rule.get("exit"):
        print("fake failure", file=sys.stderr)
        emit({"type": "result", "status": "error", "error": {"message": "quota exceeded"},
              "stats": {}})
        sys.exit(rule["exit"])
    for p in writes:
        emit({"type": "tool_use", "tool_name": "write_file", "tool_id": "t1",
              "parameters": {"file_path": os.path.abspath(p), "content": "..."}})
        emit({"type": "tool_result", "tool_id": "t1", "status": "success"})
    for i, w in enumerate(reply.split(" ")):
        emit({"type": "message", "role": "assistant", "content": (" " if i else "") + w,
              "delta": True})
    emit({"type": "result", "status": "success",
          "stats": {"input_tokens": 30, "output_tokens": 4, "total_tokens": 34}})
    sys.exit(0)

if style == "aider":  # plain-text output; the reply goes to its chat-history file
    hist = argv[argv.index("--chat-history-file") + 1]
    with open(argv[argv.index("--message-file") + 1], encoding="utf-8") as f:
        message = f.read()
    print("Aider v0.86.2\nModel: fake with whole edit format")
    if rule.get("exit"):  # like the real Aider: the model call failed, exit code may be 0
        print("litellm.APIError: fake failure")
        sys.exit(0 if rule["exit"] == "zero" else rule["exit"])
    with open(hist, "a", encoding="utf-8") as f:
        f.write("\n# aider chat started at 2026-11-22 10:00:00\n\n")
        f.write("".join(f"#### {l}\n" for l in message.splitlines()) + "\n")
        f.write(reply + "\n\n")
        for p in writes:
            f.write(f"> Applied edit to {p}\n")
    print(reply)
    for p in writes:
        print(f"Applied edit to {p}")
    sys.exit(0)

if rule.get("exit"):
    print("fake failure", file=sys.stderr)
    sys.exit(rule["exit"])

if style == "claude":
    emit({"type": "system", "subtype": "init"})
    emit({"type": "stream_event", "event": {"type": "content_block_start",
                                            "content_block": {"type": "text"}}})
    emit({"type": "stream_event", "event": {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": reply}}})
    content = [{"type": "tool_use", "name": "Write", "input": {"file_path": os.path.abspath(p)}}
               for p in writes] + [{"type": "text", "text": reply}]
    emit({"type": "assistant", "message": {"content": content}})
    emit({"type": "result", "result": reply, "is_error": False, "total_cost_usd": 0.0123,
          "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 100, "output_tokens": 7}})
else:  # codex
    if "resume" not in argv:
        emit({"type": "thread.started", "thread_id": "thread-" + uuid.uuid4().hex[:8]})
    emit({"type": "item.started", "item": {"type": "command_execution",
                                           "command": "/bin/bash -lc 'ls -la'"}})
    if writes:
        emit({"type": "item.completed", "item": {"type": "file_change", "changes": [
            {"path": os.path.abspath(p)} for p in writes]}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": reply}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 3}})
    out = argv[argv.index("-o") + 1]
    with open(out, "w", encoding="utf-8") as f:
        f.write(reply)
