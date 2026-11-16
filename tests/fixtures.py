# -*- coding: utf-8 -*-
"""测试用的 fixture：前端 API mock 数据 + 临时静态服务。

所有日期都相对“今天”生成，避免写死某一天导致测试过期。
"""

from __future__ import annotations

import datetime
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TODAY = datetime.date.today()


def iso(d: datetime.date) -> str:
    return d.strftime("%Y-%m-%d")


def day(offset: int) -> str:
    return iso(TODAY + datetime.timedelta(days=offset))


def now_iso() -> str:
    return f"{day(0)}T12:00:00+08:00"


def conn_state(connected: bool = True, message: str = "") -> dict:
    return {
        "state": "connected" if connected else "disconnected",
        "qr": None,
        "expiresIn": 0,
        "nickname": "测试账号",
        "lastRefresh": "12:00",
        "message": message,
        "error": None,
    }


def xhs_note(title: str, offset: int, read: int, likes: int, collects: int,
             fans_gain: int, comments: int = 1, shares: int = 0) -> dict:
    return {
        "title": title,
        "postDate": day(offset),
        "exposure": max(read * 2, 1),
        "read": read,
        "ctr": "10.00%",
        "likes": likes,
        "comments": comments,
        "collects": collects,
        "fansGain": fans_gain,
        "shares": shares,
        "cover": None,
    }


def video_work(title: str, offset: int, plays: int, likes: int, collects: int,
               fans_gain: int) -> dict:
    return {
        "title": title,
        "date": day(offset),
        "duration": "00:30",
        "plays": str(plays),
        "likes": str(likes),
        "comments": "1",
        "collects": str(collects),
        "shares": "0",
        "fansGain": str(fans_gain),
        "cover": None,
    }


def wechat_article(title: str, offset: int) -> dict:
    return {
        "title": title,
        "date": day(offset),
        "cover": None,
        "link": "https://mp.weixin.qq.com/s/test",
        "reads": None,
        "likes": None,
        "collects": None,
        "comments": None,
        "shares": None,
    }


def fans_metrics(pid: str) -> dict:
    """给小红书 / 抖音造 8 天官方趋势，最后一天是昨天。"""
    trend = []
    for i in range(8):
        trend.append({
            "date": day(-8 + i),
            "net": 2,
            "new": 3,
            "lost": 1,
            "visitors": 10,
        })
    summary = {
        "net": 16,
        "new": 24,
        "lost": 8,
        "visitors": 80,
        "fans": 1000 if pid == "xhs" else 2000,
    }
    if pid == "xhs":
        summary["convRate"] = 1.2
    if pid == "douyin":
        summary["revisit"] = 6
    return {"range": {"begin": trend[0]["date"], "end": trend[-1]["date"], "days": 8},
            "summary": summary, "trend": trend}


XHS = [
    xhs_note("XHS 今天", 0, 1000, 50, 10, 3),
    xhs_note("XHS 10 天前", -10, 200, 10, 2, 1),
    xhs_note("XHS 40 天前", -40, 50, 1, 0, 0),
]
DOUYIN = [
    video_work("抖音 今天", 0, 2000, 100, 10, 4),
    video_work("抖音 5 天前", -5, 500, 20, 2, 2),
]
BILI = [
    video_work("B站 今天", 0, 300, 10, 1, 0),
]
WECHAT = [
    wechat_article("公众号 20 天前", -20),
    wechat_article("公众号 40 天前", -40),
]


def api_map(bili_connected: bool = True, ai_configured: bool = True,
            include_hotlists: bool = True) -> dict:
    states = {
        "xhs": conn_state(),
        "douyin": conn_state(),
        "bilibili": conn_state(bili_connected,
                               "" if bili_connected else "登录态已失效，请重新扫码"),
        "wechat": conn_state(),
    }

    data = {
        "/api/xhs/data": {
            "platform": {"id": "xhs", "name": "小红书"},
            "account": {"nickname": "XHS测试", "fans": 1000, "likesCollects": 5000},
            "contents": XHS,
            "weekSummary": {"notes": 1, "read": 1000, "likes": 50, "collects": 10},
            "fansMetrics": fans_metrics("xhs"),
            "activities": [],
            "collectedAt": now_iso(),
        },
        "/api/douyin/data": {
            "platform": {"id": "douyin", "name": "抖音"},
            "account": {"nickname": "抖音测试", "fans": "2000", "likes": "8000"},
            "contents": DOUYIN,
            "weekSummary": {"plays": "2000", "likes": "100", "comments": "5",
                            "shares": "2", "newFans": "6", "homeVisits": "20"},
            "fansMetrics": fans_metrics("douyin"),
            "collectedAt": now_iso(),
        },
        "/api/bilibili/data": {
            "platform": {"id": "bilibili", "name": "B 站"},
            "account": {"nickname": "B站测试", "fans": 300, "likes": 1000,
                        "plays": 5000, "statDate": day(-1)},
            "contents": BILI,
            "weekSummary": {},
            "fansMetrics": None,
            "collectedAt": now_iso(),
        },
        "/api/wechat/data": {
            "platform": {"id": "wechat", "name": "公众号"},
            "account": {"nickname": "公众号测试", "fans": "400"},
            "contents": WECHAT,
            "weekSummary": {"newFans": "5", "reads": None},
            "fansMetrics": None,
            "collectedAt": now_iso(),
        },
        "/api/settings": {
            "dataDir": "/tmp/guanlan-test",
            "sessionsDir": "/tmp/guanlan-test/sessions",
            "dailyHour": 12,
            "dailyMinute": 0,
            "version": "test",
            "ai": {
                "configured": ai_configured,
                "model": "deepseek-chat",
                "baseUrl": "https://api.deepseek.com",
                "interest": "测试方向",
                "exclude": "",
            },
        },
        "/api/history": {
            "xhs": [{"date": day(-8 + i), "fans": 990 + i * 2} for i in range(8)],
            "douyin": [{"date": day(-8 + i), "fans": 1980 + i * 2} for i in range(8)],
            "bilibili": [{"date": day(-8 + i), "fans": 290 + i} for i in range(8)],
            "wechat": [{"date": day(-8 + i), "fans": 390 + i} for i in range(8)],
        },
        "/api/refresh/status": {"running": False},
        "/api/hot": {"items": [{"rank": 1, "title": "AI 热点", "url": "https://example.com",
                                "heat": 100}], "fetchedAt": "12:00"},
        "/api/ideas/generate": {
            "ok": True,
            "items": [{
                "title": "生成的测试选题", "angle": "测试切口", "format": "教程",
                "platform": "xhs", "audience": "新手", "cost": "低", "score": 80,
                "reason": "测试", "source": "热榜", "basedOn": "测试热点",
            }],
        },
        "/api/ideas/expand": {
            "ok": True, "titles": ["测试标题"], "covers": ["测试封面"],
            "hook": "开头三秒", "structure": ["第一步", "第二步"],
        },
    }

    for pid, s in states.items():
        data[f"/api/{pid}/state"] = s

    if include_hotlists:
        data["/api/hotlists"] = {
            "fetchedAt": "12:00",
            "xhs": {"source": "测试", "items": [{
                "platform": "xhs", "rank": 1, "title": "小红书热点",
                "category": "测试", "heat": 1000, "url": "https://example.com/xhs",
            }]},
            "douyin": {"source": "测试", "items": [{
                "platform": "douyin", "rank": 1, "title": "抖音热点",
                "heat": 2000, "url": "https://example.com/dy",
            }]},
            "filter": {"mode": "rule", "model": None, "error": None, "items": [{
                "platform": "xhs", "title": "筛选热点", "reason": "相关",
                "score": 90, "angle": "测试角度", "url": "https://example.com/f",
            }]},
            "errors": {},
        }

    return data


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def start_static(root: Path = ROOT):
    """起一个只读静态服务，供 Playwright 打开 index.html。"""
    def factory(*args, **kwargs):
        return _QuietHandler(*args, directory=str(root), **kwargs)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), factory)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
