"""离线回归：上海周窗口、旧 floating 事件迁移，以及真实调课的增删语义。

只用合成 Lesson 和内存 iCalendar，不读取 var/，不连接门户或 CalDAV。
搜索替身使用 CalDAV 库实际的 UTC 边界序列化，按 UTC 索引旧 floating 时间。
"""

import os
import socket
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from caldav.elements.cdav import TimeRange
from icalendar import Calendar, Event

from class2cal import cli
from class2cal.calendar_sync import CalendarSyncer, build_event
from class2cal.config import Config, StudyConfig
from class2cal.schedule import STUDY_KIND, Lesson
from class2cal.state import SyncState

SHANGHAI = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 11, 23)
SUNDAYS = [date(2026, 11, day) for day in (15, 22, 29)]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("时区回归测试不得联网")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def host_timezone(monkeypatch):
    previous = os.environ.get("TZ")

    def set_timezone(zone):
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield set_timezone
    if previous is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", previous)
    time.tzset()


def study(day):
    return Lesson(day, "18:00", "19:25", "晚自习", kind=STUDY_KIND)


class MemoryEvent:
    def __init__(self, data):
        self.data = data
        self.saved = False
        self.deleted = False

    @property
    def icalendar_component(self):
        return Calendar.from_ical(self.data).walk("VEVENT")[0]

    def save(self):
        self.saved = True

    def delete(self):
        self.deleted = True


def stored_event(lesson, *, floating=False, zone=SHANGHAI, uid=None):
    """独立构造旧/新存量，不能调用待测的 build_event。"""
    component = Event()
    component.add("uid", uid or lesson.uid)
    component.add("summary", lesson.course)
    component.add("location", lesson.room)
    component.add("description", f"教师：{lesson.teacher}" if lesson.teacher else "")
    for key, hhmm in (("dtstart", lesson.start), ("dtend", lesson.end)):
        value = datetime.fromisoformat(f"{lesson.date}T{hhmm}:00")
        if not floating:
            value = value.replace(tzinfo=SHANGHAI).astimezone(zone)
        component.add(key, value)
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add_component(component)
    return MemoryEvent(calendar.to_ical().decode())


class MemoryCalendar:
    def __init__(self, events=(), floating_zone=timezone.utc):
        self.events = list(events)
        self.floating_zone = floating_zone
        self.queries = []
        self.created = []

    def search(self, **kwargs):
        self.queries.append(kwargs)
        bounds = TimeRange(kwargs["start"], kwargs["end"]).attributes
        start, end = (
            datetime.strptime(bounds[key], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            for key in ("start", "end")
        )

        def indexed(value):
            if value.tzinfo is None:
                value = value.replace(tzinfo=self.floating_zone)
            return value.astimezone(timezone.utc)

        return [
            event
            for event in self.events
            if not event.deleted
            and indexed(event.icalendar_component["dtstart"].dt) < end
            and indexed(event.icalendar_component["dtend"].dt) > start
        ]

    def save_event(self, data):
        self.created.append(data)
        self.events.append(MemoryEvent(data))


def make_syncer(events=(), *, floating_zone=timezone.utc, cfg=None):
    syncer = CalendarSyncer(cfg or Config())
    calendar = MemoryCalendar(events, floating_zone)
    syncer._calendar = calendar
    return syncer, calendar


@pytest.mark.parametrize("kind", ["lesson", STUDY_KIND])
def test_lesson_datetimes_have_school_timezone(kind):
    lesson = Lesson(date(2026, 11, 29), "18:00", "19:25", "离线课程", kind=kind)
    assert lesson.start_dt == datetime(2026, 11, 29, 18, tzinfo=SHANGHAI)
    assert lesson.end_dt == datetime(2026, 11, 29, 19, 25, tzinfo=SHANGHAI)
    fallback = Lesson(lesson.date, "18:00", "18:00", lesson.course, kind=kind)
    assert fallback.end_dt == datetime(2026, 11, 29, 19, tzinfo=SHANGHAI)


@pytest.mark.parametrize("kind", ["lesson", STUDY_KIND])
def test_build_event_roundtrips_explicit_timezone(kind):
    lesson = Lesson(date(2026, 11, 29), "18:00", "19:25", "离线课程", kind=kind)
    calendar = Calendar.from_ical(build_event(lesson))
    component = calendar.walk("VEVENT")[0]
    assert component["dtstart"].dt == datetime(2026, 11, 29, 18, tzinfo=SHANGHAI)
    assert component["dtend"].dt == datetime(2026, 11, 29, 19, 25, tzinfo=SHANGHAI)
    assert component["dtstamp"].dt.utcoffset() == timedelta(0)
    assert abs(component["dtstamp"].dt - datetime.now(timezone.utc)) < timedelta(minutes=1)
    assert str(component["uid"]) == lesson.uid
    assert not calendar.walk("VALARM")


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
def test_search_boundaries_are_explicit_and_host_independent(host_timezone, zone):
    host_timezone(zone)
    syncer, calendar = make_syncer()
    syncer.plan_week(MONDAY, [], SyncState())

    first = calendar.queries[0]
    assert first["start"] == datetime(2026, 11, 23, tzinfo=SHANGHAI)
    assert first["end"] == datetime(2026, 11, 30, tzinfo=SHANGHAI)
    for query in calendar.queries:
        assert query["start"].utcoffset() == query["end"].utcoffset() == timedelta(hours=8)
        assert query["event"] is True
        assert query["expand"] is False


@pytest.mark.parametrize("sunday", SUNDAYS)
@pytest.mark.parametrize("floating_zone", [timezone.utc, SHANGHAI])
def test_legacy_sunday_is_migrated_in_place_and_never_deleted(sunday, floating_zone):
    lesson = study(sunday)
    old = stored_event(lesson, floating=True)
    syncer, calendar = make_syncer([old], floating_zone=floating_zone)
    monday = sunday - timedelta(days=6)

    own = syncer.plan_week(monday, [lesson], SyncState())
    following = syncer.plan_week(monday + timedelta(days=7), [], SyncState())
    assert not own.to_add
    assert own.to_update == [(lesson, old)]
    assert not own.to_delete
    assert not following.to_delete

    syncer.apply_week(own)  # 内存替身，验证同 UID 原地更新及迁移后的幂等性
    assert old.saved
    assert not calendar.created
    assert str(old.icalendar_component["uid"]) == lesson.uid
    assert old.icalendar_component["dtstart"].dt == lesson.start_dt
    assert old.icalendar_component["dtend"].dt == lesson.end_dt
    assert syncer.plan_week(monday, [lesson], SyncState()).is_noop


def test_cancelled_legacy_sunday_is_found_even_in_last_requested_week():
    old = stored_event(study(date(2026, 11, 29)), floating=True)
    kept = Lesson(MONDAY, "08:20", "09:45", "离线课程")
    syncer, _ = make_syncer([old])

    plan = syncer.plan_week(MONDAY, [kept], SyncState())
    assert plan.to_delete == [(study(date(2026, 11, 29)).uid, old)]


def test_utc_encoded_existing_event_is_compared_in_school_timezone():
    lesson = study(date(2026, 11, 29))
    existing = stored_event(lesson, zone=timezone.utc)
    syncer, _ = make_syncer([existing])
    assert syncer.plan_week(MONDAY, [lesson], SyncState()).is_noop


def test_compatibility_search_does_not_delete_next_week_events():
    next_week = Lesson(date(2026, 11, 30), "00:15", "01:00", "离线课程")
    existing = stored_event(next_week)
    syncer, _ = make_syncer([existing])
    plan = syncer.plan_week(MONDAY, [], SyncState())
    assert plan.is_noop
    assert not existing.deleted


def test_failed_week_is_safe_when_legacy_sunday_leaks_into_successful_week():
    old = stored_event(study(date(2026, 11, 29)), floating=True)
    syncer, _ = make_syncer([old])
    following = MONDAY + timedelta(days=7)
    report = syncer.sync({following: []}, SyncState(), dry_run=True)
    assert [plan.monday for plan in report.plans] == [following]
    assert report.deleted == report.updated == 0
    assert not old.saved and not old.deleted


def test_empty_guard_blocks_legacy_migration_and_deletion():
    lesson = study(date(2026, 11, 29))
    old = stored_event(lesson, floating=True)
    syncer, calendar = make_syncer([old])
    state = SyncState()
    state.update_week(MONDAY, ["c2c.previous-school-lesson"])

    plan = syncer.plan_week(MONDAY, [lesson], state)
    assert plan.was_skipped
    assert plan.is_noop
    assert not calendar.queries
    assert not old.saved and not old.deleted


def test_manual_floating_event_in_compatibility_window_is_protected():
    manual = stored_event(study(date(2026, 11, 29)), floating=True, uid="MANUAL-UID")
    syncer, _ = make_syncer([manual])
    plan = syncer.plan_week(MONDAY, [], SyncState())
    assert plan.is_noop
    assert plan.protected == 1
    assert not manual.saved and not manual.deleted


def test_four_week_dry_run_keeps_sundays_and_reschedules_pe(capsys):
    """日期/体育调课取自任务描述，其余字段均为合成数据。"""
    cfg = Config(study=StudyConfig(enabled=True, days=["sun"], start="18:00", end="19:25"))
    first = date(2026, 11, 9)
    moved = Lesson(date(2026, 11, 16), "13:45", "15:10", "体育基础")
    stale = Lesson(date(2026, 11, 13), "10:00", "11:25", "体育基础")
    weekly = {
        first: [Lesson(first, "08:20", "09:45", "离线课程")],
        first + timedelta(days=7): [moved],
        MONDAY: [Lesson(MONDAY, "08:20", "09:45", "离线课程")],
        MONDAY + timedelta(days=7): [],
    }
    weekly = cli._merge_study(cfg, weekly)
    legacy = [stored_event(study(day), floating=True) for day in SUNDAYS]
    stale_event = stored_event(stale, floating=True)
    manual = stored_event(study(SUNDAYS[0]), uid="MANUAL-UID")
    syncer, calendar = make_syncer([*legacy, stale_event, manual], cfg=cfg)

    report = syncer.sync(weekly, SyncState(), dry_run=True)
    added = {lesson.uid for plan in report.plans for lesson in plan.to_add}
    updated = {lesson.uid for plan in report.plans for lesson, _ in plan.to_update}
    deleted = {uid for plan in report.plans for uid, _ in plan.to_delete}
    sunday_uids = {study(day).uid for day in SUNDAYS}
    assert sunday_uids <= updated
    assert not sunday_uids & added
    assert not added & deleted
    assert deleted == {stale.uid}
    assert moved.uid in added
    assert report.protected == 1
    assert not calendar.created
    assert all(not event.saved and not event.deleted for event in calendar.events)

    cli._print_report(report, [], dry_run=True)
    output = capsys.readouterr().out
    assert "~ 更新  2026-11-29 18:00-19:25 晚自习" in output
    assert f"- 删除  {stale.uid}" in output
    assert "+ 新增  2026-11-16 13:45-15:10 体育基础" in output
    assert all(f"- 删除  {uid}" not in output for uid in sunday_uids)
