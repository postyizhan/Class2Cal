"""命令行入口。

典型流程：
    class2cal setup            # 存凭据、填 Apple ID
    class2cal login            # 验证 SSO 能通
    class2cal probe            # 探明课表卡片坐标（关键一步，需人工确认）
    class2cal fetch            # 看抓到的课对不对
    class2cal sync --dry-run   # 看 diff
    class2cal sync --yes       # 真正写入
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from datetime import date
from pathlib import Path

import requests

from . import (
    assisted_login,
    browser_login,
    calendar_sync,
    config,
    portal,
    probe_browser,
    schedule,
    sso,
    study,
)
from .state import SyncState

log = logging.getLogger("class2cal")


def _brief(exc: BaseException, limit: int = 120) -> str:
    """把异常压成一行短话，别把整段 DNS/连接错误糊在用户脸上。"""
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[:limit] + "…"


# 交互输入中途遇到 EOF（管道断开 / Ctrl-D）时置位，最后统一告警，
# 而不是让用户以为「配置已写入」是完整的一次配置。
_input_truncated = False


def _ask(prompt: str) -> str:
    """input() 的壳：管道断开 / Ctrl-D 时返回空串，而不是抛裸 EOFError。"""
    global _input_truncated
    try:
        return input(prompt)
    except EOFError:
        _input_truncated = True
        print()
        return ""


def _ask_secret(prompt: str) -> str:
    """getpass 的壳：非 tty / 管道断开时同样不抛裸 traceback。"""
    global _input_truncated
    try:
        return getpass.getpass(prompt)
    except EOFError:
        _input_truncated = True
        print()
        return ""


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


# ---------- setup ----------


def cmd_setup(args: argparse.Namespace) -> int:
    global _input_truncated
    _input_truncated = False
    cfg = config.load()

    print("=== 学校网站配置 ===")
    print("（金智教务管理系统，通常形如 sso.xxx.edu.cn 和 all.xxx.edu.cn）")

    sso_base = _ask(f"统一认证地址（SSO）[{cfg.sso_base}]：").strip() or cfg.sso_base
    if not sso_base:
        print("SSO 地址不能为空，示例：https://sso.example.edu.cn", file=sys.stderr)
        return 1
    if not sso_base.startswith("http"):
        sso_base = f"https://{sso_base}"
    cfg.sso_base = sso_base

    portal_base = _ask(f"门户地址（网上办事大厅）[{cfg.portal_base}]：").strip() or cfg.portal_base
    if not portal_base:
        print("门户地址不能为空，示例：https://all.example.edu.cn", file=sys.stderr)
        return 1
    if not portal_base.startswith("http"):
        portal_base = f"https://{portal_base}"
    cfg.portal_base = portal_base

    print("\n=== 账号凭据 ===")
    username = _ask(f"统一认证账号（学号）[{cfg.sso_username}]：").strip() or cfg.sso_username
    if not username:
        print("账号不能为空", file=sys.stderr)
        return 1
    cfg.sso_username = username

    # getpass 不回显；密码只进 Keychain，不写 config.toml
    password = _ask_secret("统一认证密码（存入 Keychain，不落盘）：")
    if password:
        config.set_sso_password(username, password)
        print("已存入 Keychain")

    apple_id = _ask(f"\nApple ID [{cfg.calendar.apple_id}]：").strip() or cfg.calendar.apple_id
    if apple_id:
        cfg.calendar.apple_id = apple_id
        print("Apple 应用专用密码请到 appleid.apple.com 生成（不是账户密码）")
        apple_pw = _ask_secret("Apple 应用专用密码（存入 Keychain）：")
        if apple_pw:
            # Apple 展示的专用密码带连字符，去掉更省事
            config.set_apple_password(apple_id, apple_pw.replace("-", "").strip())
            print("已存入 Keychain")

    cal_name = (
        _ask(f"目标日历名 [{cfg.calendar.calendar_name}]：").strip()
        or cfg.calendar.calendar_name
    )
    cfg.calendar.calendar_name = cal_name

    # 晚自习：学校系统里没有，问一次就存在 config.toml，之后每次同步照用。
    # 回车 = 保持现状（包括「已关掉」这个状态），要关掉就显式输入 off。
    flat_window = (
        f"{cfg.study.start}-{cfg.study.end}"
        if cfg.study.start and cfg.study.end
        else ""
    )
    has_slots = bool(cfg.study.slots)
    existing = f"{len(cfg.study.slots)} 条进阶时段" if has_slots else flat_window

    print("\n=== 晚自习（学校只排正课，这里补；回车保持不变，输入 off 关闭）===")
    if has_slots:
        hint = "回车不动这些时段"
    else:
        hint = f"如 18:00-21:00，当前 {flat_window or '未配置'}"
    time_text = _ask(f"晚自习时间（{hint}）：").strip()

    if time_text.lower() in ("off", "关", "关闭", "none", "no"):
        cfg.study.enabled = False
        print("晚自习同步已关闭（配置里的时段留着，想恢复就再填一次时间）。")
    elif time_text and has_slots:
        # 简写和 [[study.slots]] 混着改会互相覆盖，这里不猜，交回子命令。
        print(
            f"已有 {len(cfg.study.slots)} 条进阶时段（[[study.slots]]），setup 不改动它们；"
            "要加/删/改请用 `class2cal study add|remove`，或直接编辑 config.toml。"
        )
    elif time_text:
        days_hint = study.describe_days(cfg.study.days or study.DEFAULT_DAYS)
        days_text = _ask(f"晚自习是周几（周一,周二 或 mon,tue）[{days_hint}]：").strip()
        try:
            start, end = study.parse_time_range(time_text)
            days = study.parse_days(days_text or cfg.study.days or study.DEFAULT_DAYS)
        except study.StudyConfigError as exc:
            print(
                f"晚自习配置跳过：{exc}\n（之后也能用 `class2cal study add` 补上）",
                file=sys.stderr,
            )
        else:
            cfg.study.enabled = True
            cfg.study.title = cfg.study.title or "晚自习"
            cfg.study.start, cfg.study.end = start, end
            cfg.study.days = study.day_names(days)
            print(
                f"已记下：{cfg.study.title} {study.describe_weekdays(days)} {start}-{end}"
            )
    elif existing:
        state = "已启用" if cfg.study.enabled else "已关闭"
        print(f"保持原样：{existing}（{state}）")
    else:
        print("没有配置晚自习，跳过。")

    config.save(cfg)
    config.ensure_var_dirs(cfg)
    print(f"\n配置已写入 {config.CONFIG_PATH}")
    if _input_truncated:
        print(
            "注意：输入提前结束（管道断开或按了 Ctrl-D），以上是当时填到的内容，"
            "没填的部分保持原样。",
            file=sys.stderr,
        )
    return 0


# ---------- login ----------


def _user_display_name(user: dict) -> str:
    return (
        user.get("userName")
        or user.get("name")
        or user.get("realName")
        or user.get("nickName")
        or "(未知)"
    )


def cmd_login(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)

    if args.browser:
        try:
            user = assisted_login.assisted_login(
                cfg, timeout_s=args.timeout, channel=args.channel
            )
        except assisted_login.AssistedLoginError as exc:
            print(f"登录失败：{exc}", file=sys.stderr)
            return 1
        print(f"\n登录成功：{_user_display_name(user)}")
        print("接着跑 `class2cal probe` 探测课表接口。")
        return 0

    if args.manual:
        return _manual_login(cfg, args)

    try:
        session = sso.login(cfg)
    except sso.CaptchaRequired as exc:
        print(
            f"需要验证码：{exc}\n\n"
            "学校在登录时强制校验滑块验证码，账密静默登录走不通。\n"
            "改用手动登录一次、复用会话：\n"
            "    class2cal login --manual",
            file=sys.stderr,
        )
        return 2
    except sso.BadCredentials as exc:
        print(f"{exc}\n重新跑 `class2cal setup` 更新密码。", file=sys.stderr)
        return 3
    except (sso.SsoError, config.ConfigError) as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1

    print(f"登录成功：{_user_display_name(sso.verify_session(session, cfg) or {})}")
    return 0


def _manual_login(cfg: config.Config, args: argparse.Namespace) -> int:
    """手动登录：人在浏览器里过滑块，程序只接收结果会话。"""
    if args.from_file:
        try:
            user = browser_login.import_from_file(Path(args.from_file), cfg)
        except browser_login.ManualLoginError as exc:
            print(f"导入失败：{exc}", file=sys.stderr)
            return 1
        print(f"会话导入成功：{_user_display_name(user)}")
        return 0

    print(browser_login.get_instructions(cfg))
    try:
        browser_login.open_login_page(cfg)
    except browser_login.ManualLoginError as exc:
        print(f"{exc}", file=sys.stderr)

    print("\n粘贴内容，结束后按 Ctrl-D：")
    raw = sys.stdin.read()
    if not raw.strip():
        print("没有输入任何内容", file=sys.stderr)
        return 1

    try:
        user = browser_login.import_cookies(raw, cfg)
    except browser_login.ManualLoginError as exc:
        print(f"\n导入失败：{exc}", file=sys.stderr)
        return 1

    print(f"\n会话导入成功：{_user_display_name(user)}")
    print("接着跑 `class2cal probe` 探测课表接口。")
    return 0


# ---------- probe ----------


def _probe_browser(cfg: config.Config, args: argparse.Namespace) -> int:
    """浏览器抓包：你点进课表页，工具把接口抓出来。"""
    try:
        result = probe_browser.probe_with_browser(
            cfg, timeout_s=args.timeout, channel=args.channel
        )
    except probe_browser.AutoProbeError as exc:
        print(f"探测失败：{exc}", file=sys.stderr)
        return 1

    print(f"共记录 {result['captured']} 条 JSON 请求，存档在 {result['dump']}")
    candidates = result["candidates"]

    if not candidates:
        print(
            "\n没识别出课表接口。可能是没点进课表页，或者课表数据不走 JSON 接口。\n"
            f"抓到的请求都在 {result['dump']}，可以翻一下确认。",
            file=sys.stderr,
        )
        return 4

    print(f"\n找到 {len(candidates)} 个候选接口（按可能性排序）：")
    for i, c in enumerate(candidates, 1):
        coord = ""
        if c.get("card_wid"):
            coord = f"  wid={c['card_wid']} id={c['card_id']}"
        print(f"  [{i}] {c['url'][:95]}{coord}")
        print(f"      命中信号：{'、'.join(c['signals']) or '(无)'}  得分 {c['score']}")

    pickable = [c for c in candidates if c.get("card_wid")]
    if args.pick:
        idx = args.pick - 1
        if not 0 <= idx < len(candidates):
            print(f"编号超范围（1-{len(candidates)}）", file=sys.stderr)
            return 1
        chosen = candidates[idx]
        if not chosen.get("card_wid"):
            print(
                f"\n[{args.pick}] 不是 execCardMethod 接口，没有卡片坐标可写。\n"
                "把这条的 URL 和响应发我，我来适配。",
                file=sys.stderr,
            )
            return 1
        cfg.schedule.card_wid = chosen["card_wid"]
        cfg.schedule.card_id = chosen["card_id"]
        config.save(cfg)
        print(f"\n已写入 config.toml：wid={chosen['card_wid']} id={chosen['card_id']}")
        print("接着跑 `class2cal calendars` 确认哪个日历是课表。")
    elif pickable:
        print("\n确认哪个是课表后，跑 `class2cal probe --browser --pick N` 写入配置。")
        print("（也可以直接把上面的输出发我，我帮你判断）")
    else:
        print(
            "\n候选里没有 execCardMethod 形式的接口，课表可能走了别的接口。\n"
            f"把 {result['dump']} 里相关的请求发我，我来适配。"
        )
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)

    if args.browser:
        return _probe_browser(cfg, args)

    try:
        session = sso.ensure_session(cfg)
    except (sso.SsoError, config.ConfigError) as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return 1

    client = portal.PortalClient(session, cfg)
    candidates = client.probe_schedule_cards(cfg.probe_dir)

    print(f"门户结构已存档到 {cfg.probe_dir}")
    if not candidates:
        print(
            "接口枚举没找到课表卡片（本校门户没买 personalized 模板，清单接口取不到）。\n"
            "改用浏览器抓包，你只要点进课表页就行：\n"
            "    class2cal probe --browser",
            file=sys.stderr,
        )
        return 4

    print(f"\n找到 {len(candidates)} 个课表候选卡片：")
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c['label']}  wid={c['card_wid']} id={c['card_id']}")

    if args.pick:
        idx = args.pick - 1
        if not 0 <= idx < len(candidates):
            print(f"编号超范围（1-{len(candidates)}）", file=sys.stderr)
            return 1
        chosen = candidates[idx]
        cfg.schedule.card_wid = chosen["card_wid"]
        cfg.schedule.card_id = chosen["card_id"]
        config.save(cfg)
        print(f"\n已把 [{args.pick}] {chosen['label']} 写入 config.toml")
        print("接着跑 `class2cal calendars` 确认哪个日历是课表。")
    else:
        print("\n确认哪个是课表后，跑 `class2cal probe --pick N` 写入配置。")
    return 0


# ---------- calendars ----------


def cmd_calendars(args: argparse.Namespace) -> int:
    """列出门户里的日历，确认哪个是课表。

    同一张日历卡片里混着「假期」等别的日历，不过滤会把假期一起导进 Apple 日历。
    响应里的 isLessonTable 字段靠不住（「假期」标 1，「我的课表」反而标 0），
    所以这步交给人确认，不自动挑。
    """
    cfg = config.load()
    config.ensure_var_dirs(cfg)

    try:
        session = sso.ensure_session(cfg)
        cals = portal.PortalClient(session, cfg).fetch_calendars()
    except (sso.SsoError, portal.PortalError, config.ConfigError) as exc:
        print(f"取日历清单失败：{exc}", file=sys.stderr)
        return 1

    items = [c for c in (cals or []) if isinstance(c, dict) and c.get("wid")]
    if not items:
        print("没取到任何日历。先确认 `class2cal probe` 已探明卡片坐标。", file=sys.stderr)
        return 4

    print(f"门户里共 {len(items)} 个日历：")
    for i, c in enumerate(items, 1):
        current = "  ← 当前配置" if str(c["wid"]) == cfg.schedule.cal_wid else ""
        print(f"  [{i}] {c.get('calName') or '(无名)'}  wid={c['wid']}{current}")
        if c.get("calDesc"):
            print(f"      {c['calDesc']}")

    if not args.pick:
        print("\n确认哪个是课表后，跑 `class2cal calendars --pick N` 写入配置。")
        print("（课表一般叫「我的课表」，描述里会提教务系统；「假期」那个不是课）")
        return 0

    idx = args.pick - 1
    if not 0 <= idx < len(items):
        print(f"编号超范围（1-{len(items)}）", file=sys.stderr)
        return 1

    chosen = items[idx]
    cfg.schedule.cal_wid = str(chosen["wid"])
    cfg.schedule.cal_name = str(chosen.get("calName") or "")
    config.save(cfg)
    print(f"\n已写入 config.toml：cal_wid={cfg.schedule.cal_wid}（{cfg.schedule.cal_name}）")
    print("接着跑 `class2cal fetch` 验证抓到的课对不对。")
    return 0


# ---------- fetch ----------


def _fetch_weekly(
    cfg: config.Config, weeks_ahead: int
) -> tuple[dict[date, list[schedule.Lesson]], list[date]]:
    """抓取课表。返回 (周一 -> 课程列表, 失败的周)。

    接口接受任意时间范围，所以整个窗口一次请求就够，不必逐周调用。

    关键：窗口内每一周都要出现在结果字典里（没课的周是空列表）。这是安全闸 1
    生效的前提 —— 只有「这周在结果里但列表为空」才能和「这周没抓到」区分开：
    前者是学校撤课，后者要跳过不动。
    """
    session = sso.ensure_session(cfg)
    client = portal.PortalClient(session, cfg)

    mondays = list(schedule.iter_week_starts(date.today(), weeks_ahead))
    win_start, win_end = mondays[0], schedule.week_range(mondays[-1])[1]

    try:
        raw = client.fetch_schedule(win_start, win_end, archive_dir=cfg.raw_dir)
    except portal.CardNotFound:
        raise
    except portal.PortalError as exc:
        # 整段抓取失败 —— 所有周都算未抓取，一律不参与对账
        log.warning("抓取 %s ~ %s 失败：%s", win_start, win_end, exc)
        return {}, mondays

    lessons = schedule.parse_lessons(
        raw, cal_name=cfg.schedule.cal_name, cal_wid=cfg.schedule.cal_wid
    )
    grouped = schedule.group_by_week(lessons)

    weekly: dict[date, list[schedule.Lesson]] = {}
    for monday in mondays:
        start, end = schedule.week_range(monday)
        weekly[monday] = [le for le in grouped.get(monday, []) if start <= le.date <= end]

    return weekly, []


def _merge_study(
    cfg: config.Config, weekly: dict[date, list[schedule.Lesson]]
) -> dict[date, list[schedule.Lesson]]:
    """把配置里的自习时段并进每周事件列表。

    只并进本次抓取成功的周 —— 抓取失败的周不在 weekly 里，自习也不会凭空写入，
    否则一个失效的会话会让日历上多出一堆本该对账掉的旧事件。
    """
    slots = study.load_slots(cfg)
    if not slots:
        return weekly

    merged: dict[date, list[schedule.Lesson]] = {}
    for monday, lessons in weekly.items():
        # 有正课和自习时段重叠的那天不排自习：人在上课。
        both = list(lessons) + study.lessons_for_week(slots, monday, occupied=lessons)
        merged[monday] = sorted(both, key=lambda x: (x.date, x.start, x.course))
    return merged


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)
    weeks = args.weeks or cfg.schedule.weeks_ahead

    try:
        weekly, failed = _fetch_weekly(cfg, weeks)
        weekly = _merge_study(cfg, weekly)
    except (sso.SsoError, portal.PortalError, config.ConfigError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1
    except study.StudyConfigError as exc:
        print(f"自习配置有问题：{exc}", file=sys.stderr)
        return 1

    total = sum(len(v) for v in weekly.values())
    school_total = sum(1 for v in weekly.values() for le in v if not le.is_study)
    for monday in sorted(weekly):
        start, end = schedule.week_range(monday)
        lessons = weekly[monday]
        print(f"\n{start} ~ {end}（{len(lessons)} 节）")
        for le in lessons:
            periods = f" [{le.periods}节]" if le.periods else ""
            room = f" @{le.room}" if le.room else ""
            teacher = f" {le.teacher}" if le.teacher else ""
            tag = " [自习]" if le.is_study else ""
            print(f"  {le.date} {le.start}-{le.end}  {le.course}{room}{teacher}{periods}{tag}")

    study_total = total - school_total
    tail = f"（正课 {school_total}，自习 {study_total}）" if study_total else ""
    print(f"\n共 {total} 节课，覆盖 {len(weekly)} 周{tail}")
    if failed:
        print(f"有 {len(failed)} 周抓取失败（不会参与对账）：{[d.isoformat() for d in failed]}")

    if args.json:
        payload = {
            m.isoformat(): [
                {
                    "date": le.date.isoformat(),
                    "start": le.start,
                    "end": le.end,
                    "course": le.course,
                    "room": le.room,
                    "teacher": le.teacher,
                    "periods": le.periods,
                    "kind": le.kind,
                    "uid": le.uid,
                }
                for le in v
            ]
            for m, v in weekly.items()
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ---------- sync ----------


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)
    weeks = args.weeks or cfg.schedule.weeks_ahead
    st = SyncState.load(cfg.state_path)

    # 首次同步（无状态）强制 dry-run，先让人看一眼 diff
    first_run = not st.weeks
    dry_run = args.dry_run or (first_run and not args.yes)

    try:
        weekly, failed = _fetch_weekly(cfg, weeks)
        weekly = _merge_study(cfg, weekly)
    except (sso.SsoError, portal.PortalError, config.ConfigError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1
    except study.StudyConfigError as exc:
        print(f"自习配置有问题：{exc}", file=sys.stderr)
        return 1

    try:
        syncer = calendar_sync.CalendarSyncer(cfg)
        report = syncer.sync(weekly, st, dry_run=dry_run)
    except (calendar_sync.CalendarError, config.ConfigError) as exc:
        print(f"同步失败：{exc}", file=sys.stderr)
        return 1

    _print_report(report, failed, dry_run)

    if dry_run and first_run and not args.dry_run:
        print("\n首次同步默认只看不改。确认无误后跑 `class2cal sync --yes` 实际写入。")
    return 0


def _print_report(
    report: calendar_sync.SyncReport, failed: list[date], dry_run: bool
) -> None:
    head = "[试运行] " if dry_run else ""
    for plan in report.plans:
        start, end = schedule.week_range(plan.monday)
        if plan.was_skipped:
            print(f"\n{head}{start} ~ {end}  已跳过：{plan.skipped_reason}")
            continue
        if plan.is_noop and not plan.protected:
            continue

        print(f"\n{head}{start} ~ {end}")
        for le in plan.to_add:
            print(f"  + 新增  {le.date} {le.start}-{le.end} {le.course}")
        for le, _ in plan.to_update:
            print(f"  ~ 更新  {le.date} {le.start}-{le.end} {le.course}")
        for uid, _ in plan.to_delete:
            print(f"  - 删除  {uid}")
        if plan.protected:
            print(f"  · 跳过 {plan.protected} 个非工具事件（你手动加的，不动）")

    verb = "将" if dry_run else "已"
    print(
        f"\n{verb}新增 {report.added}、更新 {report.updated}、删除 {report.deleted}；"
        f"保护 {report.protected} 个手录事件"
    )
    if report.skipped_weeks:
        print(f"注意：有 {len(report.skipped_weeks)} 周因安全守卫被跳过，见上方说明")
    if failed:
        print(f"有 {len(failed)} 周抓取失败，未参与对账：{[d.isoformat() for d in failed]}")


# ---------- status ----------


def cmd_status(args: argparse.Namespace) -> int:
    cfg = config.load()
    st = SyncState.load(cfg.state_path)

    exists = "存在" if config.CONFIG_PATH.exists() else "缺失"
    print(f"配置文件      {config.CONFIG_PATH}（{exists}）")
    print(f"统一认证账号  {cfg.sso_username or '(未配置)'}")
    print(f"Apple ID      {cfg.calendar.apple_id or '(未配置)'}")
    print(f"目标日历      {cfg.calendar.calendar_name}")
    print(f"课表卡片      {'已探明' if cfg.schedule.is_probed else '(未探明，先跑 probe)'}")
    print(f"抓取窗口      当前周 + 后 {cfg.schedule.weeks_ahead} 周")
    study_state = "已启用" if cfg.study.enabled else "未启用"
    try:
        slot_count = f"{len(study.parse_slots(study.effective_slots(cfg)))} 条时段"
    except study.StudyConfigError:
        slot_count = "配置有问题"
    print(f"自习同步      {study_state}（{slot_count}）")

    session = sso.load_session(cfg.session_path)
    alive = bool(session and sso.verify_session(session, cfg))
    print(f"会话状态      {'有效' if alive else '无效/不存在'}")

    if st.weeks:
        print("\n已同步的周：")
        for k in sorted(st.weeks):
            rec = st.weeks[k]
            print(f"  {k}  {rec.count} 节  {rec.synced_at}")
    else:
        print("\n还没同步过")
    return 0


# ---------- study ----------


def _print_study_slots(cfg: config.Config) -> bool:
    """打印当前自习配置。返回配置是否可用（false 时调用方该给非零退出码）。"""
    print(f"自习同步      {'已启用' if cfg.study.enabled else '未启用'}")
    if not cfg.study.enabled:
        # 关掉时同步根本不读这份配置，别拿残留的错误配置拦住用户看列表。
        print("  （未启用，配置不做校验；`class2cal study enable` 打开时会检查）")
        return True

    try:
        slots = study.parse_slots(study.effective_slots(cfg))
    except study.StudyConfigError as exc:
        print(f"  配置有问题：{exc}", file=sys.stderr)
        return False

    if not slots:
        print("  还没配置时段。两种填法：")
        print("    class2cal setup                       # 配置时问一遍，回车跳过")
        print("    class2cal study add --time 18:00-21:00  # 单独加一条")
        return True

    for i, s in enumerate(slots, 1):
        room = f" @{s.location}" if s.location else ""
        parity = "" if s.parity == "all" else f"，{s.parity} 周"
        span = ""
        if s.date_from or s.date_to:
            span = f"，{s.date_from or '…'} ~ {s.date_to or '…'}"
        skip = f"，跳过 {len(s.skip_dates)} 天" if s.skip_dates else ""
        print(
            f"  [{i}] {s.title}  {s.describe_days()} {s.start}-{s.end}"
            f"{room}{parity}{span}{skip}"
        )

    # 自习之间没有优先级：两条撞一起就都写进日历，提醒一句免得以为是 bug。
    for a, b in study.find_overlaps(slots):
        print(
            f"  ! 「{a.title}」{a.start}-{a.end} 与「{b.title}」{b.start}-{b.end} 重叠，"
            "会各写一条事件"
        )
    return True


def _preview_study(cfg: config.Config, args: argparse.Namespace) -> int:
    try:
        slots = study.parse_slots(study.effective_slots(cfg))
    except study.StudyConfigError as exc:
        print(f"自习配置有问题：{exc}", file=sys.stderr)
        return 1

    if not slots:
        print("没有可预览的自习时段，先 `class2cal setup` 或 `class2cal study add`。", file=sys.stderr)
        return 1
    if not cfg.study.enabled:
        print("（study.enabled = false，下面只是展开预览，同步时不会写入）\n")

    weeks = 2 if args.weeks is None else args.weeks
    if weeks < 1:
        print(f"--weeks 至少是 1（收到 {args.weeks}）。", file=sys.stderr)
        return 1
    mondays = list(schedule.iter_week_starts(date.today(), weeks))

    # 尽力抓一次课表：预览里要扣掉「那天有正课」的晚自习，不然看到的比实际多。
    weekly: dict[date, list[schedule.Lesson]] = {m: [] for m in mondays}
    try:
        fetched, failed = _fetch_weekly(cfg, weeks)
    except (
        sso.SsoError,
        portal.PortalError,
        config.ConfigError,
        study.StudyConfigError,
        requests.RequestException,
        OSError,
    ) as exc:
        # 没会话/没探明接口/网络不通：不是错误，只是预览少一层信息，说清楚就行。
        print(f"（抓不到课表，预览未扣除正课：{_brief(exc)}）")
    else:
        if failed:
            print(f"（有 {len(failed)} 周没抓到课表，这些周的正课没扣）")
        elif not fetched:
            print("（接口没返回任何课程，预览未扣除正课）")
        weekly.update(fetched)
    for monday in mondays:
        start, end = schedule.week_range(monday)
        busy = weekly.get(monday, [])
        lessons = study.lessons_for_week(slots, monday, occupied=busy)
        print(f"{start} ~ {end}（自习 {len(lessons)} 次）")
        for le in lessons:
            room = f" @{le.room}" if le.room else ""
            print(f"  {le.date} {le.start}-{le.end}  {le.course}{room}")
        print()
    return 0


def cmd_study(args: argparse.Namespace) -> int:
    cfg = config.load()
    action = args.study_action

    if action in (None, "list"):
        return 0 if _print_study_slots(cfg) else 1

    if action == "preview":
        return _preview_study(cfg, args)

    if action == "enable":
        # 先校验再落盘：如果先把 enabled=true 写进文件，配置又是坏的，之后每次
        # sync 都会被这份坏配置卡住，用户还不知道为什么。
        previous = cfg.study.enabled
        cfg.study.enabled = True
        try:
            study.parse_slots(study.effective_slots(cfg))
        except study.StudyConfigError as exc:
            cfg.study.enabled = previous
            print(f"自习配置有问题，没有打开：{exc}", file=sys.stderr)
            return 1
    elif action == "disable":
        cfg.study.enabled = False
    elif action == "add":
        if args.time and (args.start or args.end):
            print(
                "时间写一套就够：--time 18:00-21:00，或者 --start 18:00 --end 21:00。",
                file=sys.stderr,
            )
            return 1
        if bool(args.start) != bool(args.end):
            print(
                "--start 和 --end 要一起给（或者用 --time 18:00-21:00 写一段）。",
                file=sys.stderr,
            )
            return 1

        try:
            start, end = args.start, args.end
            if not (start and end):
                start, end = study.parse_time_range(args.time)
            days = study.parse_days(args.days or study.DEFAULT_DAYS)
        except study.StudyConfigError as exc:
            print(f"这条时段写不通：{exc}", file=sys.stderr)
            return 1

        slot = {
            "title": args.title or "晚自习",
            "days": study.day_names(days),
            "start": start,
            "end": end,
        }
        if args.location:
            slot["location"] = args.location
        if args.parity and args.parity != "all":
            slot["parity"] = args.parity
        if args.date_from:
            slot["from"] = args.date_from
        if args.date_to:
            slot["to"] = args.date_to
        if args.skip:
            slot["skip"] = [
                d.strip()
                for d in args.skip.replace("，", ",").replace("、", ",").split(",")
                if d.strip()
            ]

        # 先校验新条目本身，再校验既有配置：两类错误不能混成一句话。
        try:
            new_slot = study.parse_slots([slot])[0]
        except study.StudyConfigError as exc:
            print(f"这条时段写不通：{exc}", file=sys.stderr)
            return 1

        try:
            current = study.parse_slots(study.effective_slots(cfg))
        except study.StudyConfigError as exc:
            print(
                f"你先前的自习配置有问题，先修它再添加：{exc}",
                file=sys.stderr,
            )
            return 1

        duplicate = any(
            (
                s.title,
                s.days,
                s.start,
                s.end,
                s.location,
                s.parity,
                s.date_from,
                s.date_to,
                s.skip_dates,
            )
            == (
                new_slot.title,
                new_slot.days,
                new_slot.start,
                new_slot.end,
                new_slot.location,
                new_slot.parity,
                new_slot.date_from,
                new_slot.date_to,
                new_slot.skip_dates,
            )
            for s in current
        )
        if duplicate:
            print("已存在完全相同的时段（标题、星期、时间都一样），没有重复添加。")
            return 0

        cfg.study.slots.append(slot)
        cfg.study.enabled = True
    elif action == "remove":
        try:
            raw = study.effective_slots(cfg)
            listed = study.parse_slots(raw)
        except study.StudyConfigError as exc:
            print(f"自习配置有问题：{exc}", file=sys.stderr)
            return 1

        if not listed:
            print("没有可删除的自习时段。", file=sys.stderr)
            return 1
        idx = args.index - 1
        if not 0 <= idx < len(listed):
            print(f"编号超范围（1-{len(listed)}）", file=sys.stderr)
            return 1

        removed = raw.pop(idx)
        print(f"已删除：{removed.get('title', '(无名)')}")

        # 编号只有一个来源（effective_slots = 简写 + slots）。删完把剩下的整体
        # 写进 slots、清空简写字段，下次列表和删除才对得上。
        cfg.study.slots = list(raw)
        cfg.study.start = ""
        cfg.study.end = ""
        # None 而不是 []：空列表是「用户显式写空」，会变成加载即报错的配置
        cfg.study.days = None
        cfg.study.location = ""
        if not raw:
            cfg.study.enabled = False
            print("已没有自习时段，自习同步同时关闭。")
    else:
        print(f"未知操作：{action}", file=sys.stderr)
        return 1

    config.save(cfg)
    print(f"已写入 {config.CONFIG_PATH}\n")
    return 0 if _print_study_slots(cfg) else 1


# ---------- 入口 ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="class2cal", description="把学校课表同步到 Apple 日历")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="配置账号与凭据").set_defaults(func=cmd_setup)
    sub.add_parser("status", help="查看配置与同步状态").set_defaults(func=cmd_status)

    sl = sub.add_parser("login", help="登录统一认证")
    sl.add_argument(
        "--browser",
        action="store_true",
        help="推荐：工具开浏览器，你登录并拖滑块，会话自动取回",
    )
    sl.add_argument(
        "--manual",
        action="store_true",
        help="手动登录：自己在浏览器里登录，再粘贴 cURL 导入会话",
    )
    sl.add_argument(
        "--from-file",
        metavar="PATH",
        help="从文件读取 cURL 命令或 Cookie（配合 --manual）",
    )
    sl.add_argument(
        "--channel",
        default="msedge",
        help="浏览器渠道，默认 msedge，也可用 chrome（配合 --browser）",
    )
    sl.add_argument(
        "--timeout",
        type=int,
        default=assisted_login.DEFAULT_TIMEOUT_S,
        help="等待登录完成的秒数，默认 300（配合 --browser）",
    )
    sl.set_defaults(func=cmd_login)

    sp = sub.add_parser("probe", help="探测课表卡片坐标")
    sp.add_argument(
        "--browser",
        action="store_true",
        help="推荐：开浏览器让你点进课表页，工具自动抓出接口",
    )
    sp.add_argument("--pick", type=int, metavar="N", help="选定第 N 个候选并写入配置")
    sp.add_argument("--channel", default="msedge", help="浏览器渠道，默认 msedge")
    sp.add_argument(
        "--timeout",
        type=int,
        default=probe_browser.DEFAULT_TIMEOUT_S,
        help="等待你操作的秒数，默认 300",
    )
    sp.set_defaults(func=cmd_probe)

    sc = sub.add_parser("calendars", help="列出门户日历，确认哪个是课表")
    sc.add_argument("--pick", type=int, metavar="N", help="选定第 N 个并写入配置")
    sc.set_defaults(func=cmd_calendars)

    sf = sub.add_parser("fetch", help="抓取并打印课表，不写日历")
    sf.add_argument("--weeks", type=int, help="抓取周数（含当前周）")
    sf.add_argument("--json", action="store_true", help="附带输出 JSON")
    sf.set_defaults(func=cmd_fetch)

    sst = sub.add_parser("study", help="配置自习时段（学校课表里没有晚自习）")
    sst.set_defaults(func=cmd_study, study_action="list")
    st_sub = sst.add_subparsers(dest="study_action", metavar="list|add|remove|enable|disable|preview")
    st_sub.add_parser("list", help="列出已配置的时段").set_defaults(func=cmd_study)
    st_add = st_sub.add_parser("add", help="加一条时段")
    st_add.add_argument("--title", default="晚自习", help="显示在日历上的名字，默认 晚自习")
    st_add.add_argument("--time", default="", help="时间段，如 18:00-21:00")
    st_add.add_argument(
        "--days", default="", help="星期，如 mon,tue,wed,thu,sun 或 周一,周二（默认周日到周四）"
    )
    st_add.add_argument("--start", default="", help="开始时间 HH:MM（配合 --end 用）")
    st_add.add_argument("--end", default="", help="结束时间 HH:MM（配合 --start 用）")
    st_add.add_argument("--location", default="", help="地点（可选）")
    st_add.add_argument(
        "--parity", default="all", choices=list(study.PARITY_VALUES), help="单双周，默认每周"
    )
    st_add.add_argument("--from", dest="date_from", default="", help="学期开始 YYYY-MM-DD")
    st_add.add_argument("--to", dest="date_to", default="", help="学期结束 YYYY-MM-DD")
    st_add.add_argument("--skip", default="", help="跳过日期，逗号分隔")
    st_add.set_defaults(func=cmd_study)
    st_rm = st_sub.add_parser("remove", help="删掉第 N 条时段")
    st_rm.add_argument("index", type=int, metavar="N")
    st_rm.set_defaults(func=cmd_study)
    st_sub.add_parser("enable", help="打开自习同步").set_defaults(func=cmd_study)
    st_sub.add_parser(
        "disable",
        help="关掉自习同步（抓取窗口内自动生成的事件会在下次 sync 清掉，窗口外的不动）",
    ).set_defaults(func=cmd_study)
    st_pv = st_sub.add_parser("preview", help="预览展开后的自习事件")
    st_pv.add_argument("--weeks", type=int, default=2, help="预览周数，默认 2")
    st_pv.set_defaults(func=cmd_study)

    ss = sub.add_parser("sync", help="对账写入日历")
    ss.add_argument("--weeks", type=int, help="抓取周数（含当前周）")
    ss.add_argument("--dry-run", action="store_true", help="只看 diff，不改日历")
    ss.add_argument("--yes", action="store_true", help="首次同步也直接写入")
    ss.set_defaults(func=cmd_sync)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except EOFError:  # 兜底：交互中途 stdin 关掉，不该吐一整段 traceback
        print("\n输入提前结束，本次操作未完成。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
