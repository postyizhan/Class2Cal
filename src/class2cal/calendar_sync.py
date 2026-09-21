"""CalDAV 对账 —— 本项目的安全核心。

目标日历「学校课程」里已有用户手工录入的事件，绝不能碰。做法是给工具建的事件
统一打 UID 前缀 c2c.，对账时只在带这个前缀的集合内做增删改：

    对每个抓取成功的周 W:
        D = 本次抓到的课（按 UID 索引）
        C = 日历中周 W 内、UID 以 c2c. 开头的现有事件
        新增 D - C，更新 D ∩ C 中指纹有变的，删除 C - D
    UID 无 c2c. 前缀的事件一律不动

四道安全闸：
  1. 非空守卫 —— 某周原本有课、这次抓到 0 条时中止该周并告警，不删。
     防的是会话静默失效返回空数据把日历清空。
  2. 未抓取周免疫 —— 只对账本次成功抓取的周，学校还没排的周完全不动。
  3. 前缀隔离 —— 删除/更新前硬校验 UID 前缀，缺失即跳过。
  4. dry-run 优先 —— 先出完整 diff 供人确认。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import caldav
from caldav.lib.error import DAVError
from icalendar import Calendar as ICalendar
from icalendar import Event as IEvent

from . import config
from .schedule import UID_PREFIX, Lesson, week_range
from .state import SyncState

log = logging.getLogger(__name__)

PRODID = "-//Class2Cal//ZH//"


class CalendarError(RuntimeError):
    """CalDAV 操作失败。"""


class EmptyGuardTripped(CalendarError):
    """非空守卫触发：该周原本有课，这次却抓到 0 条。"""


def is_owned(uid: str | None) -> bool:
    """只有带 c2c. 前缀的事件才是工具管的。第 3 道闸的唯一判据。"""
    return bool(uid) and str(uid).startswith(UID_PREFIX)


@dataclass
class WeekPlan:
    """某一周的对账计划。"""

    monday: date
    to_add: list[Lesson] = field(default_factory=list)
    to_update: list[tuple[Lesson, Any]] = field(default_factory=list)
    to_delete: list[tuple[str, Any]] = field(default_factory=list)
    protected: int = 0  # 因非工具事件而跳过的数量
    skipped_reason: str = ""

    @property
    def is_noop(self) -> bool:
        return not (self.to_add or self.to_update or self.to_delete)

    @property
    def was_skipped(self) -> bool:
        return bool(self.skipped_reason)


@dataclass
class SyncReport:
    plans: list[WeekPlan] = field(default_factory=list)
    dry_run: bool = True

    @property
    def added(self) -> int:
        return sum(len(p.to_add) for p in self.plans if not p.was_skipped)

    @property
    def updated(self) -> int:
        return sum(len(p.to_update) for p in self.plans if not p.was_skipped)

    @property
    def deleted(self) -> int:
        return sum(len(p.to_delete) for p in self.plans if not p.was_skipped)

    @property
    def protected(self) -> int:
        return sum(p.protected for p in self.plans)

    @property
    def skipped_weeks(self) -> list[WeekPlan]:
        return [p for p in self.plans if p.was_skipped]


def build_event(lesson: Lesson) -> bytes:
    """按 Lesson 生成 iCalendar 事件。不设 VALARM —— 用户明确不要提醒。"""
    cal = ICalendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")

    ev = IEvent()
    ev.add("uid", lesson.uid)
    ev.add("summary", lesson.course)
    ev.add("dtstart", lesson.start_dt)
    ev.add("dtend", lesson.end_dt)
    ev.add("dtstamp", datetime.now())
    if lesson.room:
        ev.add("location", lesson.room)

    notes = []
    if lesson.teacher:
        notes.append(f"教师：{lesson.teacher}")
    if lesson.source == "period":
        notes.append("（时间由节次推算，请核对）")
    if notes:
        ev.add("description", "\n".join(notes))

    cal.add_component(ev)
    return cal.to_ical()


def _event_uid(event: Any) -> str | None:
    """从 CalDAV 事件对象里取 UID。"""
    try:
        comp = event.icalendar_component
    except (AttributeError, KeyError):
        return None
    uid = comp.get("uid") if comp else None
    return str(uid) if uid else None


def _event_fingerprint(event: Any) -> tuple | None:
    """按与 Lesson.fingerprint 对齐的方式取现有事件的指纹。"""
    try:
        comp = event.icalendar_component
    except (AttributeError, KeyError):
        return None
    if comp is None:
        return None

    def _hhmm(key: str) -> str:
        val = comp.get(key)
        dt = getattr(val, "dt", None)
        return dt.strftime("%H:%M") if isinstance(dt, datetime) else ""

    desc = str(comp.get("description") or "")
    teacher = ""
    for line in desc.splitlines():
        if line.startswith("教师："):
            teacher = line[len("教师：") :].strip()
            break

    return (
        _hhmm("dtstart"),
        _hhmm("dtend"),
        str(comp.get("summary") or ""),
        str(comp.get("location") or ""),
        teacher,
    )


class CalendarSyncer:
    def __init__(self, cfg: config.Config) -> None:
        self.cfg = cfg
        self._calendar: caldav.Calendar | None = None

    # ---------- 连接 ----------

    def connect(self) -> caldav.Calendar:
        if self._calendar is not None:
            return self._calendar

        apple_id, password = config.require_apple_credentials(self.cfg)
        try:
            client = caldav.DAVClient(
                url=self.cfg.calendar.caldav_url,
                username=apple_id,
                password=password,
            )
            principal = client.principal()
            calendars = principal.calendars()
        except DAVError as exc:
            raise CalendarError(
                f"连不上 iCloud CalDAV：{exc}。确认用的是 Apple 应用专用密码，不是账户密码。"
            ) from exc

        target = self.cfg.calendar.calendar_name
        for cal in calendars:
            if str(cal.name or "").strip() == target:
                self._calendar = cal
                return cal

        names = ", ".join(str(c.name) for c in calendars)
        raise CalendarError(f"iCloud 里没找到日历「{target}」。现有日历：{names}")

    # ---------- 单周对账 ----------

    def plan_week(
        self,
        monday: date,
        lessons: Iterable[Lesson],
        state: SyncState,
    ) -> WeekPlan:
        """算出某周要做哪些增删改。只读，不落任何改动。"""
        plan = WeekPlan(monday=monday)
        desired = {le.uid: le for le in lessons}

        # 闸 1：非空守卫。原本有课却抓到 0 条 —— 判为异常，不删。
        prev = state.week_record(monday)
        if not desired and prev.count > 0:
            plan.skipped_reason = (
                f"上次同步有 {prev.count} 节课，这次抓到 0 条。"
                "疑似会话失效或接口变动，已跳过该周，未删除任何事件。"
            )
            return plan

        calendar = self.connect()
        start, end = week_range(monday)
        try:
            existing = calendar.search(
                start=datetime.combine(start, datetime.min.time()),
                end=datetime.combine(end + timedelta(days=1), datetime.min.time()),
                event=True,
                expand=False,
            )
        except DAVError as exc:
            raise CalendarError(f"查询 {start} ~ {end} 的事件失败：{exc}") from exc

        owned: dict[str, Any] = {}
        for ev in existing:
            uid = _event_uid(ev)
            # 闸 3：前缀隔离。用户手录的事件在这里就被挡掉了。
            if is_owned(uid):
                owned[str(uid)] = ev
            else:
                plan.protected += 1

        for uid, lesson in desired.items():
            if uid not in owned:
                plan.to_add.append(lesson)
            elif _event_fingerprint(owned[uid]) != lesson.fingerprint:
                plan.to_update.append((lesson, owned[uid]))

        for uid, ev in owned.items():
            if uid not in desired:
                plan.to_delete.append((uid, ev))

        return plan

    def apply_week(self, plan: WeekPlan) -> None:
        """落地某周的对账计划。"""
        if plan.was_skipped:
            return
        calendar = self.connect()

        for lesson in plan.to_add:
            try:
                calendar.save_event(build_event(lesson).decode("utf-8"))
            except DAVError as exc:
                raise CalendarError(
                    f"新增「{lesson.course}」（{lesson.date}）失败：{exc}"
                ) from exc

        for lesson, event in plan.to_update:
            try:
                event.data = build_event(lesson).decode("utf-8")
                event.save()
            except DAVError as exc:
                raise CalendarError(
                    f"更新「{lesson.course}」（{lesson.date}）失败：{exc}"
                ) from exc

        for uid, event in plan.to_delete:
            # 闸 3 再校验一次：删除是唯一不可逆的操作，这里不省这道检查。
            if not is_owned(uid):
                log.error("拒绝删除非工具事件 %s", uid)
                continue
            try:
                event.delete()
            except DAVError as exc:
                raise CalendarError(f"删除 {uid} 失败：{exc}") from exc

    # ---------- 多周同步 ----------

    def sync(
        self,
        weekly_lessons: dict[date, list[Lesson]],
        state: SyncState,
        dry_run: bool = True,
    ) -> SyncReport:
        """对账所有本次抓取成功的周。

        weekly_lessons 只包含抓取成功的周 —— 这就是闸 2（未抓取周免疫）：
        没抓到的周根本不进这个字典，自然不会被对账。
        """
        report = SyncReport(dry_run=dry_run)

        for monday in sorted(weekly_lessons):
            lessons = weekly_lessons[monday]
            plan = self.plan_week(monday, lessons, state)
            report.plans.append(plan)

            if plan.was_skipped:
                log.warning("跳过 %s 那周：%s", monday, plan.skipped_reason)
                continue

            if dry_run:
                continue

            self.apply_week(plan)
            state.update_week(monday, [le.uid for le in lessons])

        if not dry_run:
            state.save()

        return report
