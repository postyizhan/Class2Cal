"""手动登录：在真实浏览器里登录一次，程序复用会话。

学校在 doLogin 上强制校验滑块验证码。滑块是专门用来阻止程序化登录的控制措施，
本项目不去破解它 —— 而是让人在浏览器里正常完成登录（滑块由你亲手拖），
然后把已登录的会话 cookie 导进来复用。

代价是会话过期后（通常几小时到几天）需要重做一次。作为交换，工具不必保管
你的密码，也不触碰学校的安全控制。

注意：门户的会话 cookie 基本都是 HttpOnly 的，JS 读不到，所以 document.cookie
拿不全。必须用 DevTools 的「Copy as cURL」。
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
from pathlib import Path

import requests

from . import config, sso

log = logging.getLogger(__name__)

# 门户会话依赖的 cookie。名字随金智版本略有差异，故按前缀宽松匹配。
SESSION_COOKIE_HINTS = ("JSESSIONID", "SESSION", "CASTGC", "iPlanetDirectoryPro", "_WEU")


class ManualLoginError(RuntimeError):
    """手动登录流程失败。"""


def open_login_page(cfg: config.Config) -> None:
    """用默认浏览器打开登录页。"""
    login_url = f"{cfg.sso_base}/esc-sso/login?service={cfg.portal_base}/login"
    try:
        subprocess.run(["open", login_url], check=True, timeout=15)
    except (subprocess.SubprocessError, OSError) as exc:
        raise ManualLoginError(
            f"打不开浏览器：{exc}\n手动访问这个地址登录：{login_url}"
        ) from exc


def parse_cookie_header(raw: str) -> dict[str, str]:
    """解析 Cookie 请求头，形如 `JSESSIONID=abc; _WEU=xyz`。"""
    cookies: dict[str, str] = {}
    for part in raw.strip().strip("'\"").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            cookies[name] = value
    return cookies


def parse_curl_command(text: str) -> dict[str, str]:
    """从 DevTools「Copy as cURL」的内容里抽出 cookie。

    这是推荐路径：HttpOnly 的会话 cookie 只有这里拿得到。
    """
    cookies: dict[str, str] = {}

    # -b / --cookie 参数
    for m in re.finditer(r"(?:-b|--cookie)\s+('[^']*'|\"[^\"]*\"|\S+)", text):
        cookies.update(parse_cookie_header(m.group(1)))

    # -H 'Cookie: ...' 形式
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = []
    for i, tok in enumerate(tokens):
        if tok in ("-H", "--header") and i + 1 < len(tokens):
            header = tokens[i + 1]
            if header.lower().startswith("cookie:"):
                cookies.update(parse_cookie_header(header.split(":", 1)[1]))

    return cookies


def build_session(cookies: dict[str, str], cfg: config.Config) -> requests.Session:
    """用给定 cookie 组装会话。同时挂到门户与 SSO 两个域。"""
    if not cookies:
        raise ManualLoginError("没解析到任何 cookie")

    session = sso.new_session()
    # 从 cfg 提取域名
    from urllib.parse import urlparse
    sso_domain = urlparse(cfg.sso_base).netloc
    portal_domain = urlparse(cfg.portal_base).netloc

    for name, value in cookies.items():
        for domain in (portal_domain, sso_domain):
            session.cookies.set(name, value, domain=domain, path="/")
    return session


def import_cookies(raw: str, cfg: config.Config) -> dict:
    """导入 cookie 并验证会话有效，成功则持久化。返回登录用户信息。"""
    looks_like_curl = "curl" in raw[:200].lower()
    cookies = parse_curl_command(raw) if looks_like_curl else parse_cookie_header(raw)

    if not cookies:
        raise ManualLoginError(
            "没解析到 cookie。支持两种粘贴内容：\n"
            "  1. DevTools 里「Copy as cURL」的完整命令（推荐）\n"
            "  2. Cookie 请求头，形如 JSESSIONID=xxx; _WEU=yyy"
        )

    if not any(h.lower() in k.lower() for k in cookies for h in SESSION_COOKIE_HINTS):
        log.warning(
            "没看到常见的会话 cookie（%s）。如果是用 document.cookie 复制的，"
            "HttpOnly 的会话 cookie 读不到，请改用「Copy as cURL」。",
            "/".join(SESSION_COOKIE_HINTS[:3]),
        )

    session = build_session(cookies, cfg)
    user = sso.verify_session(session, cfg)
    if not user:
        raise ManualLoginError(
            "cookie 导入了但会话无效。常见原因：\n"
            "  - 用 document.cookie 复制的，漏了 HttpOnly 的会话 cookie\n"
            "  - 复制的是 SSO 登录页的请求，不是登录成功后门户页面的\n"
            "  - 会话已经过期\n"
            f"重新登录，在 {cfg.portal_base} 的请求上「Copy as cURL」。"
        )

    sso.save_session(session, cfg.session_path)
    log.info("会话已保存到 %s", cfg.session_path)
    return user


def import_from_file(path: Path, cfg: config.Config) -> dict:
    """从文件导入（cURL 命令很长，存文件比粘贴更省事）。"""
    if not path.exists():
        raise ManualLoginError(f"文件不存在：{path}")
    return import_cookies(path.read_text(encoding="utf-8"), cfg)


def get_instructions(cfg: config.Config) -> str:
    """生成手动登录指引（包含学校特定的 URL）。"""
    login_url = f"{cfg.sso_base}/esc-sso/login?service={cfg.portal_base}/login"
    from urllib.parse import urlparse
    portal_domain = urlparse(cfg.portal_base).netloc

    return f"""\
学校登录时强制校验滑块验证码，需要你手动登录一次，之后程序复用这个会话。

步骤：
  1. 浏览器已打开登录页（没打开就手动访问）
     {login_url}
  2. 输入账号密码、拖动滑块，正常登录
  3. 等页面跳转到门户首页 {cfg.portal_base}
  4. 按 F12 打开开发者工具 → Network（网络）面板
  5. 刷新一下页面，在请求列表里点任意一条发往 {portal_domain} 的请求
  6. 右键 → Copy（复制）→ Copy as cURL
  7. 粘贴到下面，然后按 Ctrl-D（Windows 键盘按 Ctrl-Z）结束输入

为什么不用 document.cookie：门户的会话 cookie 是 HttpOnly 的，JS 读不到，
复制出来会缺关键字段。

小技巧：用 Edge 先开 iPhone 设备模拟（F12 → 左上角设备图标），拿到的会话
更贴近移动端，课表数据更完整。\
"""
