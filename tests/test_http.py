"""The HTTP layer: only the page itself (right Host, Origin and launch token) can drive it."""
import http.client
import json
import os
import re
import threading
import time
import unittest

from tests.helpers import ChatTest, srv


class HttpBase(ChatTest):
    """Runs the real HTTP server on the test port (no tests of its own)."""
    @classmethod
    def setUpClass(cls):
        cls.server = srv.ThreadingHTTPServer(("127.0.0.1", srv.PORT), srv.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, body=None, token=True, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", srv.PORT, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{srv.PORT}"}
        if token:
            headers["X-Groupchat-Token"] = srv.TOKEN
        if origin:
            headers["Origin"] = origin
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        out = r.status, r.read()
        conn.close()
        return out

class Http(HttpBase):
    def test_page_carries_the_launch_token(self):
        code, body = self.req("GET", "/", token=False)
        self.assertEqual(code, 200)
        self.assertIn(srv.TOKEN.encode(), body)
        self.assertNotIn(b"__GC_TOKEN__", body)

    def test_post_needs_token(self):
        code, _ = self.req("POST", "/api/chats", {"folder": self.folder}, token=False)
        self.assertEqual(code, 403)

    def test_wrong_host_is_rejected(self):
        for method in ("GET", "POST"):
            code, _ = self.req(method, "/api/chats", {} if method == "POST" else None,
                               host="evil.example")
            self.assertEqual(code, 403)

    def test_foreign_origin_is_rejected(self):
        code, _ = self.req("POST", "/api/chats", {"folder": self.folder},
                           origin="https://evil.example")
        self.assertEqual(code, 403)

    def test_chat_round_trip(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "mode": "talk"})
        self.assertEqual(code, 200)
        cid = json.loads(body)["id"]
        code, _ = self.req("POST", f"/api/chats/{cid}/send", {"text": "hello"})
        self.assertEqual(code, 200)
        for _ in range(100):
            chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
            if not chat["busy"] and len(chat["messages"]) == 3:
                break
            time.sleep(0.05)
        self.assertEqual(sorted(m["who"] for m in chat["messages"][1:]), ["claude", "codex"])
        code, md = self.req("GET", f"/api/chats/{cid}/export")
        self.assertEqual(code, 200)
        self.assertIn(b"fake claude reply", md)
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/delete", {})[0], 200)
        self.assertNotIn(cid, srv.chats)


    def test_create_chat_with_agents(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": [
            {"adapter": "claude"}, {"adapter": "claude", "model": "sonnet"}, {"adapter": "codex"}]})
        self.assertEqual(code, 200, body)
        cid = json.loads(body)["id"]
        chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
        self.assertEqual([a["name"] for a in chat["agents"]], ["Claude 1", "Claude 2", "Codex"])
        self.assertEqual(chat["agents"][1]["model"], "sonnet")
        self.assertEqual(chat["aliases"]["claude"], ["claude-1", "claude-2"])
        self.assertEqual(chat["aliases"]["claude2"], ["claude-2"])
        self.assertEqual(chat["colors"]["codex"], "#10a37f")
        self.assertEqual(chat["resume"], {})
        listing = json.loads(self.req("GET", "/api/chats")[1])
        self.assertEqual([a["id"] for a in listing["adapters"]][:2], ["claude", "codex"])
        self.req("POST", f"/api/chats/{cid}/delete", {})

    def test_bad_agents_are_rejected(self):
        for agents in ([], [{"adapter": "skynet"}], [{"adapter": "claude", "model": "a b"}]):
            code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": agents})
            self.assertEqual(code, 400, agents)
            self.assertIn("error", json.loads(body))

    def test_roles_and_permissions_api(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder, "agents": [
            {"adapter": "claude", "roles": ["lead"]}, {"adapter": "codex", "roles": ["reviewer"]}]})
        self.assertEqual(code, 200, body)
        cid = json.loads(body)["id"]
        chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
        self.assertEqual(chat["perm"]["claude"]["default"], "work")
        self.assertEqual(chat["perm"]["codex"]["level"], "read")
        self.assertIn("edit", chat["perm"]["codex"]["choices"])
        self.assertTrue(chat["perm"]["codex"]["choices"]["edit"])  # greyed out, with a reason
        # raise the Reviewer to work: allowed, and flagged as raised
        code, _ = self.req("POST", f"/api/chats/{cid}/agents", {"agents": [
            {"id": "codex", "roles": ["reviewer"], "level": "work"}]})
        self.assertEqual(code, 200)
        self.assertTrue(srv.level_info(srv.chats[cid], "codex")["raised"])
        # the same agent can't be Lead and Reviewer
        code, body = self.req("POST", f"/api/chats/{cid}/agents", {"agents": [
            {"id": "claude", "roles": ["lead", "reviewer"]}]})
        self.assertEqual(code, 400)
        self.assertIn("review its own work", json.loads(body)["error"])
        # Codex can't do "edit"
        code, _ = self.req("POST", f"/api/chats/{cid}/agents", {"agents": [
            {"id": "codex", "roles": [], "level": "edit"}]})
        self.assertEqual(code, 400)
        # lock + fix rounds
        self.req("POST", f"/api/chats/{cid}/settings", {"lock": True, "fix_rounds": 9})
        self.assertEqual(srv.chats[cid]["fix_rounds"], srv.MAX_FIX_ROUNDS)
        self.assertEqual(srv.level_of(srv.chats[cid], "claude"), "talk")
        listing = json.loads(self.req("GET", "/api/chats")[1])
        self.assertIn("planner", [r["id"] for r in listing["roles"]])
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/go", {})[0], 409)  # nothing waits
        self.req("POST", f"/api/chats/{cid}/delete", {})


    def test_adapter_test_endpoint(self):
        code, body = self.req("GET", "/api/adapters/claude/test")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["ok"], True)  # the fake claude answers --version
        r = json.loads(self.req("GET", "/api/adapters/nope/test")[1])
        self.assertEqual(r["ok"], False)
        listing = json.loads(self.req("GET", "/api/chats")[1])
        aider = next(a for a in listing["adapters"] if a["id"] == "aider")
        self.assertTrue(aider["needs_model"])
        self.assertEqual(aider["levels"], ["talk", "read", "edit"])


    def test_bad_input_gets_a_400_not_a_dropped_connection(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder})
        cid = json.loads(body)["id"]
        for path, payload in (("/api/chats", ["not", "an", "object"]),
                              ("/api/chats", {"folder": self.folder, "rounds": "abc"}),
                              ("/api/chats", {"folder": self.folder, "fix_rounds": "x"}),
                              (f"/api/chats/{cid}/settings", {"rounds": "abc"}),
                              (f"/api/chats/{cid}/settings", ["x"]),
                              (f"/api/chats/{cid}/send", {"text": 42}),
                              ("/api/config", {"teams": [{"name": "T", "agents": [], "rounds": "x"}]}),
                              ("/api/config", "just a string")):
            with self.subTest(path=path, payload=payload):
                code, body = self.req("POST", path, payload)
                self.assertEqual(code, 400, body)
                self.assertIn("error", json.loads(body))
        self.req("POST", f"/api/chats/{cid}/delete", {})

    def test_deleting_a_chat_while_an_agent_replies(self):
        self.rules({"sleep": 1.5})
        code, body = self.req("POST", "/api/chats", {"folder": self.folder,
                                                     "agents": [{"adapter": "claude"}]})
        cid = json.loads(body)["id"]
        self.req("POST", f"/api/chats/{cid}/send", {"text": "hello"})
        for _ in range(100):  # wait until the agent is running
            if (cid, "claude") in srv.procs:
                break
            time.sleep(0.05)
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/delete", {})[0], 200)
        for _ in range(200):  # let the worker finish
            if cid not in srv.workers:
                break
            time.sleep(0.05)
        time.sleep(0.3)
        self.assertFalse(os.path.exists(srv.chat_path(cid)), "the deleted chat was saved again")
        self.assertFalse(os.path.exists(os.path.join(srv.CHATS_DIR, cid + ".diffs")))
        srv.load_all()
        self.assertNotIn(cid, srv.chats)  # it doesn't come back after a restart


    def test_git_status_and_init(self):
        code, body = self.req("POST", "/api/chats", {"folder": self.folder})
        cid = json.loads(body)["id"]
        with open(os.path.join(self.folder, "notes.md"), "w") as f:
            f.write("hello\n")
        chat = json.loads(self.req("GET", f"/api/chats/{cid}")[1])
        self.assertEqual((chat["git"]["ok"], chat["git"]["can_init"]), (False, True))
        code, body = self.req("POST", f"/api/chats/{cid}/git-init", {})
        self.assertEqual(code, 200, body)
        self.assertTrue(json.loads(body)["git"]["ok"])
        self.assertTrue(json.loads(self.req("GET", f"/api/chats/{cid}")[1])["git"]["ok"])
        with open(os.path.join(self.folder, "notes.md")) as f:
            self.assertEqual(f.read(), "hello\n")  # the files didn't change
        self.assertEqual(self.req("POST", f"/api/chats/{cid}/git-init", {})[0], 400)  # already a repo
        self.req("POST", f"/api/chats/{cid}/delete", {})


if __name__ == "__main__":
    unittest.main()
