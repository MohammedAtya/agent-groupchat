#!/usr/bin/env python3
"""A pretend agent CLI for the tests: answers instantly, costs nothing.

Usage: fake_agent.py claude|codex <the real CLI's arguments...>
It prints the same kind of JSON event stream as the real CLI, so the app's real
adapter code parses it. Controlled by environment variables:
  FAKE_LOG    append one JSON line per run: {"style", "argv", "cwd"}
  FAKE_RULES  JSON list of {"style"?, "if"?, "reply"?, "write"?: {path: text}, "exit"?: code};
              the first rule whose style matches and whose "if" text is in its arguments
              (prompt or system prompt) wins.
"""
import json
import os
import sys
import uuid

style, argv = sys.argv[1], sys.argv[2:]
prompt = argv[-1] if argv else ""
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({"style": style, "argv": argv, "cwd": os.getcwd()}) + "\n")

rule = {}
for r in json.loads(os.environ.get("FAKE_RULES") or "[]"):
    if r.get("style", style) == style and r.get("if", "") in "\n".join(argv):
        rule = r
        break
reply = rule.get("reply", f"fake {style} reply")
writes = rule.get("write") or {}
for path, text in writes.items():
    with open(path, "w") as f:
        f.write(text)


def emit(ev):
    print(json.dumps(ev), flush=True)


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
    with open(out, "w") as f:
        f.write(reply)
