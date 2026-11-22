"""Any OpenAI-compatible chat API: OpenAI, Ollama, OpenRouter, Groq, LM Studio...

Talk only: a chat API has no tools, so it never sees or touches files; it sees the
group-chat messages. Its conversation history is kept in the app's data folder
(<data>/sessions/api-<session>.json), so it continues like a CLI session would.

Configured in ~/.agent-groupchat/config.json:
  {"apis": [{"id": "ollama", "name": "Llama", "base_url": "http://localhost:11434/v1",
             "model": "llama3.1", "key_env": ""},
            {"id": "openai", "name": "GPT", "base_url": "https://api.openai.com/v1",
             "model": "gpt-4.1", "key_env": "OPENAI_API_KEY"}]}
key_env is the NAME of the environment variable holding the API key, never the key.
"""
import json
import os
import sys
import tempfile
import urllib.request
import uuid

from .base import Adapter

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openai_stream.py")
MAX_HISTORY_CHARS = 200_000  # oldest turns are dropped beyond this


class OpenAIChatAdapter(Adapter):
    kind = "a chat model through an API (no tools: it only sees the chat messages)"
    color = "#6b7280"
    skill_syntax = "none"
    levels = ("talk",)
    level_notes = {lv: "A chat API has no tools: it can only talk."
                   for lv in ("read", "edit", "work", "auto")}
    mode_texts = {"talk": "TALK ONLY: you are connected through a chat API with no tools, so you "
                          "can't see or change files; you only see the chat messages."}

    def __init__(self, id, name, base_url, model, key_env="", color=None):
        self.id, self.name, self.base_url, self.model = id, name, base_url, model
        self.key_env = key_env or ""
        if color:
            self.color = color

    def run(self, ctx, prompt, level):
        sid = ctx.session or str(uuid.uuid4())
        os.makedirs(ctx.state_dir, exist_ok=True)
        path = os.path.join(ctx.state_dir, f"api-{sid}.json")
        try:
            with open(path, encoding="utf-8") as f:
                history = json.load(f)
        except (OSError, ValueError):
            history = []
        while history and sum(len(m["content"]) for m in history) > MAX_HISTORY_CHARS:
            history = history[2:]
        messages = [{"role": "system", "content": ctx.system_prompt}, *history,
                    {"role": "user", "content": prompt}]
        out = {"text": "", "usage": None, "error": None}

        def on_event(ev):
            t = ev.get("type")
            if t == "delta":
                if not out["text"]:
                    ctx.preview("")
                    ctx.step("💬 writing")
                out["text"] += ev.get("text", "")
                ctx.preview(ev.get("text", ""), append=True)
            elif t == "done":
                out["usage"] = ev.get("usage")
            elif t == "error":
                out["error"] = ev.get("message")

        fd, req = tempfile.mkstemp(suffix=".json", prefix="gc-api-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"base_url": self.base_url, "model": ctx.model or self.model,
                           "key_env": self.key_env, "messages": messages}, f)
            ctx.step(f"🌐 asking {ctx.model or self.model}")
            code, err, why = ctx.stream([sys.executable, "-I", HELPER, req], on_event)
        finally:
            os.unlink(req)
        u = out["usage"] or {}
        meta = {"files": [], "cost": None,
                "tokens": [u.get("prompt_tokens", 0), u.get("completion_tokens", 0)] if u else None}
        if why:
            return f"({why})", meta
        text = out["text"].strip()
        if out["error"] or code or not text:
            return "(error) " + (out["error"] or err or (f"exit code {code}" if code else
                                                         "the API sent an empty reply"))[-800:], meta
        history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": text}]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f)
        ctx.save_session(sid)
        return text, meta

    def installed(self):
        return True  # configured in config.json; detect() checks it's reachable

    def detect(self):
        """(ok, message) for the Test button: can we reach the API? Costs no tokens."""
        req = urllib.request.Request(self.base_url.rstrip("/") + "/models")
        key = os.environ.get(self.key_env, "") if self.key_env else ""
        if self.key_env and not key:
            return False, f"The environment variable {self.key_env} isn't set."
        if key:
            req.add_header("Authorization", "Bearer " + key)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                models = [m.get("id") for m in json.load(r).get("data", [])]
        except Exception as e:
            return False, f"Can't reach {self.base_url}: {e}"
        if models and self.model not in models:
            return False, f"Reached the API, but it has no model {self.model!r}."
        return True, f"Reached {self.base_url}" + (f" ({len(models)} models)" if models else "")
