"""自习时段测试。

学校教务系统只排正课，晚自习靠本地配置补。重点覆盖：
  1. 配置写得不对时要报错并指到字段，不能静默丢掉时段
  2. 按星期/单双周/日期范围/跳过日期正确展开
  3. 自习事件与正课共用对账流程，但绝不能削弱「非空守卫」——
     课表抓取失败时，自习不能让日历上的正课被当成「已取消」删掉
"""

from datetime import date, datetime

import pytest

from class2cal.calendar_sync import CalendarSyncer, build_event
from class2cal.config import CalendarConfig, Config, StudyConfig
from class2cal.schedule import STUDY_KIND, Lesson
from class2cal.state import SyncState
from class2cal.study import (
    DEFAULT_DAYS,
    StudyConfigError,
    StudySlot,
    effective_slots,
    lessons_for_week,
    parse_days,
    parse_slots,
    parse_time_range,
)

MONDAY = date(2026, 9, 21)


def slot(**kwargs) -> dict:
    base = {"title": "晚自习", "days": ["mon", "wed"], "start": "18:00", "end": "21:00"}
    base.update(kwargs)
    return base


# ---------- days 解析 ----------


@pytest.mark.parametrize(
    "value,expected",
    [
        (["mon", "wed"], (0, 2)),
        (["Mon", "WED"], (0, 2)),
        (["monday"], (0,)),
        (["周一", "周三"], (0, 2)),
        (["星期一"], (0,)),
        ([1, 3], (0, 2)),
        ([7], (6,)),  # 7 也当周日
        ([0], (6,)),
        (["1", "3"], (0, 2)),
        ("mon,tue,thu", (0, 1, 3)),
        ("周一，周三", (0, 2)),
        ([2, 2, 2], (1,)),  # 去重
    ],
)
def test_parse_days(value, expected):
    assert parse_days(value) == expected


@pytest.mark.parametrize("value", [None, "", [], ["放假"], [8], [-1], 3])
def test_parse_days_rejects_bad_values(value):
    with pytest.raises(StudyConfigError):
        parse_days(value)


# ---------- 配置校验 ----------


def test_parse_slots_ok():
    slots = parse_slots([slot(location="图书馆", parity="odd")])
    assert len(slots) == 1
    s = slots[0]
    assert (s.title, s.start, s.end, s.location, s.parity) == (
        "晚自习",
        "18:00",
        "21:00",
        "图书馆",
        "odd",
    )
    assert s.days == (0, 2)


def test_parse_slots_normalizes_time():
    s = parse_slots([slot(start="8:05", end="21:00")])[0]
    assert (s.start, s.end) == ("08:05", "21:00")


@pytest.mark.parametrize(
    "bad,fragment",
    [
        (slot(title=""), "title"),
        (slot(days=[]), "days"),
        (slot(start=""), "start"),
        (slot(end="18:00", start="21:00"), "不晚于开始"),
        (slot(end="18:00"), "不晚于开始"),
        (slot(start="25:00"), "超出范围"),
        (slot(start="晚上"), "格式不对"),
        (slot(parity="weekly"), "parity"),
        (slot(**{"from": "2026-13-01"}), "不是合法日期"),
        (slot(skip=["2026/9/1x"]), "不是合法日期"),
    ],
)
def test_parse_slots_rejects_bad_config(bad, fragment):
    with pytest.raises(StudyConfigError) as exc:
        parse_slots([bad])
    assert fragment in str(exc.value)


def test_parse_slots_empty():
    assert parse_slots(None) == []
    assert parse_slots([]) == []


def test_parse_slots_requires_array():
    with pytest.raises(StudyConfigError):
        parse_slots({"title": "晚自习"})


# ---------- 时间段简写 ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("18:00-21:00", ("18:00", "21:00")),
        ("18:00~21:00", ("18:00", "21:00")),
        ("18:00～21:00", ("18:00", "21:00")),
        ("18:00 到 21:00", ("18:00", "21:00")),
        ("8:00-9:30", ("08:00", "09:30")),
    ],
)
def test_parse_time_range(text, expected):
    assert parse_time_range(text) == expected


@pytest.mark.parametrize("text", ["", "18:00", "abc-def", "18:00-"])
def test_parse_time_range_rejects_bad(text):
    with pytest.raises(StudyConfigError):
        parse_time_range(text)


# ---------- 简写配置 ----------


def test_effective_slots_uses_flat_fields():
    """setup 里只填一次「星期 + 时间」，就该等价于一条时段。"""
    cfg = Config(
        study=StudyConfig(enabled=True, title="晚自习", days=["mon", "wed"], start="18:00", end="21:00")
    )
    raw = effective_slots(cfg)
    assert len(raw) == 1
    assert raw[0] == {"title": "晚自习", "days": ["mon", "wed"], "start": "18:00", "end": "21:00"}

    lessons = lessons_for_week(parse_slots(raw), MONDAY)
    assert [le.date for le in lessons] == [date(2026, 9, 21), date(2026, 9, 23)]


def test_effective_slots_flat_defaults_days():
    cfg = Config(study=StudyConfig(enabled=True, start="18:00", end="21:00"))
    raw = effective_slots(cfg)
    assert raw[0]["days"] == DEFAULT_DAYS
    assert raw[0]["title"] == "晚自习"


def test_effective_slots_rejects_half_filled_flat():
    """只填了一半就报错：静默丢掉会让人以为在同步，实际一条都没写。"""
    cfg = Config(study=StudyConfig(enabled=True, start="18:00"))
    with pytest.raises(StudyConfigError) as exc:
        effective_slots(cfg)
    assert "只填了一半" in str(exc.value)


def test_effective_slots_rejects_explicit_empty_days():
    """days = [] 是显式写空，不能偷偷替换成默认星期。"""
    cfg = Config(study=StudyConfig(enabled=True, days=[], start="18:00", end="21:00"))
    with pytest.raises(StudyConfigError) as exc:
        effective_slots(cfg)
    assert "空列表" in str(exc.value)


def test_effective_slots_flat_then_slots():
    cfg = Config(
        study=StudyConfig(
            enabled=True,
            start="18:00",
            end="21:00",
            slots=[slot(title="早自习", start="07:30", end="08:00")],
        )
    )
    raw = effective_slots(cfg)
    assert [r["title"] for r in raw] == ["晚自习", "早自习"]


# ---------- 有正课就不排自习 ----------


def test_study_skipped_when_school_class_overlaps():
    """晚自习 18:00-21:00 撞上 9-10 节（18:00-19:25）—— 那天上课，不排自习。"""
    slots = parse_slots([slot(days=["mon"], start="18:00", end="21:00")])
    busy = [Lesson(MONDAY, "18:00", "19:25", "思想道德与法治")]

    assert lessons_for_week(slots, MONDAY, occupied=busy) == []


def test_study_kept_when_school_class_is_outside():
    """正课在下午，晚自习照排。"""
    slots = parse_slots([slot(days=["mon"], start="18:00", end="21:00")])
    busy = [Lesson(MONDAY, "15:25", "16:50", "高等数学")]

    assert len(lessons_for_week(slots, MONDAY, occupied=busy)) == 1


def test_study_skipped_on_partial_overlap():
    """哪怕只压到一小时，也算有正课。"""
    slots = parse_slots([slot(days=["mon"], start="18:00", end="21:00")])
    busy = [Lesson(MONDAY, "17:00", "18:40", "实验课")]

    assert lessons_for_week(slots, MONDAY, occupied=busy) == []


def test_conflict_only_affects_that_day():
    slots = parse_slots([slot(days=["mon", "wed"], start="18:00", end="21:00")])
    tuesday_class = [Lesson(date(2026, 9, 23), "18:00", "19:25", "晚课")]

    lessons = lessons_for_week(slots, MONDAY, occupied=tuesday_class)
    assert [le.date for le in lessons] == [date(2026, 9, 21)]


def test_merge_study_drops_conflicting_day():
    """端到端：把正课和自习合进同一周时，撞车的晚自习不出现。"""
    from class2cal.cli import _merge_study

    cfg = Config(study=StudyConfig(enabled=True, days=["mon", "wed"], start="18:00", end="21:00"))
    weekly = {
        MONDAY: [Lesson(MONDAY, "18:00", "19:25", "思想道德与法治")],
    }
    merged = _merge_study(cfg, weekly)

    # 周一的晚自习被正课顶掉，周三那天照排
    assert [le.date for le in merged[MONDAY] if le.is_study] == [date(2026, 9, 23)]
    assert [le.course for le in merged[MONDAY] if not le.is_study] == ["思想道德与法治"]


def test_merge_study_keeps_free_day():
    from class2cal.cli import _merge_study

    cfg = Config(study=StudyConfig(enabled=True, days=["mon", "wed"], start="18:00", end="21:00"))
    weekly = {
        MONDAY: [Lesson(MONDAY, "08:20", "09:45", "高等数学")],
    }
    merged = _merge_study(cfg, weekly)

    # 上午正课不挡晚自习：周一、周三都排上
    assert [le.date for le in merged[MONDAY] if le.is_study] == [
        date(2026, 9, 21),
        date(2026, 9, 23),
    ]


# ---------- 按周展开 ----------


def test_lessons_for_week_hits_configured_days():
    slots = parse_slots([slot(days=["mon", "wed", "sun"])])
    lessons = lessons_for_week(slots, MONDAY)
    assert [le.date for le in lessons] == [
        date(2026, 9, 21),  # 周一
        date(2026, 9, 23),  # 周三
        date(2026, 9, 27),  # 周日
    ]
    assert all(le.kind == STUDY_KIND for le in lessons)
    assert all((le.start, le.end) == ("18:00", "21:00") for le in lessons)


def test_lessons_for_week_skips_listed_dates():
    slots = parse_slots([slot(days=["mon", "wed"], skip=["2026-09-23"])])
    assert [le.date for le in lessons_for_week(slots, MONDAY)] == [date(2026, 9, 21)]


def test_lessons_for_week_respects_date_range():
    slots = parse_slots(
        [slot(days=["mon", "wed"], **{"from": "2026-09-23", "to": "2026-09-25"})]
    )
    assert [le.date for le in lessons_for_week(slots, MONDAY)] == [date(2026, 9, 23)]


def test_parity_odd_and_even_pick_alternate_weeks():
    """按 ISO 周次单双周：2026-09-21 是第 39 周（奇）。"""
    odd = parse_slots([slot(days=["mon"], parity="odd")])
    even = parse_slots([slot(days=["mon"], parity="even")])

    assert lessons_for_week(odd, date(2026, 9, 21))
    assert not lessons_for_week(even, date(2026, 9, 21))
    assert not lessons_for_week(odd, date(2026, 9, 28))
    assert lessons_for_week(even, date(2026, 9, 28))


def test_multiple_slots_are_merged_and_sorted():
    slots = parse_slots(
        [
            slot(title="晚自习", days=["mon"], start="18:00", end="21:00"),
            slot(title="早自习", days=["mon"], start="07:30", end="08:00"),
        ]
    )
    lessons = lessons_for_week(slots, MONDAY)
    assert [le.course for le in lessons] == ["早自习", "晚自习"]


# ---------- UID 与事件 ----------


def test_study_uid_is_separate_namespace():
    normal = Lesson(MONDAY, "18:00", "21:00", "晚自习")
    study = Lesson(MONDAY, "18:00", "21:00", "晚自习", kind=STUDY_KIND)

    assert study.is_study and not normal.is_study
    assert study.uid.startswith("c2c.study.")
    assert normal.uid.startswith("c2c.") and not normal.uid.startswith("c2c.study.")
    # 名字一样也要分开：正课和自习各自对账
    assert study.uid != normal.uid


def test_study_uid_stable_across_location_change():
    a = Lesson(MONDAY, "18:00", "21:00", "晚自习", kind=STUDY_KIND)
    b = Lesson(MONDAY, "18:00", "21:00", "晚自习", room="图书馆", kind=STUDY_KIND)
    assert a.uid == b.uid
    assert a.fingerprint != b.fingerprint


def test_build_event_marks_study_source():
    le = Lesson(MONDAY, "18:00", "21:00", "晚自习", room="图书馆", kind=STUDY_KIND)
    ical = build_event(le).decode("utf-8")
    assert "晚自习" in ical
    assert "本地配置" in ical
    assert "VALARM" not in ical


# ---------- 与对账流程的配合 ----------


class _FakeDt:
    def __init__(self, hhmm: str):
        h, m = hhmm.split(":")
        self.dt = datetime(2026, 9, 21, int(h), int(m))


class FakeEvent:
    def __init__(self, uid, summary="", start="18:00", end="21:00", location="", description=""):
        self._uid = uid
        self.saved = False
        self.deleted = False
        self.data = ""
        self._comp = {
            "uid": uid,
            "summary": summary,
            "location": location,
            "description": description,
            "dtstart": _FakeDt(start),
            "dtend": _FakeDt(end),
        }

    @property
    def icalendar_component(self):
        return self._comp

    def save(self):
        self.saved = True

    def delete(self):
        self.deleted = True


class FakeCalendar:
    def __init__(self, events=None):
        self._events = events or []
        self.created: list[str] = []

    def search(self, **kwargs):
        return list(self._events)

    def save_event(self, ical: str):
        self.created.append(ical)


def make_syncer(events=None):
    cfg = Config(calendar=CalendarConfig(apple_id="someone@example.com"))
    syncer = CalendarSyncer(cfg)
    fake = FakeCalendar(events)
    syncer._calendar = fake
    return syncer, fake


def school(day=MONDAY, course="高等数学", start="08:20", end="09:50"):
    return Lesson(day, start, end, course)


def study(day=MONDAY, title="晚自习", start="18:00", end="21:00", room=""):
    return Lesson(day, start, end, title, room=room, kind=STUDY_KIND)


def test_study_events_are_added_alongside_lessons():
    syncer, fake = make_syncer([])
    plan = syncer.plan_week(MONDAY, [school(), study()], SyncState())

    assert len(plan.to_add) == 2
    syncer.apply_week(plan)
    assert len(fake.created) == 2


def test_study_alone_does_not_disable_empty_guard():
    """关键回归：课表抓取失败时只剩自习，正课必须被守卫保护，不能删。"""
    existing = FakeEvent("c2c.20260921.0820.ab12cd34@class2cal", summary="高等数学", start="08:20")
    syncer, fake = make_syncer([existing])

    state = SyncState()
    state.update_week(MONDAY, ["c2c.20260921.0820.ab12cd34@class2cal"])

    plan = syncer.plan_week(MONDAY, [study()], state)

    assert plan.was_skipped
    assert not plan.to_delete
    syncer.apply_week(plan)
    assert existing.deleted is False
    assert not fake.created  # 自习也不写：这周整体状态可疑，先不动


def test_disabled_study_events_are_cleaned_up():
    """关掉自习后，日历里旧的自动生自习事件应被对账删掉。"""
    stale = FakeEvent(study().uid, summary="晚自习")
    syncer, _ = make_syncer([stale])

    plan = syncer.plan_week(MONDAY, [school()], SyncState())

    assert [uid for uid, _ in plan.to_delete] == [study().uid]
    syncer.apply_week(plan)
    assert stale.deleted is True


def test_state_records_only_school_lessons(tmp_path):
    """状态里的节数必须是「学校抓到几节」，否则非空守卫会被自习灌水。"""
    syncer, _ = make_syncer([])
    state = SyncState(path=tmp_path / "state.json")

    state_lessons = [school(), study()]
    syncer.sync({MONDAY: state_lessons}, state, dry_run=False)
    rec = state.week_record(MONDAY)
    assert rec.count == 1
    assert rec.uids == [school().uid]


def test_study_study_slot_week_expansion_into_sync(tmp_path):
    """端到端：配置 -> 会话 -> Lesson -> 对账计划。"""
    slots = parse_slots([slot(days=["mon"])])
    lessons = lessons_for_week(slots, MONDAY)

    syncer, _ = make_syncer([])
    plan = syncer.plan_week(MONDAY, lessons, SyncState())
    assert len(plan.to_add) == 1
    assert plan.to_add[0].uid.startswith("c2c.study.")


# ---------- 配置往返 ----------


def test_config_roundtrip_keeps_study(tmp_path, monkeypatch):
    from class2cal import config as cfg_mod

    target = tmp_path / "config.toml"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", target)

    cfg = Config(
        study=StudyConfig(
            enabled=True,
            slots=[slot(parity="odd", skip=["2026-10-01"])],
        )
    )
    cfg_mod.save(cfg)
    loaded = cfg_mod.load()

    assert loaded.study.enabled is True
    assert loaded.study.slots[0]["title"] == "晚自习"
    assert loaded.study.slots[0]["parity"] == "odd"
    assert loaded.study.slots[0]["skip"] == ["2026-10-01"]


def test_disabled_by_default():
    assert Config().study.enabled is False
