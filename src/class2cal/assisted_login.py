"""辅助登录：工具开浏览器，你登录，工具取会话。

比 browser_login 省事 —— 不用你在 DevTools 里翻请求、复制 cURL。流程是：
  1. 工具用 Playwright 启动一个 Edge 窗口（非无头，你能看见）
  2. 你在里面输账号密码、拖滑块，正常登录
  3. 工具轮询等到门户会话建立，自动读出 cookie 存好，关掉窗口

滑块始终由你亲手拖 —— 工具不识别、不模拟，只是等你完成。
密码也由你直接输进浏览器，工具不经手。
"""

from __future__ import annotations

import logging
import time

from . import config, sso

log = logging.getLogger(__name__)

LOGIN_URL = f"{config.SSO_BASE}/esc-sso/login?service={config.PORTAL_BASE}/login"

# 轮询间隔与上限：留足时间让人慢慢输密码、拖滑块
POLL_INTERVAL_S = 2.0
DEFAULT_TIMEOUT_S = 300


class AssistedLoginError(RuntimeError):
    """辅助登录失败。"""


def _cookies_to_session(raw_cookies: list[dict]):
    """把 Playwright 的 cookie 列表转成 requests 会话。"""
    session = sso.new_session()
    for c in raw_cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        session.cookies.set(
            name,
            value,
            domain=str(c.get("domain", "")).lstrip("."),
            path=c.get("path", "/"),
        )
    return session


def assisted_login(
    cfg: config.Config,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    mobile: bool = True,
    channel: str = "msedge",
) -> dict:
    """开浏览器让用户登录，成功后取回会话。返回登录用户信息。

    mobile=True 时用 iPhone UA + 手机视口，这样拿到的会话走移动端展示方案，
    课表数据才完整（桌面端会漏课、只给节次号）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise AssistedLoginError(
            "缺少 playwright。跑 `uv sync` 安装，或改用 `class2cal login --manual`。"
        ) from exc

    print("正在打开浏览器……")
    print("在弹出的窗口里输账号密码、拖滑块完成登录，成功后我会自动接手。\n")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=channel, headless=False)
        except Exception as exc:
            raise AssistedLoginError(
                f"启动浏览器失败（channel={channel}）：{exc}\n"
                "可以试 --channel chrome，或改用 `class2cal login --manual`。"
            ) from exc

        context_args: dict = {}
        if mobile:
            context_args.update(
                user_agent=config.MOBILE_UA,
                viewport={"width": 390, "height": 844},
                device_scale_factor=3,
                is_mobile=True,
                has_touch=True,
            )

        context = browser.new_context(**context_args)
        page = context.new_page()

        try:
            page.goto(LOGIN_URL, timeout=60_000)
        except Exception as exc:
            browser.close()
            raise AssistedLoginError(f"打不开登录页：{exc}") from exc

        try:
            session = _wait_for_login(page, context, timeout_s)
        finally:
            try:
                browser.close()
            except Exception:
                pass

    user = sso.verify_session(session)
    if not user:
        raise AssistedLoginError(
            "取到了 cookie 但会话无效。可能登录没真正完成，或会话被服务端立刻失效了。"
        )

    sso.save_session(session, cfg.session_path)
    log.info("会话已保存到 %s", cfg.session_path)
    return user


def _wait_for_login(page, context, timeout_s: int):
    """轮询等待门户会话建立。

    判定依据是拿 cookie 去问门户的 /getLoginUser —— 比看 URL 可靠，
    因为 SPA 跳转时机和会话建立时机不一定同步。
    """
    deadline = time.monotonic() + timeout_s
    next_note = time.monotonic() + 30

    while time.monotonic() < deadline:
        if page.is_closed():
            raise AssistedLoginError("浏览器窗口被关闭了，登录未完成。")

        try:
            cookies = context.cookies()
        except Exception:
            cookies = []

        if cookies:
            session = _cookies_to_session(cookies)
            if sso.verify_session(session):
                print("检测到登录成功，正在取回会话……")
                return session

        now = time.monotonic()
        if now >= next_note:
            print(f"等待登录完成……（还有约 {int(deadline - now)} 秒）")
            next_note = now + 30

        time.sleep(POLL_INTERVAL_S)

    raise AssistedLoginError(
        f"等了 {timeout_s} 秒还没检测到登录成功。\n"
        "如果你已经登录了但一直没被识别，改用 `class2cal login --manual`。"
    )
