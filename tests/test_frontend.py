# -*- coding: utf-8 -*-
"""前端逻辑与交互测试：直接调用页面里的真实函数，不复制实现。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import ROOT, api_map, day, dump, start_static  # noqa: E402


class FrontendBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.port = start_static()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()

    def open(self, api=None, view: str = "ideas"):
        api = api or api_map()
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append("console:" + m.text) if m.type == "error" else None)
        self.api = api
        self.api_calls = {}

        def handler(route):
            path = urlparse(route.request.url).path
            self.api_calls[path] = self.api_calls.get(path, 0) + 1
            if path in api:
                route.fulfill(status=200, content_type="application/json", body=dump(api[path]))
            else:
                route.fulfill(status=404, content_type="application/json", body='{"error":"no data"}')

        page.route("**/api/**", handler)
        page.goto(f"{self.base}/#/{view}", wait_until="networkidle")
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_function(
            "() => typeof CONTENTS !== 'undefined' && CONTENTS.some(c => c.title === 'XHS 今天')"
        )
        self.assertFalse(errors, f"页面报错：{errors}")
        return page

    def run_js(self, page, js):
        return page.evaluate(js)

    def seed_ideas(self, page, titles):
        page.evaluate(
            """(titles) => {
                IDEAS.items = titles.map((t, i) => ({
                    title: t, angle: '测试切口', format: '教程', platform: 'xhs',
                    audience: '新手', cost: '低', score: 80 - i, reason: '测试',
                    source: '热榜', basedOn: '测试热点',
                }));
                renderIdeaCards();
            }""",
            titles,
        )

    def ensure_stage(self, page):
        if not page.evaluate("document.getElementById('ideasStage').hasAttribute('data-open')"):
            page.click("#ideaStart")
        # 等 pill 长成面板的过渡结束，避免点击被动画中的容器拦截
        page.wait_for_timeout(850)


class TestFrontendLogic(FrontendBase):
    def test_date_window(self):
        page = self.open()
        self.assertTrue(self.run_js(page, f"withinDays('{day(0)}')"))
        self.assertTrue(self.run_js(page, f"withinDays('{day(-29)}')"))
        self.assertTrue(self.run_js(page, f"withinDays('{day(-30)}')"))
        self.assertFalse(self.run_js(page, f"withinDays('{day(-31)}')"))
        self.assertFalse(self.run_js(page, f"withinDays('{day(1)}')"))
        self.assertFalse(self.run_js(page, "withinDays(null)"))
        self.assertFalse(self.run_js(page, "withinDays('不是日期')"))

    def test_parse_and_format(self):
        page = self.open()
        self.assertEqual(self.run_js(page, "parseCn('1.2万')"), 12000)
        self.assertEqual(self.run_js(page, "parseCn('3亿')"), 300000000)
        self.assertEqual(self.run_js(page, "parseCn('1,234')"), 1234)
        self.assertEqual(self.run_js(page, "parseCn(42)"), 42)
        self.assertIsNone(self.run_js(page, "parseCn('—')"))
        self.assertIsNone(self.run_js(page, "parseCn(null)"))
        self.assertEqual(self.run_js(page, "ideaNorm('标题，带标点！')"), "标题带标点")

    def test_content_window(self):
        page = self.open()
        titles = self.run_js(page, "contentInWindow().map(c => c.title)")
        self.assertIn("XHS 今天", titles)
        self.assertIn("XHS 10 天前", titles)
        self.assertNotIn("XHS 40 天前", titles)
        self.assertIn("公众号 20 天前", titles)
        self.assertIn("公众号 40 天前", self.run_js(page, "CONTENTS.map(c => c.title)"))
        self.assertEqual(len(titles), 6)

    def test_rqic_scores(self):
        page = self.open()
        data = page.evaluate(
            """() => {
                computeQCI();
                return CONTENTS.map(c => ({ title: c.title, total: c.total }));
            }"""
        )
        by_title = {x["title"]: x["total"] for x in data}
        self.assertIsNone(by_title["XHS 40 天前"])
        self.assertIsNone(by_title["公众号 40 天前"])
        for title in ("XHS 今天", "XHS 10 天前", "抖音 今天", "B站 今天"):
            self.assertIsNotNone(by_title[title], title)
            self.assertGreaterEqual(by_title[title], 0, title)
            self.assertLessEqual(by_title[title], 100, title)

    def test_schedule_format(self):
        page = self.open()
        today = page.evaluate(
            "() => scheduleInfo({scheduleDate: '%s', scheduleTime: '20:00'})" % day(0)
        )
        tomorrow = page.evaluate(
            "() => scheduleInfo({scheduleDate: '%s', scheduleTime: '19:30'})" % day(1)
        )
        past = page.evaluate(
            "() => scheduleInfo({scheduleDate: '%s', scheduleTime: '10:00'})" % day(-1)
        )
        self.assertTrue(today["text"].startswith("今天"))
        self.assertEqual(today["state"], "today")
        self.assertTrue(tomorrow["text"].startswith("明天"))
        self.assertEqual(tomorrow["state"], "next")
        self.assertTrue(past["text"].startswith("已过"))
        self.assertEqual(past["state"], "past")
        self.assertEqual(self.run_js(page, "scheduleInfo({})"), None)

    def test_plan_limit(self):
        page = self.open()
        self.seed_ideas(page, [f"排期测试选题 {i + 1}" for i in range(11)])
        page.evaluate("IDEAS.items.slice(0, 10).forEach((_, i) => addIdeaToPlan(i))")
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 10)
        btn = page.locator(".idea-row").nth(10).locator(".mini.primary")
        self.assertEqual(btn.inner_text().strip(), "已满")
        self.assertTrue(btn.is_disabled())

    def test_plan_dedupe_series(self):
        page = self.open()
        self.seed_ideas(page, ["系列选题 1", "系列选题 2", "系列选题 1"])
        page.evaluate("addIdeaToPlan(0); addIdeaToPlan(1); addIdeaToPlan(2)")
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 2)
        self.assertEqual(self.run_js(page, "IDEAS.plan.map(p => p.title)"),
                         ["系列选题 1", "系列选题 2"])

    def test_archive_reopen_lifecycle(self):
        page = self.open()
        page.evaluate(
            """() => {
                IDEAS.history = [{ title: '闭环选题', angle: '旧切口', format: '教程',
                    platform: 'xhs', done: true, archivedAt: '%s' }];
                IDEAS.historyLoaded = true; saveIdeaHistory();
                renderIdeaPlan(); renderIdeaHistoryLink();
            }""" % day(-1)
        )
        page.evaluate("reAddHistoryIdea(0)")
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 1)
        self.assertEqual(self.run_js(page, "visibleHistory().length"), 0)

        page.evaluate("togglePlanDone({ stopPropagation(){} }, 0)")
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 0)
        self.assertEqual(self.run_js(page, "IDEAS.history.length"), 1)
        self.assertFalse(self.run_js(page, "!!IDEAS.history[0].reopened"))

        page.evaluate("reAddHistoryIdea(0)")
        page.evaluate("removeIdeaFromPlan(0)")
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 0)
        self.assertEqual(self.run_js(page, "visibleHistory().length"), 1)

        page.evaluate("reAddHistoryIdea(0)")
        page.evaluate("""() => {
            IDEAS.plan[0].title = '闭环选题（改名）';
            IDEAS.plan[0].reopenedFrom = ideaNorm('闭环选题（改名）');
            IDEAS.history[0].title = '闭环选题（改名）';
            saveIdeaHistory();
        }""")
        page.evaluate("togglePlanDone({ stopPropagation(){} }, 0)")
        self.assertEqual(self.run_js(page, "IDEAS.history.length"), 1)
        self.assertEqual(self.run_js(page, "IDEAS.history[0].title"), "闭环选题（改名）")

    def test_history_load_dedupes_old_data(self):
        page = self.open()
        page.evaluate(
            """() => {
                localStorage.setItem('guanlan_ideas_history_v1', JSON.stringify([
                    { title: '重复选题', archivedAt: '%s', matchedKey: null },
                    { title: '重复选题', archivedAt: '%s', matchedKey: 'xhs|x|y' },
                ]));
            }""" % (day(0), day(-5))
        )
        page.reload(wait_until="networkidle")
        page.wait_for_function("() => IDEAS.history.length > 0")
        self.assertEqual(self.run_js(page, "IDEAS.history.length"), 1)

    def test_review_action_state_by_title(self):
        page = self.open(view="review")
        page.wait_for_selector("#reviewActions .action-item")
        first_title = page.locator("#reviewActions .action-title").first.inner_text()
        page.locator("#reviewActions .action-item").first.click()
        self.assertTrue(page.locator("#reviewActions .action-item").first.evaluate(
            "e => e.classList.contains('done')"))
        page.evaluate("renderReviewPage()")
        page.wait_for_selector("#reviewActions .action-item")
        checked = page.evaluate(
            """() => [...document.querySelectorAll('#reviewActions .action-item')]
                .map(e => [e.querySelector('.action-title').textContent,
                           e.classList.contains('done')])"""
        )
        by_title = {t: d for t, d in checked}
        self.assertTrue(by_title[first_title])

    def test_expand_caches_result(self):
        page = self.open()
        self.seed_ideas(page, ["展开缓存选题"])
        page.evaluate("addIdeaToPlan(0)")
        page.evaluate("showIdeaDetail(IDEAS.items[0])")
        page.wait_for_function("() => IDEAS.items[0] && IDEAS.items[0].expanded")
        page.evaluate("showIdeaDetail(IDEAS.items[0])")
        page.wait_for_timeout(300)
        self.assertEqual(self.api_calls.get("/api/ideas/expand", 0), 1)

    def test_content_filters_and_pagination(self):
        page = self.open(view="content")
        self.assertEqual(
            page.evaluate("""() => {
                CONTENT_VIEW.platform = 'xhs'; CONTENT_VIEW.keyword = ''; CONTENT_VIEW.page = 1;
                renderContentPage();
                return document.querySelectorAll('#cpBody tr').length;
            }"""), 3)
        self.assertEqual(
            page.evaluate("""() => {
                CONTENT_VIEW.keyword = 'XHS 今天';
                renderContentPage();
                return document.querySelectorAll('#cpBody tr').length;
            }"""), 1)
        self.assertEqual(
            page.evaluate("""() => {
                CONTENT_VIEW.platform = 'all'; CONTENT_VIEW.keyword = '';
                renderContentPage();
                return document.querySelectorAll('#cpBody tr').length;
            }"""), 8)

    def test_home_scope_latest_and_health(self):
        page = self.open(view="")
        page.wait_for_function("() => document.getElementById('srcScope').innerText.includes('小红书')")
        scope = page.locator("#srcScope").inner_text()
        self.assertIn("小红书 2 篇", scope)
        self.assertIn("抖音 2 条", scope)
        self.assertIn("B 站 1 条", scope)
        self.assertIn("公众号 1 篇", scope)
        self.assertEqual(page.locator("#latestSub").inner_text(), "4 个平台有更新")
        statuses = page.evaluate(
            "() => [...document.querySelectorAll('#latestStatus .status-row')]"
            ".map(r => r.innerText.replace(/\\n/g, '|'))")
        self.assertEqual(len(statuses), 4)
        self.assertTrue(all("已抓取" in s for s in statuses), statuses)
        self.assertEqual(page.locator("#healthCount").inner_text(), "4/4 已接入")

    def test_fans_gains_and_table(self):
        page = self.open(view="fans")
        page.wait_for_function("() => document.getElementById('fanGainConclusion').innerText.includes('近 30 天')")
        self.assertEqual(
            page.locator("#fanGainConclusion").inner_text().replace("\n", " "),
            "近 30 天共 +10",
        )
        self.assertIn("近 30 天", page.locator("#fanGainSub").inner_text())
        self.assertEqual(page.locator("#fanGainList .fg-row").count(), 4)
        self.assertEqual(page.locator("#fanTableBody tr").count(), 4)
        bili_row = page.locator("#fanTableBody tr", has_text="B 站").inner_text()
        self.assertIn("每日快照", bili_row)
        self.assertIn("4 个平台已接入", page.locator("#fansPageSub").inner_text())

    def test_hotlist_error_is_visible(self):
        api = api_map()
        api["/api/hotlists"] = {
            "fetchedAt": "12:00",
            "xhs": {"items": []},
            "douyin": {"items": []},
            "filter": {"mode": "rule", "items": []},
            "errors": {"xhs": "登录态已失效，请重新扫码"},
        }
        page = self.open(api=api, view="content")
        page.wait_for_function("() => HOTLIST.data && HOTLIST.data.errors")
        page.wait_for_timeout(200)
        self.assertIn("登录态已失效", page.locator("#hotlistNote").inner_text())

    def test_manual_match_history(self):
        page = self.open()
        page.evaluate(
            """() => {
                IDEAS.history = [{ title: '手动关联选题', angle: 'x', format: '教程',
                    platform: 'xhs', done: true, archivedAt: '%s' }];
                IDEAS.historyLoaded = true; saveIdeaHistory();
                renderIdeaHistoryLink(); openIdeaHistory();
            }""" % day(-1)
        )
        page.wait_for_selector(".history-item")
        page.evaluate("[...document.querySelectorAll('.history-ops button')]"
                      ".filter(b => b.textContent.includes('关联'))[0].click()")
        page.wait_for_selector("#matchList .match-row")
        page.locator("#matchList .match-row").first.click()
        page.wait_for_timeout(300)
        self.assertTrue(self.run_js(page, "!!IDEAS.history[0].matchedKey"))
        self.assertIn("实际", page.locator("#ideaDetail").inner_text())

    def test_plan_drag_reorders(self):
        page = self.open()
        order = page.evaluate(
            """() => {
                IDEAS.plan = ['A', 'B', 'C'].map(t => ({
                    title: t, angle: 'a', format: '教程', platform: 'xhs',
                    audience: '新手', cost: '低', score: 70, source: '经历',
                }));
                renderIdeaPlan();
                planDragStart({ currentTarget: { classList: { add(){} } },
                                dataTransfer: { setData(){}, effectAllowed: '' } }, 0);
                planDrop({ preventDefault(){}, stopPropagation(){},
                           currentTarget: { classList: { remove(){} } } }, 2);
                return IDEAS.plan.map(p => p.title);
            }"""
        )
        self.assertEqual(order, ["B", "C", "A"])

    def test_dislike_feedback_removes_card(self):
        page = self.open()
        self.seed_ideas(page, ["会被不喜欢的选题", "保留的选题"])
        self.ensure_stage(page)
        page.locator(".idea-row .idea-dislike").first.click()
        page.wait_for_selector("#dislikeMenu[data-open]")
        page.locator("#dislikeMenu .dislike-reason").first.click()
        page.wait_for_timeout(300)
        self.assertEqual(self.run_js(page, "IDEAS.items.length"), 1)
        self.assertEqual(self.run_js(page, "IDEAS.feedback.dislikes[0].reason"), "方向太偏")
        self.assertEqual(self.run_js(page, "IDEAS.feedback.dislikes[0].title"), "会被不喜欢的选题")

    def test_editor_saves_and_remembers_style(self):
        page = self.open()
        self.seed_ideas(page, ["待编辑选题"])
        self.ensure_stage(page)
        page.evaluate("openIdeaEditor(0)")
        page.wait_for_selector("#editTitle")
        page.fill("#editTitle", "编辑后的选题名")
        page.fill("#editAngle", "编辑后的切口")
        page.evaluate("[...document.querySelectorAll('.idea-detail-foot button')]"
                      ".filter(b => b.textContent === '保存')[0].click()")
        page.wait_for_timeout(300)
        self.assertEqual(self.run_js(page, "IDEAS.items[0].title"), "编辑后的选题名")
        self.assertEqual(self.run_js(page, "IDEAS.style.samples[0].title"), "编辑后的选题名")

    def test_generate_payload_includes_plan_and_results(self):
        page = self.open()
        page.wait_for_function("() => SETTINGS && SETTINGS.ai && SETTINGS.ai.configured")
        page.evaluate(
            """() => {
                IDEAS.history = [{
                    title: '已验证选题', angle: '旧切口', format: '教程', platform: 'xhs',
                    done: true, archivedAt: '%s',
                    matchedKey: contentKey(CONTENTS.find(c => c.title === 'XHS 今天')),
                }];
                IDEAS.historyLoaded = true;
                IDEAS.plan = Array.from({ length: 10 }, (_, i) => ({
                    title: `计划 ${i + 1}`, angle: 'a', format: '教程', platform: 'xhs',
                    audience: '新手', cost: '低', score: 70, source: '热榜',
                }));
                saveIdeaHistory(); saveIdeaPlan();
                renderIdeaPlan(); renderIdeaCards();
            }""" % day(-1)
        )
        self.ensure_stage(page)
        page.evaluate(
            """() => {
                const real = window.fetch;
                window.__body = null;
                window.fetch = async (url, opt) => {
                    if (String(url).includes('/api/ideas/generate')) {
                        window.__body = JSON.parse(opt.body);
                        return new Response(JSON.stringify({ ok: true, items: [] }),
                            { status: 200, headers: { 'Content-Type': 'application/json' } });
                    }
                    return real(url, opt);
                };
            }"""
        )
        page.click("#ideaGenerate")
        page.wait_for_function("() => window.__body")
        body = page.evaluate("window.__body")
        self.assertEqual(len(body["feedback"]["plan"]), 10)
        self.assertEqual(body["feedback"]["plan"][-1]["title"], "计划 10")
        self.assertEqual(body["feedback"]["published"][0]["title"], "已验证选题")
        self.assertEqual(body["feedback"]["published"][0]["verdict"], "good")
        self.assertIn("已验证选题", body["feedback"]["done"])


class TestFrontendUI(FrontendBase):
    def test_schedule_save_persist_copy(self):
        page = self.open()
        self.seed_ideas(page, ["排期测试选题"])
        page.evaluate("addIdeaToPlan(0)")
        page.wait_for_selector(".plan-item .plan-sched")
        self.assertEqual(page.locator(".plan-item .plan-sched").inner_text().strip(), "＋ 排期")

        page.locator(".plan-item .plan-sched").click()
        page.wait_for_selector("#schedDate")
        page.evaluate("[...document.querySelectorAll('.sched-presets button')]"
                      ".filter(b => b.textContent.includes('明天'))[0].click()")
        self.assertIn("明天", page.locator("#schedHint").inner_text())
        page.locator("#ideaDetail .btn-primary").click()
        page.wait_for_timeout(300)

        self.assertTrue(self.run_js(page, "!!IDEAS.plan[0].scheduleDate"))
        self.assertIn("明天", page.locator(".plan-item .plan-sched").inner_text())
        self.assertEqual(page.locator("#ideaPlanSched").inner_text().strip(), "已排期 1")

        page.reload(wait_until="networkidle")
        page.wait_for_function("() => IDEAS.plan.length === 1")
        self.assertTrue(self.run_js(page, "!!IDEAS.plan[0].scheduleDate"))

        page.evaluate("window.__copied = ''; copyText = (e, t) => { window.__copied = t; };")
        page.evaluate("copyIdeaPlan()")
        self.assertIn("排期：", page.evaluate("window.__copied"))

        page.locator(".plan-item .plan-sched").click()
        page.wait_for_selector("#schedDate")
        page.evaluate("[...document.querySelectorAll('#ideaDetail .btn-ghost')]"
                      ".filter(b => b.textContent.includes('清除'))[0].click()")
        page.wait_for_timeout(300)
        self.assertFalse(self.run_js(page, "!!IDEAS.plan[0].scheduleDate"))
        self.assertEqual(page.locator("#ideaPlanSched").inner_text().strip(), "已排期 0")

    def test_clear_plan_dialog(self):
        page = self.open()
        self.seed_ideas(page, ["弹窗测试选题"])
        page.evaluate("addIdeaToPlan(0)")
        self.ensure_stage(page)
        page.evaluate("() => { window.confirm = () => { throw new Error('native confirm'); }; }")

        page.locator(".idea-clear-plan").click()
        page.wait_for_selector("#dlgMask[data-open]")
        self.assertEqual(page.locator("#dlgTitle").inner_text(), "清空本周计划？")
        page.locator("#dlgCancel").click()
        page.wait_for_timeout(200)
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 1)

        page.locator(".idea-clear-plan").click()
        page.wait_for_selector("#dlgMask[data-open]")
        page.locator("#dlgOk").click()
        page.wait_for_timeout(300)
        self.assertEqual(self.run_js(page, "IDEAS.plan.length"), 0)

    def test_connection_badge_and_settings(self):
        page = self.open(api=api_map(bili_connected=False))
        page.wait_for_function("() => CONN_STATE.bilibili && CONN_STATE.bilibili.state")
        self.assertEqual(page.locator("#healthCount").inner_text(), "3/4 已接入")
        badge = page.locator(".p-card", has_text="B 站").locator(".p-badge")
        self.assertEqual(badge.inner_text(), "登录失效")
        self.assertEqual(
            page.locator(".p-card", has_text="B 站").locator(".pc-label").inner_text(),
            "重新连接",
        )
        page.goto(f"{self.base}/#/settings", wait_until="networkidle")
        page.wait_for_timeout(500)
        row = page.locator(".acct-row", has_text="B 站")
        self.assertIn("登录失效", row.inner_text())

    def test_page_windows_are_consistent(self):
        page = self.open(view="content")
        sub = page.locator("#contentPageSub").inner_text()
        self.assertIn("共 8 条内容", sub)
        self.assertIn("近 30 天 6 条参与评分", sub)
        page.goto(f"{self.base}/#/review", wait_until="networkidle")
        page.wait_for_selector("#reviewPageSub")
        self.assertIn("近 7 天", page.locator("#reviewPageSub").inner_text())
        page.goto(f"{self.base}/#/fans", wait_until="networkidle")
        page.wait_for_timeout(500)
        self.assertIn("4 个平台已接入", page.locator("#fansPageSub").inner_text())

    def test_generate_error_is_visible(self):
        api = api_map()
        api["/api/ideas/generate"] = {"ok": False, "error": "测试失败：请重试"}
        page = self.open(api=api)
        page.wait_for_function("() => SETTINGS && SETTINGS.ai && SETTINGS.ai.configured")
        self.ensure_stage(page)
        page.click("#ideaGenerate")
        page.wait_for_function(
            "() => document.getElementById('ideaList').innerText.includes('测试失败')")
        self.assertIn("测试失败", page.locator("#ideaList").inner_text())

    def test_toast_stack_and_dialog(self):
        page = self.open()
        self.ensure_stage(page)
        page.evaluate("showToast('一', 'a', {kind:'success'});"
                      "showToast('二', 'b', {kind:'warning'});"
                      "showToast('三', 'c', {kind:'info'});")
        page.wait_for_timeout(300)
        self.assertEqual(page.locator(".tst-toast").count(), 3)
        self.assertEqual(page.evaluate("document.getElementById('toastStack').style.height"), "92px")
        page.hover("#toastStack")
        page.wait_for_timeout(400)
        self.assertEqual(page.evaluate("document.getElementById('toastStack').style.height"), "212px")

        page.evaluate("void askConfirm({ title: '测试确认', body: '正文' })")
        page.wait_for_selector("#dlgMask[data-open]")
        self.assertEqual(page.locator("#dlgTitle").inner_text(), "测试确认")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        self.assertFalse(page.evaluate("document.getElementById('dlgMask').hasAttribute('data-open')"))
        # Esc 只关弹窗，不收起选题画布
        self.assertTrue(page.evaluate("document.getElementById('ideasStage').hasAttribute('data-open')"))

    def test_toast_auto_dismiss(self):
        page = self.open()
        self.ensure_stage(page)
        page.evaluate("showToast('短暂提示', '', {duration: 300})")
        page.wait_for_timeout(900)
        self.assertEqual(page.locator(".tst-toast").count(), 0)

    def test_hotlist_tabs(self):
        page = self.open(view="content")
        page.wait_for_function("() => HOTLIST.data")
        page.locator('#hotlistTabs button[data-tab="xhs"]').click()
        page.wait_for_timeout(200)
        self.assertIn("小红书", page.locator("#hotlistConcl").inner_text())
        self.assertIn("小红书热点", page.locator("#hotlistList").inner_text())
        page.locator('#hotlistTabs button[data-tab="douyin"]').click()
        page.wait_for_timeout(200)
        self.assertIn("抖音", page.locator("#hotlistConcl").inner_text())
        self.assertIn("抖音热点", page.locator("#hotlistList").inner_text())

    def test_content_sort_by_views(self):
        page = self.open(view="content")
        first = page.evaluate(
            """() => {
                CONTENT_VIEW.platform = 'all'; CONTENT_VIEW.keyword = '';
                CONTENT_VIEW.sort = 'views'; CONTENT_VIEW.page = 1;
                renderContentPage();
                return document.querySelector('#cpBody .ct-name').textContent;
            }"""
        )
        self.assertIn("抖音 今天", first)

    def test_fan_range_toggle(self):
        page = self.open(view="fans")
        page.wait_for_function("() => typeof FAN_VIEW !== 'undefined'")
        page.locator('#fanRangeSwitch button[data-range="7"]').click()
        page.wait_for_timeout(200)
        self.assertEqual(self.run_js(page, "FAN_VIEW.range"), 7)
        self.assertIn("on", page.locator('#fanRangeSwitch button[data-range="7"]').get_attribute("class"))
        page.locator('#fanRangeSwitch button[data-range="30"]').click()
        self.assertEqual(self.run_js(page, "FAN_VIEW.range"), 30)

    def test_responsive_pages_no_overflow(self):
        page = self.open()
        self.ensure_stage(page)
        page.set_viewport_size({"width": 390, "height": 900})
        for view in ("ideas", "content", "fans", "review", "settings", ""):
            page.goto(f"{self.base}/#/{view}", wait_until="networkidle")
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth")
            self.assertEqual(overflow, 0, f"{view} 出现横向溢出")

    def test_platform_row_breakpoints(self):
        page = self.open(view="")
        page.wait_for_function("() => document.querySelectorAll('.p-card').length === 4")

        def columns():
            return page.evaluate(
                "getComputedStyle(document.querySelector('.platform-row'))"
                ".gridTemplateColumns.split(' ').length")

        # data-full=1 的 4 列规则必须被 1180 / 760 两个断点正确覆盖
        for width, expected in ((1440, 4), (1181, 4), (1180, 2),
                                (761, 2), (760, 1), (390, 1)):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(160)
            self.assertEqual(columns(), expected, f"{width}px 下平台列数不对")

    def test_mobile_platform_cards_not_clipped(self):
        page = self.open(view="")
        page.set_viewport_size({"width": 390, "height": 900})
        page.wait_for_timeout(300)
        cards = page.evaluate(
            """() => [...document.querySelectorAll('.p-card')].map(card => {
                const cr = card.getBoundingClientRect();
                const sp = card.querySelector('.p-spark').getBoundingClientRect();
                return { width: cr.width, cardRight: cr.right, sparkRight: sp.right };
            })"""
        )
        self.assertEqual(len(cards), 4)
        for card in cards:
            self.assertGreater(card["width"], 300, "平台卡被压窄，迷你趋势图会溢出")
            self.assertLessEqual(card["sparkRight"], card["cardRight"] + 1,
                                 "迷你趋势图超出卡片右边界")

    def test_settings_mobile_stacks_rows(self):
        page = self.open(view="settings")
        page.set_viewport_size({"width": 390, "height": 900})
        page.wait_for_timeout(300)
        data = page.evaluate(
            """() => {
                const row = document.querySelector('.set-row');
                const val = row.querySelector('.set-val');
                const input = document.getElementById('aiKey');
                const ir = input.getBoundingClientRect();
                return {
                    direction: getComputedStyle(row).flexDirection,
                    valWidth: val.getBoundingClientRect().width,
                    inputWidth: ir.width,
                    inputRight: ir.right,
                    pathWrap: getComputedStyle(document.getElementById('setDataDir')).wordBreak,
                    overflow: document.documentElement.scrollWidth
                        - document.documentElement.clientWidth,
                };
            }"""
        )
        self.assertEqual(data["direction"], "column")
        self.assertLessEqual(data["inputRight"], 390)
        self.assertAlmostEqual(data["inputWidth"], data["valWidth"], delta=2)
        self.assertEqual(data["pathWrap"], "break-all")
        self.assertEqual(data["overflow"], 0)

        # 761px 起恢复横向键值行
        page.set_viewport_size({"width": 761, "height": 900})
        page.wait_for_timeout(200)
        self.assertEqual(
            page.evaluate("getComputedStyle(document.querySelector('.set-row')).flexDirection"),
            "row")

    def test_no_native_dialogs_in_source(self):
        text = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("window.confirm(", text)
        self.assertNotIn("window.alert(", text)
        self.assertNotIn("window.prompt(", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
