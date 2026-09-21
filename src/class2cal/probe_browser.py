"""浏览器抓包探测课表接口。

逐个猜门户接口路径效率太低（本校没买 personalized 模板，getPageInfo 也要参数）。
更直接的办法：用已登录会话打开浏览器，你手动点进课表页，工具把页面发出的
所有 XHR 请求连同响应一起记下来。课表接口就在里面，不用猜。

抓到的数据全部落到 var/probe/，之后可以离线分析字段结构。
"""

from __future__ import annotations

import json
import logging
import time

from . import config, sso

log = logging.getLogger(__name__)

# 看起来像课表数据的信号词，用于自动挑出候选请求
SCHEDULE_SIGNALS = ("课", "kcmc", "courseName", "jsmc", "classroom", "skrq", "jc", "teacher")
# 门户取卡片数据的接口特征
CARD_METHOD_MARKER = "execCardMethod"

POLL_INTERVAL_S = 1.0
DEFAULT_TIMEOUT_S = 300


class AutoProbeError(RuntimeError):
    """浏览器探测失败。"""


def _session_cookies_for_playwright(cfg: config.Config) -> list[dict]:
    """把已存的会话 cookie 转成 Playwright 格式。"""
    path = cfg.session_path
    if not path.exists():
        raise AutoProbeError("没有已保存的会话。先跑 `class2cal login --browser`。")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AutoProbeError(f"会话文件读不出来：{exc}") from exc

    out: list[dict] = []
    for c in raw:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        out.append(
            {
                "name": name,
                "value": value,
                "domain": str(c.get("domain") or "all.example.edu.cn").lstrip("."),
                "path": c.get("path", "/"),
            }
        )
    if not out:
        raise AutoProbeError("会话文件里没有可用 cookie。重新跑 `class2cal login --browser`。")
    return out


def probe_with_browser(
    cfg: config.Config,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    channel: str = "msedge",
) -> dict:
    """打开浏览器，捕获课表页发出的请求。

    返回 {"captured": 请求数, "candidates": [...], "dump": 存档路径}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise AutoProbeError("缺少 playwright，跑 `uv sync` 安装。") from exc

    cookies = _session_cookies_for_playwright(cfg)
    captured: list[dict] = []

    print("正在打开浏览器（已带上你的登录状态）……\n")
    print("请在窗口里点进课表页面 —— 找到能看到课程表的那个界面。")
    print("我会记下页面发出的所有请求。看到课表显示出来后，关掉浏览器窗口即可。\n")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=channel, headless=False)
        except Exception as exc:
            raise AutoProbeError(f"启动浏览器失败：{exc}") from exc

        context = browser.new_context(
            user_agent=config.MOBILE_UA,
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
        )
        context.add_cookies(cookies)

        def on_response(response):
            """记下所有可能带数据的响应。"""
            try:
                url = response.url
                if "example.edu.cn" not in url:
                    return
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return

                body_text = ""
                try:
                    body_text = response.text()
                except Exception:
                    pass

                captured.append(
                    {
                        "url": url,
                        "method": response.request.method,
                        "post_data": response.request.post_data or "",
                        "status": response.status,
                        "body": body_text[:20000],
                    }
                )
            except Exception as exc:  # 抓包失败不该打断用户操作
                log.debug("记录响应失败：%s", exc)

        context.on("response", on_response)

        page = context.new_page()
        try:
            page.goto(f"{config.PORTAL_BASE}/index.html", timeout=60_000)
        except Exception as exc:
            browser.close()
            raise AutoProbeError(f"打不开门户首页：{exc}") from exc

        _wait_until_closed(page, context, timeout_s)

        try:
            browser.close()
        except Exception:
            pass

    dump_dir = cfg.probe_dir
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / "network.json"
    dump_path.write_text(
        json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    candidates = _pick_candidates(captured)
    (dump_dir / "candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {"captured": len(captured), "candidates": candidates, "dump": dump_path}


def _browser_gone(page, context) -> bool:
    """判断浏览器是否已经没了。

    整个窗口被关掉时，Playwright 的连接会断开，此时 page.is_closed() 会抛异常
    而不是返回 True。只看 is_closed() 会导致循环一直空转到超时 —— 用户以为
    关掉窗口就结束了，实际还要白等几分钟，抓到的数据也没写盘。
    """
    try:
        if page.is_closed():
            return True
    except Exception:
        return True

    # 连接断了但 page 对象还在的情况：探一下 context 是否可用
    try:
        context.cookies()
    except Exception:
        return True

    return False


def _wait_until_closed(page, context, timeout_s: int) -> None:
    """等用户操作完毕关掉窗口，或超时。"""
    deadline = time.monotonic() + timeout_s
    next_note = time.monotonic() + 60

    while time.monotonic() < deadline:
        if _browser_gone(page, context):
            print("\n窗口已关闭，正在分析抓到的请求……")
            return
        now = time.monotonic()
        if now >= next_note:
            print(f"（还在记录……剩余约 {int(deadline - now)} 秒，看完课表后关掉窗口即可）")
            next_note = now + 60
        time.sleep(POLL_INTERVAL_S)

    print("\n到时间了，正在分析已抓到的请求……")


def _pick_candidates(captured: list[dict]) -> list[dict]:
    """从抓到的请求里挑出像课表的。

    优先 execCardMethod 且响应里带课程字样的 —— 那就是课表卡片。
    """
    scored: list[tuple[int, dict]] = []

    for item in captured:
        url, body = item["url"], item.get("body", "")
        score = 0

        if CARD_METHOD_MARKER in url:
            score += 10
        hits = [s for s in SCHEDULE_SIGNALS if s in body]
        score += len(hits) * 2
        if "课程" in body or "kcmc" in body:
            score += 5

        if score < 4:
            continue

        entry = {
            "url": url,
            "method": item["method"],
            "post_data": item.get("post_data", "")[:500],
            "signals": hits,
            "score": score,
            "body_preview": body[:600],
        }
        # execCardMethod/{cardWid}/{cardId} -> 抽出坐标
        if CARD_METHOD_MARKER in url:
            parts = url.split(CARD_METHOD_MARKER + "/", 1)[-1].split("?")[0].split("/")
            if len(parts) >= 2:
                entry["card_wid"], entry["card_id"] = parts[0], parts[1]
        scored.append((score, entry))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored]
