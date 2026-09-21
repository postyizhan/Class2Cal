"""课表解析：门户日历卡片响应 -> Lesson。

数据来源是门户的日历卡片 CUS_CARD_CALENDAR_DETAIL 的 renderData 方法，
响应结构（已由抓包确认）：

    {"data": {"datas": {
        "2026-10-08": [{
            "calName":    "我的课表",
            "eventTitle": "电工电子技术基础",
            "startTime":  "2026-10-08T08:20:00",
            "endTime":    "2026-10-08T11:25:00",
            "location":   "善学楼304",
            "eventDesc":  "上课节次：1-4 节， 授课教师：王艳",
            "eventId":    "3afa72b2...",
        }]
    }}}

两个要点：
  1. startTime/endTime 就是具体时刻，连堂课的起止由服务端算好（1-4 节 =
     08:20-11:25），不需要用节次表推算。timetable.py 只作校验参考。
  2. 同一个日历卡片里还有「假期」等其他日历，必须按 calName/calWid 过滤，
     否则假期、自建日程会被一起导进来。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterator

log = logging.getLogger(__name__)

UID_PREFIX = "c2c."
UID_DOMAIN = "class2cal"

# 课表所在的日历名。其他日历（假期、自建日程）不是课，要排除。
LESSON_CAL_NAME = "我的课表"

# eventDesc 形如「上课节次：1-4 节， 授课教师：王艳」
_PERIOD_RE = re.compile(r"上课节次[：:]\s*([\d\-～~,，]+)")
_TEACHER_RE = re.compile(r"授课教师[：:]\s*([^，,；;]+)")


class ParseError(RuntimeError):
    """课表数据解析失败。"""


@dataclass(frozen=True)
class Lesson:
    """一节课（具体某天的一次上课）。"""

    date: date
    start: str  # HH:MM
    end: str  # HH:MM
    course: str
    room: str = ""
    teacher: str = ""
    periods: str = ""  # 原始节次描述，如 "1-4"
    event_id: str = ""  # 服务端事件 ID，仅作排查用
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def uid(self) -> str:
        """事件 UID。c2c. 前缀是所有权标记 —— 对账只动带这个前缀的事件。

        身份取「日期 + 开始时间 + 课程名」：改教室或换老师算更新，
        改时间算另一节课（旧的删、新的建），符合调课的直觉。

        不用服务端的 eventId —— 它在学校重排课时会变，会导致同一节课
        被当成新事件重复导入。
        """
        digest = hashlib.sha256(self.course.encode("utf-8")).hexdigest()[:8]
        return f"{UID_PREFIX}{self.date:%Y%m%d}.{self.start.replace(':', '')}.{digest}@{UID_DOMAIN}"

    @property
    def fingerprint(self) -> tuple:
        """判断是否需要更新的依据。"""
        return (self.start, self.end, self.course, self.room, self.teacher)

    @property
    def start_dt(self) -> datetime:
        h, m = self.start.split(":")
        return datetime.combine(self.date, datetime.min.time()).replace(
            hour=int(h), minute=int(m)
        )

    @property
    def end_dt(self) -> datetime:
        h, m = self.end.split(":")
        dt = datetime.combine(self.date, datetime.min.time()).replace(
            hour=int(h), minute=int(m)
        )
        # 容错：结束不晚于开始时按 1 小时算，避免写出非法事件
        return self.start_dt + timedelta(hours=1) if dt <= self.start_dt else dt

    def summary(self) -> str:
        return self.course if not self.room else f"{self.course}（{self.room}）"


# ---------- 周窗口 ----------


def week_start(d: date) -> date:
    """该日期所在周的周一。"""
    return d - timedelta(days=d.weekday())


def iter_week_starts(anchor: date, weeks_ahead: int) -> Iterator[date]:
    """从 anchor 所在周开始，往后数 weeks_ahead 周（含当前周）。"""
    base = week_start(anchor)
    for i in range(max(1, weeks_ahead)):
        yield base + timedelta(weeks=i)


def week_range(monday: date) -> tuple[date, date]:
    """周一 -> (周一, 周日)。"""
    return monday, monday + timedelta(days=6)


# ---------- 字段解析 ----------


def parse_event_desc(desc: str) -> tuple[str, str]:
    """从 eventDesc 里抠出 (节次, 教师)。

    >>> parse_event_desc("上课节次：1-4 节， 授课教师：王艳")
    ('1-4', '王艳')
    """
    if not desc:
        return "", ""
    period = _PERIOD_RE.search(desc)
    teacher = _TEACHER_RE.search(desc)
    return (
        period.group(1).strip() if period else "",
        teacher.group(1).strip() if teacher else "",
    )


def _hhmm(iso: str) -> str | None:
    """从 "2026-10-08T08:20:00" 取 "08:20"。"""
    if not iso:
        return None
    m = re.search(r"[T ](\d{2}):(\d{2})", str(iso))
    return f"{m.group(1)}:{m.group(2)}" if m else None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", str(value or ""))
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


# ---------- 主解析 ----------


def parse_lessons(
    data: Any,
    cal_name: str = LESSON_CAL_NAME,
    cal_wid: str = "",
) -> list[Lesson]:
    """把日历卡片响应解析成 Lesson 列表。

    只保留课表日历里的事件 —— 同一响应里还有假期、自建日程等，那些不是课。
    cal_wid 非空时优先按它过滤（比名字可靠）。
    """
    datas = _locate_datas(data)
    if datas is None:
        raise ParseError(
            "响应里找不到 data.datas —— 接口结构可能变了。"
            "翻一下 var/raw/ 里的存档确认。"
        )

    lessons: list[Lesson] = []
    skipped_other_cal = 0
    skipped_bad = 0

    for day_key, events in datas.items():
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict):
                continue

            # 过滤非课表日历
            if cal_wid:
                if str(ev.get("calWid") or "") != str(cal_wid):
                    skipped_other_cal += 1
                    continue
            elif cal_name and str(ev.get("calName") or "").strip() != cal_name:
                skipped_other_cal += 1
                continue

            course = str(ev.get("eventTitle") or "").strip()
            d = _parse_date(ev.get("startDate") or ev.get("startTime") or day_key)
            start = _hhmm(ev.get("startTime"))
            end = _hhmm(ev.get("endTime"))

            if not (course and d and start):
                skipped_bad += 1
                log.warning("跳过字段不全的事件：%s / %s", day_key, course or "(无课程名)")
                continue

            if not end:
                # 只有开始时间：按 45 分钟一节兜底
                h, m = start.split(":")
                end = (datetime(2000, 1, 1, int(h), int(m)) + timedelta(minutes=45)).strftime(
                    "%H:%M"
                )

            periods, teacher = parse_event_desc(str(ev.get("eventDesc") or ""))

            lessons.append(
                Lesson(
                    date=d,
                    start=start,
                    end=end,
                    course=course,
                    room=str(ev.get("location") or "").strip(),
                    teacher=teacher,
                    periods=periods,
                    event_id=str(ev.get("eventId") or ""),
                    raw=ev,
                )
            )

    if skipped_other_cal:
        log.debug("跳过 %d 个非课表日历的事件", skipped_other_cal)
    if skipped_bad:
        log.warning("有 %d 个事件因字段不全被跳过", skipped_bad)

    # 同 UID 去重（跨节的课可能被重复返回）
    deduped: dict[str, Lesson] = {}
    for le in lessons:
        deduped.setdefault(le.uid, le)

    return sorted(deduped.values(), key=lambda x: (x.date, x.start, x.course))


def _locate_datas(data: Any) -> dict | None:
    """定位 datas 字典。调用方可能传整个响应体，也可能只传 data 部分。"""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("datas"), dict):
        return data["datas"]
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("datas"), dict):
        return inner["datas"]
    return None


def group_by_week(lessons: list[Lesson]) -> dict[date, list[Lesson]]:
    """按周分组，键是该周周一 —— 对账是按周做的。"""
    out: dict[date, list[Lesson]] = {}
    for le in lessons:
        out.setdefault(week_start(le.date), []).append(le)
    return out
