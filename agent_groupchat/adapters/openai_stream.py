"""Helper process for the chat-API adapter: one streaming chat completion.

Usage: python -I openai_stream.py REQUEST.json
REQUEST.json: {"base_url", "model", "key_env", "messages"}. The API key is read from the
environment variable named by key_env, so it is never written to any file.
Prints one JSON event per line: {"type": "delta", "text"} ... then
{"type": "done", "usage"} or {"type": "error", "message"} (exit code 1).
It runs as its own process so the app's Stop button and timeout can kill it like a CLI.
No imports from the app: it must run standalone.
"""
import json
import os
import sys
import urllib.error
import urllib.request


def emit(ev):
    print(json.dumps(ev), flush=True)


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    return urllib.request.urlopen(req, timeout=600)


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        req = json.load(f)
    key = os.environ.get(req["key_env"], "") if req.get("key_env") else ""
    if req.get("key_env") and not key:
        emit({"type": "error", "message": f"The environment variable {req['key_env']} "
                                          "(the API key) isn't set for the chat server."})
        sys.exit(1)
    url = req["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    body = {"model": req["model"], "messages": req["messages"], "stream": True,
            "stream_options": {"include_usage": True}}
    try:
        try:
            resp = post(url, body, headers)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            body.pop("stream_options")  # some servers reject it: try once without
            resp = post(url, body, headers)
        usage = None
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("error"):
                raise RuntimeError(ev["error"].get("message") if isinstance(ev["error"], dict)
                                   else ev["error"])
            usage = ev.get("usage") or usage
            for ch in ev.get("choices") or []:
                text = (ch.get("delta") or {}).get("content")
                if text:
                    emit({"type": "delta", "text": text})
        emit({"type": "done", "usage": usage})
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        emit({"type": "error", "message": f"HTTP {e.code} from {url}: {detail}"})
        sys.exit(1)
    except Exception as e:  # connection refused, bad JSON, timeout...
        emit({"type": "error", "message": f"{type(e).__name__}: {e} ({url})"})
        sys.exit(1)


if __name__ == "__main__":
    main()
