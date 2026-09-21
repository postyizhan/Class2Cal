"""对账与安全闸测试。

这个文件是本项目最重要的测试：日历里有用户手工录入的事件，误删是不可逆的。
四道闸各自都有对应用例：
  1. 非空守卫      test_empty_guard_*
  2. 未抓取周免疫  test_unfetched_week_is_untouched
  3. 前缀隔离      test_manual_events_are_protected / test_apply_refuses_*
  4. dry-run       test_dry_run_changes_nothing
"""

from datetime import date, datetime

import pytest

from class2cal.calendar_sync import CalendarSyncer, build_event, is_owned
from class2cal.config import CalendarConfig, Config, ConfigError
from class2cal.schedule import Lesson
from class2cal.state import SyncState

MONDAY = date(2026, 9, 21)


# ---------- 测试替身 ----------


class _FakeDt:
    def __init__(self, hhmm: str):
        h, m = hhmm.split(":")
        self.dt = datetime(2026, 9, 21, int(h), int(m))


class FakeEvent:
    """CalDAV 事件替身，记录被调用过什么。"""

    def __init__(
        self,
        uid,
        summary="",
        start="08:20",
        end="09:50",
        location="",
        description="",
    ):
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


def make_syncer(events=None) -> tuple[CalendarSyncer, FakeCalendar]:
    cfg = Config(calendar=CalendarConfig(apple_id="someone@example.com"))
    syncer = CalendarSyncer(cfg)
    fake = FakeCalendar(events)
    # 绕开真实 CalDAV 连接
    syncer._calendar = fake
    return syncer, fake


def lesson(course="高等数学", start="08:20", end="09:50", room="", teacher="", day=MONDAY):
    return Lesson(date=day, start=start, end=end, course=course, room=room, teacher=teacher)


# ---------- 闸 3：前缀隔离 ----------


def test_is_owned_only_accepts_prefix():
    assert is_owned("c2c.20260921.0820.ab12cd34@class2cal")
    assert not is_owned("ABCD-1234-NOT-OURS")
    assert not is_owned("")
    assert not is_owned(None)


def test_manual_events_are_protected():
    """用户手录的事件既不参与对账，也不会被删。"""
    manual = FakeEvent("ABCD-1234-NOT-OURS", summary="网络安全课")
    syncer, _ = make_syncer([manual])

    plan = syncer.plan_week(MONDAY, [lesson()], SyncState())

    assert plan.protected == 1
    assert not plan.to_delete  # 手录事件绝不进删除列表
    assert len(plan.to_add) == 1


def test_manual_event_with_same_course_is_not_touched():
    """手录的「高等数学」和抓回来的同名课并存 —— 工具只管自己那份。"""
    le = lesson(course="高等数学")
    manual = FakeEvent("MANUAL-UID", summary="高等数学", start="08:20", end="09:50")
    syncer, _ = make_syncer([manual])

    plan = syncer.plan_week(MONDAY, [le], SyncState())

    assert plan.protected == 1
    assert [x.course for x in plan.to_add] == ["高等数学"]
    assert not plan.to_delete


def test_apply_refuses_to_delete_unowned_uid():
    """即便计划里混入了非工具 UID，落地时也要再挡一次。"""
    syncer, _ = make_syncer()
    rogue = FakeEvent("NOT-OURS")
    plan = syncer.plan_week(MONDAY, [], SyncState())
    plan.to_delete.append(("NOT-OURS", rogue))

    syncer.apply_week(plan)

    assert rogue.deleted is False


# ---------- 增 / 改 / 删 三路 ----------


def test_add_new_lesson():
    syncer, fake = make_syncer([])
    plan = syncer.plan_week(MONDAY, [lesson()], SyncState())
    assert len(plan.to_add) == 1

    syncer.apply_week(plan)
    assert len(fake.created) == 1
    assert "高等数学" in fake.created[0]


def test_update_when_room_changes():
    """改教室应判为更新，而不是新增一条重复事件。"""
    le = lesson(room="B203")
    existing = FakeEvent(le.uid, summary="高等数学", location="A101")
    syncer, fake = make_syncer([existing])

    plan = syncer.plan_week(MONDAY, [le], SyncState())

    assert not plan.to_add
    assert len(plan.to_update) == 1
    syncer.apply_week(plan)
    assert existing.saved is True
    assert not fake.created


def test_no_change_is_noop():
    """字段全一致时不该产生任何写操作。"""
    le = lesson(room="A101")
    existing = FakeEvent(le.uid, summary="高等数学", location="A101")
    syncer, fake = make_syncer([existing])

    plan = syncer.plan_week(MONDAY, [le], SyncState())

    assert plan.is_noop
    syncer.apply_week(plan)
    assert not fake.created
    assert existing.saved is False


def test_delete_cancelled_lesson():
    """学校撤掉的课要删 —— 但前提是该周确实抓到了别的课。"""
    stale = FakeEvent("c2c.20260921.0820.deadbeef@class2cal", summary="已取消的课")
    kept = lesson(course="英语", start="15:25", end="16:55")
    syncer, _ = make_syncer([stale])

    state = SyncState()
    state.update_week(MONDAY, ["c2c.20260921.0820.deadbeef@class2cal"])
    plan = syncer.plan_week(MONDAY, [kept], state)

    assert len(plan.to_delete) == 1
    syncer.apply_week(plan)
    assert stale.deleted is True


# ---------- 闸 1：非空守卫 ----------


def test_empty_guard_blocks_deletion_when_fetch_returns_nothing():
    """原本有课、这次抓到 0 条 —— 判为异常，一个都不删。"""
    existing = FakeEvent("c2c.20260921.0820.ab12cd34@class2cal", summary="高等数学")
    syncer, _ = make_syncer([existing])

    state = SyncState()
    state.update_week(MONDAY, ["c2c.20260921.0820.ab12cd34@class2cal"])

    plan = syncer.plan_week(MONDAY, [], state)

    assert plan.was_skipped
    assert "0 条" in plan.skipped_reason
    assert not plan.to_delete

    syncer.apply_week(plan)
    assert existing.deleted is False


def test_empty_guard_allows_genuinely_empty_new_week():
    """从没同步过的周本来就是空的，不该触发守卫。"""
    syncer, _ = make_syncer([])
    plan = syncer.plan_week(MONDAY, [], SyncState())

    assert not plan.was_skipped
    assert plan.is_noop


def test_empty_guard_does_not_block_partial_data():
    """抓到的比上次少但非零 —— 属正常调课，照常对账。"""
    old = FakeEvent("c2c.20260921.0820.deadbeef@class2cal", summary="旧课")
    syncer, _ = make_syncer([old])

    state = SyncState()
    state.update_week(MONDAY, ["c2c.20260921.0820.deadbeef@class2cal", "c2c.x", "c2c.y"])

    plan = syncer.plan_week(MONDAY, [lesson()], state)

    assert not plan.was_skipped
    assert len(plan.to_delete) == 1


# ---------- 闸 2：未抓取周免疫 ----------


def test_unfetched_week_is_untouched():
    """没抓到的周不进 weekly_lessons，自然不该被对账。"""
    syncer, _ = make_syncer([])
    other_monday = date(2026, 9, 28)

    report = syncer.sync({MONDAY: [lesson()]}, SyncState(), dry_run=True)

    weeks_planned = [p.monday for p in report.plans]
    assert weeks_planned == [MONDAY]
    assert other_monday not in weeks_planned


# ---------- 闸 4：dry-run ----------


def test_dry_run_changes_nothing():
    stale = FakeEvent("c2c.20260921.0820.deadbeef@class2cal", summary="待删")
    syncer, fake = make_syncer([stale])

    state = SyncState()
    state.update_week(MONDAY, ["c2c.20260921.0820.deadbeef@class2cal"])

    report = syncer.sync({MONDAY: [lesson()]}, state, dry_run=True)

    assert report.added == 1
    assert report.deleted == 1
    # 但实际上什么都没动
    assert not fake.created
    assert stale.deleted is False
    assert stale.saved is False


def test_apply_updates_state(tmp_path):
    syncer, _ = make_syncer([])
    state = SyncState(path=tmp_path / "state.json")
    le = lesson()

    syncer.sync({MONDAY: [le]}, state, dry_run=False)

    rec = state.week_record(MONDAY)
    assert rec.count == 1
    assert rec.uids == [le.uid]
    assert rec.synced_at
    assert (tmp_path / "state.json").exists()


# ---------- 事件构造 ----------


def test_build_event_has_no_alarm():
    """用户明确要求不加提醒 —— 生成的事件里不能有 VALARM。"""
    ical = build_event(lesson(room="A101", teacher="张老师")).decode("utf-8")
    assert "VALARM" not in ical
    assert "BEGIN:VEVENT" in ical
    assert "高等数学" in ical
    assert "A101" in ical
    assert "张老师" in ical


def test_build_event_marks_estimated_time():
    """节次推算出来的时间要在备注里提醒用户核对。"""
    le = Lesson(MONDAY, "15:25", "16:55", "计算机应用基础", source="period")
    ical = build_event(le).decode("utf-8")
    assert "推算" in ical


def test_build_event_uid_matches_lesson():
    le = lesson()
    ical = build_event(le).decode("utf-8")
    assert le.uid in ical


# ---------- 状态持久化 ----------


def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = SyncState(path=path)
    state.update_week(MONDAY, ["c2c.a", "c2c.b"])
    state.save()

    reloaded = SyncState.load(path)
    assert reloaded.week_record(MONDAY).count == 2
    assert reloaded.week_record(MONDAY).uids == ["c2c.a", "c2c.b"]


def test_state_load_survives_corrupt_file(tmp_path):
    """状态文件坏了不该让同步崩掉；退回空状态会让守卫更保守。"""
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    assert SyncState.load(path).weeks == {}


def test_state_prune():
    state = SyncState()
    state.update_week(date(2026, 8, 3), ["c2c.old"])
    state.update_week(MONDAY, ["c2c.new"])

    removed = state.prune(before=date(2026, 9, 1))

    assert removed == 1
    assert state.week_record(date(2026, 8, 3)).count == 0
    assert state.week_record(MONDAY).count == 1


def test_missing_apple_credentials_raises():
    """没配 Apple ID 时要明确报错，而不是默默连不上。"""
    syncer = CalendarSyncer(Config())
    with pytest.raises(ConfigError):
        syncer.connect()
