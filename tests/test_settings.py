"""Settings page backend: chat APIs, custom roles, saved teams, importing old chats."""
import json
import os
import shutil

from tests.helpers import LEGACY, ChatTest, srv
from tests.test_http import HttpBase
from agent_groupchat.adapters import ADAPTERS, load_adapters

API = {"id": "llama", "name": "Llama", "base_url": "http://localhost:11434/v1", "model": "llama3.1"}


class Settings(HttpBase):
    def tearDown(self):
        if os.path.exists(srv.CONFIG_FILE):
            os.remove(srv.CONFIG_FILE)
        load_adapters(srv.CONFIG_FILE, force=True)
        super().tearDown()

    def post(self, body):
        code, raw = self.req("POST", "/api/config", body)
        return code, json.loads(raw)

    def test_apis(self):
        code, r = self.post({"apis": [API]})
        self.assertEqual(code, 200, r)
        self.assertIn("llama", ADAPTERS)
        self.assertEqual(json.loads(self.req("GET", "/api/config")[1])["apis"][0]["model"], "llama3.1")
        code, r = self.post({"apis": [{**API, "key_env": "sk-proj-abcdefgh12345"}]})
        self.assertEqual(code, 400)
        self.assertIn("looks like an API key", r["error"])
        code, r = self.post({"apis": [{**API, "id": "codex"}]})
        self.assertEqual(code, 400)
        self.assertIn("llama", ADAPTERS)  # a refused save changes nothing

    def test_roles(self):
        code, r = self.post({"roles": [{"name": "Translator", "job": "Translate to Turkish.",
                                        "level": "edit"}]})
        self.assertEqual(code, 200, r)
        self.assertEqual(srv.roles_now()["translator"].level, "edit")
        for bad in ([{"name": "Lead"}], [{"name": "X", "level": "god"}],
                    [{"name": "A b"}, {"name": "a B"}], [{"name": "<script>"}]):
            with self.subTest(bad=bad):
                self.assertEqual(self.post({"roles": bad})[0], 400)
        listing = json.loads(self.req("GET", "/api/chats")[1])
        self.assertIn("translator", [x["id"] for x in listing["roles"]])

    def test_teams_and_sections_are_kept(self):
        self.post({"apis": [API]})
        team = {"name": "Claude builds, Codex reviews", "fix_rounds": 2, "agents": [
            {"adapter": "claude", "roles": ["lead"]}, {"adapter": "codex", "roles": ["reviewer"]}]}
        code, r = self.post({"teams": [team]})
        self.assertEqual(code, 200, r)
        cfg = json.loads(self.req("GET", "/api/config")[1])
        self.assertEqual(cfg["apis"][0]["id"], "llama")  # untouched by the teams save
        self.assertEqual(cfg["teams"][0]["fix_rounds"], 2)
        self.assertEqual(cfg["teams"][0]["agents"][1]["roles"], ["reviewer"])
        for bad in ([{**team, "agents": [{"adapter": "claude", "roles": ["lead", "reviewer"]}]}],
                    [team, team], [{**team, "name": ""}]):
            with self.subTest(bad=bad):
                self.assertEqual(self.post({"teams": bad})[0], 400)


class LegacyImport(ChatTest):
    def setUp(self):
        super().setUp()
        self.old = {"id": "abcdef123456", "title": "old chat", "folder": self.folder,
                    "mode": "work", "rounds": 1, "created": 0, "updated": 0, "messages": [
                        {"who": "user", "text": "hi", "ts": 0}],
                    "seen": {"claude": 1, "codex": 1}, "sessions": {"claude": "s1", "codex": None}}
        with open(os.path.join(LEGACY, "abcdef123456.json"), "w") as f:
            json.dump(self.old, f)
        os.makedirs(os.path.join(LEGACY, "abcdef123456.diffs"))
        with open(os.path.join(LEGACY, "abcdef123456.diffs", "1.diff"), "w") as f:
            f.write("+x\n")

    def tearDown(self):
        for n in os.listdir(LEGACY):
            p = os.path.join(LEGACY, n)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        srv.chats.pop("abcdef123456", None)
        for p in (srv.chat_path("abcdef123456"), srv.CONFIG_FILE):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(os.path.join(srv.CHATS_DIR, "abcdef123456.diffs"), ignore_errors=True)
        super().tearDown()

    def test_import_copies_and_migrates(self):
        self.assertEqual(srv.legacy_files(), ["abcdef123456.json"])
        self.assertEqual(srv.import_legacy(), 1)
        c = srv.chats["abcdef123456"]
        self.assertEqual(srv.agent_ids(c), ["claude", "codex"])
        self.assertEqual(c["sessions"]["claude"], "s1")
        self.assertTrue(os.path.exists(os.path.join(LEGACY, "abcdef123456.json")))  # original kept
        self.assertTrue(os.path.exists(srv.diff_path("abcdef123456", 1)))
        self.assertEqual(srv.legacy_files(), [])  # offered only once

    def test_dismiss(self):
        import agent_groupchat.config as cfg
        cfg.update(srv.CONFIG_FILE, legacy_import="dismissed")
        self.assertEqual(srv.legacy_files(), [])
