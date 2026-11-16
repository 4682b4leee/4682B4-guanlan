#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观澜 · 小红书扫码接入本地服务

设计要点（对应已确认方案）：
1. 只监听 127.0.0.1，不对外暴露
2. 二维码直接截取小红书登录页渲染出的真实图片，不做任何自造
3. cookie 用 AES-256-GCM 加密落盘，主密钥存 macOS 钥匙串，明文不落文件
4. 只读：只发 GET/页面加载，不做点赞、关注、发布
5. 遇到滑块/二次确认，状态转 blocked 并停止轮询，等人处理

启动：python server/server.py   （在项目根目录执行）
访问：http://127.0.0.1:8765/
"""

# 让类型注解延迟求值：这样 str | None 这类写法在 Python 3.9 上也不会报错
# （必须放在文件最前面、文档字符串之后，且位于其它 import 之前）
from __future__ import annotations

import base64
import datetime
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------- 路径与常量 ----------
# 项目根目录：本文件位于 <项目根>/server/ 下，所以上一级就是根目录
# 用 __file__ 推导，保证无论从哪个目录启动，静态文件都能正确指向根目录下的 index.html
ROOT = Path(__file__).resolve().parent.parent

# 运行期数据目录：跟着系统走，别人 clone 下来也能落在自己该在的位置
if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "guanlan"
elif os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "guanlan"
else:
    APP_DIR = Path(os.environ.get("XDG_DATA_HOME")
                   or Path.home() / ".local" / "share") / "guanlan"
SESS_DIR = APP_DIR / "sessions"
PROFILE_DIR = APP_DIR / "browser-profile"
DATA_DIR = APP_DIR / "data"
AI_DIR = APP_DIR / "config"
AI_CFG_PATH = AI_DIR / "ai.bin"
KEYCHAIN_SERVICE = "guanlan-session-key"

PROFILE_URL = "https://creator.xiaohongshu.com/new/home?source=official"
LOGIN_URL = "https://www.xiaohongshu.com/login"
DATA_URL = "https://creator.xiaohongshu.com/statistics/data-analysis?source=official"
# 账号涨粉数据页（近 7 日 / 近 30 日，含净涨粉、新增、取关、主页访客、转粉率）
STATS_URL = "https://creator.xiaohongshu.com/statistics/account/v2?source=official"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

QR_TTL = 180          # 二维码有效期（秒），与页面倒计时一致
POLL_INTERVAL = 2     # 轮询间隔
POLL_MAX = 90         # 最多 90 次 ≈ 180 秒

# 每日采集时间：账号概览 / 本周内容明细 / 每日快照 三项共用这一次采集
DAILY_HOUR = 12
DAILY_MINUTE = 0

# 内容采集窗口：全站统一「近 30 天发布」。
# 注意：不能用「本周一到今天」——周一当天窗口为空，会把已发布的内容全部滤掉。
CONTENT_DAYS = 30

# ---------- 全局状态 ----------
LOCK = threading.Lock()
STATE = {
    "state": "disconnected",   # 十态机
    "qr": None,                # dataURL，真实渲染图
    "expiresIn": 0,
    "nickname": None,
    "lastRefresh": None,
    "message": None,
    "error": None,
    "_expires_at": 0,          # 内部字段：二维码截止时间戳
}
_worker = None

VALID_STATES = {
    "disconnected", "qr_loading", "qr_ready", "scanned",
    "authorized", "connected", "expired", "blocked", "invalid", "error",
}


def set_state(state, **kw):
    """线程安全地更新状态。"""
    assert state in VALID_STATES, state
    with LOCK:
        STATE["state"] = state
        for k, v in kw.items():
            if k in STATE:
                STATE[k] = v


def snapshot():
    with LOCK:
        d = dict(STATE)
    # 倒计时由后端算，前端不信任本地时钟
    if d["state"] == "qr_ready" and d.get("_expires_at"):
        d["expiresIn"] = max(0, int(d.pop("_expires_at") - time.time()))
    d.pop("_expires_at", None)
    return d


# ---------- 每日快照 ----------
HISTORY_PATH = DATA_DIR / "history.json"


def _num(v):
    """把「3.3万」这类计数转成整数，失败返回 None。"""
    if v is None:
        return None
    s = str(v).strip()
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 1e8)
        if "万" in s:
            return int(float(s.replace("万", "")) * 1e4)
        return int(float(s.replace(",", "")))
    except Exception:
        return None


def save_snapshot(platform: str, row: dict):
    """每天一条；同一天重复采集就覆盖为最新值，保留最近 180 天。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if HISTORY_PATH.exists():
        try:
            data = json.loads(HISTORY_PATH.read_text())
        except Exception:
            data = {}

    today = time.strftime("%Y-%m-%d")
    rows = data.get(platform) or []
    row = {"date": today, **row}
    for i, r in enumerate(rows):
        if r.get("date") == today:
            rows[i] = row
            break
    else:
        rows.append(row)

    rows.sort(key=lambda r: r.get("date", ""))
    data[platform] = rows[-180:]
    HISTORY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ---------- 加密存储 ----------
KEY_FILE = APP_DIR / "master.key"   # 非 macOS 的兜底：0600 权限的密钥文件


def _key_from_file() -> bytes:
    """Linux / Windows 没有钥匙串命令，用本机密钥文件顶替（权限 0600）。"""
    if KEY_FILE.exists():
        return base64.b64decode(KEY_FILE.read_text().strip())
    key = secrets.token_bytes(32)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(base64.b64encode(key).decode())
    os.chmod(KEY_FILE, 0o600)
    return key


def _master_key() -> bytes:
    """主密钥：macOS 走钥匙串，其它系统退回本机密钥文件。

    钥匙串读不到、写入也失败时同样退回文件，避免生成一把用完就丢的临时密钥。
    """
    if sys.platform != "darwin":
        return _key_from_file()

    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return base64.b64decode(r.stdout.strip())

    key = secrets.token_bytes(32)
    w = subprocess.run(
        ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", "guanlan", "-w", base64.b64encode(key).decode()],
        check=False, capture_output=True,
    )
    return key if w.returncode == 0 else _key_from_file()


def save_session(platform: str, cookies: list) -> Path:
    """AES-256-GCM 加密写入，权限 0600。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    SESS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "platform": platform,
        "cookies": cookies,
        "savedAt": int(time.time()),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master_key()).encrypt(nonce, raw, platform.encode())
    path = SESS_DIR / f"{platform}.bin"
    path.write_bytes(nonce + ct)
    os.chmod(path, 0o600)
    return path


def load_session(platform: str):
    """读回 cookie（仅内存使用，不返回给前端）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    path = SESS_DIR / f"{platform}.bin"
    if not path.exists():
        return None
    blob = path.read_bytes()
    nonce, ct = blob[:12], blob[12:]
    raw = AESGCM(_master_key()).decrypt(nonce, ct, platform.encode())
    return json.loads(raw.decode())


# ---------- 二维码 ----------
def _grab_qr(page) -> str | None:
    """取页面真实渲染的二维码。

    登录页里有两个 data:image —— 左上角 logo(68×32) 和
    二维码 .qrcode-img(128×128)，必须按选择器 + 尺寸双重过滤，
    否则会抓到 logo。
    """
    selectors = (
        ".qrcode-img",
        "[class*='qrcode'] img",
        # 微信公众平台的二维码是 <img class="...qrcode">，类名在 img 自己身上
        "img[class*='qrcode']",
        "img[src*='qrcode']",
        "[class*='qrcode'] canvas",
        "canvas",
    )
    for sel in selectors:
        try:
            for el in page.locator(sel).all():
                box = el.bounding_box()
                if not box or box["width"] < 100 or box["height"] < 100:
                    continue
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                if tag == "img":
                    src = el.get_attribute("src") or ""
                    if src.startswith("data:image"):
                        return src
                png = el.screenshot()
                if png and len(png) > 2000:
                    return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception:
            continue
    return None


def _page_text(page) -> str:
    try:
        return page.inner_text("body")[:800]
    except Exception:
        return ""


def _logged_in(page) -> bool:
    """创作服务平台的登录判据：离开 /login 且能看到账号数据。

    不用 SSR loggedIn：creator 页面结构和前台不同；
    也不用接口：接口要 x-s 签名，页面内直接 fetch 会 500。
    这里只读页面已渲染的文本，不发任何额外请求。
    """
    try:
        if "/login" in (page.url or ""):
            return False
        t = _page_text(page)
        return ("粉丝数" in t and "获赞与收藏" in t) or ("发布笔记" in t and "笔记管理" in t)
    except Exception:
        return False


def _risk_blocked(text: str) -> bool:
    """页面出现真正的风控特征（滑块/人机校验）。

    注意：登录页本身有「获取验证码」按钮，不能把「验证码」当风控。
    """
    keys = ("拖动滑块", "向右滑动", "滑块验证", "请完成安全验证",
            "人机验证", "验证码校验")
    return any(k in text for k in keys)


# ---------- 登录主流程 ----------
def _login_flow():
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    set_state("qr_loading", message="正在唤起登录页", qr=None, error=None)

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=True,
                locale="zh-CN",
                user_agent=UA,
                viewport={"width": 1280, "height": 860},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            # 1) 先看创作服务平台是否已登录（cookie 与前台共享）
            page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            if _logged_in(page):
                set_state("authorized", message="检测到已登录会话")
                return _after_login(ctx, page)

            # 2) 未登录：到前台登录页取二维码（那里二维码是稳定可用的）
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2800)

            qr = _grab_qr(page)
            if not qr:
                set_state("error", error="未能获取二维码图片", message="登录页未渲染出二维码")
                return

            deadline = time.time() + QR_TTL
            set_state("qr_ready", qr=qr, _expires_at=deadline,
                      message="请用小红书 App 扫码")

            for n in range(POLL_MAX):
                time.sleep(POLL_INTERVAL)
                txt = _page_text(page)

                # 1) 登录态优先：只读页面 SSR，不发任何网络请求，
                #    也就不受"页面文案还停在已扫码"影响
                if _logged_in(page):
                    set_state("authorized", message="已授权，正在保存会话")
                    return _after_login(ctx, page)

                # 2) 风控拦截
                if _risk_blocked(txt):
                    set_state("blocked",
                              message="检测到安全验证，请在浏览器完成后再点继续")
                    return

                # 3) 已扫码、等待手机确认
                #    注意：这里不能 continue，否则会跳过上面的登录检查和下面的过期判定，
                #    这正是上一版卡在 scanned 的直接原因
                if any(k in txt for k in ("扫码成功", "已扫码", "请在手机上确认", "确认登录")):
                    set_state("scanned", message="已扫码，请在手机上点确认")

                # 4) 过期兜底
                if time.time() > deadline:
                    set_state("expired", message="二维码已过期，请重新生成", qr=None)
                    return

            # 循环耗尽仍未登录
            set_state("expired", message="等待超时，请重新生成", qr=None)

    except Exception as e:
        set_state("error", error=str(e)[:200], message="登录流程异常")


def _after_login(ctx, page):
    """登录成功：存 cookie → 采集账号概览与本周笔记。"""
    # 注意：cookie 只在内存与加密文件中流转，绝不打印
    cookies = ctx.cookies()
    save_session("xhs", cookies)
    # 先停在 authorized：前端据此显示"正在同步数据"，采集完再切 connected
    set_state("authorized", message="已授权，正在同步账号数据…")

    try:
        data = _collect(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        out = DATA_DIR / "xhs_creator.json"
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2))

        ws = data.get("weekSummary") or {}
        try:
            save_snapshot("xhs", {
                "fans": _num(data["account"].get("fans")),
                "likesCollects": _num(data["account"].get("likesCollects")),
                "weekRead": ws.get("read"),
                "weekLikes": ws.get("likes"),
                "weekCollects": ws.get("collects"),
            })
        except Exception:
            pass

        set_state("connected",
                  nickname=(data.get("account") or {}).get("nickname"),
                  lastRefresh=time.strftime("%H:%M"),
                  message=f"已连接 · 本周 {ws.get('notes', 0)} 篇 · 阅读 {ws.get('read', 0)}")
    except Exception as e:
        set_state("connected", message=f"已连接，但采集失败：{str(e)[:80]}")


def _collect_fans(page) -> dict | None:
    """采集账号近 30 日涨粉数据。

    做法：只监听页面自己发出的接口响应，不构造任何签名请求。
    页面加载时会请求 /api/galaxy/v2/creator/datacenter/account/base，
    里面的 data.thirty 同时含五条按日曲线：
      net_rise_fans_count_list  净涨粉
      rise_fans_list            新增关注
      loss_fans_count_list      取消关注
      home_view_list            主页访客
      home_conversion_rise_fans_rate_list  主页转粉率
    与官方「导出数据」的 xlsx 完全一致。
    """
    captured = {}

    def on_resp(r):
        # 只要这一条接口；拿到就停，避免重复解析
        if "/datacenter/account/base" in r.url and not captured:
            try:
                captured.update(json.loads(r.text()))
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(STATS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6500)
    finally:
        page.remove_listener("response", on_resp)

    t = (captured.get("data") or {}).get("thirty")
    if not isinstance(t, dict):
        return None

    def map_by_date(key):
        """把 [{date: 毫秒, count: n}] 转成 {毫秒: n}"""
        return {int(x["date"]): x.get("count") for x in (t.get(key) or []) if x.get("date")}

    net = map_by_date("net_rise_fans_count_list")
    if not net:
        return None
    new = map_by_date("rise_fans_list")
    lost = map_by_date("loss_fans_count_list")
    visits = map_by_date("home_view_list")
    # 转粉率要小数位，用 count_with_double
    conv = {int(x["date"]): x.get("count_with_double")
            for x in (t.get("home_conversion_rise_fans_rate_list") or []) if x.get("date")}

    trend = [{
        "date": datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d"),
        "net": net.get(ts),
        "new": new.get(ts),
        "lost": lost.get(ts),
        "visitors": visits.get(ts),
        "conv": conv.get(ts),
    } for ts in sorted(net)]

    return {
        "range": {"begin": trend[0]["date"], "end": trend[-1]["date"], "days": len(trend)},
        "summary": {
            "net": t.get("net_rise_fans_count"),
            "netRing": t.get("net_rise_fans_count_rate"),
            "new": t.get("rise_fans_count"),
            "newRing": t.get("rise_fans_count_rate"),
            "lost": t.get("loss_fans_count"),
            "lostRing": t.get("loss_fans_count_rate"),
            "visitors": t.get("home_view_count"),
            "visitorsRing": t.get("home_view_count_rate"),
            "convRate": t.get("home_conversion_rise_fans_rate"),
            "convRing": t.get("home_conversion_rise_fans_cycle_rate"),
        },
        "trend": trend,
    }


def _collect(page) -> dict:
    """从创作服务平台只读采集：账号概览 + 笔记数据表。

    为什么改走这里：创作服务平台有「观看/阅读」和「曝光」，
    前台主页只有点赞收藏，阅读量永远拿不到。
    """
    import datetime

    # 1) 账号概览：粉丝、关注、获赞与收藏
    #    昵称与小红书号从页面自己的 /api/galaxy/user/info 读，不写死在代码里
    prof = {}
    act = {}

    def on_user(r):
        if "/api/galaxy/user/info" in r.url and not prof:
            try:
                prof.update((json.loads(r.text()) or {}).get("data") or {})
            except Exception:
                pass
        # 首页同时会拉活动中心列表，这里顺手接住，不额外发请求
        if "activity_center/list" in r.url and not act:
            try:
                act.update(json.loads(r.text()) or {})
            except Exception:
                pass

    page.on("response", on_user)
    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5500)
    finally:
        page.remove_listener("response", on_user)

    acc = page.evaluate(r"""() => {
      const t = document.body.innerText.replace(/\u200b/g, '');
      const g = re => { const m = t.match(re); return m ? m[1] : null; };
      return {
        follows: g(/([0-9,]+)\s*关注数/),
        fans: g(/([0-9,]+)\s*粉丝数/),
        likesCollects: g(/([0-9,]+)\s*获赞与收藏/),
        redId: (t.match(/小红书账号:\s*([A-Za-z0-9_]+)/) || [])[1] || null
      };
    }""")

    # 2) 笔记数据表（表头：曝光/观看/点击率/点赞/评论/收藏/涨粉/分享）
    page.goto(DATA_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    rows = page.evaluate("""() => [...document.querySelectorAll('tr')].map(tr => {
        const c = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
        if (c.length < 8) return null;
        const img = tr.querySelector('img');
        return {cells: c, cover: img ? (img.currentSrc || img.src) : null};
      }).filter(Boolean)""")

    def num(x):
        x = (x or "").replace(",", "").replace("\u200b", "").strip()
        try:
            return int(float(x))
        except Exception:
            return x

    notes = []
    for r in rows:
        c = [x.replace("\u200b", "") for x in r["cells"]]
        # 审核未通过 / 已删除的笔记不计入（数据不完整，算分不公平）
        if "未通过" in c[0] or "已删除" in c[0]:
            continue
        m = re.search(r"([0-9]{4}-[0-9]{2}-[0-9]{2})", c[0])
        notes.append({
            "title": c[0].split("\n")[0].strip(),
            "postDate": m.group(1) if m else None,
            "exposure": num(c[1]), "read": num(c[2]), "ctr": c[3],
            "likes": num(c[4]), "comments": num(c[5]), "collects": num(c[6]),
            "fansGain": num(c[7]), "shares": num(c[8]) if len(c) > 8 else None,
            "cover": r.get("cover"),
        })

    # 本周 = 本周一到今天（只用于 weekSummary 的口径说明）
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    week = [n for n in notes if n["postDate"]
            and datetime.date.fromisoformat(n["postDate"]) >= monday]

    def s(k):
        return sum(n[k] for n in week if isinstance(n[k], int))

    # 3) 近 30 日涨粉数据：独立页面，失败不影响上面两项
    try:
        fans = _collect_fans(page)
    except Exception:
        fans = None

    # 4) 热门活动：取还在进行中的前 6 个（接口已按热度排序）
    activities = []
    try:
        now_ms = int(time.time() * 1000)
        for a in ((act.get("data") or {}).get("activity_list") or []):
            end = a.get("end_time") or 0
            name = (a.get("activity_name") or "").strip()
            # activity_link 是活动详情页（可能是 xhsdiscover:// 深链），
            # 不是 http 就退回 pc_post_link（发布页，但至少浏览器能打开）
            link = a.get("activity_link") or ""
            url = link if link.startswith("http") else (a.get("pc_post_link") or "")
            if not name or not url or (end and end < now_ms):
                continue
            fmt = lambda ms: datetime.datetime.fromtimestamp(ms / 1000).strftime("%m/%d") if ms else ""
            activities.append({
                "id": a.get("activity_id"),
                "name": name,
                "url": url,
                "start": fmt(a.get("start_time")),
                "end": fmt(end),
            })
            if len(activities) >= 5:
                break
    except Exception:
        pass

    return {
        "platform": {"id": "xhs", "name": "小红书", "sourceType": "creator_platform"},
        "account": {
            "nickname": prof.get("userName") or "小红书账号",
            "redId": prof.get("redId") or acc.get("redId"),
            "fans": num(acc.get("fans")),
            "follows": num(acc.get("follows")),
            "likesCollects": num(acc.get("likesCollects")),
        },
        # 全部笔记：内容页与近 30 天统计都从这里取，不在后端按「本周」截断。
        # 之前只给 weekNotes，周一打开时窗口是空的，导致整页显示「近 30 天无内容」。
        "contents": notes,
        "weekSummary": {"notes": len(week), "read": s("read"),
                        "likes": s("likes"), "collects": s("collects")},
        "weekStart": str(monday),
        "fansMetrics": fans,
        "activities": activities,
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


# =====================================================================
# 抖音创作者中心（creator.douyin.com）
# 与小红书同一套思路：真实二维码 → 加密存 cookie → 只读采集
# =====================================================================
DY_HOME = "https://creator.douyin.com/creator-micro/home"

DSTATE = {
    "state": "disconnected", "qr": None, "expiresIn": 0,
    "nickname": None, "lastRefresh": None, "message": None, "error": None,
    "_expires_at": 0,
}
_dworker = None


def dset(state, **kw):
    with LOCK:
        DSTATE["state"] = state
        for k, v in kw.items():
            if k in DSTATE:
                DSTATE[k] = v


def dsnapshot():
    with LOCK:
        d = dict(DSTATE)
    if d["state"] == "qr_ready" and d.get("_expires_at"):
        d["expiresIn"] = max(0, int(d.pop("_expires_at") - time.time()))
    d.pop("_expires_at", None)
    return d


def _douyin_logged_in(page) -> bool:
    try:
        if "/login" in (page.url or ""):
            return False
        t = _page_text(page)
        return ("数据中心" in t or "作品数据" in t or "粉丝总量" in t) and "扫码登录" not in t
    except Exception:
        return False


def _douyin_qr(page):
    """抖音二维码是 178×178 的 data:image/png，class 随机。
    用「正方形 + 尺寸区间」过滤，避免抓到 800×629 的卡片背景。
    """
    try:
        for el in page.locator('img[src^="data:image/png"]').all():
            box = el.bounding_box()
            if not box:
                continue
            w, h = box["width"], box["height"]
            if 140 <= w <= 230 and abs(w - h) < 12:
                return el.get_attribute("src")
    except Exception:
        pass
    return None


def _collect_douyin_works(page) -> list:
    """解析「内容管理」里的作品列表（近 CONTENT_DAYS 天）。

    坑：`video-card` 这个 class 有三层嵌套（外层卡片 / 内层 / 仅时长的壳），
    直接 querySelectorAll 会拿到重复项，按索引配封面必然错位。
    所以先只保留最外层卡片，再逐卡片解析——封面和标题天然同源。
    """
    import datetime

    page.goto("https://creator.douyin.com/creator-micro/content/manage",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)

    cards = page.evaluate(r"""() => {
      const all = [...document.querySelectorAll('[class*="video-card"]')];
      // 只保留最外层：没有任何别的匹配元素包含它
      const top = all.filter(e => !all.some(o => o !== e && o.contains(e)));
      return top.map(card => {
        let cover = null;
        for (const el of card.querySelectorAll('*')) {
          const bg = getComputedStyle(el).backgroundImage || '';
          const m = bg.match(/url\("([^"]*douyinpic[^"]*)"/);
          if (m) { cover = m[1]; break; }
        }
        return {text: card.innerText || '', cover};
      }).filter(x => x.text.includes('播放'));
    }""")

    today = datetime.date.today()
    # 取近 CONTENT_DAYS 天，而不是「本周」：周一当天本周窗口为空，作品会全部消失
    since = today - datetime.timedelta(days=CONTENT_DAYS)

    out = []
    for card in cards:
        text = card["text"]
        # 定时未发布的作品没有真实数据，直接跳过
        if "定时发布中" in text or "定时:" in text:
            continue
        # 已删除 / 未通过 / 审核中 / 仅自己可见 都不算有效内容
        if any(k in text for k in ("已删除", "未通过", "审核中", "仅自己可见")):
            continue

        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if not lines:
            continue

        date = None
        for l in lines:
            m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", l)
            if m:
                date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                break
        if not date:
            continue
        if datetime.date.fromisoformat(date) < since:
            continue

        item = {
            "duration": lines[0] if re.fullmatch(r"\d{2}:\d{2}", lines[0]) else None,
            "title": lines[1] if len(lines) > 1 else None,
            "date": date,
            "cover": card.get("cover"),
        }
        keymap = {"播放": "plays", "点赞": "likes", "评论": "comments",
                  "分享": "shares", "收藏": "collects",
                  "完播率": "finishRate", "吸粉量": "fansGain"}
        for i, l in enumerate(lines[:-1]):
            if l in keymap:
                item[keymap[l]] = lines[i + 1]

        # 播放量拿不到（"-"）：通常是已不可见/仅自己可见，不参与展示与评分
        if item.get("plays") in (None, "-", ""):
            continue
        out.append(item)

    return out


DY_OPERATION_URL = "https://creator.douyin.com/creator-micro/data-center/operation"
# 页面自己请求的是 recent_days=7，这里只把窗口换成 30（同源只读 GET，无签名参数）
DY_FANS_API = "/janus/douyin/creator/data/overview/dashboard/fans?recent_days=30"


def _collect_douyin_fans(page) -> dict | None:
    """抖音近 30 日涨粉数据。

    接口返回 6 组指标，每组都是按日曲线：
      net_fans_cnt        净增粉丝
      new_fans_cnt        新增粉丝量
      cancel_fans_cnt     取关粉丝
      homepage_view_cnt   主页访问
      home_view_fans_cnt  回访粉丝量
      total_fans_cnt      粉丝总数
    """
    page.goto(DY_OPERATION_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    raw = page.evaluate("""async (api) => {
      const res = await fetch(api, { credentials: 'include' });
      return res.ok ? await res.text() : '';
    }""", DY_FANS_API)
    if not raw:
        return None

    try:
        d = json.loads(raw)
    except Exception:
        return None

    metrics = {m.get("english_metric_name"): m for m in (d.get("metrics") or [])}
    if "net_fans_cnt" not in metrics:
        return None

    def series(name):
        """{'20261106': 5, ...} —— 接口日期是 YYYYMMDD 字符串"""
        m = metrics.get(name) or {}
        return {str(x.get("date_time")): x.get("value") for x in (m.get("trends") or [])}

    def dash(s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"

    net = series("net_fans_cnt")
    new = series("new_fans_cnt")
    lost = series("cancel_fans_cnt")
    visits = series("homepage_view_cnt")

    trend = [{
        "date": dash(k),
        "net": net.get(k), "new": new.get(k),
        "lost": lost.get(k), "visitors": visits.get(k),
    } for k in sorted(net)]

    val = lambda k: (metrics.get(k) or {}).get("metric_value")
    return {
        "range": {"begin": trend[0]["date"], "end": trend[-1]["date"], "days": len(trend)},
        "summary": {
            "net": val("net_fans_cnt"),
            "new": val("new_fans_cnt"),
            "lost": val("cancel_fans_cnt"),
            "visitors": val("homepage_view_cnt"),
            "revisit": val("home_view_fans_cnt"),
            "fans": val("total_fans_cnt"),
        },
        "trend": trend,
    }


def _collect_douyin(page) -> dict:
    """读创作者中心首页：账号概览 + 近 7 日汇总 + 最新作品。

    页面结构（示例）：
      关注 100 / 粉丝指数 1200 / 获赞 8600
      账号总览：播放量 4.5万、主页访问量 680、作品点赞 2300、
                作品分享 120、作品评论 96、净增粉丝 18
    「账号总览」之后那一段才是汇总，避免抓到最新作品卡的播放量。
    """
    page.goto(DY_HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    d = page.evaluate(r"""() => {
      const t = document.body.innerText.replace(/\u200b/g, '');
      const seg = t.split('账号总览')[1] || '';
      const g = (s, re) => { const m = s.match(re); return m ? m[1] : null; };
      return {
        follows:  g(t, /关注\s*([0-9.,万]+)/),
        fans:     g(t, /粉丝指数\s*([0-9.,万]+)/),
        likes:    g(t, /获赞\s*([0-9.,万]+)/),
        douyinId: g(t, /抖音号[:：]\s*([A-Za-z0-9_.-]+)/),
        // 昵称固定出现在「抖音号：xxx」的上一行
        nickname: (() => {
          const lines = t.split('\n').map(s => s.trim()).filter(Boolean);
          const i = lines.findIndex(l => l.includes('抖音号'));
          return i > 0 ? lines[i - 1] : null;
        })(),
        weekPlays:    g(seg, /播放量\s*([0-9.,万]+)/),
        weekLikes:    g(seg, /作品点赞\s*([0-9.,万]+)/),
        weekComments: g(seg, /作品评论\s*([0-9.,万]+)/),
        weekShares:   g(seg, /作品分享\s*([0-9.,万]+)/),
        weekNewFans:  g(seg, /净增粉丝\s*([0-9.,万]+)/),
        homeVisits:   g(seg, /主页访问量\s*([0-9.,万]+)/)
      };
    }""")

    # 本周作品明细（内容管理页）
    try:
        works = _collect_douyin_works(page)
    except Exception:
        works = []

    # 近 30 日涨粉（数据中心 / 运营分析），失败不影响其它数据
    try:
        fans = _collect_douyin_fans(page)
    except Exception:
        fans = None

    return {
        "platform": {"id": "douyin", "name": "抖音", "sourceType": "creator_platform"},
        "account": {
            "nickname": d.get("nickname") or "抖音账号",
            "douyinId": d.get("douyinId"),
            "follows": d["follows"],
            "fans": d["fans"],            # 页面标签为「粉丝指数」
            "fansLabel": "粉丝指数",
            "likes": d["likes"],
        },
        "weekSummary": {
            "plays": d["weekPlays"], "likes": d["weekLikes"],
            "comments": d["weekComments"], "shares": d["weekShares"],
            "newFans": d["weekNewFans"], "homeVisits": d["homeVisits"],
        },
        # 近 30 天作品（内容页与全站统计共用这一份，不再只给「本周」）
        "contents": works,
        "fansMetrics": fans,
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


def _douyin_after_login(ctx, page):
    save_session("douyin", ctx.cookies())
    dset("authorized", message="已授权，正在同步账号数据…")
    try:
        data = _collect_douyin(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "douyin_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))
        try:
            ws = data.get("weekSummary") or {}
            save_snapshot("douyin", {
                "fans": _num(data["account"].get("fans")),
                "likes": _num(data["account"].get("likes")),
                "weekPlays": _num(ws.get("plays")),
                "weekLikes": _num(ws.get("likes")),
            })
        except Exception:
            pass
        dset("connected", lastRefresh=time.strftime("%H:%M"),
             message=f"已连接 · 粉丝 {data['account'].get('fans') or '—'}")
    except Exception as e:
        dset("connected", message=f"已连接，但采集失败：{str(e)[:80]}")


def _douyin_flow():
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    dset("qr_loading", message="正在唤起抖音登录页", qr=None, error=None)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, locale="zh-CN",
                user_agent=UA, viewport={"width": 1280, "height": 860})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(DY_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            if _douyin_logged_in(page):
                dset("authorized", message="检测到已登录会话")
                return _douyin_after_login(ctx, page)

            qr = _douyin_qr(page)
            if not qr:
                dset("error", error="未能获取二维码", message="登录页未渲染出二维码")
                return

            deadline = time.time() + QR_TTL
            dset("qr_ready", qr=qr, _expires_at=deadline, message="请用抖音 App 扫码")

            for _ in range(POLL_MAX):
                time.sleep(POLL_INTERVAL)
                if _douyin_logged_in(page):
                    dset("authorized", message="已授权，正在保存会话")
                    return _douyin_after_login(ctx, page)

                txt = _page_text(page)
                if _risk_blocked(txt):
                    dset("blocked", message="检测到安全验证，请在手机上完成后重试")
                    return
                if "已扫码" in txt or "请在手机上确认" in txt:
                    dset("scanned", message="已扫码，请在手机上点确认")

                if time.time() > deadline:
                    dset("expired", message="二维码已过期，请重新生成", qr=None)
                    return
            dset("expired", message="等待超时，请重新生成", qr=None)
    except Exception as e:
        dset("error", error=str(e)[:200], message="登录流程异常")


def start_douyin_login():
    global _dworker
    with LOCK:
        if DSTATE["state"] in ("qr_loading", "qr_ready", "scanned", "authorized"):
            return False
    _dworker = threading.Thread(target=_douyin_flow, daemon=True)
    _dworker.start()
    return True


# =====================================================================
# B 站创作中心（member.bilibili.com）
# 与上面两个平台同一套思路：真实二维码 → 加密存 cookie → 只读采集
# =====================================================================
BILI_HOME = "https://member.bilibili.com/platform/home"
BILI_LOGIN = ("https://passport.bilibili.com/login"
              "?gourl=https%3A%2F%2Fmember.bilibili.com%2F")
# 稿件管理（内容管理 → 视频管理）：实测是这个地址，页面会请求 /x/web/archives
BILI_WORK_URL = "https://member.bilibili.com/platform/upload-manager/article"

BSTATE = {
    "state": "disconnected", "qr": None, "expiresIn": 0,
    "nickname": None, "lastRefresh": None, "message": None, "error": None,
    "_expires_at": 0,
}
_bili_worker = None


def bset(state, **kw):
    assert state in VALID_STATES, state
    with LOCK:
        BSTATE["state"] = state
        for k, v in kw.items():
            if k in BSTATE:
                BSTATE[k] = v


def bsnapshot():
    with LOCK:
        d = dict(BSTATE)
    if d["state"] == "qr_ready" and d.get("_expires_at"):
        d["expiresIn"] = max(0, int(d.pop("_expires_at") - time.time()))
    d.pop("_expires_at", None)
    return d


def _bili_logged_in(ctx, page) -> bool:
    """B 站登录判据：已经回到创作中心 + 有 SESSDATA。

    不靠页面文字：创作中心是单页应用，未登录时也可能先渲染出骨架，
    而 SESSDATA 是 B 站登录态的核心 cookie，比文案稳。
    """
    try:
        url = page.url or ""
        if "passport.bilibili.com" in url or "member.bilibili.com" not in url:
            return False
        return any(c.get("name") == "SESSDATA" and "bilibili.com" in (c.get("domain") or "")
                   for c in ctx.cookies())
    except Exception:
        return False


def _bili_recent_works(archives: dict) -> list:
    """把 /x/web/archives 的稿件列表转成近 30 天明细。

    字段来源（实测）：
      Archive.bvid / title / cover / duration(秒) / ptime(发布戳) / state_desc
      stat.view / like / reply / favorite / share
    只保留「近 CONTENT_DAYS 天内发布、且不是仅自己可见」的稿件。
    """
    today = datetime.date.today()
    # 用 30 天窗口而不是本周：周一当天本周窗口为空，稿件会被全部滤掉
    since = today - datetime.timedelta(days=CONTENT_DAYS)
    data = (archives or {}).get("data") or {}
    out = []
    for it in data.get("arc_audits") or []:
        arc = it.get("Archive") or {}
        st = it.get("stat") or {}
        ts = arc.get("ptime") or 0
        if not ts:
            continue
        dt = datetime.datetime.fromtimestamp(ts)
        if dt.date() < since:
            continue
        if arc.get("state_desc") == "仅自己可见":
            continue
        dur = arc.get("duration") or 0
        out.append({
            "title": arc.get("title"),
            "date": dt.strftime("%Y-%m-%d"),
            "duration": f"{dur // 60:02d}:{dur % 60:02d}" if dur else None,
            "cover": (arc.get("cover") or "").replace("http://", "https://"),
            "plays": st.get("view"),
            "likes": st.get("like"),
            "comments": st.get("reply"),
            "collects": st.get("favorite"),
            "shares": st.get("share"),
            "bvid": arc.get("bvid"),
        })
    return out


def _collect_bilibili(page) -> dict:
    """B 站创作中心：账号概览 + 本周稿件。

    全部走页面自己发出的接口（被动监听，不构造签名请求）：
      /x/web/data/index/stat  播放/粉丝/点赞等总量 + 当日增量 + 数据截止日 log_date
      /x/web/data/fan         粉丝概览（summary.total）
      /x/web/archives         稿件列表 + 每条 stat（view/like/reply/favorite/share）
      /x/web-interface/nav    昵称 / UID
    接口没抓到时才退回读首页文本，避免页面改版直接变空。
    """
    box = {}

    def on_resp(r):
        u = r.url or ""
        for name, key in (("stat", "/x/web/data/index/stat"),
                          ("fan", "/x/web/data/fan"),
                          ("archives", "/x/web/archives"),
                          ("nav", "/web-interface/nav")):
            if key in u:
                try:
                    box.setdefault(name, r.json())
                except Exception:
                    pass
                break

    page.on("response", on_resp)

    # 1) 首页：概览接口 + 文本兜底
    page.goto(BILI_HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)
    dom = page.evaluate(r"""() => {
      const t = (document.body.innerText || '').replace(/\u200b/g, '');
      const g = re => { const m = t.match(re); return m ? m[1] : null; };
      // 首页结构是「播放量 \n 当日增量 \n 总量」，所以取后面那个上千位的数
      const plays = t.match(/播放量[\s\S]{0,40}?([0-9][0-9,]{2,})/);
      return {
        fans: g(/粉丝总数\s*([0-9.,万]+)/),
        plays: plays ? plays[1] : null,
      };
    }""")

    stat = (box.get("stat") or {}).get("data") or {}
    fan = (box.get("fan") or {}).get("data") or {}
    nav = (box.get("nav") or {}).get("data") or {}

    # 2) 稿件页：只有这一页会请求 /x/web/archives
    try:
        page.goto(BILI_WORK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
    except Exception:
        pass
    works = _bili_recent_works(box.get("archives") or {})

    # log_date 是 20261112 这种格式，转成 2026-11-12
    log_date = str(stat.get("log_date") or "")
    stat_date = (f"{log_date[:4]}-{log_date[4:6]}-{log_date[6:8]}"
                 if len(log_date) == 8 else None)

    fans = stat.get("total_fans")
    if fans is None:
        fans = (fan.get("summary") or {}).get("total") or dom.get("fans")

    # 原始响应留档：只落 URL 和 JSON，不记 cookie（核对字段用）
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "bilibili_raw.json").write_text(
            json.dumps(box, ensure_ascii=False)[:300000])
    except Exception:
        pass

    return {
        "platform": {"id": "bilibili", "name": "B 站", "sourceType": "creator_platform"},
        "account": {
            "nickname": nav.get("uname") or "B 站账号",
            "uid": nav.get("mid"),
            "fans": fans,
            "fansLabel": "粉丝数",
            "likes": stat.get("total_like"),
            "plays": stat.get("total_click") if stat else dom.get("plays"),
            "incrFans": stat.get("incr_fans"),
            "incrPlays": stat.get("incr_click"),
            "statDate": stat_date,
        },
        # 播放/获赞是累计口径，不是本周，所以不放 weekSummary
        "weekSummary": {},
        "contents": works,
        # 30 天涨粉趋势接口还没确认，先返回 None，前端自动退回每日快照口径
        "fansMetrics": None,
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


def _bilibili_after_login(ctx, page):
    """登录成功：存 cookie → 采集账号概览与本周稿件。"""
    save_session("bilibili", ctx.cookies())
    bset("authorized", message="已授权，正在同步账号数据…")
    try:
        data = _collect_bilibili(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "bilibili_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        acc = data.get("account") or {}
        try:
            save_snapshot("bilibili", {
                "fans": _num(acc.get("fans")),
                "likes": _num(acc.get("likes")),
                "weekPlays": _num(acc.get("plays")),
            })
        except Exception:
            pass

        bset("connected",
             nickname=acc.get("nickname"),
             lastRefresh=time.strftime("%H:%M"),
             message=f"已连接 · 粉丝 {acc.get('fans') or '—'}"
                     f" · 近 {CONTENT_DAYS} 天 {len(data.get('contents') or [])} 条")
    except Exception as e:
        bset("connected", message=f"已连接，但采集失败：{str(e)[:80]}")


def _bilibili_flow():
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    bset("qr_loading", message="正在唤起 B 站登录页", qr=None, error=None)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, locale="zh-CN",
                user_agent=UA, viewport={"width": 1280, "height": 860})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 1) 先看创作中心是否已有登录态（profile 里的 cookie 还在）
            page.goto(BILI_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if _bili_logged_in(ctx, page):
                bset("authorized", message="检测到已登录会话")
                return _bilibili_after_login(ctx, page)

            # 2) 未登录：创作中心会自己跳到 passport 登录页
            if "passport.bilibili.com" not in (page.url or ""):
                page.goto(BILI_LOGIN, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)

            qr = _grab_qr(page)
            if not qr:
                bset("error", error="未能获取二维码", message="登录页未渲染出二维码")
                return

            deadline = time.time() + QR_TTL
            bset("qr_ready", qr=qr, _expires_at=deadline, message="请用哔哩哔哩 App 扫码")

            for _ in range(POLL_MAX):
                time.sleep(POLL_INTERVAL)
                if _bili_logged_in(ctx, page):
                    bset("authorized", message="已授权，正在保存会话")
                    return _bilibili_after_login(ctx, page)

                txt = _page_text(page)
                if _risk_blocked(txt):
                    bset("blocked", message="检测到安全验证，请在手机端完成后重试")
                    return
                if any(k in txt for k in ("扫码成功", "已扫码", "请在手机上确认", "请确认")):
                    bset("scanned", message="已扫码，请在手机上点确认")

                if time.time() > deadline:
                    bset("expired", message="二维码已过期，请重新生成", qr=None)
                    return
            bset("expired", message="等待超时，请重新生成", qr=None)
    except Exception as e:
        bset("error", error=str(e)[:200], message="登录流程异常")


def start_bilibili_login():
    global _bili_worker
    with LOCK:
        if BSTATE["state"] in ("qr_loading", "qr_ready", "scanned", "authorized"):
            return False
    _bili_worker = threading.Thread(target=_bilibili_flow, daemon=True)
    _bili_worker.start()
    return True


def start_login():
    global _worker
    with LOCK:
        if STATE["state"] in ("qr_loading", "qr_ready", "scanned", "authorized"):
            return False
    _worker = threading.Thread(target=_login_flow, daemon=True)
    _worker.start()
    return True


# =====================================================================
# 微信公众号（mp.weixin.qq.com）
# 与上面三个平台同一套思路：真实二维码 → 加密存 cookie → 只读采集
# =====================================================================
WX_HOME = "https://mp.weixin.qq.com/"

WSTATE = {
    "state": "disconnected", "qr": None, "expiresIn": 0,
    "nickname": None, "lastRefresh": None, "message": None, "error": None,
    "_expires_at": 0,
}
_wx_worker = None


def wset(state, **kw):
    assert state in VALID_STATES, state
    with LOCK:
        WSTATE["state"] = state
        for k, v in kw.items():
            if k in WSTATE:
                WSTATE[k] = v


def wsnapshot():
    with LOCK:
        d = dict(WSTATE)
    if d["state"] == "qr_ready" and d.get("_expires_at"):
        d["expiresIn"] = max(0, int(d.pop("_expires_at") - time.time()))
    d.pop("_expires_at", None)
    return d


def _wechat_logged_in(ctx, page) -> bool:
    """微信公众平台登录判据：进到 /cgi-bin/ 后台 + 有登录 cookie。

    登录前停在 mp.weixin.qq.com 首页（扫码框），登录后会跳 /cgi-bin/home?token=...。
    cookie 里 slave_sid / bizuin 是后台会话，扫码前不会出现。
    """
    try:
        url = page.url or ""
        if "mp.weixin.qq.com" not in url or "/cgi-bin/" not in url:
            return False
        names = {c.get("name") for c in ctx.cookies()
                 if "weixin.qq.com" in (c.get("domain") or "")}
        return bool(names & {"slave_sid", "bizuin", "slave_user"})
    except Exception:
        return False


def _wechat_recent_articles(publish: dict, limit: int = 5) -> list:
    """把 /cgi-bin/appmsgpublish 的发表记录转成图文列表。

    返回结构（实测）：
      data.publish_page 是一个 JSON 字符串，里面有 publish_list
      每项的 publish_info 又是 JSON 字符串，里面 appmsgex[] 才是文章
      appmsgex 里有 title / cover / link / update_time，**没有阅读数**
    阅读数在「内容分析」页，那一页在无头浏览器下会返回「系统错误(-1)」，
    所以这里只给标题/封面/日期/链接，阅读数留空，不编。
    """
    raw = (publish or {}).get("publish_page")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    out = []
    seen = set()                     # 同一次群发里多篇文章标题会重复，按「标题+日期」去重
    for it in (raw or {}).get("publish_list") or []:
        info = it.get("publish_info")
        if isinstance(info, str):
            try:
                info = json.loads(info)
            except Exception:
                continue
        if not isinstance(info, dict):
            continue
        sent = (info.get("sent_info") or {}).get("time") or 0
        for a in info.get("appmsgex") or []:
            title = (a.get("title") or "").strip()
            link = a.get("link") or ""
            if not title or not link:
                continue
            ts = a.get("update_time") or sent
            dt = datetime.datetime.fromtimestamp(ts) if ts else None
            key = (title, dt.strftime("%Y-%m-%d") if dt else "")
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "date": dt.strftime("%Y-%m-%d") if dt else None,
                "cover": (a.get("cover") or "").replace("http://", "https://"),
                "link": link,
                "reads": None, "likes": None, "collects": None,
                "comments": None, "shares": None,
            })
            if len(out) >= limit:
                return out
    return out


def _collect_wechat(page) -> dict:
    """公众号后台：账号概览 + 最近图文。

    数据来源：
      1) 首页文本 → 总用户数 527、今日新增 +5
      2) window.wx.data.nick_name → 公众号昵称（比猜 DOM 选择器稳）
      3) /cgi-bin/appmsgpublish → 发表记录（首页会自动请求）
    阅读数在「内容分析」页（无头环境报错），所以图文先只给列表，不编数字。
    """
    box = {}

    def on_resp(r):
        u = r.url or ""
        if "/cgi-bin/appmsgpublish" in u and "json" in (r.headers.get("content-type") or ""):
            try:
                box.setdefault("publish", r.json())
            except Exception:
                pass

    page.on("response", on_resp)

    # 根路径会自动跳到 /cgi-bin/home?token=xxx
    page.goto(WX_HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(8000)

    dom = page.evaluate(r"""() => {
      const t = (document.body.innerText || '').replace(/\u200b/g, '');
      const m = t.match(/总用户数\s*([0-9,]+)\s*\+?([0-9,]+)?/);
      const d = (window.wx && wx.data) || {};
      return {
        nickname: d.nick_name || null,
        fans: m ? m[1] : null,
        newFans: m && m[2] ? m[2] : null,
      };
    }""")

    articles = _wechat_recent_articles(box.get("publish"))

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "wechat_raw.json").write_text(
            json.dumps({"publish": box.get("publish")}, ensure_ascii=False)[:300000])
    except Exception:
        pass

    return {
        "platform": {"id": "wechat", "name": "公众号", "sourceType": "mp_platform"},
        "account": {
            "nickname": dom.get("nickname") or "公众号",
            "fans": dom.get("fans"),
            "fansLabel": "总用户数",
        },
        "weekSummary": {
            "newFans": dom.get("newFans"),
            "reads": None,
        },
        # 最近发布的图文（不限本周）：公众号更新频率低，只按周会一条都看不到
        "contents": articles,
        "fansMetrics": None,
        "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
    }


def _wechat_after_login(ctx, page):
    save_session("wechat", ctx.cookies())
    wset("authorized", message="已授权，正在同步账号数据…")
    try:
        data = _collect_wechat(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "wechat_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        acc = data.get("account") or {}
        try:
            save_snapshot("wechat", {
                "fans": _num(acc.get("fans")),
                "likes": None,
                "weekPlays": None,
            })
        except Exception:
            pass

        wset("connected",
             nickname=acc.get("nickname"),
             lastRefresh=time.strftime("%H:%M"),
             message=f"已连接 · 用户 {acc.get('fans') or '—'}")
    except Exception as e:
        wset("connected", message=f"已连接，但采集失败：{str(e)[:80]}")


def _wechat_flow():
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    wset("qr_loading", message="正在唤起公众号登录页", qr=None, error=None)
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, locale="zh-CN",
                user_agent=UA, viewport={"width": 1280, "height": 900})
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 根路径会自动跳到带 token 的后台首页；未登录则停在扫码页
            page.goto(WX_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            if _wechat_logged_in(ctx, page):
                wset("authorized", message="检测到已登录会话")
                return _wechat_after_login(ctx, page)

            # 2) 未登录：回首页取二维码（扫码登录框在首页）
            page.goto(WX_HOME, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            qr = _grab_qr(page)
            if not qr:
                wset("error", error="未能获取二维码", message="登录页未渲染出二维码")
                return

            deadline = time.time() + QR_TTL
            wset("qr_ready", qr=qr, _expires_at=deadline, message="请用微信扫码选择公众号")

            for _ in range(POLL_MAX):
                time.sleep(POLL_INTERVAL)
                if _wechat_logged_in(ctx, page):
                    wset("authorized", message="已授权，正在保存会话")
                    return _wechat_after_login(ctx, page)

                txt = _page_text(page)
                if _risk_blocked(txt):
                    wset("blocked", message="检测到安全验证，请在手机上完成后重试")
                    return
                if "扫码成功" in txt or "请在手机上确认" in txt or "已扫描" in txt:
                    wset("scanned", message="已扫码，请在手机上确认登录")

                if time.time() > deadline:
                    wset("expired", message="二维码已过期，请重新生成", qr=None)
                    return
            wset("expired", message="等待超时，请重新生成", qr=None)
    except Exception as e:
        wset("error", error=str(e)[:200], message="登录流程异常")


def start_wechat_login():
    global _wx_worker
    with LOCK:
        if WSTATE["state"] in ("qr_loading", "qr_ready", "scanned", "authorized"):
            return False
    _wx_worker = threading.Thread(target=_wechat_flow, daemon=True)
    _wx_worker.start()
    return True


# ---------- AI 行业热点（AIHOT） ----------
HOT_URL = "https://aihot.news/"
HOT_TTL = 600                     # 10 分钟内复用缓存，别把人家站点当接口刷
_hot = {"at": 0.0, "data": None, "err": None}


def _fetch_hot(limit: int = 5) -> dict:
    """抓 aihot.news 首页「当前热点」前 N 条。

    直接用 urllib 请求（带浏览器 UA）：该站在无头浏览器下会返回 567，
    但普通 HTTP 请求能拿到 SSR 好的完整 HTML，所以只解析、不执行 JS。
    """
    now = time.time()
    if _hot["data"] and now - _hot["at"] < HOT_TTL:
        return _hot["data"]

    req = urllib.request.Request(HOT_URL, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "ignore")

    i = page.find("当前热点")
    if i < 0:
        raise ValueError("未找到「当前热点」区块")
    seg = page[i:]
    end = seg.find("</ol>")
    if end > 0:
        seg = seg[:end]

    items = []
    for li in seg.split("<li")[1:]:
        link = re.search(r'href="(/story/[^"]+)"', li)
        title = re.search(r'class="line-clamp-1[^"]*"[^>]*>([^<]+)<', li)
        if not (link and title):
            continue
        heat = re.search(r'text-ink-2">([\d,]+)</span>\s*热度', li)
        rank = re.search(r'text-rank-\d[^"]*">(\d+)</span>', li)
        items.append({
            "rank": int(rank.group(1)) if rank else len(items) + 1,
            "title": unescape(title.group(1)).strip(),
            "url": "https://aihot.news" + link.group(1),
            "heat": int(heat.group(1).replace(",", "")) if heat else None,
        })
        if len(items) >= limit:
            break

    data = {
        "source": "AIHOT",
        "sourceUrl": "https://aihot.news/",
        "fetchedAt": time.strftime("%H:%M"),
        "items": items,
    }
    if not items:
        raise ValueError("热点列表为空")
    _hot.update(at=now, data=data, err=None)
    return data


def hot_payload() -> dict:
    """接口用：抓取失败时退回上一次成功的结果，并标 stale。"""
    try:
        return _fetch_hot()
    except Exception as e:
        _hot["err"] = str(e)[:120]
        if _hot["data"]:
            return {**_hot["data"], "stale": True}
        return {"source": "AIHOT", "items": [], "error": _hot["err"]}


# ---------- 平台热榜：小红书创作灵感 + 抖音热榜 ----------
# 数据来源说明：
#   小红书：创作服务平台「笔记灵感」页自己发出的 /api/galaxy/creator/select/topic/detail
#           只监听页面请求，不构造签名；需要小红书登录态
#   抖音：  公开的 web 热榜接口，无需登录
# 筛选：
#   配了 DeepSeek → 让模型从原始热榜里挑，只能选不能编；
#   没配 / 调用失败 → 按关注词做规则筛选，保证页面仍可用。
XHS_INSPIRATION_URL = "https://creator.xiaohongshu.com/new/inspiration?source=official"
DOUYIN_HOT_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
HOTLISTS_TTL = 1800          # 半小时内复用缓存

AI_DEFAULTS = {
    "apiKey": "",
    "model": "deepseek-chat",
    "baseUrl": "https://api.deepseek.com",
    "interest": "AI 工具、内容创作、个人开发项目、效率提升。",
    "exclude": "纯娱乐八卦、体育赛事、明星绯闻、时政争议",
}

_hotlists = {"at": 0.0, "data": None}
_hotlists_lock = threading.Lock()


# ---------- AI 配置：加密落盘，接口永不回传 API Key ----------
def load_ai_config() -> dict:
    cfg = dict(AI_DEFAULTS)
    if not AI_CFG_PATH.exists():
        return cfg
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = AI_CFG_PATH.read_bytes()
        nonce, ct = blob[:12], blob[12:]
        raw = AESGCM(_master_key()).decrypt(nonce, ct, b"ai-config")
        cfg.update(json.loads(raw.decode()))
    except Exception:
        # 密钥变了 / 文件损坏：按未配置处理，不让设置页崩
        pass
    return cfg


def save_ai_config(cfg: dict) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    AI_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(cfg, ensure_ascii=False).encode()
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_master_key()).encrypt(nonce, raw, b"ai-config")
    AI_CFG_PATH.write_bytes(nonce + ct)
    os.chmod(AI_CFG_PATH, 0o600)


def ai_public_settings() -> dict:
    """给前端看的配置：只告诉有没有 Key，不返回 Key 本身。"""
    cfg = load_ai_config()
    return {
        "configured": bool((cfg.get("apiKey") or "").strip()),
        "model": cfg.get("model") or AI_DEFAULTS["model"],
        "baseUrl": cfg.get("baseUrl") or AI_DEFAULTS["baseUrl"],
        "interest": cfg.get("interest") or AI_DEFAULTS["interest"],
        "exclude": cfg.get("exclude") or AI_DEFAULTS["exclude"],
    }


def _deepseek_chat(messages: list, cfg: dict | None = None,
                   max_tokens: int = 1200, timeout: int = 45) -> str:
    """最小 OpenAI 兼容调用，只依赖标准库，不新增 SDK。"""
    cfg = cfg or load_ai_config()
    key = (cfg.get("apiKey") or "").strip()
    if not key:
        raise RuntimeError("未配置 DeepSeek API Key")
    base = (cfg.get("baseUrl") or AI_DEFAULTS["baseUrl"]).rstrip("/")
    body = {
        "model": cfg.get("model") or AI_DEFAULTS["model"],
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"DeepSeek HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"DeepSeek 连接失败：{e.reason}")
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


# ---------- 原始热榜采集 ----------
def _fetch_xhs_hot() -> dict:
    """打开创作服务平台「笔记灵感」，只监听页面自己发出的选题接口。"""
    if _COLLECTING.is_set():
        raise RuntimeError("正在采集账号数据，稍后再刷新热榜")
    sess = load_session("xhs")
    if not sess:
        raise RuntimeError("小红书未接入")
    cookies = sess.get("cookies") or []

    from playwright.sync_api import sync_playwright

    captured: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(locale="zh-CN", user_agent=UA,
                                  viewport={"width": 1440, "height": 900})
        try:
            ctx.add_cookies([{k: c[k] for k in ("name", "value", "domain", "path") if k in c}
                             for c in cookies])
            page = ctx.new_page()

            def on_resp(r):
                if "/api/galaxy/creator/select/topic/detail" in r.url:
                    try:
                        captured["json"] = r.json()
                    except Exception:
                        pass

            page.on("response", on_resp)
            page.goto(XHS_INSPIRATION_URL, wait_until="domcontentloaded", timeout=60000)
            for _ in range(24):          # 最多等 12 秒
                if captured:
                    break
                page.wait_for_timeout(500)
        finally:
            browser.close()

    data = (captured.get("json") or {}).get("data") or []
    items, rank = [], 0
    for cat in data:
        for t in (cat.get("selectTopics") or []):
            title = (t.get("title") or "").strip()
            if not title:
                continue
            rank += 1
            items.append({
                "platform": "xhs",
                "rank": rank,
                "title": title,
                "category": cat.get("labelName") or "",
                "heat": t.get("viewNum"),
                "join": t.get("joinNum"),
                "samples": [n.get("title") for n in (t.get("notes") or [])
                            if n.get("title")][:3],
                "url": "https://www.xiaohongshu.com/search_result?keyword="
                       + urllib.parse.quote(title),
            })
    if not items:
        raise RuntimeError("未拿到小红书创作灵感，登录态可能已失效")
    return {"source": "创作服务平台 · 笔记灵感", "items": items}


def _fetch_douyin_hot() -> dict:
    req = urllib.request.Request(DOUYIN_HOT_URL, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    items = []
    for i, w in enumerate((d.get("word_list") or [])[:30], 1):
        title = (w.get("word") or "").strip()
        if not title:
            continue
        items.append({
            "platform": "douyin",
            "rank": i,
            "title": title,
            "heat": w.get("hot_value"),
            "label": w.get("label"),
            "url": "https://www.douyin.com/search/" + urllib.parse.quote(title),
        })
    if not items:
        raise RuntimeError("抖音热榜为空")
    return {"source": "抖音热榜（公开接口）", "items": items}


# ---------- 筛选：DeepSeek 优先，规则兜底 ----------
# 规则兜底只做"粗筛"：宁可少选，也不要把美妆教程硬塞成 AI 选题
DEFAULT_INTEREST_WORDS = [
    "AI", "人工智能", "TRAE", "vibe", "开发", "编程", "代码", "效率",
    "创作", "内容", "自媒体", "知识", "科普", "商业", "职场", "兼职", "论文",
]


def _split_words(text: str) -> list:
    return [x.strip().lower() for x in re.split(r"[,，、;；/|]+", text or "")
            if len(x.strip()) >= 2]


def _hit(token: str, text: str) -> bool:
    """英文短词用词边界，避免 AI 命中 detail、mail 这类词。"""
    if re.fullmatch(r"[a-z0-9+#. ]{1,8}", token):
        return re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", text) is not None
    return token in text


def _rule_filter(raw: dict, cfg: dict) -> list:
    """没配 AI 时的兜底：标题命中为主，样例笔记只做弱证据。"""
    inc = [w.lower() for w in DEFAULT_INTEREST_WORDS]
    inc += _split_words(cfg.get("interest") or "")
    for phrase in list(inc):
        inc.extend([w for w in re.split(r"\s+", phrase) if len(w) >= 2])
    inc = list(dict.fromkeys([w for w in inc if w]))
    exc = [w.lower() for w in _split_words(cfg.get("exclude") or "")]

    picked = []
    for pid in ("xhs", "douyin"):
        for it in (raw.get(pid) or {}).get("items", []):
            title = (it.get("title") or "").lower()
            samples = " ".join(it.get("samples") or []).lower()
            if any(_hit(x, title + " " + samples) for x in exc):
                continue
            title_hits = [x for x in inc if _hit(x, title)]
            sample_hits = [x for x in inc if _hit(x, samples)]
            # 标题没命中时，至少要两条样例命中，避免"美妆教程"因为教程两个字被选进来
            if not title_hits and len(set(sample_hits)) < 2:
                continue
            score = min(95, 55 + len(title_hits) * 14 + len(set(sample_hits)) * 4)
            picked.append({
                "platform": pid,
                "title": it["title"],
                "score": score,
                "reason": "命中关注词：" + "、".join((title_hits or list(dict.fromkeys(sample_hits)))[:3]),
                "angle": "结合你的账号方向，做一条「观点 + 3 个步骤 / 前后对比」的内容。",
                "heat": it.get("heat"),
                "url": it.get("url"),
                "rank": it.get("rank"),
            })

    picked.sort(key=lambda x: -(x.get("score") or 0))
    out, count = [], {"xhs": 0, "douyin": 0}
    for p in picked:
        if count[p["platform"]] >= 4 or len(out) >= 8:
            continue
        out.append(p)
        count[p["platform"]] += 1
    return out


def _ai_filter(raw: dict, cfg: dict) -> list:
    flat = []
    for pid in ("xhs", "douyin"):
        for it in (raw.get(pid) or {}).get("items", [])[:30]:
            flat.append({
                "platform": pid,
                "rank": it.get("rank"),
                "title": it["title"],
                "heat": it.get("heat"),
                "samples": it.get("samples") or [],
            })
    if not flat:
        return []

    # 账号近期内容：让模型知道"你平时写什么"，不是只看一段抽象的关注方向
    recent = []
    try:
        for pid, fn in (("xhs", "xhs_creator.json"), ("douyin", "douyin_creator.json")):
            p = DATA_DIR / fn
            if not p.exists():
                continue
            d = json.loads(p.read_text())
            rows = d.get("contents") or []
            for x in rows[:4]:
                if x.get("title"):
                    recent.append(f"{pid}: {x['title'][:40]}")
    except Exception:
        pass

    lines = []
    for i, it in enumerate(flat, 1):
        sample = ("；样例：" + " / ".join(it["samples"][:2])) if it["samples"] else ""
        lines.append(f"[{i}] {it['platform']} | {it['title']} | 热度 {it['heat']}{sample}")

    prompt = f"""你是自媒体选题助手，只做筛选，不编造。
账号关注方向：{cfg.get('interest') or AI_DEFAULTS['interest']}
明确排除：{cfg.get('exclude') or AI_DEFAULTS['exclude']}
账号近期内容：{'；'.join(recent) or '暂无'}

以下是平台热榜 / 创作话题，只能从里面选，不能新增标题：
{chr(10).join(lines)}

请选出最多 8 条最适合这个账号的选题，严格输出 JSON：
{{"items":[{{"platform":"xhs 或 douyin","title":"必须与输入标题完全一致","score":0,"reason":"一句话说明为什么适合这个账号","angle":"一句话给出具体可拍 / 可写的选题角度"}}]}}
要求：
1. score 是 0-100 的匹配度；
2. 优先能自然结合 AI / 开发 / 创作 / 生活记录的方向；
3. 完全不相关就不要选，宁可少选；
4. 不要编造输入里没有的标题。"""

    content = _deepseek_chat([
        {"role": "system", "content": "你只输出合法 JSON，不输出解释。"},
        {"role": "user", "content": prompt},
    ], cfg, max_tokens=1400, timeout=50)

    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise RuntimeError("DeepSeek 返回内容不是 JSON")
    data = json.loads(m.group(0))
    allowed = {(it["platform"], it["title"]): it for it in flat}
    out = []
    for x in (data.get("items") or []):
        key = (x.get("platform"), x.get("title"))
        if key not in allowed:
            continue
        src = allowed[key]
        out.append({
            "platform": key[0],
            "title": key[1],
            "score": int(x.get("score") or 0),
            "reason": str(x.get("reason") or "")[:160],
            "angle": str(x.get("angle") or "")[:200],
            "heat": src.get("heat"),
            "url": None,
            "rank": src.get("rank"),
        })
        if len(out) >= 8:
            break
    # 补上原始链接
    for o in out:
        pid = o["platform"]
        for it in (raw.get(pid) or {}).get("items", []):
            if it["title"] == o["title"]:
                o["url"] = it.get("url")
                break
    return out


def _filter_hotlists(raw: dict, cfg: dict) -> dict:
    if (cfg.get("apiKey") or "").strip():
        try:
            return {"mode": "ai", "model": cfg.get("model"), "error": None,
                    "items": _ai_filter(raw, cfg)}
        except Exception as e:
            return {"mode": "rule", "model": None, "error": str(e)[:160],
                    "items": _rule_filter(raw, cfg)}
    return {"mode": "rule", "model": None, "error": None,
            "items": _rule_filter(raw, cfg)}


def hotlists_payload(force: bool = False) -> dict:
    """聚合 + 筛选；失败时退回上一份缓存，并在 errors 里说明原因。"""
    now = time.time()
    if not force and _hotlists["data"] and now - _hotlists["at"] < HOTLISTS_TTL:
        return _hotlists["data"]

    with _hotlists_lock:
        # 等锁期间可能已经有人刷完了，再查一次
        if not force and _hotlists["data"] and time.time() - _hotlists["at"] < HOTLISTS_TTL:
            return _hotlists["data"]

        old = _hotlists["data"] or {}
        errors = {}
        try:
            xhs = _fetch_xhs_hot()
        except Exception as e:
            errors["xhs"] = str(e)[:160]
            xhs = old.get("xhs") or {"source": "创作服务平台 · 笔记灵感", "items": []}
        try:
            douyin = _fetch_douyin_hot()
        except Exception as e:
            errors["douyin"] = str(e)[:160]
            douyin = old.get("douyin") or {"source": "抖音热榜（公开接口）", "items": []}

        raw = {"xhs": xhs, "douyin": douyin}
        cfg = load_ai_config()
        data = {
            "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "xhs": xhs,
            "douyin": douyin,
            "filter": _filter_hotlists(raw, cfg),
            "errors": errors,
        }
        _hotlists.update(at=time.time(), data=data)
        return data


# ---------- 选题工作台 ----------
def _idea_top_content(limit: int = 5) -> list:
    """从两个平台的近期内容里挑互动最高的几条，作为"你已经验证过什么"的上下文。"""
    rows = []
    for pid, fn in (
        ("xhs", "xhs_creator.json"),
        ("douyin", "douyin_creator.json"),
    ):
        p = DATA_DIR / fn
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
            for x in (d.get("contents") or []):
                title = (x.get("title") or "").strip()
                if not title:
                    continue
                heat = x.get("read") or x.get("plays") or x.get("likes") or 0
                rows.append((pid, title, _num(heat) or 0))
        except Exception:
            pass
    rows.sort(key=lambda r: -r[2])
    return [{"platform": p, "title": t[:60]} for p, t, _ in rows[:limit]]


def _idea_hot_context(per_platform: int = 20) -> dict:
    """选题工作台用：给模型足够多的当下热点，而不是只给几条筛选结果。"""
    try:
        data = hotlists_payload()
    except Exception:
        return {"filtered": [], "xhs": [], "douyin": []}
    filtered = [{
        "platform": i.get("platform"), "title": i.get("title"),
        "reason": i.get("reason"),
    } for i in ((data.get("filter") or {}).get("items") or [])[:8]]
    xhs = [{
        "title": i.get("title"), "category": i.get("category"),
    } for i in ((data.get("xhs") or {}).get("items") or [])[:per_platform]]
    douyin = [{
        "title": i.get("title"),
    } for i in ((data.get("douyin") or {}).get("items") or [])[:per_platform]]
    return {"filtered": filtered, "xhs": xhs, "douyin": douyin}


def _json_from_ai(prompt: str, cfg: dict, max_tokens: int) -> dict:
    content = _deepseek_chat([
        {"role": "system", "content": "你只输出合法 JSON，不输出解释，不要用 Markdown 代码块。"},
        {"role": "user", "content": prompt},
    ], cfg, max_tokens=max_tokens, timeout=60)
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise RuntimeError("DeepSeek 返回内容不是 JSON")
    return json.loads(m.group(0))


def _norm_idea(x: dict) -> dict:
    platform = x.get("platform") if x.get("platform") in ("xhs", "douyin", "both") else "both"
    cost = x.get("cost") if x.get("cost") in ("低", "中", "高") else "中"
    try:
        score = max(0, min(100, int(x.get("score") or 0)))
    except Exception:
        score = 0
    return {
        "title": str(x.get("title") or "")[:90],
        "angle": str(x.get("angle") or "")[:220],
        "format": str(x.get("format") or "教程")[:12],
        "platform": platform,
        "audience": str(x.get("audience") or "")[:60],
        "cost": cost,
        "score": score,
        "reason": str(x.get("reason") or "")[:180],
        "source": str(x.get("source") or "经历")[:12],
        "basedOn": str(x.get("basedOn") or x.get("based_on") or "")[:90],
    }


def generate_ideas(payload: dict) -> dict:
    cfg = load_ai_config()
    if not (cfg.get("apiKey") or "").strip():
        return {"ok": False, "error": "请先在设置页接入 DeepSeek API Key"}

    did = str(payload.get("did") or "").strip()[:800]
    stuck = str(payload.get("stuck") or "").strip()[:800]
    asked = str(payload.get("asked") or "").strip()[:800]
    platform = payload.get("platform") if payload.get("platform") in ("xhs", "douyin", "both") else "both"

    top = _idea_top_content()
    hot = _idea_hot_context()
    # 热榜一条都没有（通常是小红书登录失效 + 抖音接口失败）时不硬生成：
    # 否则提示词要求"至少 7 条来自热榜"，模型只能编。
    if not (hot["xhs"] or hot["douyin"] or hot["filtered"]):
        return {"ok": False,
                "error": "平台热榜为空：请到内容页刷新热榜，或检查小红书登录态后重试"}
    top_lines = "；".join(f"{x['platform']}: {x['title']}" for x in top) or "暂无"
    xhs_lines = "、".join(
        f"{x['title']}（{x.get('category') or '话题'}）" for x in hot["xhs"]
    ) or "暂无"
    dy_lines = "、".join(x["title"] for x in hot["douyin"]) or "暂无"
    filtered_lines = "；".join(
        f"{x['platform']}: {x['title']}（{x.get('reason') or '相关'}）"
        for x in hot["filtered"]
    ) or "暂无"
    platform_text = {"xhs": "小红书", "douyin": "抖音", "both": "小红书 + 抖音"}.get(platform, "小红书 + 抖音")

    style = payload.get("style") or {}
    tone = str(style.get("tone") or "").strip()[:200]
    samples = [s for s in (style.get("samples") or []) if isinstance(s, dict)][:8]
    sample_lines = "；".join(
        f"{s.get('title', '')} / {s.get('angle', '')}" for s in samples
    ) or "暂无"

    fb = payload.get("feedback") or {}
    dislikes = [d for d in (fb.get("dislikes") or []) if isinstance(d, dict)][:10]
    planned = [p for p in (fb.get("plan") or []) if isinstance(p, dict)][:10]
    dislike_lines = "；".join(
        f"{d.get('title', '')}（{d.get('reason', '')}"
        + (f"：{d.get('other')}" if d.get('other') else "") + "）"
        for d in dislikes
    ) or "暂无"
    like_lines = "；".join(
        f"{p.get('title', '')} / {p.get('angle', '')}" for p in planned
    ) or "暂无"

    # 发布后的真实结果 + 已做过的主题：这是闭环反哺的核心
    published = [p for p in (fb.get("published") or []) if isinstance(p, dict)][:8]
    done = [str(t)[:60] for t in (fb.get("done") or []) if str(t).strip()][:40]
    pname = {"xhs": "小红书", "douyin": "抖音", "bilibili": "B 站", "wechat": "公众号"}

    def pub_line(p):
        try:
            ratio = float(p.get("ratio"))
        except Exception:
            ratio = None
        cmp_text = ""
        if ratio is not None:
            cmp_text = f"，{'高于' if ratio >= 1 else '低于'}同平台中位 {ratio:.1f}×"
        plat = pname.get(str(p.get("platform")), str(p.get("platform") or ""))
        return (f"{p.get('title', '')}（{plat}，播放/阅读 {p.get('views', '—')}，"
                f"涨粉 {p.get('fansGain', 0)}，评分 {p.get('score', '—')}{cmp_text}）")

    good_lines = "；".join(pub_line(p) for p in published
                          if p.get("verdict") == "good") or "暂无"
    bad_lines = "；".join(pub_line(p) for p in published
                         if p.get("verdict") == "bad") or "暂无"
    done_lines = "；".join(done) or "暂无"

    prompt = f"""你是资深自媒体选题策划。请把下面这些真实素材，变成可以直接开工的选题。
账号关注方向：{cfg.get('interest') or AI_DEFAULTS['interest']}
目标平台：{platform_text}
写作口吻：{tone or '未指定'}
我改过的选题（模仿选题偏好和口吻，不要照抄标题）：{sample_lines}

【反馈，必须用于调整下一轮】
我不喜欢的选题及原因：{dislike_lines}
我已经加入本周计划的选题（喜欢的方向）：{like_lines}
原因对应规则：方向太偏→少跨领域；文案太差→标题和切口更具体；不够热点→优先热榜前 10；太普通→加反差和具体场景；其他→按补充说明避开。

【已经验证过的发布结果，必须参考】
有效（高于同平台中位）：{good_lines}
无效（低于同平台中位）：{bad_lines}
已做过 / 已计划过的主题（不要重复，也不要换汤不换药）：{done_lines}

【当下平台热点，优先使用】
小红书创作灵感（前 20 条）：{xhs_lines}
抖音热榜（前 20 条）：{dy_lines}
已按账号方向筛选过的热榜：{filtered_lines}

【你自己的存量内容，只占少数】
近期表现较好的内容：{top_lines}

【本周素材，只占少数】
1. 这周做了什么：{did or '（未填写）'}
2. 这周被什么卡住了：{stuck or '（未填写）'}
3. 读者最常问什么：{asked or '（未填写）'}

请生成 10 个具体、可执行的选题，硬性要求：
- 配比：至少 7 条必须直接基于上面的平台热点 / 热榜（basedOn 写对应标题），最多 2 条来自已有内容，最多 1 条来自读者问题 / 卡点；
- 优先复用「有效」选题的结构、切口和开头方式，但标题必须重新写；
- 明显避开「无效」选题的同类方向；
- 不得与「已做过 / 已计划过」的主题重复，也不要只换标题不换切口；
- 每条热点选题都要说明「这个热点为什么现在能火」以及「你的账号怎么接」，不要只把热点词塞进标题；
- 覆盖不同格式：教程、避坑、对比、清单、复盘、故事；
- 每个选题必须是一个具体的事 / 问题 / 场景，不要"聊聊 XX""分享一些心得"这种空泛题；
- 切口要具体到能直接写开头，不要只给一个领域词；
- 优先考虑一个人能在一周内做完的成本，并标注低 / 中 / 高。

严格输出 JSON：
{{"items":[{{"title":"选题名（一句话）","angle":"具体切口，说明从哪个点切入","format":"教程/避坑/对比/清单/复盘/故事","platform":"xhs/douyin/both","audience":"给谁看","cost":"低/中/高","score":0-100,"reason":"为什么值得做","source":"热榜/爆款/经历/读者问题","basedOn":"对应的热点/内容标题，必须来自上面的列表"}}]}}"""

    try:
        data = _json_from_ai(prompt, cfg, max_tokens=4200)
        items = [_norm_idea(x) for x in (data.get("items") or [])]
        items = [x for x in items if x["title"] and x["angle"]][:10]
        if not items:
            raise RuntimeError("DeepSeek 没有返回有效选题")
        return {"ok": True, "items": items, "model": cfg.get("model"),
                "context": {"top": top, "hot": hot}}
    except Exception as e:
        return {"ok": False, "error": str(e)[:240]}


def expand_idea(payload: dict) -> dict:
    cfg = load_ai_config()
    if not (cfg.get("apiKey") or "").strip():
        return {"ok": False, "error": "请先在设置页接入 DeepSeek API Key"}

    idea = payload.get("idea") or {}
    title = str(idea.get("title") or "").strip()[:120]
    angle = str(idea.get("angle") or "").strip()[:260]
    based_on = str(idea.get("basedOn") or "").strip()[:120]
    platform = idea.get("platform") if idea.get("platform") in ("xhs", "douyin", "both") else "both"
    if not title:
        return {"ok": False, "error": "缺少选题"}

    top = _idea_top_content(3)
    top_lines = "；".join(f"{x['platform']}: {x['title']}" for x in top) or "暂无"
    platform_text = {"xhs": "小红书", "douyin": "抖音", "both": "小红书 + 抖音"}.get(platform, "小红书 + 抖音")

    style = payload.get("style") or {}
    tone = str(style.get("tone") or "").strip()[:200]
    samples = [s for s in (style.get("samples") or []) if isinstance(s, dict)][:8]
    sample_lines = "；".join(
        f"{s.get('title', '')} / {s.get('angle', '')}" for s in samples
    ) or "暂无"

    fb = payload.get("feedback") or {}
    dislikes = [d for d in (fb.get("dislikes") or []) if isinstance(d, dict)][:10]
    dislike_lines = "；".join(
        f"{d.get('title', '')}（{d.get('reason', '')}"
        + (f"：{d.get('other')}" if d.get('other') else "") + "）"
        for d in dislikes
    ) or "暂无"

    prompt = f"""你是自媒体内容策划。把下面这个选题展开成可直接开写的方案。
账号关注方向：{cfg.get('interest') or AI_DEFAULTS['interest']}
写作口吻：{tone or '未指定'}
我改过的选题（模仿口吻，不要照抄）：{sample_lines}
不喜欢的选题及原因（避免类似表达）：{dislike_lines}
近期表现较好的内容：{top_lines}
目标平台：{platform_text}
选题：{title}
切口：{angle}
参考热点 / 来源：{based_on or '无'}

严格输出 JSON：
{{"titles":["5 个标题"],"covers":["3 个封面文案"],"hook":"开头 3 秒 / 第一句话","structure":["4-6 个内容步骤"]}}
要求：
1. 标题要具体、有信息量，不夸张、不虚构；
2. 封面文案每条不超过 14 个字；
3. 开头第一句先给结果或冲突，不要寒暄；
4. 结构按"先做什么、再做什么"写，能直接照着拍 / 写。"""

    try:
        data = _json_from_ai(prompt, cfg, max_tokens=1600)
        titles = [str(x)[:80] for x in (data.get("titles") or [])][:5]
        covers = [str(x)[:40] for x in (data.get("covers") or [])][:3]
        structure = [str(x)[:160] for x in (data.get("structure") or [])][:6]
        hook = str(data.get("hook") or "")[:200]
        if not titles and not structure:
            raise RuntimeError("DeepSeek 没有返回有效展开内容")
        return {"ok": True, "titles": titles, "covers": covers,
                "hook": hook, "structure": structure}
    except Exception as e:
        return {"ok": False, "error": str(e)[:240]}


# ---------- 解绑 ----------
# 只清对应平台的 cookie；两个平台共用一个浏览器 profile，不能一键全清
SESSION_DOMAIN = {
    "xhs": re.compile(r"xiaohongshu\.com$"),
    "douyin": re.compile(r"douyin\.com$"),
    "bilibili": re.compile(r"bilibili\.com$"),
    "wechat": re.compile(r"weixin\.qq\.com$"),
}


def _disconnect_flow(platform: str) -> None:
    """解绑：清掉该平台的一切本地痕迹。

    清什么：
      1) 浏览器 cookie（只清这个平台的域名）
      2) 加密会话文件 sessions/{pid}.bin
      3) 采集数据 data/{pid}_creator.json、B 站的 bilibili_raw.json
      4) history.json 里该平台的每日快照
    这四样缺一样，页面上就会留下"已经解绑但卡片/曲线还在"的残留。
    """
    from playwright.sync_api import sync_playwright

    if _COLLECTING.is_set():
        return
    _COLLECTING.set()
    try:
        pattern = SESSION_DOMAIN[platform]
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, locale="zh-CN",
                user_agent=UA, viewport={"width": 1280, "height": 860})
            try:
                # 逐个删，避免把另一个平台的登录态一起清掉
                for c in ctx.cookies():
                    if pattern.search(c.get("domain", "")):
                        ctx.clear_cookies(name=c["name"],
                                          domain=c["domain"], path=c["path"])
            finally:
                ctx.close()

        # 会话文件 + 采集数据 + 原始响应
        victims = [SESS_DIR / f"{platform}.bin",
                   DATA_DIR / f"{platform}_creator.json"]
        if platform == "bilibili":
            victims.append(DATA_DIR / "bilibili_raw.json")
        elif platform == "wechat":
            victims.append(DATA_DIR / "wechat_raw.json")
        for f in victims:
            if f.exists():
                f.unlink()

        # 历史快照：不删的话趋势图还会画着已解绑平台的柱子
        try:
            if HISTORY_PATH.exists():
                h = json.loads(HISTORY_PATH.read_text())
                if platform in h:
                    h.pop(platform)
                    HISTORY_PATH.write_text(
                        json.dumps(h, ensure_ascii=False, indent=2))
        except Exception:
            pass

        if platform == "xhs":
            set_state("disconnected", qr=None, nickname=None,
                      lastRefresh=None, message="已解除绑定")
        elif platform == "douyin":
            dset("disconnected", qr=None, nickname=None,
                 lastRefresh=None, message="已解除绑定")
        elif platform == "bilibili":
            bset("disconnected", qr=None, nickname=None,
                 lastRefresh=None, message="已解除绑定")
        else:
            wset("disconnected", qr=None, nickname=None,
                 lastRefresh=None, message="已解除绑定")
    except Exception as e:
        if platform == "xhs":
            set_state("error", message=f"解绑失败：{str(e)[:80]}")
        elif platform == "douyin":
            dset("error", message=f"解绑失败：{str(e)[:80]}")
        elif platform == "bilibili":
            bset("error", message=f"解绑失败：{str(e)[:80]}")
        else:
            wset("error", message=f"解绑失败：{str(e)[:80]}")
    finally:
        _COLLECTING.clear()


def start_disconnect(platform: str) -> bool:
    if _COLLECTING.is_set():
        return False
    threading.Thread(target=_disconnect_flow, args=(platform,), daemon=True).start()
    return True


# ---------- 每日采集（12:00） ----------
# 三项数据其实来自同一次采集：一次 _collect 同时产出
#   ① 账号概览   ② 本周内容明细   ③ 写入当日快照
# 所以只需要一个 12:00 的任务。

_LOGIN_BUSY = ("qr_loading", "qr_ready", "scanned", "authorized")
_COLLECTING = threading.Event()   # 采集进行中标记：避免定时采集和扫码抢同一个浏览器配置目录


def _seconds_until_next_run() -> float:
    """距离下一次 12:00 还有多少秒；今天已过 12:00 就顺延到明天。"""
    now = datetime.datetime.now()
    target = now.replace(hour=DAILY_HOUR, minute=DAILY_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def _refresh_xhs(ctx) -> None:
    """小红书：账号概览 + 本周笔记明细 + 当日快照。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)

        # 登录态失效时不硬闯，把状态交回前端，让用户重新扫码；
        # 页面偶尔还没跳转完，先多等一次再判失败，避免误报"登录失效"
        if not _logged_in(page):
            page.wait_for_timeout(2500)
        if not _logged_in(page):
            set_state("disconnected", qr=None, message="登录态已失效，请重新扫码")
            return

        data = _collect(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "xhs_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        ws = data.get("weekSummary") or {}
        try:
            save_snapshot("xhs", {
                "fans": _num(data["account"].get("fans")),
                "likesCollects": _num(data["account"].get("likesCollects")),
                "weekRead": ws.get("read"),
                "weekLikes": ws.get("likes"),
                "weekCollects": ws.get("collects"),
            })
        except Exception:
            pass

        set_state("connected",
                  nickname=(data.get("account") or {}).get("nickname"),
                  lastRefresh=time.strftime("%H:%M"),
                  message=(f"每日采集 · 近 {CONTENT_DAYS} 天 {len(data.get('contents') or [])} 篇"
                           f" · 本周 {ws.get('notes', 0)} 篇"))
    except Exception as e:
        set_state(STATE["state"], message=f"每日采集失败：{str(e)[:80]}")


def _refresh_douyin(ctx) -> None:
    """抖音：账号概览 + 近 7 日作品明细 + 当日快照。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(DY_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        if not _douyin_logged_in(page):
            page.wait_for_timeout(2500)
        if not _douyin_logged_in(page):
            dset("disconnected", qr=None, message="登录态已失效，请重新扫码")
            return

        data = _collect_douyin(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "douyin_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        ws = data.get("weekSummary") or {}
        try:
            save_snapshot("douyin", {
                "fans": _num(data["account"].get("fans")),
                "likes": _num(data["account"].get("likes")),
                "weekPlays": _num(ws.get("plays")),
                "weekLikes": _num(ws.get("likes")),
            })
        except Exception:
            pass

        dset("connected", lastRefresh=time.strftime("%H:%M"),
             message=f"每日采集 · 粉丝 {data['account'].get('fans') or '—'}")
    except Exception as e:
        dset(DSTATE["state"], message=f"每日采集失败：{str(e)[:80]}")


def _refresh_bilibili(ctx) -> None:
    """B 站：账号概览 + 本周稿件 + 当日快照。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto(BILI_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        if not _bili_logged_in(ctx, page):
            page.wait_for_timeout(2500)
        if not _bili_logged_in(ctx, page):
            bset("disconnected", qr=None, message="登录态已失效，请重新扫码")
            return

        data = _collect_bilibili(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "bilibili_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        acc = data.get("account") or {}
        try:
            save_snapshot("bilibili", {
                "fans": _num(acc.get("fans")),
                "likes": _num(acc.get("likes")),
                "weekPlays": _num(acc.get("plays")),
            })
        except Exception:
            pass

        bset("connected", lastRefresh=time.strftime("%H:%M"),
             message=f"每日采集 · 粉丝 {acc.get('fans') or '—'}")
    except Exception as e:
        bset(BSTATE["state"], message=f"每日采集失败：{str(e)[:80]}")


def _refresh_wechat(ctx) -> None:
    """公众号：账号概览 + 最近图文 + 当日快照。"""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        # 根路径会自动跳到带 token 的后台首页；直接拼死地址反而会报错
        page.goto(WX_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        if not _wechat_logged_in(ctx, page):
            page.wait_for_timeout(2500)
        if not _wechat_logged_in(ctx, page):
            wset("disconnected", qr=None, message="登录态已失效，请重新扫码")
            return

        data = _collect_wechat(page)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "wechat_creator.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

        acc = data.get("account") or {}
        try:
            save_snapshot("wechat", {"fans": _num(acc.get("fans")),
                                     "likes": None, "weekPlays": None})
        except Exception:
            pass

        wset("connected", nickname=acc.get("nickname"),
             lastRefresh=time.strftime("%H:%M"),
             message=f"每日采集 · 用户 {acc.get('fans') or '—'}")
    except Exception as e:
        wset(WSTATE["state"], message=f"每日采集失败：{str(e)[:80]}")


def refresh_all(reason: str = "每日采集") -> bool:
    """四个平台各采集一次。返回是否真的跑完了一轮。"""
    if _COLLECTING.is_set():
        return False
    _COLLECTING.set()
    try:
        from playwright.sync_api import sync_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            # 复用同一个持久化 profile，扫码时保存的 cookie 直接生效
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=True, locale="zh-CN",
                user_agent=UA, viewport={"width": 1280, "height": 860})
            try:
                _refresh_xhs(ctx)
                _refresh_douyin(ctx)
                _refresh_bilibili(ctx)
                _refresh_wechat(ctx)
            finally:
                ctx.close()
        return True
    except Exception as e:
        set_state(STATE["state"], message=f"{reason}异常：{str(e)[:80]}")
        return False
    finally:
        _COLLECTING.clear()


def refresh_async(reason: str = "手动巡检") -> bool:
    """把全量采集丢到后台线程，HTTP 立刻返回。

    采集要开无头浏览器，同步跑会让前端请求等上一分钟，
    所以这里只负责"点火"，成没成由前端轮询 /api/refresh/status。
    """
    if _COLLECTING.is_set():
        return False                    # 已经在采，不重复开浏览器
    threading.Thread(target=refresh_all, args=(reason,), daemon=True).start()
    return True


def _scheduler_loop() -> None:
    """常驻线程：每天 12:00 触发一次全量采集。"""
    while True:
        time.sleep(_seconds_until_next_run())

        # 正卡在扫码里就先等，最多等 5 分钟，避免两个流程同时开浏览器
        deadline = time.time() + 300
        while time.time() < deadline and (
                STATE["state"] in _LOGIN_BUSY or DSTATE["state"] in _LOGIN_BUSY
                or BSTATE["state"] in _LOGIN_BUSY or WSTATE["state"] in _LOGIN_BUSY):
            time.sleep(5)
        if (STATE["state"] in _LOGIN_BUSY or DSTATE["state"] in _LOGIN_BUSY
                or BSTATE["state"] in _LOGIN_BUSY or WSTATE["state"] in _LOGIN_BUSY):
            print("12:00 采集跳过：扫码流程仍占用浏览器", flush=True)
            continue

        ok = refresh_all("每日采集")
        print(f"12:00 每日采集{'完成' if ok else '失败'}", flush=True)
        time.sleep(120)   # 跨过 12:00 这一分钟，避免重复触发


def _age_hours(path: Path) -> float:
    """数据文件距今多少小时；读不出来就当作很旧，触发补采。"""
    try:
        d = json.loads(path.read_text())
        t = datetime.datetime.fromisoformat(d.get("collectedAt") or "")
        now = datetime.datetime.now(t.tzinfo) if t.tzinfo else datetime.datetime.now()
        return (now - t).total_seconds() / 3600
    except Exception:
        return float("inf")


def _catch_up_if_stale() -> None:
    """服务启动时补采：已有数据但超过 24 小时没更新，就立刻补一次。

    解决"12:00 时服务没开着"导致当天数据空缺的问题。
    """
    files = [p for p in (DATA_DIR / "xhs_creator.json",
                         DATA_DIR / "douyin_creator.json",
                         DATA_DIR / "bilibili_creator.json",
                         DATA_DIR / "wechat_creator.json") if p.exists()]
    if not files:
        return                      # 从没采集过，等用户扫码
    # 以「最新的一份」为准：最少年龄 < 24 小时，说明今天已经采过。
    # 某个平台长期登录失效时，它的文件会一直很旧；按 all() 或 max() 判断，
    # 每次重启都会重新全量采集一遍，白白开浏览器。
    if min(_age_hours(p) for p in files) < 24:
        return                      # 今天已经采过
    refresh_all("启动补采")


# ---------- HTTP ----------
def read_platform_data(path: Path) -> dict:
    """读平台采集文件，并兼容旧格式。

    旧版本只写 weekNotes / weekWorks（且按「本周」截断），
    新版本统一叫 contents（近 30 天）；这里做一次读时归一化，
    避免升级后旧数据直接显示为空，也不改动磁盘上的原始文件。
    """
    d = json.loads(path.read_text())
    if not d.get("contents"):
        d["contents"] = d.get("weekNotes") or d.get("weekWorks") or []
    return d


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, fmt, *args):
        # 关闭访问日志，避免任何请求细节落盘
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许 file:// 打开页面时跨域读状态（服务只监听本机）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 前端切页 / 关页面时会主动断开，属于正常现象，不打印堆栈
            pass

    def _body_json(self) -> dict:
        """读 POST body；解析失败就当空对象，避免接口整段 500。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "ignore"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/xhs/state"):
            return self._json(snapshot())
        if self.path.startswith("/api/douyin/state"):
            return self._json(dsnapshot())
        if self.path.startswith("/api/bilibili/state"):
            return self._json(bsnapshot())
        if self.path.startswith("/api/wechat/state"):
            return self._json(wsnapshot())
        if self.path.startswith("/api/wechat/data"):
            p = DATA_DIR / "wechat_creator.json"
            if p.exists():
                return self._json(read_platform_data(p))
            return self._json({"error": "no data"}, 404)
        if self.path.startswith("/api/bilibili/data"):
            p = DATA_DIR / "bilibili_creator.json"
            if p.exists():
                return self._json(read_platform_data(p))
            return self._json({"error": "no data"}, 404)
        if self.path.startswith("/api/douyin/data"):
            p = DATA_DIR / "douyin_creator.json"
            if p.exists():
                return self._json(read_platform_data(p))
            return self._json({"error": "no data"}, 404)
        if self.path.startswith("/api/hotlists"):
            return self._json(hotlists_payload())
        if self.path.startswith("/api/hot"):
            return self._json(hot_payload())
        if self.path.startswith("/api/settings"):
            # 设置页用：只返回路径、采集节奏与"有没有配 AI"，不返回任何凭证
            return self._json({
                "dataDir": str(DATA_DIR),
                "sessionsDir": str(SESS_DIR),
                "dailyHour": DAILY_HOUR,
                "dailyMinute": DAILY_MINUTE,
                "version": "0.1",
                "ai": ai_public_settings(),
            })
        if self.path.startswith("/api/refresh/status"):
            return self._json({"running": _COLLECTING.is_set()})
        if self.path.startswith("/api/history"):
            if HISTORY_PATH.exists():
                return self._json(json.loads(HISTORY_PATH.read_text()))
            return self._json({"xhs": [], "douyin": []})
        if self.path.startswith("/api/xhs/data") or self.path.startswith("/api/xhs/notes"):
            p = DATA_DIR / "xhs_creator.json"
            if p.exists():
                return self._json(read_platform_data(p))
            return self._json({"error": "no data"}, 404)
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/refresh/status"):
            return self._json({"running": _COLLECTING.is_set()})
        if self.path.startswith("/api/refresh"):
            return self._json({"started": refresh_async()})
        if self.path.startswith("/api/hotlists/refresh"):
            # 手动刷新：同步采一轮，前端按钮转圈等结果
            return self._json(hotlists_payload(force=True))
        if self.path.startswith("/api/ideas/generate"):
            return self._json(generate_ideas(self._body_json()))
        if self.path.startswith("/api/ideas/expand"):
            return self._json(expand_idea(self._body_json()))
        if self.path.startswith("/api/settings/ai/test"):
            body = self._body_json()
            cfg = load_ai_config()
            if (body.get("apiKey") or "").strip():
                cfg["apiKey"] = body["apiKey"].strip()
            if body.get("model"):
                cfg["model"] = str(body["model"]).strip()
            if body.get("baseUrl"):
                cfg["baseUrl"] = str(body["baseUrl"]).strip()
            try:
                reply = _deepseek_chat(
                    [{"role": "user", "content": "只回复两个字：正常"}],
                    cfg, max_tokens=20, timeout=25)
                return self._json({"ok": True, "reply": reply.strip()[:50],
                                   "model": cfg.get("model")})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path.startswith("/api/settings/ai"):
            body = self._body_json()
            cfg = load_ai_config()
            if body.get("clearKey"):
                cfg["apiKey"] = ""
            elif (body.get("apiKey") or "").strip():
                cfg["apiKey"] = body["apiKey"].strip()
            if body.get("model"):
                cfg["model"] = str(body["model"]).strip()
            if body.get("baseUrl"):
                cfg["baseUrl"] = str(body["baseUrl"]).strip().rstrip("/")
            if body.get("interest") is not None:
                cfg["interest"] = str(body["interest"]).strip()
            if body.get("exclude") is not None:
                cfg["exclude"] = str(body["exclude"]).strip()
            save_ai_config(cfg)
            # 配置变了，热榜缓存作废，下次请求会用新标准重新筛
            _hotlists.update(at=0.0, data=None)
            return self._json({"ok": True, "ai": ai_public_settings()})
        if self.path.startswith("/api/douyin/connect"):
            ok = start_douyin_login()
            return self._json({"started": ok, "state": dsnapshot()["state"]})
        if self.path.startswith("/api/xhs/connect"):
            ok = start_login()
            return self._json({"started": ok, "state": snapshot()["state"]})
        if self.path.startswith("/api/bilibili/connect"):
            ok = start_bilibili_login()
            return self._json({"started": ok, "state": bsnapshot()["state"]})
        if self.path.startswith("/api/wechat/connect"):
            ok = start_wechat_login()
            return self._json({"started": ok, "state": wsnapshot()["state"]})
        if self.path.startswith("/api/douyin/disconnect"):
            return self._json({"started": start_disconnect("douyin")})
        if self.path.startswith("/api/xhs/disconnect"):
            return self._json({"started": start_disconnect("xhs")})
        if self.path.startswith("/api/bilibili/disconnect"):
            return self._json({"started": start_disconnect("bilibili")})
        if self.path.startswith("/api/wechat/disconnect"):
            return self._json({"started": start_disconnect("wechat")})
        return self._json({"error": "not found"}, 404)


def _restore_states() -> None:
    """服务重启后恢复连接状态。

    STATE/DSTATE 只存在内存里，重启就会变回 disconnected，
    但会话文件与浏览器 profile 里的 cookie 其实还在。
    这里按「会话文件存在 + 24 小时内成功采集过」判断为已连接：
    只凭会话文件会把早已过期的 cookie 也说成已连接（B 站踩过），
    而数据时间能证明这个登录态最近确实还能用。
    """
    for pid, setter in (("xhs", set_state), ("douyin", dset),
                        ("bilibili", bset), ("wechat", wset)):
        if not (SESS_DIR / f"{pid}.bin").exists():
            continue
        data_path = DATA_DIR / f"{pid}_creator.json"
        nickname, last = None, None
        try:
            d = json.loads(data_path.read_text())
            nickname = (d.get("account") or {}).get("nickname")
            ts = d.get("collectedAt") or ""
            last = ts[11:16] if len(ts) >= 16 else None
        except Exception:
            pass

        if _age_hours(data_path) <= 24:
            setter("connected", message="已恢复登录态",
                   nickname=nickname, lastRefresh=last)
        else:
            # 有历史会话，但最近一次采集已超过 24 小时：不能替平台打包票
            setter("disconnected", message="登录态待验证，请点击重新连接",
                   nickname=nickname, lastRefresh=last)


def main():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _restore_states()

    # 每天 12:00 的定时采集（守护线程，随服务一起退出）
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    # 启动时如果发现数据超过 24 小时没更新，立刻补一次
    threading.Thread(target=_catch_up_if_stale, daemon=True).start()

    wait = _seconds_until_next_run()
    print("观澜本地服务已启动： http://127.0.0.1:8765/", flush=True)
    print(f"下次采集：{wait / 3600:.1f} 小时后（每天 {DAILY_HOUR:02d}:{DAILY_MINUTE:02d}）", flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
