"""课表解析测试。

重点覆盖两件事：
  1. 优先取移动端给的具体时间，只有缺时间时才退回节次推算
  2. 不同学校字段名、不同嵌套层级都能解析出来
"""

from datetime import date

from class2cal.schedule import (
    Lesson,
    iter_week_starts,
    parse_lessons,
    week_range,
    week_start,
)


def test_parse_mobile_concrete_time():
    """移动端给了具体时间就直接用，不碰节次表。"""
    data = {
        "data": {
            "courseList": [
                {
                    "courseName": "高等数学",
                    "date": "2026-09-21",
                    "startTime": "08:20",
                    "endTime": "09:50",
                    "classroom": "A101",
                    "teacherName": "张老师",
                }
            ]
        }
    }
    lessons = parse_lessons(data)
    assert len(lessons) == 1
    le = lessons[0]
    assert (le.date, le.start, le.end) == (date(2026, 9, 21), "08:20", "09:50")
    assert le.course == "高等数学"
    assert le.room == "A101"
    assert le.teacher == "张老师"
    assert le.source == "mobile"


def test_period_fallback_only_when_time_missing():
    """只给节次号时才用节次表兜底，并标记为推算。"""
    data = {"list": [{"kcmc": "计算机应用基础", "skrq": "2026-09-21", "jc": "7"}]}
    lessons = parse_lessons(data, period_times={"7": ["15:25", "16:55"]})
    assert len(lessons) == 1
    assert (lessons[0].start, lessons[0].end) == ("15:25", "16:55")
    assert lessons[0].source == "period"


def test_record_without_time_or_period_is_skipped():
    """既没时间也没可用节次映射 —— 宁可跳过也不要猜错时间。"""
    data = {"list": [{"kcmc": "神秘课程", "skrq": "2026-09-21", "jc": "99"}]}
    assert parse_lessons(data, period_times={"7": ["15:25", "16:55"]}) == []


def test_alternate_field_names_and_nesting():
    """换一套字段名、多包一层也要能解析。"""
    data = {
        "data": {
            "rows": [
                {
                    "list": [
                        {
                            "kcm": "英语",
                            "rq": "2026/09/22",
                            "kssj": "1525",
                            "jssj": "1655",
                            "jsmc": "B203",
                        }
                    ]
                }
            ]
        }
    }
    lessons = parse_lessons(data)
    assert len(lessons) == 1
    assert (lessons[0].course, lessons[0].start, lessons[0].end) == (
        "英语",
        "15:25",
        "16:55",
    )
    assert lessons[0].date == date(2026, 9, 22)


def test_missing_end_time_defaults_to_45_minutes():
    data = {"list": [{"courseName": "体育", "date": "2026-09-23", "startTime": "10:00"}]}
    lessons = parse_lessons(data)
    assert lessons[0].end == "10:45"


def test_dedup_by_uid():
    """移动端可能把跨节的课重复返回，同 UID 只留一条。"""
    rec = {
        "courseName": "高等数学",
        "date": "2026-09-21",
        "startTime": "08:20",
        "endTime": "09:50",
    }
    lessons = parse_lessons({"list": [rec, dict(rec)]})
    assert len(lessons) == 1


def test_uid_is_stable_and_prefixed():
    """UID 必须带 c2c. 前缀（对账靠它区分所有权），且同一节课稳定不变。"""
    le1 = Lesson(date(2026, 9, 21), "08:20", "09:50", "高等数学")
    le2 = Lesson(date(2026, 9, 21), "08:20", "09:50", "高等数学", room="A101")
    assert le1.uid.startswith("c2c.")
    assert le1.uid.endswith("@class2cal")
    # 换教室不改身份 —— 应判为更新而非新增
    assert le1.uid == le2.uid
    assert le1.fingerprint != le2.fingerprint


def test_uid_differs_across_time_and_course():
    base = Lesson(date(2026, 9, 21), "08:20", "09:50", "高等数学")
    assert base.uid != Lesson(date(2026, 9, 21), "10:00", "11:30", "高等数学").uid
    assert base.uid != Lesson(date(2026, 9, 22), "08:20", "09:50", "高等数学").uid
    assert base.uid != Lesson(date(2026, 9, 21), "08:20", "09:50", "英语").uid


def test_end_before_start_gets_one_hour():
    """结束时间不晚于开始时间时兜底成 1 小时，避免写出非法事件。"""
    le = Lesson(date(2026, 9, 21), "08:20", "08:20", "高等数学")
    assert (le.end_dt - le.start_dt).total_seconds() == 3600


def test_week_helpers():
    # 2026-09-21 是周一
    monday = date(2026, 9, 21)
    assert week_start(monday) == monday
    assert week_start(date(2026, 9, 27)) == monday  # 周日归上一个周一
    assert week_range(monday) == (monday, date(2026, 9, 27))

    weeks = list(iter_week_starts(date(2026, 9, 23), 3))
    assert weeks == [monday, date(2026, 9, 28), date(2026, 10, 5)]
