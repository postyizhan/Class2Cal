"""门户（网上办事服务大厅）接口调用。

门户是 Vue SPA，页面内容不在 HTML 里，而是：
  /getPageInfo                        取展示方案结构与卡片清单
  /execCardMethod/{cardWid}/{cardId}  取某张卡片的数据

关键：PC 与移动端是后台各自独立配置的两套「展示方案」，调的是不同卡片。
桌面端会漏课且只给节次号，所以这里全程走移动端（会话已挂 iPhone UA）。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)

GET_PAGE_INFO = "/getPageInfo"
GET_USER_PERMISSION_ROUTERS = "/getUserPermissionRouters"
GET_PERSONALIZED_MENUS = "/personalized/getPersonalizedMenus"
EXEC_CARD_METHOD = "/execCardMethod"

# 卡片名里出现这些词就当课表候选。
SCHEDULE_HINTS = ("课表", "课程表", "我的课程", "课程", "日程", "上课")


class PortalError(RuntimeError):
    """门户接口调用失败。"""


class NotLoggedIn(PortalError):
    """会话失效。"""


class CardNotFound(PortalError):
    """没找到课表卡片。"""


class PortalClient:
    def __init__(self, session: requests.Session, cfg: config.Config) -> None:
        self.session = session
        self.cfg = cfg

    # ---------- 底层请求 ----------

    def _post(self, path: str, payload: dict | None = None) -> Any:
        url = f"{config.PORTAL_BASE}{path}"
        resp = self.session.post(url, json=payload or {}, timeout=20)
        return self._unwrap(resp, path)

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{config.PORTAL_BASE}{path}"
        resp = self.session.get(url, params=params, timeout=20)
        return self._unwrap(resp, path)

    @staticmethod
    def _unwrap(resp: requests.Response, path: str) -> Any:
        """门户统一返回 {errcode, errmsg, data}；非 JSON 基本就是出错页。"""
        try:
            body = resp.json()
        except ValueError as exc:
            raise PortalError(
                f"{path} 没返回 JSON（HTTP {resp.status_code}），可能是接口路径变了"
            ) from exc

        errcode = str(body.get("errcode", ""))
        errmsg = str(body.get("errmsg", ""))
        if errmsg == "notLogin" or (errcode == "999" and "ogin" in errmsg):
            raise NotLoggedIn(f"{path} 报未登录")
        if errcode not in ("0", ""):
            raise PortalError(f"{path} 失败：errcode={errcode} errmsg={errmsg}")
        return body.get("data")

    # ---------- 结构探测 ----------

    def get_permission_routers(self) -> Any:
        return self._get(GET_USER_PERMISSION_ROUTERS)

    def get_personalized_menus(self) -> Any:
        return self._post(GET_PERSONALIZED_MENUS)

    def get_page_info(self, **params: Any) -> Any:
        """取展示方案结构。参数随门户版本而异，故原样透传。"""
        return self._post(GET_PAGE_INFO, dict(params))

    def exec_card_method(self, card_wid: str, card_id: str, payload: dict) -> Any:
        """调卡片数据方法。这是课表数据的真正来源。"""
        path = f"{EXEC_CARD_METHOD}/{card_wid}/{card_id}"
        return self._post(path, payload)

    # ---------- probe：定位课表卡片 ----------

    def probe_schedule_cards(self, dump_dir: Path) -> list[dict]:
        """把门户返回的结构原样存档，并挑出课表候选卡片。

        课表卡片的 cardWid/cardId 未登录状态拿不到，只能登录后现场探。
        存档是为了在自动识别失配时，人能直接翻 JSON 找。
        """
        dump_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[dict] = []

        probes: list[tuple[str, Any]] = []
        for name, fn in (
            ("permission_routers", self.get_permission_routers),
            ("personalized_menus", self.get_personalized_menus),
            ("page_info", self.get_page_info),
        ):
            try:
                probes.append((name, fn()))
            except PortalError as exc:
                log.warning("探测 %s 失败：%s", name, exc)
                probes.append((name, {"_error": str(exc)}))

        for name, data in probes:
            (dump_dir / f"{name}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            candidates.extend(_find_card_candidates(data))

        # 同一张卡片可能在多处出现，按 (wid, id) 去重
        seen: set[tuple[str, str]] = set()
        unique: list[dict] = []
        for c in candidates:
            key = (c["card_wid"], c["card_id"])
            if key not in seen:
                seen.add(key)
                unique.append(c)

        (dump_dir / "candidates.json").write_text(
            json.dumps(unique, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return unique

    # ---------- fetch：取课表 ----------

    def fetch_schedule(self, start: date, end: date, archive_dir: Path | None = None) -> Any:
        """取 [start, end] 区间的课表原始数据。"""
        sched = self.cfg.schedule
        if not sched.is_probed:
            raise CardNotFound("还没探明课表卡片坐标，先跑 `class2cal probe`。")

        payload = _build_schedule_payload(start, end, sched.field_map)
        data = self.exec_card_method(sched.card_wid, sched.card_id, payload)

        if archive_dir is not None:
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = f"{start.isoformat()}_{end.isoformat()}"
            (archive_dir / f"{stamp}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return data


def _build_schedule_payload(start: date, end: date, field_map: dict[str, Any]) -> dict:
    """构造课表查询载荷。

    金智卡片的入参字段名各校不同，probe 后写进 config 的 field_map。
    未配置时给一组常见默认名。
    """
    start_key = field_map.get("start_date", "startDate")
    end_key = field_map.get("end_date", "endDate")
    payload: dict[str, Any] = {
        start_key: start.isoformat(),
        end_key: end.isoformat(),
    }
    # 额外入参（如 semester、xnxq）由 probe 阶段写入
    payload.update(field_map.get("extra_params") or {})
    return payload


def _find_card_candidates(node: Any, path: str = "") -> list[dict]:
    """递归找同时带 cardWid 与 cardId、且名字像课表的节点。"""
    found: list[dict] = []

    if isinstance(node, dict):
        wid = node.get("cardWid") or node.get("cardWID") or node.get("wid")
        cid = node.get("cardId") or node.get("cardID")
        if wid and cid:
            label = " ".join(
                str(node.get(k, ""))
                for k in ("cardName", "name", "title", "serviceName", "menuName")
            )
            if any(h in label for h in SCHEDULE_HINTS):
                found.append(
                    {
                        "card_wid": str(wid),
                        "card_id": str(cid),
                        "label": label.strip(),
                        "found_at": path,
                    }
                )
        for k, v in node.items():
            found.extend(_find_card_candidates(v, f"{path}.{k}"))

    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_find_card_candidates(v, f"{path}[{i}]"))

    return found
