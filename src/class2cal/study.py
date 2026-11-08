"""自习时段：学校教务只排正课，晚自习按本地配置补进日历。

学校系统里根本没有晚自习这节课，所以它不会出现在课表接口的返回里。这里让用户
自己写几条时段（晚自习、早自习、周末自习），工具按周展开成和正课同样结构的事件，
交给 calendar_sync 一起对账 —— 改时间、改星期、关掉某条，日历都会自动跟着变，
不需要手工删事件。

config.toml 示例：

    [study]
    enabled = true

    [[study.slots]]
    title = "晚自习"
    days = ["mon", "tue", "wed", "thu", "sun"]
    start = "18:00"
    end = "21:00"
    location = "图书馆"
    parity = "all"            # all | odd | even（按 ISO 周次单双周）
    from = "2026-09-01"       # 可选，学期开始
    to = "2027-01-15"         # 可选，学期结束
    skip = ["2026-10-01"]     # 可选，跳过这些日期

days 支持三种写法，混用也行：英文星期（mon..sun）、中文（周一..周日）、
数字（1..7 = 周一到周日，0 视作周日）。

有正课和自习时段重叠的那天不写自习 —— 人在上课，「有正课就上正课」。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .schedule import STUDY_KIND, Lesson

log = logging.getLogger(__name__)

# 英文 / 中文 -> weekday()（0 = 周一）
_WEEKDAY_NAMES: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}

PARITY_VALUES = ("all", "odd", "even")


class StudyConfigError(RuntimeError):
    """自习时段配置写得不对。信息要能直接指到出错的字段。"""


@dataclass(frozen=True)
class StudySlot:
    """一条自习时段配置。"""

    title: str
    days: tuple[int, ...]  # 0=周一 ... 6=周日
    start: str
    end: str
    location: str = ""
    parity: str = "all"  # all / odd / even，按 ISO 周次
    date_from: date | None = None
    date_to: date | None = None
    skip_dates: tuple[date, ...] = ()

    def lessons_for_week(
        self, monday: date, occupied: Iterable[Lesson] = ()
    ) -> list[Lesson]:
        """展开某一周的自习事件。

        occupied 是这一周学校排的正课：有正课和自习时段重叠的那天，人是在上课，
        不写自习 —— 「有正课就上正课，不排晚自习」。
        """
        busy_by_day: dict[date, list[Lesson]] = {}
        for le in occupied:
            if not le.is_study:
                busy_by_day.setdefault(le.date, []).append(le)

        out: list[Lesson] = []
        for weekday in self.days:
            d = monday + timedelta(days=weekday)
            if self.date_from and d < self.date_from:
                continue
            if self.date_to and d > self.date_to:
                continue
            if d in self.skip_dates:
                continue
            if not self._parity_matches(d):
                continue
            if self._conflicting(d, busy_by_day.get(d, ())):
                continue
            out.append(
                Lesson(
                    date=d,
                    start=self.start,
                    end=self.end,
                    course=self.title,
                    room=self.location,
                    kind=STUDY_KIND,
                )
            )
        return out

    def explain_week(
        self, monday: date, occupied: Iterable[Lesson] = ()
    ) -> tuple[list[Lesson], list[tuple[date, str]]]:
        """展开一周，并给出**没排上的那些天为什么没排**。

        周日/某天少了晚自习时，光看结果没法判断是配置没写、单双周不符、学期范围外、
        跳过列表，还是被正课顶掉了 —— 这里把原因带出来，省得猜。
        """
        busy_by_day: dict[date, list[Lesson]] = {}
        for le in occupied:
            if not le.is_study:
                busy_by_day.setdefault(le.date, []).append(le)

        lessons: list[Lesson] = []
        skipped: list[tuple[date, str]] = []
        for weekday in self.days:
            d = monday + timedelta(days=weekday)
            if self.date_from and d < self.date_from:
                skipped.append((d, f"早于学期开始 {self.date_from}"))
                continue
            if self.date_to and d > self.date_to:
                skipped.append((d, f"晚于学期结束 {self.date_to}"))
                continue
            if d in self.skip_dates:
                skipped.append((d, "在 skip 列表里"))
                continue
            if not self._parity_matches(d):
                parity = "单" if self.parity == "odd" else "双"
                skipped.append((d, f"配的是{parity}周，这天不是"))
                continue
            hit = self._conflicting(d, busy_by_day.get(d, ()))
            if hit is not None:
                skipped.append(
                    (d, f"与正课「{hit.course}」{hit.start}-{hit.end} 重叠（有正课就不排自习）")
                )
                continue
            lessons.append(
                Lesson(
                    date=d,
                    start=self.start,
                    end=self.end,
                    course=self.title,
                    room=self.location,
                    kind=STUDY_KIND,
                )
            )
        return lessons, skipped

    def _conflicting(self, d: date, lessons: Iterable[Lesson]) -> Lesson | None:
        """这一天的正课是否和自习时段有重叠。"""
        start, end = _minutes(self.start), _minutes(self.end)
        for le in lessons:
            if le.date != d:
                continue
            theirs_start = _minutes(le.start)
            theirs_end = _minutes(le.end)
            if start < theirs_end and end > theirs_start:
                return le
        return None

    def _parity_matches(self, d: date) -> bool:
        if self.parity == "all":
            return True
        odd = d.isocalendar().week % 2 == 1
        return odd if self.parity == "odd" else not odd

    def describe_days(self) -> str:
        names = "一二三四五六日"
        return "、".join(f"周{names[i]}" for i in sorted(self.days))


def lessons_for_week(
    slots: Iterable[StudySlot],
    monday: date,
    occupied: Iterable[Lesson] = (),
) -> list[Lesson]:
    """把一周里所有时段展开出来，按日期+时间排序。

    occupied 传这一周学校排的正课，重叠的那天自动跳过。
    """
    occupied = list(occupied)
    out: list[Lesson] = []
    for slot in slots:
        out.extend(slot.lessons_for_week(monday, occupied=occupied))

    # 两条时段可能展开出同一个 UID（同日、同标题、同开始时间）。UID 相同就只
    # 能有一个事件，这里先合并并告警，避免 fetch 显示两条、sync 只写一条。
    unique: dict[str, Lesson] = {}
    for le in out:
        if le.uid in unique:
            log.warning(
                "自习时段重复：%s %s 起，标题「%s」—— 同日同标题同开始时间只保留一条（结束/地点以先出现的为准）",
                le.date,
                le.start,
                le.course,
            )
            continue
        unique[le.uid] = le
    return sorted(unique.values(), key=lambda x: (x.date, x.start, x.course))


def _may_share_days(a: StudySlot, b: StudySlot) -> bool:
    """两条时段有没有可能真的落在同一天：单双周和日期范围都要兼容。"""
    if a.parity != "all" and b.parity != "all" and a.parity != b.parity:
        return False
    if a.date_to and b.date_from and a.date_to < b.date_from:
        return False
    if b.date_to and a.date_from and b.date_to < a.date_from:
        return False
    return True


def explain_week(
    slots: Iterable[StudySlot],
    monday: date,
    occupied: Iterable[Lesson] = (),
) -> tuple[list[Lesson], list[tuple[date, str]]]:
    """这一周会排哪些自习，以及没排的那些天为什么没排。

    「某天没有晚自习」光看结果无法区分是配置问题还是被正课顶掉，所以把原因一并
    返回，`study preview` 会打出来。
    """
    items = list(slots)
    lessons = lessons_for_week(items, monday, occupied=occupied)
    reasons: list[tuple[date, str]] = []
    for slot in items:
        _, why = slot.explain_week(monday, occupied=occupied)
        reasons.extend((d, f"{slot.title}：{reason}") for d, reason in why)
    return lessons, sorted(reasons)


def find_overlaps(slots: Iterable[StudySlot]) -> list[tuple[StudySlot, StudySlot]]:
    """找出真正会撞在一起的自习时段（自习之间没有正课那种优先级，只能提醒）。"""
    items = list(slots)
    out: list[tuple[StudySlot, StudySlot]] = []
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if not set(a.days) & set(b.days):
                continue
            if not _may_share_days(a, b):
                continue
            if _minutes(a.start) < _minutes(b.end) and _minutes(a.end) > _minutes(b.start):
                out.append((a, b))
    return out


def parse_slots(raw_slots: Any) -> list[StudySlot]:
    """把 config.toml 里的 [[study.slots]] 解析成 StudySlot。

    一条写坏了就整体报错：宁可让人改配置，也不要静默丢掉某个时段 ——
    那会让日历上少几节课而没人发现。
    """
    if raw_slots in (None, ""):
        return []
    if not isinstance(raw_slots, list):
        raise StudyConfigError("study.slots 应该是数组表（用 [[study.slots]] 写）。")

    slots: list[StudySlot] = []
    for i, raw in enumerate(raw_slots, 1):
        if not isinstance(raw, dict):
            raise StudyConfigError(f"第 {i} 条自习时段不是表结构。")
        where = f"第 {i} 条自习时段"

        title = str(raw.get("title") or "").strip()
        if not title:
            raise StudyConfigError(f"{where}缺少 title（显示在日历上的名字，如「晚自习」）。")

        days = parse_days(raw.get("days"), where=where)

        start = _hhmm(raw.get("start"), f"{where}的 start")
        end = _hhmm(raw.get("end"), f"{where}的 end")
        if _minutes(end) <= _minutes(start):
            raise StudyConfigError(
                f"{where}的结束时间 {end} 不晚于开始时间 {start}，日历会显示成 0 长度。"
            )

        parity = str(raw.get("parity") or "all").strip().lower()
        if parity not in PARITY_VALUES:
            raise StudyConfigError(
                f"{where}的 parity 只能是 {'/'.join(PARITY_VALUES)}，现在是 {parity!r}。"
            )

        date_from = _parse_date(raw.get("from"), f"{where}的 from")
        date_to = _parse_date(raw.get("to"), f"{where}的 to")
        if date_from and date_to and date_to < date_from:
            raise StudyConfigError(
                f"{where}的 from（{date_from}）晚于 to（{date_to}），这样永远展开不出事件。"
            )

        slots.append(
            StudySlot(
                title=title,
                days=days,
                start=start,
                end=end,
                location=str(raw.get("location") or "").strip(),
                parity=parity,
                date_from=date_from,
                date_to=date_to,
                skip_dates=_parse_date_list(raw.get("skip"), f"{where}的 skip"),
            )
        )
    return slots


# 简写没写 days 时的默认值：周日到周四在校自习，周五周六回家
DEFAULT_DAYS = ["sun", "mon", "tue", "wed", "thu"]

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def day_names(days: Iterable[int]) -> list[str]:
    """[0, 6] -> ["mon", "sun"]，写回配置时用英文键。"""
    return [WEEKDAY_KEYS[i] for i in sorted(set(days))]


def describe_weekdays(days: Iterable[int]) -> str:
    """已解析的 weekday()（0=周一）-> 「周一、周三」。

    注意别和 describe_days 混用：那个吃的是配置原文（1=周一），把解析结果再喂
    进去会整体错位一天。
    """
    return "、".join(f"周{'一二三四五六日'[i]}" for i in sorted(set(days)))


def describe_days(days: Any) -> str:
    """配置原文 -> 中文摘要（给交互提示用）。"""
    try:
        parsed = parse_days(days)
    except StudyConfigError:
        return ""
    return describe_weekdays(parsed)


def effective_slots(cfg) -> list[dict]:
    """把配置整理成 slots 列表。

    简写字段（title/days/start/end/location）当作第一条，``slots`` 里的追加在后面。
    这样用户只填一次「星期 + 时间」就够，想细分再写 [[study.slots]]。
    """
    st = cfg.study
    raw: list[dict] = []

    start = str(st.start or "").strip()
    end = str(st.end or "").strip()
    if start or end:
        if not (start and end):
            # 半填说明用户在改配置时被打断了。静默丢掉会让人以为在同步，实际没有。
            raise StudyConfigError(
                "简写的自习时段只填了一半：start 和 end 要一起给"
                f"（现在 start={start!r}、end={end!r}）；不想排自习就把这两行删掉。"
            )
        days = DEFAULT_DAYS if st.days is None else st.days
        if isinstance(days, (list, tuple)) and not days:
            raise StudyConfigError(
                "简写时段的 days 是空列表：要么写周几（如 \"mon\",\"wed\"），要么整段删掉。"
            )
        flat: dict[str, Any] = {
            "title": str(st.title or "").strip() or "晚自习",
            "days": days,
            "start": start,
            "end": end,
        }
        if str(st.location or "").strip():
            flat["location"] = st.location
        raw.append(flat)

    raw.extend(st.slots or [])
    return raw


def load_slots(cfg) -> list[StudySlot]:
    """按配置取自习时段。未启用时返回空列表。

    注意：enabled=false 时不解析配置，所以写坏的配置要等到打开时才会报错 ——
    这是有意的，关掉自习不该被一条陈旧配置卡住。
    """
    if not cfg.study.enabled:
        return []
    return parse_slots(effective_slots(cfg))


def parse_time_range(text: Any) -> tuple[str, str]:
    """把 "18:00-21:00" 这类写法拆成 (开始, 结束)。

    setup 交互和 `study add --time` 用它，省得分别敲 --start / --end。
    """
    raw = str(text or "").strip()
    if not raw:
        raise StudyConfigError("时间段是空的，应该像 18:00-21:00 这样写。")

    for sep in ("-", "~", "～", "—", "–", "到", "至"):
        if sep in raw:
            left, _, right = raw.partition(sep)
            return _hhmm(left, "时间段开始"), _hhmm(right, "时间段结束")

    raise StudyConfigError(
        f"时间段 {raw!r} 认不出来，应该像 18:00-21:00 这样用 - 或 ~ 连接。"
    )


def parse_days(value: Any, where: str = "自习时段") -> tuple[int, ...]:
    """解析 days 字段。接受列表或逗号分隔字符串。"""
    if value in (None, ""):
        raise StudyConfigError(f"{where}缺少 days（如 [\"mon\", \"wed\"] 或 [1, 3]）。")

    items: list[Any]
    if isinstance(value, str):
        text = value
        for sep in ("，", "、", "/", " "):
            text = text.replace(sep, ",")
        items = [p.strip() for p in text.split(",") if p.strip()]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        raise StudyConfigError(
            f"{where}的 days 要写成列表（如 [1, 3] 或 [\"mon\", \"wed\"]），"
            f"现在是个单独的值 {value!r}。"
        )
    else:
        raise StudyConfigError(f"{where}的 days 应该是列表或逗号分隔的字符串。")

    if not items:
        raise StudyConfigError(f"{where}的 days 是空的。")

    out: set[int] = set()
    for item in items:
        out.add(_one_day(item, where))
    return tuple(sorted(out))


def _one_day(item: Any, where: str) -> int:
    if isinstance(item, bool):  # bool 是 int 的子类，先挡掉
        raise StudyConfigError(f"{where}的 days 里 {item!r} 不是星期。")

    if isinstance(item, int):
        if item == 0 or item == 7:
            return 6  # 0 / 7 都当周日
        if 1 <= item <= 6:
            return item - 1
        raise StudyConfigError(
            f"{where}的 days 里 {item} 不是星期（用 1-7 或 0 表示周日）。"
        )

    text = str(item).strip().lower()
    if text.isdigit():
        try:
            return _one_day(int(text), where)
        except ValueError:  # '²' 这类 isdigit 为真但 int() 不认
            raise StudyConfigError(f"{where}的 days 里 {item!r} 不是星期。") from None

    for prefix in ("周", "星期", "礼拜"):
        if text.startswith(prefix):
            text = text[len(prefix) :]

    if text in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES[text]
    raise StudyConfigError(
        f"{where}的 days 里 {item!r} 认不出来（支持 mon..sun、周一..周日、1..7）。"
    )


_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{1,2})$")


def _hhmm(value: Any, label: str) -> str:
    """把 "8:05" / "18:00" 规范化成 "08:05" / "18:00"。

    多段的（"18:00:30"）直接拒绝，而不是悄悄截断成"18:00" —— 截断会让用户以为
    自己写的时间被接受了。
    """
    if value in (None, ""):
        raise StudyConfigError(f"{label}缺失（形如 \"18:00\"）。")
    text = str(value).strip()
    m = _HHMM_RE.match(text)
    if not m:
        raise StudyConfigError(f"{label} 格式不对（{text!r}），应该是 HH:MM。")
    h, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= minute <= 59):
        raise StudyConfigError(f"{label} 超出范围：{text!r}。")
    return f"{h:02d}:{minute:02d}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _parse_date(value: Any, label: str) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        y, m, d = text.replace("/", "-").split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        raise StudyConfigError(f"{label} 不是合法日期：{text!r}（用 YYYY-MM-DD）。") from None


def _parse_date_list(value: Any, label: str) -> tuple[date, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        # 单个字符串里可能写了多天：2026-10-01,2026-10-02
        text = str(value)
        for sep in ("，", "、", ";", "；"):
            text = text.replace(sep, ",")
        items = [p.strip() for p in text.split(",") if p.strip()]
    dates = []
    for item in items:
        if item in (None, ""):
            continue
        parsed = _parse_date(item, label)
        if parsed is not None:
            dates.append(parsed)
    return tuple(dates)
