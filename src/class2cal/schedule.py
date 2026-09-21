"""课表解析：门户原始响应 -> Lesson。

学校移动端返回的字段名在 probe 阶段才能确定，所以这里的取值逻辑对字段名宽容：
每个语义（课程名/时间/教室/教师）都给一组候选键，命中即用。

时间优先取移动端返回的具体时刻（08:20 这种）。只有当某条记录只给了节次号时，
才退回 config 里的节次时间表兜底，并在 Lesson.source 上标记为推算。
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

# 各语义的候选字段名。金智各校字段不一，按出现频率从高到低试。
KEYS_COURSE = ("courseName", "kcmc", "course", "title", "className", "kcm", "name")
KEYS_DATE = ("date", "courseDate", "skrq", "classDate", "day", "rq")
KEYS_START = ("startTime", "beginTime", "kssj", "start", "sksj")
KEYS_END = ("endTime", "jssj", "end", "xksj")
KEYS_ROOM = ("classroom", "roomName", "jsmc", "room", "place", "address", "skdd")
KEYS_TEACHER = ("teacherName", "teacher", "jsxm", "skjs", "jgmc")
KEYS_PERIOD = ("period", "jc", "sectionNo", "section", "jcdm", "periodName")
# 一条记录里课表数组可能挂在这些键下
KEYS_LIST = ("courseList", "list", "rows", "data", "items", "schedules", "kbList", "records")

_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_DATE_RE = re.compile(r"(\d{4})\D(\d{1,2})\D(\d{1,2})")
_PERIOD_RE = re.compile(r"(\d{1,2})")


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
    # "mobile" = 直接取自移动端的具体时间；"period" = 由节次号推算
    source: str = "mobile"
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def uid(self) -> str:
        """事件 UID。c2c. 前缀是所有权标记 —— 对账只动带这个前缀的事件。

        以「日期 + 开始时间 + 课程名」为身份：同一节课改教室或换老师算更新，
        改时间算另一节课（旧的会被删、新的会被建），符合调课的直觉。
        """
        digest = hashlib.sha256(self.course.encode("utf-8")).hexdigest()[:8]
        stamp = self.date.strftime("%Y%m%d")
        hhmm = self.start.replace(":", "")
        return f"{UID_PREFIX}{stamp}.{hhmm}.{digest}@{UID_DOMAIN}"

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
        # 容错：结束时间不晚于开始时间时按 1 小时课时处理
        if dt <= self.start_dt:
            return self.start_dt + timedelta(hours=1)
        return dt

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


# ---------- 取值 helper ----------


def _pick(node: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in node and node[k] not in (None, "", []):
            return node[k]
    # 再试一遍大小写不敏感匹配
    lowered = {str(k).lower(): v for k, v in node.items()}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, "", []):
            return v
    return None


def _parse_time(value: Any) -> str | None:
    """从各种格式里抠出 HH:MM。"""
    if value is None:
        return None
    text = str(value)
    m = _TIME_RE.search(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    # 纯数字形式，如 0820 / 820
    digits = re.sub(r"\D", "", text)
    if len(digits) in (3, 4):
        return f"{int(digits[:-2]):02d}:{digits[-2:]}"
    return None


def _parse_date(value: Any, fallback: date | None = None) -> date | None:
    if value is None:
        return fallback
    if isinstance(value, date):
        return value
    text = str(value)
    m = _DATE_RE.search(text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    return fallback


def _parse_period(value: Any) -> str | None:
    if value is None:
        return None
    m = _PERIOD_RE.search(str(value))
    return m.group(1) if m else None


def _iter_records(node: Any) -> Iterator[dict]:
    """从任意嵌套结构里把课程记录挖出来。

    卡片返回的包装层级各校不同（data.courseList、data.rows[].list ...），
    所以按「看起来像课程记录」来判定，而不是依赖固定路径。
    """
    if isinstance(node, dict):
        if _looks_like_lesson(node):
            yield node
            return
        for k in KEYS_LIST:
            if k in node:
                yield from _iter_records(node[k])
        for k, v in node.items():
            if k not in KEYS_LIST and isinstance(v, (dict, list)):
                yield from _iter_records(v)

    elif isinstance(node, list):
        for item in node:
            yield from _iter_records(item)


def _looks_like_lesson(node: dict) -> bool:
    """有课程名，且有时间或节次，就当是一条课程记录。"""
    if _pick(node, KEYS_COURSE) is None:
        return False
    return _pick(node, KEYS_START) is not None or _pick(node, KEYS_PERIOD) is not None


# ---------- 主解析 ----------


def parse_lessons(
    data: Any,
    period_times: dict[str, list[str]] | None = None,
    default_date: date | None = None,
) -> list[Lesson]:
    """把卡片原始响应解析成 Lesson 列表。

    period_times: 节次 -> [开始, 结束]，仅当记录没给具体时间时兜底。
    """
    period_times = period_times or {}
    lessons: list[Lesson] = []
    skipped = 0

    for rec in _iter_records(data):
        course = _pick(rec, KEYS_COURSE)
        if not course:
            continue

        d = _parse_date(_pick(rec, KEYS_DATE), default_date)
        if d is None:
            skipped += 1
            log.debug("跳过无日期的记录：%s", course)
            continue

        start = _parse_time(_pick(rec, KEYS_START))
        end = _parse_time(_pick(rec, KEYS_END))
        source = "mobile"

        if not start:
            # 没给具体时间，退回节次表推算
            period = _parse_period(_pick(rec, KEYS_PERIOD))
            slot = period_times.get(period or "")
            if not slot:
                skipped += 1
                log.warning("「%s」（%s）既没具体时间也没可用节次映射，跳过", course, d)
                continue
            start = slot[0]
            end = slot[1] if len(slot) > 1 else None
            source = "period"
            log.info("「%s」（%s）时间由节次 %s 推算", course, d, period)

        if not end:
            # 只有开始时间：按 45 分钟一节兜底
            h, m = start.split(":")
            end_dt = datetime(2000, 1, 1, int(h), int(m)) + timedelta(minutes=45)
            end = end_dt.strftime("%H:%M")

        lessons.append(
            Lesson(
                date=d,
                start=start,
                end=end,
                course=str(course).strip(),
                room=str(_pick(rec, KEYS_ROOM) or "").strip(),
                teacher=str(_pick(rec, KEYS_TEACHER) or "").strip(),
                source=source,
                raw=rec,
            )
        )

    if skipped:
        log.warning("有 %d 条记录因缺时间信息被跳过", skipped)

    # 同一 UID 只保留一条（移动端可能把跨节的课重复返回）
    deduped: dict[str, Lesson] = {}
    for lesson in lessons:
        deduped.setdefault(lesson.uid, lesson)

    return sorted(deduped.values(), key=lambda x: (x.date, x.start, x.course))
