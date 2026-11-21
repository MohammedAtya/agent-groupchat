"""The HTTP layer: only the page itself (right Host, Origin and launch token) can drive it."""
import http.client
import json
import re
import threading
import time
import unittest

from tests.helpers import ChatTest, srv


class Http(ChatTest):
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


if __name__ == "__main__":
    unittest.main()
