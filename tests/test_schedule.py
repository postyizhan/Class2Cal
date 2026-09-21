"""课表解析测试。

数据形状取自真实抓包（门户日历卡片 CUS_CARD_CALENDAR_DETAIL 的 renderData）。
重点覆盖：
  1. 直接取 startTime/endTime 的具体时刻 —— 这正是用户要的「移动端显示的时间」
  2. 按日历过滤，别把「假期」等非课程事件导进来
  3. eventDesc 里的节次与教师能正确抽出
"""

from datetime import date

import pytest

from class2cal.schedule import (
    Lesson,
    ParseError,
    group_by_week,
    iter_week_starts,
    parse_event_desc,
    parse_lessons,
    week_range,
    week_start,
)


def response(*events: dict) -> dict:
    """按真实响应结构包装事件。"""
    datas: dict[str, list[dict]] = {}
    for ev in events:
        datas.setdefault(ev["startDate"], []).append(ev)
    return {"errcode": "0", "errmsg": "请求成功", "data": {"datas": datas}}


def event(
    course="高等数学",
    day="2026-09-22",
    start="13:45",
    end="15:10",
    room="善思楼204",
    desc="上课节次：5-6 节， 授课教师：奚敏",
    cal_name="我的课表",
    cal_wid="1046441504596287488",
    event_id="abc123",
) -> dict:
    return {
        "calWid": cal_wid,
        "calName": cal_name,
        "eventId": event_id,
        "eventTitle": course,
        "startDate": day,
        "startTime": f"{day}T{start}:00",
        "endTime": f"{day}T{end}:00",
        "location": room,
        "eventDesc": desc,
    }


# ---------- 时间取值 ----------


def test_takes_concrete_time_from_api():
    """直接用接口给的具体时刻，不做任何推算。"""
    lessons = parse_lessons(response(event()))
    assert len(lessons) == 1
    le = lessons[0]
    assert (le.date, le.start, le.end) == (date(2026, 9, 22), "13:45", "15:10")
    assert le.course == "高等数学"
    assert le.room == "善思楼204"
    assert le.teacher == "奚敏"
    assert le.periods == "5-6"


def test_consecutive_periods_keep_full_span():
    """连堂课的起止由服务端算好（1-4 节 = 08:20-11:25），原样保留。"""
    lessons = parse_lessons(
        response(
            event(
                course="电工电子技术基础",
                day="2026-10-08",
                start="08:20",
                end="11:25",
                room="善学楼304",
                desc="上课节次：1-4 节， 授课教师：王艳",
            )
        )
    )
    le = lessons[0]
    assert (le.start, le.end) == ("08:20", "11:25")
    assert le.periods == "1-4"
    assert le.teacher == "王艳"


def test_missing_end_time_defaults_to_45_minutes():
    ev = event()
    ev.pop("endTime")
    assert parse_lessons(response(ev))[0].end == "14:30"


def test_event_without_title_is_skipped():
    ev = event()
    ev["eventTitle"] = ""
    assert parse_lessons(response(ev)) == []


# ---------- 日历过滤 ----------


def test_filters_out_other_calendars_by_name():
    """「假期」不是课，不能导进来。"""
    lessons = parse_lessons(
        response(
            event(),
            event(course="国庆节", day="2026-10-01", cal_name="假期", cal_wid="3"),
        )
    )
    assert [le.course for le in lessons] == ["高等数学"]


def test_filters_by_cal_wid_when_given():
    """给了 wid 就按 wid 过滤 —— 比名字可靠（名字可能被改）。"""
    lessons = parse_lessons(
        response(
            event(cal_wid="1046441504596287488"),
            event(course="自建日程", day="2026-09-23", cal_wid="9999", cal_name="我的课表"),
        ),
        cal_wid="1046441504596287488",
    )
    assert [le.course for le in lessons] == ["高等数学"]


# ---------- eventDesc 解析 ----------


@pytest.mark.parametrize(
    "desc,expected",
    [
        ("上课节次：1-4 节， 授课教师：王艳", ("1-4", "王艳")),
        ("上课节次：5-6 节， 授课教师：周健颖", ("5-6", "周健颖")),
        ("上课节次：9-10 节， 授课教师：衡朝阳", ("9-10", "衡朝阳")),
        ("上课节次：1 节", ("1", "")),
        ("", ("", "")),
        ("格式完全不同的描述", ("", "")),
    ],
)
def test_parse_event_desc(desc, expected):
    assert parse_event_desc(desc) == expected


# ---------- 结构容错 ----------


def test_accepts_inner_data_dict():
    """调用方可能只传 data 部分，也要能解析。"""
    full = response(event())
    assert len(parse_lessons(full["data"])) == 1


def test_missing_datas_raises_parse_error():
    """结构变了要明确报错，而不是静默返回空 —— 静默会触发误删。"""
    with pytest.raises(ParseError):
        parse_lessons({"errcode": "0", "data": {"allChannels": []}})


def test_empty_datas_is_not_an_error():
    """学校还没排课时 datas 是空的，这是正常状态。"""
    assert parse_lessons({"data": {"datas": {}}}) == []


def test_dedup_by_uid():
    """同一节课被重复返回时只留一条。"""
    ev = event()
    assert len(parse_lessons(response(ev, dict(ev)))) == 1


# ---------- UID 与指纹 ----------


def test_uid_is_prefixed_and_stable():
    """UID 必须带 c2c. 前缀 —— 对账靠它区分所有权。"""
    le1 = Lesson(date(2026, 9, 22), "13:45", "15:10", "高等数学")
    le2 = Lesson(date(2026, 9, 22), "13:45", "15:10", "高等数学", room="善思楼204")
    assert le1.uid.startswith("c2c.")
    assert le1.uid.endswith("@class2cal")
    # 换教室不改身份 —— 应判为更新而非新增
    assert le1.uid == le2.uid
    assert le1.fingerprint != le2.fingerprint


def test_uid_ignores_server_event_id():
    """不能用服务端 eventId 做身份 —— 学校重排课时它会变，会导致重复导入。"""
    a = parse_lessons(response(event(event_id="first")))[0]
    b = parse_lessons(response(event(event_id="second")))[0]
    assert a.uid == b.uid


def test_uid_differs_across_time_and_course():
    base = Lesson(date(2026, 9, 22), "13:45", "15:10", "高等数学")
    assert base.uid != Lesson(date(2026, 9, 22), "15:25", "16:50", "高等数学").uid
    assert base.uid != Lesson(date(2026, 9, 23), "13:45", "15:10", "高等数学").uid
    assert base.uid != Lesson(date(2026, 9, 22), "13:45", "15:10", "英语").uid


def test_end_before_start_gets_one_hour():
    le = Lesson(date(2026, 9, 22), "13:45", "13:45", "高等数学")
    assert (le.end_dt - le.start_dt).total_seconds() == 3600


# ---------- 周分组 ----------


def test_group_by_week():
    lessons = parse_lessons(
        response(
            event(day="2026-09-22"),  # 周二
            event(course="英语", day="2026-09-28"),  # 下周一
        )
    )
    grouped = group_by_week(lessons)
    assert set(grouped) == {date(2026, 9, 21), date(2026, 9, 28)}
    assert [le.course for le in grouped[date(2026, 9, 21)]] == ["高等数学"]


def test_week_helpers():
    monday = date(2026, 9, 21)
    assert week_start(monday) == monday
    assert week_start(date(2026, 9, 27)) == monday  # 周日归上一个周一
    assert week_range(monday) == (monday, date(2026, 9, 27))
    assert list(iter_week_starts(date(2026, 9, 23), 3)) == [
        monday,
        date(2026, 9, 28),
        date(2026, 10, 5),
    ]


# ---------- 与学校作息表交叉验证 ----------


def test_times_match_official_timetable():
    """抓到的真实时间应与《武进校区作息表》一致 —— 这是数据可信度的旁证。"""
    from class2cal.timetable import ZONE_A_PERIODS

    samples = [
        ("1-2", "08:20", "09:45"),
        ("5-6", "13:45", "15:10"),
        ("7-8", "15:25", "16:50"),
        ("9-10", "18:00", "19:25"),
    ]
    for periods, start, end in samples:
        first, last = periods.split("-")
        assert ZONE_A_PERIODS[first][0] == start, f"第{first}节开始时间不符"
        assert ZONE_A_PERIODS[last][1] == end, f"第{last}节结束时间不符"
