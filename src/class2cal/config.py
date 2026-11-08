"""配置与凭据。

非敏感项放 config.toml，凭据只进 macOS Keychain。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
import tomli_w

# Keychain 服务名。统一认证与 Apple 专用密码分开存，避免互相覆盖。
KEYRING_SERVICE_SSO = "class2cal.sso"
KEYRING_SERVICE_APPLE = "class2cal.apple"

# [study] 段里由 StudyConfig 建模的键；其余键原样保留
_STUDY_KNOWN_KEYS = frozenset(
    {"enabled", "title", "days", "start", "end", "location", "slots"}
)
# [schedule] 同理：period_times 是文档里写的用户配置，保存时不能吞掉
_SCHEDULE_KNOWN_KEYS = frozenset(
    {"card_wid", "card_id", "cal_wid", "cal_name", "weeks_ahead", "field_map"}
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.toml"
VAR_DIR = PROJECT_ROOT / "var"

# 只走移动端展示方案：桌面端漏课且只给节次号，移动端才有具体时间。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


class ConfigError(RuntimeError):
    """配置缺失或不完整。"""


@dataclass
class ScheduleConfig:
    """课表卡片坐标。probe 阶段探明后写回 config.toml。"""

    card_wid: str = ""
    card_id: str = ""
    # 课表所在日历的 wid。同一张卡片里还有「假期」等其他日历，必须过滤，
    # 否则假期和自建日程会被一起导进来。按 wid 比按名字可靠。
    cal_wid: str = ""
    cal_name: str = "我的课表"
    # 抓取窗口：当前周 + 后 N 周。学校分批排课，窗口往前铺就能自然接住后续批次。
    weeks_ahead: int = 4
    # 课表接口入参的覆盖项，学校改字段时用，见 portal._build_schedule_payload。
    field_map: dict[str, Any] = field(default_factory=dict)
    # 用户手写的其他键（period_times 等），save 时原样写回
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_probed(self) -> bool:
        return bool(self.card_wid and self.card_id)


@dataclass
class StudyConfig:
    """自习时段（学校教务系统里没有晚自习，按本地配置补）。

    两种写法，content 都能用：
      简写 —— 标题 + 星期 + 起止时间，`class2cal setup` 里问一遍就写进来，够用；
      进阶 —— slots 列表，一条一个时段，可带 parity / from / to / skip。
    slots 原样保存 TOML 结构，由 study.parse_slots 校验，save() 能原封不动写回。
    """

    enabled: bool = False
    # 简写：只有一条时段时用这几个字段，setup 交互填完即存
    title: str = "晚自习"
    # 原样保留 TOML 值（列表或 "mon,wed" 这种字符串），校验交给 study.parse_days。
    # None = 配置里没写（简写时用默认星期），[] = 用户显式写了空列表（报错）。
    days: Any = None
    start: str = ""
    end: str = ""
    location: str = ""
    # 进阶：多条时段
    slots: list[dict[str, Any]] = field(default_factory=list)
    # [study] 下用户自己加的其他键，save() 原样写回，不吞用户手写配置
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalendarConfig:
    """CalDAV 目标日历。"""

    apple_id: str = ""
    # 用户已有的日历，内含手工录入事件 —— 对账靠 UID 前缀隔离保护它们。
    calendar_name: str = "学校课程"
    caldav_url: str = "https://caldav.icloud.com"


@dataclass
class Config:
    sso_username: str = ""
    sso_base: str = ""      # 统一认证地址，如 https://sso.example.edu.cn
    portal_base: str = ""   # 门户地址，如 https://all.example.edu.cn
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    study: StudyConfig = field(default_factory=StudyConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)

    @property
    def session_path(self) -> Path:
        return VAR_DIR / "session.json"

    @property
    def state_path(self) -> Path:
        return VAR_DIR / "state.json"

    @property
    def probe_dir(self) -> Path:
        return VAR_DIR / "probe"

    @property
    def raw_dir(self) -> Path:
        return VAR_DIR / "raw"


def load() -> Config:
    if not CONFIG_PATH.exists():
        return Config()

    with CONFIG_PATH.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    sso = raw.get("sso", {})
    sched = raw.get("schedule", {})
    study_raw = raw.get("study", {}) or {}
    cal = raw.get("calendar", {})

    return Config(
        sso_username=sso.get("username", ""),
        sso_base=sso.get("base", ""),
        portal_base=raw.get("portal_base", ""),
        schedule=ScheduleConfig(
            card_wid=sched.get("card_wid", ""),
            card_id=sched.get("card_id", ""),
            cal_wid=sched.get("cal_wid", ""),
            cal_name=sched.get("cal_name", "我的课表"),
            weeks_ahead=sched.get("weeks_ahead", 4),
            field_map=sched.get("field_map", {}),
            extra={k: v for k, v in sched.items() if k not in _SCHEDULE_KNOWN_KEYS},
        ),
        study=StudyConfig(
            enabled=bool(study_raw.get("enabled", False)),
            title=str(study_raw.get("title") or "晚自习"),
            days=study_raw.get("days"),
            start=str(study_raw.get("start") or ""),
            end=str(study_raw.get("end") or ""),
            location=str(study_raw.get("location") or ""),
            slots=study_raw.get("slots") or [],
            extra={
                k: v
                for k, v in study_raw.items()
                if k not in _STUDY_KNOWN_KEYS
            },
        ),
        calendar=CalendarConfig(
            apple_id=cal.get("apple_id", ""),
            calendar_name=cal.get("calendar_name", "学校课程"),
            caldav_url=cal.get("caldav_url", "https://caldav.icloud.com"),
        ),
    )


def save(cfg: Config) -> None:
    """写回 config.toml。只含非敏感项，凭据永不落盘。"""
    payload = {
        "sso": {
            "username": cfg.sso_username,
            "base": cfg.sso_base,
        },
        "portal_base": cfg.portal_base,
        "schedule": {
            **{
                k: v
                for k, v in cfg.schedule.extra.items()
                if k not in _SCHEDULE_KNOWN_KEYS
            },
            "card_wid": cfg.schedule.card_wid,
            "card_id": cfg.schedule.card_id,
            "cal_wid": cfg.schedule.cal_wid,
            "cal_name": cfg.schedule.cal_name,
            "weeks_ahead": cfg.schedule.weeks_ahead,
            "field_map": cfg.schedule.field_map,
        },
        "study": _study_payload(cfg.study),
        "calendar": {
            "apple_id": cfg.calendar.apple_id,
            "calendar_name": cfg.calendar.calendar_name,
            "caldav_url": cfg.calendar.caldav_url,
        },
    }
    CONFIG_PATH.write_text(tomli_w.dumps(payload), encoding="utf-8")


def _study_payload(study_cfg: StudyConfig) -> dict[str, Any]:
    """[study] 段的落盘内容。

    days 为 None 表示「配置里没写，用默认星期」，这时候**不能**写成 []：空列表是
    「用户显式写空」，加载时会被当成错误。少了这一条，一份「只写 start/end」的
    合法配置保存一次就变成坏的。
    """
    payload: dict[str, Any] = {
        k: v for k, v in study_cfg.extra.items() if k not in _STUDY_KNOWN_KEYS
    }
    payload.update(
        {
            "enabled": study_cfg.enabled,
            "title": study_cfg.title,
            "start": study_cfg.start,
            "end": study_cfg.end,
            "location": study_cfg.location,
            "slots": study_cfg.slots,
        }
    )
    if study_cfg.days is not None:
        payload["days"] = study_cfg.days
    return payload


def ensure_var_dirs(cfg: Config) -> None:
    for d in (VAR_DIR, cfg.probe_dir, cfg.raw_dir):
        d.mkdir(parents=True, exist_ok=True)


def get_sso_password(username: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE_SSO, username)


def set_sso_password(username: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE_SSO, username, password)


def get_apple_password(apple_id: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE_APPLE, apple_id)


def set_apple_password(apple_id: str, password: str) -> None:
    keyring.set_password(KEYRING_SERVICE_APPLE, apple_id, password)


def require_sso_credentials(cfg: Config) -> tuple[str, str]:
    if not cfg.sso_username:
        raise ConfigError("未配置统一认证账号，先跑 `class2cal setup`。")
    password = get_sso_password(cfg.sso_username)
    if not password:
        raise ConfigError(
            f"Keychain 里没有 {cfg.sso_username} 的密码，先跑 `class2cal setup`。"
        )
    return cfg.sso_username, password


def require_apple_credentials(cfg: Config) -> tuple[str, str]:
    if not cfg.calendar.apple_id:
        raise ConfigError("未配置 Apple ID，先跑 `class2cal setup`。")
    password = get_apple_password(cfg.calendar.apple_id)
    if not password:
        raise ConfigError(
            "Keychain 里没有 Apple 应用专用密码。到 appleid.apple.com 生成后跑 `class2cal setup`。"
        )
    return cfg.calendar.apple_id, password
