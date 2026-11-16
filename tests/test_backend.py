# -*- coding: utf-8 -*-
"""后端单元测试 + HTTP 接口契约测试。"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import server  # noqa: E402


class BackendBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.data = self.dir / "data"
        self.sess = self.dir / "sessions"
        self.data.mkdir()
        self.sess.mkdir()

        self._saved = {}
        for name in ("DATA_DIR", "SESS_DIR", "HISTORY_PATH", "AI_DIR", "AI_CFG_PATH",
                     "_master_key", "refresh_all", "hotlists_payload", "hot_payload",
                     "refresh_async", "load_ai_config", "_deepseek_chat",
                     "_idea_top_content", "_idea_hot_context",
                     "_fetch_xhs_hot", "_fetch_douyin_hot", "_filter_hotlists"):
            if hasattr(server, name):
                self._saved[name] = getattr(server, name)

        server.DATA_DIR = self.data
        server.SESS_DIR = self.sess
        server.HISTORY_PATH = self.data / "history.json"
        server.AI_DIR = self.dir
        server.AI_CFG_PATH = self.dir / "ai.bin"
        server._master_key = lambda: b"0" * 32
        server._hotlists.update(at=0.0, data=None)
        server._COLLECTING.clear()

        for setter in (server.set_state, server.dset, server.bset, server.wset):
            setter("disconnected")

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(server, name, value)
        self.tmp.cleanup()

    def write_creator(self, pid: str, age_hours: float):
        ts = datetime.datetime.now() - datetime.timedelta(hours=age_hours)
        (self.data / f"{pid}_creator.json").write_text(json.dumps({
            "account": {"nickname": f"{pid}-nick"},
            "collectedAt": ts.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        }))

    def write_session(self, pid: str):
        (self.sess / f"{pid}.bin").write_bytes(b"test")


class TestBackendUnits(BackendBase):
    def test_num(self):
        self.assertEqual(server._num("3.3万"), 33000)
        self.assertEqual(server._num("1.2亿"), 120000000)
        self.assertEqual(server._num("1,234"), 1234)
        self.assertEqual(server._num(42), 42)
        self.assertIsNone(server._num("—"))
        self.assertIsNone(server._num(None))

    def test_read_platform_data_legacy(self):
        p = self.data / "legacy.json"
        p.write_text(json.dumps({"weekNotes": [{"title": "旧笔记"}]}, ensure_ascii=False))
        d = server.read_platform_data(p)
        self.assertEqual(d["contents"][0]["title"], "旧笔记")

        p.write_text(json.dumps({"contents": [{"title": "新笔记"}]}, ensure_ascii=False))
        d = server.read_platform_data(p)
        self.assertEqual(d["contents"][0]["title"], "新笔记")

    def test_save_snapshot_overwrites_same_day(self):
        server.save_snapshot("xhs", {"fans": 100})
        server.save_snapshot("xhs", {"fans": 120})
        h = json.loads(server.HISTORY_PATH.read_text())
        self.assertEqual(len(h["xhs"]), 1)
        self.assertEqual(h["xhs"][0]["fans"], 120)

    def test_save_snapshot_caps_180_days(self):
        base = datetime.date(2020, 1, 1)
        rows = [{"date": str(base + datetime.timedelta(days=i)), "fans": i}
                for i in range(200)]
        server.HISTORY_PATH.write_text(json.dumps({"xhs": rows}))
        server.save_snapshot("xhs", {"fans": 999})
        h = json.loads(server.HISTORY_PATH.read_text())
        self.assertEqual(len(h["xhs"]), 180)
        self.assertEqual(h["xhs"][-1]["fans"], 999)

    def test_catch_up_uses_newest_file(self):
        calls = []
        server.refresh_all = lambda reason="": calls.append(reason) or True
        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            self.write_creator(pid, 1)
        server._catch_up_if_stale()
        self.assertEqual(calls, [])

        self.write_creator("bilibili", 48)
        server._catch_up_if_stale()
        self.assertEqual(calls, [], "只要有平台是新的，就不该全量补采")

        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            self.write_creator(pid, 48)
        server._catch_up_if_stale()
        self.assertEqual(len(calls), 1)

    def test_catch_up_no_files_does_nothing(self):
        calls = []
        server.refresh_all = lambda reason="": calls.append(reason) or True
        server._catch_up_if_stale()
        self.assertEqual(calls, [])

    def test_catch_up_all_old_refreshes_once(self):
        calls = []
        server.refresh_all = lambda reason="": calls.append(reason) or True
        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            self.write_creator(pid, 30)
        server._catch_up_if_stale()
        self.assertEqual(calls, ["启动补采"])

    def test_catch_up_24h_boundary(self):
        calls = []
        server.refresh_all = lambda reason="": calls.append(reason) or True
        for pid in ("xhs", "douyin", "wechat"):
            self.write_creator(pid, 23.5)
        # 最旧的一个刚好超过 24 小时，但最新的仍然新鲜 -> 不补采
        self.write_creator("bilibili", 24.5)
        server._catch_up_if_stale()
        self.assertEqual(calls, [])

        # 四份都超过 24 小时 -> 补采
        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            self.write_creator(pid, 24.5)
        server._catch_up_if_stale()
        self.assertEqual(len(calls), 1)

    def test_catch_up_ignores_invalid_timestamps(self):
        calls = []
        server.refresh_all = lambda reason="": calls.append(reason) or True
        # 三份新鲜 + 一份时间戳损坏：最新一份仍新鲜，跳过
        for pid in ("xhs", "douyin", "wechat"):
            self.write_creator(pid, 1)
        (self.data / "bilibili_creator.json").write_text('{"collectedAt": "bad"}')
        server._catch_up_if_stale()
        self.assertEqual(calls, [])

        # 全部损坏 -> 当作很旧，补采
        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            (self.data / f"{pid}_creator.json").write_text('{"collectedAt": "bad"}')
        server._catch_up_if_stale()
        self.assertEqual(len(calls), 1)

    def test_restore_states_requires_fresh_data(self):
        for pid in ("xhs", "douyin", "bilibili", "wechat"):
            self.write_session(pid)
            self.write_creator(pid, 1)
        self.write_creator("bilibili", 48)
        server._restore_states()
        self.assertEqual(server.snapshot()["state"], "connected")
        self.assertEqual(server.dsnapshot()["state"], "connected")
        self.assertEqual(server.wsnapshot()["state"], "connected")
        self.assertEqual(server.bsnapshot()["state"], "disconnected")
        self.assertIn("待验证", server.bsnapshot()["message"])

    def test_ai_config_roundtrip(self):
        cfg = dict(server.AI_DEFAULTS)
        cfg.update({"apiKey": "sk-test", "model": "deepseek-chat"})
        server.save_ai_config(cfg)
        loaded = server.load_ai_config()
        self.assertEqual(loaded["apiKey"], "sk-test")
        pub = server.ai_public_settings()
        self.assertNotIn("apiKey", pub)
        self.assertTrue(pub["configured"])

    def test_wechat_recent_articles_dedupe(self):
        ts = int(datetime.datetime.now().timestamp())
        info = {
            "sent_info": {"time": ts},
            "appmsgex": [
                {"title": "A", "link": "l1", "update_time": ts},
                {"title": "A", "link": "l2", "update_time": ts},
                {"title": "B", "link": "l3", "update_time": ts},
            ],
        }
        publish = {"publish_page": json.dumps({
            "publish_list": [{"publish_info": json.dumps(info)}]
        })}
        rows = server._wechat_recent_articles(publish, limit=5)
        self.assertEqual([x["title"] for x in rows], ["A", "B"])

    def test_bili_recent_works(self):
        now = datetime.datetime.now()
        recent = int(now.timestamp())
        old = int((now - datetime.timedelta(days=40)).timestamp())

        def arc(title, ts, state="开放浏览"):
            return {"Archive": {"title": title, "ptime": ts, "state_desc": state,
                                "duration": 30, "cover": ""},
                    "stat": {"view": 100, "like": 5, "reply": 1,
                             "favorite": 2, "share": 0}}

        archives = {"data": {"arc_audits": [
            arc("近期稿件", recent),
            arc("旧稿件", old),
            arc("仅自己可见", recent, "仅自己可见"),
        ]}}
        rows = server._bili_recent_works(archives)
        self.assertEqual([x["title"] for x in rows], ["近期稿件"])

    def test_norm_idea_clamps(self):
        x = server._norm_idea({"title": "长" * 200, "score": 150,
                               "platform": "bad", "format": "未知"})
        self.assertEqual(len(x["title"]), 90)
        self.assertEqual(x["score"], 100)
        self.assertEqual(x["platform"], "both")
        y = server._norm_idea({"score": -5})
        self.assertEqual(y["score"], 0)

    def test_generate_ideas_prompt_contains_feedback(self):
        server.load_ai_config = lambda: {
            "apiKey": "x", "model": "deepseek-chat",
            "interest": "测试方向", "baseUrl": "https://api.deepseek.com",
        }
        server._idea_top_content = lambda limit=5: [{"platform": "xhs", "title": "旧高分"}]
        server._idea_hot_context = lambda: {
            "filtered": [{"platform": "xhs", "title": "筛选热点", "reason": "相关"}],
            "xhs": [{"title": "小红书热点", "category": "测试"}],
            "douyin": [{"title": "抖音热点"}],
        }
        captured = {}

        def fake_chat(messages, cfg=None, max_tokens=1200, timeout=45):
            captured["prompt"] = messages[-1]["content"]
            return '{"items":[{"title":"T","angle":"A"}]}'

        server._deepseek_chat = fake_chat
        res = server.generate_ideas({
            "did": "测试", "platform": "both", "style": {},
            "feedback": {
                "published": [
                    {"title": "有效选题", "platform": "xhs", "views": 1000,
                     "fansGain": 5, "score": 80, "ratio": 2.0, "verdict": "good"},
                    {"title": "无效选题", "platform": "douyin", "views": 100,
                     "fansGain": 0, "score": 30, "ratio": 0.3, "verdict": "bad"},
                ],
                "done": ["做过的主题"],
                "plan": [{"title": f"计划选题 {i}", "angle": "a"} for i in range(10)],
            },
        })
        self.assertTrue(res["ok"], res)
        prompt = captured["prompt"]
        self.assertIn("有效选题", prompt)
        self.assertIn("无效选题", prompt)
        self.assertIn("做过的主题", prompt)
        self.assertIn("计划选题 9", prompt)
        self.assertIn("优先复用", prompt)

    def test_generate_ideas_rejects_empty_hotlist(self):
        server.load_ai_config = lambda: {"apiKey": "x", "model": "deepseek-chat"}
        server._idea_hot_context = lambda: {"filtered": [], "xhs": [], "douyin": []}
        res = server.generate_ideas({"did": "x", "feedback": {}})
        self.assertFalse(res["ok"])
        self.assertIn("热榜", res["error"])

    def test_expand_idea(self):
        server.load_ai_config = lambda: {"apiKey": "x", "model": "deepseek-chat"}
        server._deepseek_chat = lambda messages, cfg=None, max_tokens=1200, timeout=45: \
            '{"titles":["标题一"],"covers":["封面一"],"hook":"开头","structure":["第一步"]}'
        res = server.expand_idea({"idea": {"title": "T", "angle": "A"},
                                  "style": {}, "feedback": {}})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["titles"], ["标题一"])
        self.assertEqual(res["structure"], ["第一步"])

    def test_hotlists_cache_and_fallback(self):
        server._hotlists.update(at=0.0, data=None)
        counts = {"xhs": 0, "douyin": 0}

        def boom():
            counts["xhs"] += 1
            raise RuntimeError("xhs down")

        def ok():
            counts["douyin"] += 1
            return {"source": "测试", "items": [{"title": "抖音热点"}]}

        server._fetch_xhs_hot = boom
        server._fetch_douyin_hot = ok
        server._filter_hotlists = lambda raw, cfg: {
            "mode": "rule", "model": None, "error": None,
            "items": [{"title": "筛选"}],
        }
        d1 = server.hotlists_payload()
        self.assertIn("xhs", d1["errors"])
        self.assertEqual(d1["douyin"]["items"][0]["title"], "抖音热点")
        d2 = server.hotlists_payload()
        self.assertEqual(counts, {"xhs": 1, "douyin": 1})
        server.hotlists_payload(force=True)
        self.assertEqual(counts, {"xhs": 2, "douyin": 2})


class TestBackendApi(BackendBase):
    def setUp(self):
        super().setUp()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode() or "{}"
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, {}

    def post(self, path, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def test_data_endpoint_normalizes_legacy(self):
        (self.data / "xhs_creator.json").write_text(json.dumps({
            "platform": {"id": "xhs"},
            "account": {"nickname": "测试", "fans": 1},
            "weekNotes": [{"title": "旧字段笔记", "postDate": "2026-01-01"}],
            "collectedAt": "2026-01-01T12:00:00+08:00",
        }, ensure_ascii=False))
        status, d = self.get("/api/xhs/data")
        self.assertEqual(status, 200)
        self.assertEqual(d["contents"][0]["title"], "旧字段笔记")

    def test_route_order_hotlists_before_hot(self):
        server.hotlists_payload = lambda force=False: {"marker": "hotlists"}
        server.hot_payload = lambda: {"marker": "hot"}
        _, a = self.get("/api/hotlists")
        _, b = self.get("/api/hot")
        self.assertEqual(a["marker"], "hotlists")
        self.assertEqual(b["marker"], "hot")

    def test_unknown_api_is_404(self):
        status, _ = self.get("/api/not-exist")
        self.assertEqual(status, 404)

    def test_state_and_settings(self):
        _, s = self.get("/api/xhs/state")
        self.assertIn("state", s)
        _, cfg = self.get("/api/settings")
        self.assertIn("ai", cfg)
        self.assertIn("dataDir", cfg)
        self.assertNotIn("apiKey", cfg["ai"])

    def test_refresh_endpoint(self):
        server.refresh_async = lambda reason="": True
        status, d = self.post("/api/refresh", {})
        self.assertEqual(status, 200)
        self.assertTrue(d["started"])

    def test_ai_settings_clear_key(self):
        cfg = dict(server.AI_DEFAULTS)
        cfg["apiKey"] = "sk-test"
        server.save_ai_config(cfg)
        status, d = self.post("/api/settings/ai", {"clearKey": True})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])
        self.assertFalse(d["ai"]["configured"])
        self.assertNotIn("apiKey", d["ai"])

    def test_hotlists_errors_do_not_break_response(self):
        server.hotlists_payload = lambda force=False: {
            "fetchedAt": "12:00",
            "xhs": {"items": []},
            "douyin": {"items": []},
            "filter": {"items": []},
            "errors": {"xhs": "登录态已失效"},
        }
        status, d = self.get("/api/hotlists")
        self.assertEqual(status, 200)
        self.assertIn("xhs", d["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
