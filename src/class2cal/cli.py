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

from . import assisted_login, browser_login, calendar_sync, config, portal, schedule, sso
from .state import SyncState

log = logging.getLogger("class2cal")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )


# ---------- setup ----------


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = config.load()

    username = input(f"统一认证账号（学号）[{cfg.sso_username}]：").strip() or cfg.sso_username
    if not username:
        print("账号不能为空", file=sys.stderr)
        return 1
    cfg.sso_username = username

    # getpass 不回显；密码只进 Keychain，不写 config.toml
    password = getpass.getpass("统一认证密码（存入 Keychain，不落盘）：")
    if password:
        config.set_sso_password(username, password)
        print("已存入 Keychain")

    apple_id = input(f"Apple ID [{cfg.calendar.apple_id}]：").strip() or cfg.calendar.apple_id
    if apple_id:
        cfg.calendar.apple_id = apple_id
        print("Apple 应用专用密码请到 appleid.apple.com 生成（不是账户密码）")
        apple_pw = getpass.getpass("Apple 应用专用密码（存入 Keychain）：")
        if apple_pw:
            # Apple 展示的专用密码带连字符，去掉更省事
            config.set_apple_password(apple_id, apple_pw.replace("-", "").strip())
            print("已存入 Keychain")

    cal_name = (
        input(f"目标日历名 [{cfg.calendar.calendar_name}]：").strip()
        or cfg.calendar.calendar_name
    )
    cfg.calendar.calendar_name = cal_name

    config.save(cfg)
    config.ensure_var_dirs(cfg)
    print(f"配置已写入 {config.CONFIG_PATH}")
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

    print(f"登录成功：{_user_display_name(sso.verify_session(session) or {})}")
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

    print(browser_login.INSTRUCTIONS)
    try:
        browser_login.open_login_page()
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


def cmd_probe(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)
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
            "没自动识别出课表卡片。接下来这样做：\n"
            f"  1. 翻一下 {cfg.probe_dir}/*.json，搜「课」字找卡片的 cardWid / cardId\n"
            "  2. 或用 Edge 开 iPhone 设备模拟登录课表页，在 Network 面板找 execCardMethod 请求\n"
            "  3. 把找到的坐标填进 config.toml 的 [schedule] card_wid / card_id",
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
        print("接着跑 `class2cal fetch` 验证能不能取到课。")
    else:
        print("\n确认哪个是课表后，跑 `class2cal probe --pick N` 写入配置。")
    return 0


# ---------- fetch ----------


def _fetch_weekly(
    cfg: config.Config, weeks_ahead: int
) -> tuple[dict[date, list[schedule.Lesson]], list[date]]:
    """按周抓取。返回 (成功的周 -> 课程列表, 失败的周)。

    失败的周不进结果字典 —— 这是安全闸 2：只有抓取成功的周才参与对账。
    """
    session = sso.ensure_session(cfg)
    client = portal.PortalClient(session, cfg)

    weekly: dict[date, list[schedule.Lesson]] = {}
    failed: list[date] = []

    for monday in schedule.iter_week_starts(date.today(), weeks_ahead):
        start, end = schedule.week_range(monday)
        try:
            raw = client.fetch_schedule(start, end, archive_dir=cfg.raw_dir)
        except portal.CardNotFound:
            raise
        except portal.PortalError as exc:
            log.warning("抓取 %s ~ %s 失败：%s", start, end, exc)
            failed.append(monday)
            continue

        lessons = schedule.parse_lessons(
            raw, period_times=cfg.schedule.period_times
        )
        # 只保留落在本周窗口内的课，避免接口返回超范围数据污染对账
        weekly[monday] = [le for le in lessons if start <= le.date <= end]

    return weekly, failed


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = config.load()
    config.ensure_var_dirs(cfg)
    weeks = args.weeks or cfg.schedule.weeks_ahead

    try:
        weekly, failed = _fetch_weekly(cfg, weeks)
    except (sso.SsoError, portal.PortalError, config.ConfigError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1

    total = sum(len(v) for v in weekly.values())
    for monday in sorted(weekly):
        start, end = schedule.week_range(monday)
        lessons = weekly[monday]
        print(f"\n{start} ~ {end}（{len(lessons)} 节）")
        for le in lessons:
            mark = " [时间推算]" if le.source == "period" else ""
            room = f" @{le.room}" if le.room else ""
            teacher = f" {le.teacher}" if le.teacher else ""
            print(f"  {le.date} {le.start}-{le.end}  {le.course}{room}{teacher}{mark}")

    print(f"\n共 {total} 节课，覆盖 {len(weekly)} 周")
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
                    "source": le.source,
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
    except (sso.SsoError, portal.PortalError, config.ConfigError) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
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

    session = sso.load_session(cfg.session_path)
    alive = bool(session and sso.verify_session(session))
    print(f"会话状态      {'有效' if alive else '无效/不存在'}")

    if st.weeks:
        print("\n已同步的周：")
        for k in sorted(st.weeks):
            rec = st.weeks[k]
            print(f"  {k}  {rec.count} 节  {rec.synced_at}")
    else:
        print("\n还没同步过")
    return 0


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
    sp.add_argument("--pick", type=int, metavar="N", help="选定第 N 个候选并写入配置")
    sp.set_defaults(func=cmd_probe)

    sf = sub.add_parser("fetch", help="抓取并打印课表，不写日历")
    sf.add_argument("--weeks", type=int, help="抓取周数（含当前周）")
    sf.add_argument("--json", action="store_true", help="附带输出 JSON")
    sf.set_defaults(func=cmd_fetch)

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


if __name__ == "__main__":
    sys.exit(main())
