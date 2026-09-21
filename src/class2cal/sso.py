"""统一身份认证登录（金智 esc-sso v3）。

流程：
  1. GET  /esc-sso/api/v3/auth/queryAllValid  取 RSA 公钥与可用登录方式
  2. POST /esc-sso/api/v3/auth/doLogin        密码经 RSA PKCS#1 v1.5 加密后提交
  3. GET  /esc-sso/login?service=...          走 CAS 跳转换门户会话 cookie

密码加密方式与前端 JSEncrypt 一致：裸 base64 DER 公钥包上 PEM 头，
PKCS#1 v1.5 填充，输出 base64。
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config

log = logging.getLogger(__name__)

QUERY_ALL_VALID = "/esc-sso/api/v3/auth/queryAllValid"
DO_LOGIN = "/esc-sso/api/v3/auth/doLogin"
CAS_LOGIN = "/esc-sso/login"
GET_LOGIN_USER = "/getLoginUser"

AUTH_TYPE_LOCAL = "webLocalAuth"


class SsoError(RuntimeError):
    """登录失败的基类。"""


class BadCredentials(SsoError):
    """账号或密码错误。"""


class CaptchaRequired(SsoError):
    """学校启用了验证码，账密静默登录走不通了。"""


class SsoChanged(SsoError):
    """SSO 接口结构与预期不符，大概率是学校改版。"""


def _wrap_pem(der_b64: str) -> bytes:
    """把裸 base64 DER 公钥包成 PEM。"""
    body = "\n".join(der_b64[i : i + 64] for i in range(0, len(der_b64), 64))
    return f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----\n".encode()


def encrypt_password(plain: str, public_key_b64: str) -> str:
    """复刻前端 JSEncrypt：RSA PKCS#1 v1.5，输出 base64。"""
    key = serialization.load_pem_public_key(_wrap_pem(public_key_b64))
    cipher = key.encrypt(plain.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(cipher).decode("ascii")


def new_session() -> requests.Session:
    """建会话。统一挂移动端 UA —— 课表只在移动端展示方案里完整。"""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": config.MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return s


def save_session(session: requests.Session, path: Path) -> None:
    """持久化 cookie。含会话凭据，权限收到 600。"""
    cookies = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
        }
        for c in session.cookies
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def load_session(path: Path) -> requests.Session | None:
    if not path.exists():
        return None
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("会话文件读不出来，按未登录处理")
        return None

    s = new_session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return s


def verify_session(session: requests.Session, cfg: config.Config) -> dict | None:
    """校验会话是否还活着。返回登录用户信息，未登录返回 None。"""
    try:
        resp = session.get(f"{cfg.portal_base}{GET_LOGIN_USER}", timeout=15)
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    # 未登录时门户返回 {"errcode":"0","data":null}
    return data.get("data") or None


def _fetch_auth_params(session: requests.Session, cfg: config.Config) -> dict:
    """取 RSA 公钥与登录方式配置。此接口无需登录。"""
    resp = session.get(f"{cfg.sso_base}{QUERY_ALL_VALID}", timeout=15)
    try:
        data = resp.json()["data"]
    except (ValueError, KeyError) as exc:
        raise SsoChanged(f"queryAllValid 返回结构异常：{exc}") from exc

    param = data.get("param") or {}
    if not param.get("publicKey") or not param.get("publicKeyId"):
        raise SsoChanged("queryAllValid 里没拿到 publicKey / publicKeyId")

    local = (data.get("login") or {}).get(AUTH_TYPE_LOCAL) or {}
    if str(local.get("status")) != "1":
        raise SsoChanged("学校关闭了账号密码登录，需改用扫码等方式")
    # displayVcode 只控制界面是否显示旧式图形验证码，管不到滑块。
    # 实测本校 displayVcode=0 但 doLogin 仍强制校验滑块，所以这里不能据此
    # 判断「无需验证码」—— 真正的滑块要求只能由 doLogin 的返回告知。
    if local.get("displayVcode") not in (0, "0", None):
        raise CaptchaRequired(
            "学校启用了图形验证码。改用 `class2cal login --manual` 手动登录一次、复用会话。"
        )

    return {
        "public_key": param["publicKey"],
        "public_key_id": str(param["publicKeyId"]),
    }


def login(cfg: config.Config) -> requests.Session:
    """完整登录，返回带门户会话的 Session。"""
    username, password = config.require_sso_credentials(cfg)
    session = new_session()

    auth = _fetch_auth_params(session, cfg)
    portal_callback = f"{cfg.portal_base}/login"
    # publicKeyId 必须放在 dataField 内部。前端的加密函数是
    #   gt(明文, 公钥信息, dataField) { dataField.publicKeyId = 公钥信息.publicKeyId; ... }
    # 放到顶层服务端取不到 keyId，解密失败后统一报「用户名或密码错误」，极易误判成密码错。
    payload = {
        "authType": AUTH_TYPE_LOCAL,
        "dataField": {
            "username": username,
            "password": encrypt_password(password, auth["public_key"]),
            "vcode": "",
            "publicKeyId": auth["public_key_id"],
        },
        "redirectUri": portal_callback,
    }

    resp = session.post(
        f"{cfg.sso_base}{DO_LOGIN}",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    try:
        body = resp.json()
    except ValueError as exc:
        raise SsoChanged(f"doLogin 没返回 JSON（HTTP {resp.status_code}）") from exc

    _raise_for_login_error(body)

    # doLogin 成功后走 CAS 跳转，把 ticket 换成门户会话 cookie。
    session.get(
        f"{cfg.sso_base}{CAS_LOGIN}",
        params={"service": portal_callback},
        allow_redirects=True,
        timeout=20,
    )

    user = verify_session(session, cfg)
    if not user:
        raise SsoError("登录接口报成功，但门户会话没建立起来。学校可能改了回调流程。")

    save_session(session, cfg.session_path)
    return session


# 这些值出现在 msg 里表示「没有错误」，不能当成错误消息往外抛，
# 否则会打印出「登录失败：success」这种自相矛盾的话。
_SUCCESS_WORDS = ("success", "成功", "ok", "0")


def _raise_for_login_error(body: dict) -> None:
    """把 doLogin 的失败分类，别笼统报错。"""
    err = body.get("err") or body.get("error")
    data = body.get("data") or {}
    # 成功时通常带 redirectUri / ticket
    if not err and (data.get("redirectUri") or data.get("ticket") or data.get("url")):
        return

    msg = str(
        (isinstance(err, dict) and (err.get("message") or err.get("msg")))
        or body.get("message")
        or body.get("msg")
        or err
        or ""
    ).strip()
    lowered = msg.lower()

    if any(k in msg for k in ("密码", "用户名", "账号")) or "credential" in lowered:
        raise BadCredentials(f"账号或密码不对：{msg}")
    if any(k in msg for k in ("验证码", "滑块")) or "captcha" in lowered:
        raise CaptchaRequired(f"需要验证码：{msg}")
    if any(k in msg for k in ("锁定", "冻结", "停用")):
        raise SsoError(f"账号被锁定或停用：{msg}")
    if msg and lowered not in _SUCCESS_WORDS:
        raise SsoError(f"登录失败：{msg}")
    # 无错误信息（或只是成功标志）时交给上层的会话校验兜底。


def ensure_session(cfg: config.Config) -> requests.Session:
    """优先复用已存会话，失效则尝试重登。

    学校开着滑块验证码时自动重登必然失败，这时给出手动登录的明确指引，
    而不是抛一个看不懂的错误。
    """
    session = load_session(cfg.session_path)
    if session and verify_session(session, cfg):
        return session

    log.info("会话不可用，尝试重新登录")
    relogin_hint = (
        "会话已过期，需要重新登录一次：\n"
        "    class2cal login --browser\n"
        "（学校强制滑块验证码，账密无法静默重登 —— 浏览器里拖一下滑块即可）"
    )
    try:
        return login(cfg)
    except CaptchaRequired as exc:
        raise CaptchaRequired(f"{exc}\n\n{relogin_hint}") from exc
    except SsoError as exc:
        # 自动重登失败的原因多半就是滑块，别把底层报错原样丢给用户
        raise SsoError(f"{exc}\n\n{relogin_hint}") from exc
