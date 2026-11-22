"""The OpenAI-compatible chat API adapter, against a fake API server (no network, no cost)."""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import ChatTest, srv
from agent_groupchat.adapters import ADAPTERS, load_adapters, parse_apis


class FakeAPI(BaseHTTPRequestHandler):
    requests = []        # every chat request body + auth header
    mode = "ok"          # ok | error | reject_stream_options | empty

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self._json({"data": [{"id": "llama-test"}, {"id": "other"}]})
        else:
            self._json({"error": "no"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAPI.requests.append({"body": body, "auth": self.headers.get("Authorization")})
        if FakeAPI.mode == "error":
            return self._json({"error": {"message": "model overloaded"}}, 500)
        if FakeAPI.mode == "reject_stream_options" and "stream_options" in body:
            return self._json({"error": {"message": "unknown field stream_options"}}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        words = [] if FakeAPI.mode == "empty" else ["Hello", " from", f" {body['model']}"]
        for w in words:
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': {'content': w}}]})}\n\n".encode())
        usage = {"prompt_tokens": 42, "completion_tokens": 3}
        self.wfile.write(f"data: {json.dumps({'choices': [], 'usage': usage})}\n\ndata: [DONE]\n\n".encode())

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ApiAgent(ChatTest):
    @classmethod
    def setUpClass(cls):
        cls.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        threading.Thread(target=cls.api.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.api.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()

    def setUp(self):
        super().setUp()
        FakeAPI.requests.clear()
        FakeAPI.mode = "ok"
        self.config({"apis": [
            {"id": "llama", "name": "Llama", "base_url": self.url, "model": "llama-test"},
            {"id": "gpt", "name": "GPT", "base_url": self.url, "model": "gpt-test",
             "key_env": "GC_TEST_API_KEY"}]})
        os.environ["GC_TEST_API_KEY"] = "sk-test-123"

    def tearDown(self):
        os.remove(srv.CONFIG_FILE)
        load_adapters(srv.CONFIG_FILE, force=True)
        os.environ.pop("GC_TEST_API_KEY", None)
        super().tearDown()

    def config(self, cfg):
        with open(srv.CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        load_adapters(srv.CONFIG_FILE, force=True)

    def test_configured_apis_become_agents(self):
        self.assertIn("llama", ADAPTERS)
        self.assertEqual(ADAPTERS["gpt"].levels, ("talk",))

    def test_reply_stats_and_history(self):
        c = self.chat(agents=["claude", "llama"])
        self.send(c, "@llama hi")
        (r,) = self.replies(c, "llama")
        self.assertEqual(r["text"], "Hello from llama-test")
        self.assertEqual(r["meta"]["tokens"], [42, 3])
        body = FakeAPI.requests[0]["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("You are Llama", body["messages"][0]["content"])
        self.assertIn("TALK ONLY: you are connected through a chat API", body["messages"][0]["content"])
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "[User]: @llama hi"})
        self.assertIsNone(FakeAPI.requests[0]["auth"])
        sid = c["sessions"]["llama"]
        self.assertTrue(sid)
        self.send(c, "@llama and now?")
        msgs = FakeAPI.requests[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[2]["content"], "Hello from llama-test")
        self.assertEqual(c["sessions"]["llama"], sid)

    def test_key_comes_from_the_environment_only(self):
        c = self.chat(agents=["gpt"])
        self.send(c, "hi")
        self.assertEqual(FakeAPI.requests[0]["auth"], "Bearer sk-test-123")
        with open(srv.CONFIG_FILE) as f:
            self.assertNotIn("sk-test-123", f.read())
        with open(srv.chat_path(c["id"])) as f:
            self.assertNotIn("sk-test-123", f.read())

    def test_missing_key_is_a_clear_error(self):
        os.environ.pop("GC_TEST_API_KEY")
        c = self.chat(agents=["gpt"])
        self.send(c, "hi")
        (r,) = self.replies(c)
        self.assertIn("GC_TEST_API_KEY", r["text"])
        self.assertTrue(r["text"].startswith("(error)"))
        self.assertEqual(FakeAPI.requests, [])

    def test_http_error_and_empty_reply(self):
        for mode, expect in (("error", "HTTP 500"), ("empty", "empty reply")):
            with self.subTest(mode=mode):
                FakeAPI.mode = mode
                c = self.chat(agents=["llama"])
                self.send(c, "hi")
                (r,) = self.replies(c)
                self.assertTrue(r["text"].startswith("(error)"), r["text"])
                self.assertIn(expect, r["text"])
                self.assertIsNone(c["sessions"]["llama"])

    def test_server_without_stream_options(self):
        FakeAPI.mode = "reject_stream_options"
        c = self.chat(agents=["llama"])
        self.send(c, "hi")
        self.assertEqual(self.replies(c)[0]["text"], "Hello from llama-test")
        self.assertNotIn("stream_options", FakeAPI.requests[-1]["body"])

    def test_model_per_copy(self):
        c = self.chat(agents=[{"adapter": "llama", "model": "other"}])
        self.send(c, "hi")
        self.assertEqual(FakeAPI.requests[0]["body"]["model"], "other")

    def test_only_talk_is_possible(self):
        for level in ("read", "work"):
            with self.subTest(level=level), self.assertRaises(ValueError):
                srv.parse_agents([{"adapter": "llama", "level": level}])
        c = self.chat(agents=[{"adapter": "llama", "roles": ["reviewer"]}, "claude"])
        info = srv.level_info(c, "llama")  # Reviewer wants read; a chat API can only talk
        self.assertEqual((info["level"], info["requested"]), ("talk", "read"))
        self.assertIn("can only talk", info["limited"])

    def test_detect(self):
        self.assertEqual(ADAPTERS["llama"].detect()[0], True)
        self.config({"apis": [{"id": "llama", "name": "Llama", "base_url": self.url,
                               "model": "missing-model"}]})
        ok, msg = ADAPTERS["llama"].detect()
        self.assertFalse(ok)
        self.assertIn("missing-model", msg)

    def test_removed_api_gives_a_clear_error(self):
        c = self.chat(agents=["llama", "claude"])
        self.config({})
        self.send(c, "@llama hi")
        self.assertIn("isn't configured anymore", self.replies(c, "llama")[0]["text"])


class ConfigValidation(unittest.TestCase):
    def test_bad_entries(self):
        good = {"id": "x", "name": "X", "base_url": "http://localhost:1/v1", "model": "m"}
        for bad in ({**good, "id": "claude"}, {**good, "id": "Bad Id"},
                    {**good, "base_url": "file:///etc/passwd"}, {**good, "model": ""},
                    {**good, "key_env": "sk-abc123 real key"}, {**good, "name": ""}, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_apis([bad])
        with self.assertRaises(ValueError):
            parse_apis([good, good])  # duplicate id
        self.assertEqual(parse_apis([good])[0].id, "x")


if __name__ == "__main__":
    unittest.main()
