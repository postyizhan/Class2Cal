"""CLI 层测试（study / setup 的交互与编号）。

这批用例是交叉审核发现的回归：编号来源不一致会让 remove 删错条目、删空后误关
同步，setup 在已有配置时回车会把「已关掉」重新打开。只测函数层是抓不到的。
"""

from __future__ import annotations

import builtins
from datetime import date

import pytest

from class2cal import cli, config
from class2cal.config import Config, StudyConfig

FLAT_CONFIG = """
[study]
enabled = true
title = "晚自习"
days = ["mon", "wed", "sun"]
start = "19:00"
end = "21:30"
"""


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    """把配置与 var/ 都指到临时目录，测试绝不碰仓库里的 config.toml。"""
    path = tmp_path / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config, "VAR_DIR", tmp_path / "var")
    return path


def run(*argv):
    args = cli.build_parser().parse_args(list(argv))
    return args.func(args)


def read_cfg(cfg_path) -> Config:
    return config.load()


# ---------- remove 的编号必须和 list 一致 ----------


def test_remove_deletes_the_item_list_shows(cfg_path, capsys):
    """简写 + slots 共存时，remove 1 必须删列表里的第 1 条，而不是 slots 的第 1 条。"""
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("study", "add", "--time", "07:30-08:00", "--title", "早自习") == 0
    capsys.readouterr()

    assert run("study") == 0
    listed = capsys.readouterr().out
    assert "[1] 晚自习" in listed
    assert "[2] 早自习" in listed

    assert run("study", "remove", "1") == 0
    out = capsys.readouterr().out
    assert "已删除：晚自习" in out

    cfg = read_cfg(cfg_path)
    assert cfg.study.enabled is True  # 还剩早自习，不能顺手关掉同步
    assert [s["title"] for s in cfg.study.slots] == ["早自习"]
    assert cfg.study.start == "" and cfg.study.end == ""


def test_remove_last_item_disables(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")

    assert run("study", "remove", "1") == 0
    out = capsys.readouterr().out
    assert "已没有自习时段" in out

    cfg = read_cfg(cfg_path)
    assert cfg.study.enabled is False
    assert cfg.study.slots == []


def test_remove_out_of_range(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("study", "remove", "5") == 1
    assert "编号超范围（1-1）" in capsys.readouterr().err


def test_remove_without_slots(cfg_path, capsys):
    cfg_path.write_text("[study]\nenabled = true\n", encoding="utf-8")
    assert run("study", "remove", "1") == 1
    assert "没有可删除的自习时段" in capsys.readouterr().err


# ---------- status 与 list 用同一个来源 ----------


def test_status_counts_effective_slots(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "已启用（1 条时段）" in out


def test_list_exit_code_reflects_broken_config(cfg_path, capsys):
    cfg_path.write_text(
        '[study]\nenabled = true\ndays = ["mon"]\nstart = "18:00"\nend = "21:00"\n'
        "my_note = 1\n[study.slots]\ntitle = \"x\"\n",
        encoding="utf-8",
    )
    # slots 写成了表而不是数组表 -> 配置不可用
    assert run("study") == 1


# ---------- setup 交互 ----------


def _feed(monkeypatch, answers):
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")


def _base_answers(study_lines):
    return [
        "https://sso.example.edu.cn",
        "https://all.example.edu.cn",
        "20240001",
        "you@example.com",
        "课表",
        *study_lines,
    ]


def test_setup_writes_flat_study_config(cfg_path, monkeypatch, capsys):
    _feed(monkeypatch, _base_answers(["19:00-21:30", "周一,周三,周日"]))
    assert run("setup") == 0

    out = capsys.readouterr().out
    # 曾经的 bug：把解析后的 int 再喂回 parse_days，显示整体错一天
    assert "周一、周三、周日 19:00-21:30" in out

    cfg = read_cfg(cfg_path)
    assert cfg.study.enabled is True
    assert cfg.study.days == ["mon", "wed", "sun"]
    assert (cfg.study.start, cfg.study.end) == ("19:00", "21:30")


def test_setup_enter_keeps_disabled_state(cfg_path, monkeypatch, capsys):
    """已关掉的自习，重跑 setup 全回车不能被悄悄重新打开。"""
    cfg_path.write_text(
        FLAT_CONFIG.replace("enabled = true", "enabled = false"), encoding="utf-8"
    )
    _feed(monkeypatch, _base_answers([""]))
    assert run("setup") == 0

    out = capsys.readouterr().out
    assert "保持原样" in out
    assert read_cfg(cfg_path).study.enabled is False


def test_setup_off_disables(cfg_path, monkeypatch, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    _feed(monkeypatch, _base_answers(["off"]))
    assert run("setup") == 0

    assert "已关闭" in capsys.readouterr().out
    assert read_cfg(cfg_path).study.enabled is False


def test_setup_survives_eof(cfg_path, monkeypatch, capsys):
    """管道在晚自习那步结束：不该抛裸 traceback，前面填的配置也要落盘。"""
    answers = _base_answers([])  # 到「日历名」为止，之后没有输入了
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")

    assert run("setup") == 0
    assert cfg_path.exists()
    assert "没有配置晚自习" in capsys.readouterr().out


# ---------- add 的参数校验 ----------


def test_add_rejects_time_together_with_start_end(cfg_path, capsys):
    assert (
        run(
            "study", "add", "--time", "07:00-08:00", "--start", "19:00", "--end", "21:00"
        )
        == 1
    )
    assert "时间写一套就够" in capsys.readouterr().err


@pytest.mark.parametrize("flag,value", [("--start", "19:00"), ("--end", "21:00")])
def test_add_rejects_half_time(cfg_path, capsys, flag, value):
    assert run("study", "add", flag, value) == 1
    assert "要一起给" in capsys.readouterr().err


def test_add_skips_duplicate(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    # 与简写完全相同
    assert run("study", "add", "--time", "19:00-21:30", "--days", "周一,周三,周日") == 0

    assert "没有重复添加" in capsys.readouterr().out
    cfg = read_cfg(cfg_path)
    assert cfg.study.slots == []


# ---------- preview 的诚实性 ----------


def test_preview_says_lessons_not_deducted(cfg_path, monkeypatch, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    mondays = [date(2026, 11, 2)]
    # 门户整体抓取失败时 _fetch_weekly 返回 ({}, 全部周)，不抛异常
    monkeypatch.setattr(cli, "_fetch_weekly", lambda cfg, weeks: ({}, mondays))

    assert run("study", "preview", "--weeks", "1") == 0
    out = capsys.readouterr().out
    # 关键是不能装作已经扣过正课
    assert "没抓到课表" in out or "抓不到课表" in out


def test_preview_reports_partial_failure(cfg_path, monkeypatch, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    mondays = [date(2026, 11, 2)]
    monkeypatch.setattr(cli, "_fetch_weekly", lambda cfg, weeks: ({mondays[0]: []}, mondays))

    assert run("study", "preview", "--weeks", "1") == 0
    assert "没抓到课表" in capsys.readouterr().out


def test_preview_prints_skip_reason(cfg_path, monkeypatch, capsys):
    """周日被正课顶掉时，preview 要直说原因，不能只默默少一条。"""
    from class2cal.schedule import Lesson

    cfg_path.write_text(
        '[study]\nenabled = true\ndays = ["mon", "sun"]\nstart = "19:00"\nend = "21:30"\n',
        encoding="utf-8",
    )
    monday = date(2026, 11, 2)
    sunday = date(2026, 11, 8)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return monday

    monkeypatch.setattr(cli, "date", FixedDate)
    monkeypatch.setattr(
        cli,
        "_fetch_weekly",
        lambda cfg, weeks: ({monday: [Lesson(sunday, "19:00", "20:00", "晚点名")]}, []),
    )

    assert run("study", "preview", "--weeks", "1") == 0
    out = capsys.readouterr().out
    assert "没排" in out and "晚点名" in out


# ---------- 配置层的边界（TOML 原文形态） ----------


def test_days_string_from_toml(cfg_path):
    cfg_path.write_text(
        '[study]\nenabled = true\ndays = "mon,wed"\nstart = "18:00"\nend = "21:00"\n',
        encoding="utf-8",
    )
    cfg = read_cfg(cfg_path)
    from class2cal import study as study_mod

    slots = study_mod.load_slots(cfg)
    assert slots[0].days == (0, 2)


def test_scalar_days_raises_config_error(cfg_path):
    from class2cal import study as study_mod

    cfg_path.write_text(
        "[study]\nenabled = true\ndays = 3\nstart = \"18:00\"\nend = \"21:00\"\n",
        encoding="utf-8",
    )
    cfg = read_cfg(cfg_path)
    with pytest.raises(study_mod.StudyConfigError):
        study_mod.load_slots(cfg)


def test_save_keeps_unknown_study_keys(cfg_path):
    cfg_path.write_text(
        "[study]\nenabled = true\nmy_note = \"手写备注\"\nstart = \"18:00\"\nend = \"21:00\"\n",
        encoding="utf-8",
    )
    cfg = read_cfg(cfg_path)
    config.save(cfg)

    text = cfg_path.read_text(encoding="utf-8")
    assert "my_note" in text and "手写备注" in text


def test_half_filled_flat_blocks_sync_config(cfg_path):
    from class2cal import study as study_mod

    cfg_path.write_text('[study]\nenabled = true\nstart = "18:00"\n', encoding="utf-8")
    cfg = read_cfg(cfg_path)
    with pytest.raises(study_mod.StudyConfigError):
        study_mod.load_slots(cfg)


# ---------- 第二轮复核抓到的回归 ----------


def test_save_keeps_days_unspecified(cfg_path):
    """「只写 start/end、不写 days」是合法配置：保存一次不能把它变成坏配置。"""
    from class2cal import study as study_mod

    cfg_path.write_text('[study]\nenabled = true\nstart = "19:00"\nend = "21:30"\n', encoding="utf-8")
    cfg = read_cfg(cfg_path)
    assert study_mod.load_slots(cfg)  # 保存前可用

    config.save(cfg)
    assert "days" not in cfg_path.read_text(encoding="utf-8")

    again = read_cfg(cfg_path)
    slots = study_mod.load_slots(again)
    assert slots[0].days == (0, 1, 2, 3, 6)  # 默认周日到周四


def test_save_keeps_schedule_period_times(cfg_path):
    """[schedule.period_times] 是文档里的用户配置，save 不能吞。"""
    cfg_path.write_text(
        '[study]\nenabled = false\n\n[schedule]\nweeks_ahead = 6\n\n'
        '[schedule.period_times]\n"1" = ["08:00", "08:45"]\n',
        encoding="utf-8",
    )
    cfg = read_cfg(cfg_path)
    config.save(cfg)

    text = cfg_path.read_text(encoding="utf-8")
    assert "period_times" in text and "08:45" in text


def test_setup_does_not_clear_advanced_slots(cfg_path, monkeypatch, capsys):
    """setup 里填时间不能把 [[study.slots]] 静默清空。"""
    cfg_path.write_text(
        '[study]\nenabled = true\n\n'
        '[[study.slots]]\ntitle = "早自习"\ndays = ["mon"]\nstart = "07:30"\nend = "08:00"\n',
        encoding="utf-8",
    )
    _feed(monkeypatch, _base_answers(["19:00-21:30"]))
    assert run("setup") == 0

    out = capsys.readouterr().out
    assert "不改动它们" in out
    cfg = read_cfg(cfg_path)
    assert len(cfg.study.slots) == 1
    assert cfg.study.start == ""  # 没被写成简写


def test_setup_enter_mentions_advanced_slots(cfg_path, monkeypatch, capsys):
    cfg_path.write_text(
        '[study]\nenabled = true\n\n'
        '[[study.slots]]\ntitle = "早自习"\ndays = ["mon"]\nstart = "07:30"\nend = "08:00"\n',
        encoding="utf-8",
    )
    _feed(monkeypatch, _base_answers([""]))
    assert run("setup") == 0

    out = capsys.readouterr().out
    assert "1 条进阶时段" in out
    assert "没有配置晚自习" not in out


def test_add_accepts_chinese_comma_in_skip(cfg_path, capsys):
    assert (
        run(
            "study",
            "add",
            "--time",
            "19:00-21:00",
            "--skip",
            "2026-10-01，2026-10-02",
        )
        == 0
    )
    assert "跳过 2 天" in capsys.readouterr().out


def test_add_allows_same_time_different_parity(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("study", "add", "--time", "19:00-21:30", "--days", "周一,周三,周日", "--parity", "odd") == 0

    out = capsys.readouterr().out
    assert "没有重复添加" not in out
    assert len(read_cfg(cfg_path).study.slots) == 1


def test_add_reports_broken_existing_config_separately(cfg_path, capsys):
    cfg_path.write_text(
        '[study]\nenabled = true\ndays = ["x"]\nstart = "19:00"\nend = "21:30"\n',
        encoding="utf-8",
    )
    assert run("study", "add", "--time", "07:30-08:00") == 1
    assert "你先前的自习配置有问题" in capsys.readouterr().err


def test_enable_refuses_to_persist_broken_config(cfg_path, capsys):
    """enable 失败时不能把 enabled=true 落盘，否则之后每次 sync 都被坏配置卡住。"""
    cfg_path.write_text(
        '[study]\nenabled = false\nstart = "18:00"\n', encoding="utf-8"
    )
    assert run("study", "enable") == 1
    assert "没有打开" in capsys.readouterr().err
    assert read_cfg(cfg_path).study.enabled is False


def test_remove_does_not_write_empty_days(cfg_path):
    """删除后清简写时写 days=None，别写出「显式空列表」这个坏形态。"""
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("study", "remove", "1") == 0

    text = cfg_path.read_text(encoding="utf-8")
    assert "days = []" not in text
    # 手补 start/end 之后仍然可用（days 未写 = 用默认星期）
    assert run("study", "add", "--time", "19:00-21:30", "--days", "mon") == 0


def test_preview_rejects_non_positive_weeks(cfg_path, capsys):
    cfg_path.write_text(FLAT_CONFIG, encoding="utf-8")
    assert run("study", "preview", "--weeks", "0") == 1
    assert "至少是 1" in capsys.readouterr().err


def test_setup_handles_getpass_eof(cfg_path, monkeypatch, capsys):
    """密码那步 stdin 断开：不抛裸 traceback，配置仍然落盘并提示输入提前结束。"""
    answers = iter(
        ["https://sso.example.edu.cn", "https://all.example.edu.cn", "20240001"]
    )

    def fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)

    def boom(prompt=""):
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", boom)

    assert run("setup") == 0
    assert cfg_path.exists()
    assert "输入提前结束" in capsys.readouterr().err


def test_disabled_config_is_not_parsed(cfg_path, capsys):
    """关掉自习时，配置里残留的坏时段不该让命令崩，也不该让 list 报错退出。"""
    from class2cal import study as study_mod

    cfg_path.write_text('[study]\nenabled = false\nstart = "18:00"\n', encoding="utf-8")
    cfg = read_cfg(cfg_path)
    assert study_mod.load_slots(cfg) == []

    assert run("study") == 0
    assert "未启用" in capsys.readouterr().out
