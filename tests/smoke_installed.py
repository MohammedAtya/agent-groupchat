"""Smoke test for an INSTALLED agent-groupchat (pipx install . / pip install agent-groupchat).

Starts the `agent-groupchat` command with a throwaway data folder, checks that the page and
the API answer, then stops it. Used by CI on Linux, macOS and Windows. Not a unittest file
(it needs the package installed), so `unittest discover` skips it.
Usage: python tests/smoke_installed.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    exe = shutil.which("agent-groupchat")
    if not exe:
        sys.exit("agent-groupchat is not on PATH")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    print(version.stdout.strip())
    assert version.returncode == 0 and "agent-groupchat" in version.stdout, version

    port, data = free_port(), tempfile.mkdtemp()
    env = {**os.environ, "AGENT_GROUPCHAT_HOME": data, "AGENT_GROUPCHAT_LEGACY": data}
    proc = subprocess.Popen([exe, "--port", str(port), "--no-browser"], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(base + "/", timeout=2) as r:
                    page, header = r.read().decode(), r.headers.get("X-Agent-Groupchat")
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise SystemExit("the server never answered")
        assert 'id="newBtn"' in page and header, "the page or its header is missing"
        assert "__GC_TOKEN__" not in page, "the launch token wasn't filled in"
        with urllib.request.urlopen(base + "/api/chats", timeout=5) as r:
            listing = json.load(r)
        ids = [a["id"] for a in listing["adapters"]]
        assert ids[:4] == ["claude", "codex", "gemini", "aider"], ids
        assert os.path.isdir(os.path.join(data, "chats")), "no data folder was made"
        print(f"OK: page and API answer on {base} ({len(page)} bytes; adapters {ids})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
