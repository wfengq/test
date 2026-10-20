"""Command line: attention-budget init | login-bili | login-xhs | check-sources | run | review | bot | install-tasks."""
import argparse
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .agent.review import apply_accepted, run_review, weekly_stats
from .bot.app import run_bot, send_messages, send_photo
from .bot.format import Message, format_daily, format_review
from .collectors.bilibili import BiliCollector, BiliLoginError
from .collectors.xhs import XhsCollector, XhsProcess
from .config import Config, load_config
from .ledger import AlreadyRanToday, Ledger
from .llm import DeepSeekLLM, LLMError
from .models import SourceFailure
from .pipeline import SOURCE_NAME, rebuild_result, run_daily, write_report
from .xhs_build import build_is_current, build_xhs_mcp, default_exe, tools_dir, xhs_exe_path

DEFAULT_HOME = Path.home() / ".attention-budget"
TEMPLATES = Path(__file__).resolve().parent / "templates"
LOGIN_WAIT = 180
LOCK_STALE = 3 * 3600  # seconds; a lock older than this belongs to a crashed run
XHS_LOGIN_HINT = "登录后不要在电脑浏览器登录小红书网页版，否则会把它挤下线（手机 App 不受影响）"


class FailedXhs:
    """Stands in for the Xiaohongshu collector when xiaohongshu-mcp could not start."""

    warnings: list[str] = []

    def __init__(self, failure: SourceFailure):
        self.failure = failure

    def __getattr__(self, name):
        def fail(*a, **k):
            raise self.failure
        return fail

    def remember(self, items):
        pass


def make_runtime(cfg: Config):
    llm = DeepSeekLLM(cfg.deepseek)
    bili = BiliCollector(cfg.data_dir / "bili_credential.json")

    @contextmanager
    def xhs_factory():
        try:
            proc = XhsProcess(xhs_exe_path(cfg), cfg.xhs.port, log_path=cfg.data_dir / "logs" / "xiaohongshu-mcp.log",
                    headless=cfg.xhs.headless).__enter__()
        except SourceFailure as e:
            yield FailedXhs(e)
            return
        try:
            yield XhsCollector(proc, cfg.xhs)
        finally:
            proc.__exit__(None, None, None)

    return llm, bili, xhs_factory


def _cfg(args) -> Config:
    return load_config(Path(args.config) if args.config else DEFAULT_HOME / "config.toml")


def _ledger(cfg: Config) -> Ledger:
    return Ledger(cfg.data_dir / "ledger.db")


def _emit(cfg, ledger, messages: list[Message], dry: bool, run_id: str = "") -> None:
    if dry:
        for m in messages:
            print(m.text)
            for row in m.buttons:
                print("   " + "  ".join(f"[{t}]" for t, _ in row))
            print("-" * 40)
        return
    send_messages(cfg.telegram.bot_token, cfg.telegram.chat_id, messages, ledger, run_id=run_id)


def _offer_login(cfg, bili, xhs, status: dict, dry: bool) -> None:
    """Send fresh login QR codes to Telegram and wait a few minutes for a scan."""
    if dry:
        return
    if "bili" in status and "登录过期" in status["bili"]:
        try:
            png, poll = bili.login_qr()
            send_photo(cfg.telegram.bot_token, cfg.telegram.chat_id, png, "B 站登录过期了，用 B 站 App 扫这个码（3 分钟内有效）")
            _wait(poll)
        except Exception as e:
            print(f"发 B 站登录二维码失败：{e}")
    if "xhs" in status and "未登录" in status["xhs"]:
        try:
            png = xhs.login_qr()
            send_photo(cfg.telegram.bot_token, cfg.telegram.chat_id, png,
                       "小红书没登录或被挤下线了，用小红书 App 扫这个码。" + XHS_LOGIN_HINT)
            xhs._logged_in = None
            _wait(xhs.logged_in)
        except Exception as e:
            print(f"发小红书登录二维码失败：{e}")


def _wait(check, seconds: int = LOGIN_WAIT) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(3)
    return False


def _logger(cfg) -> logging.Logger:
    log = logging.getLogger("attention_budget")
    path = (cfg.data_dir / "logs" / "attention-budget.log").resolve()
    if not any(getattr(h, "baseFilename", None) == str(path) for h in log.handlers):
        for old in list(log.handlers):
            log.removeHandler(old)
            old.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


@contextmanager
def _run_lock(cfg):
    """One round at a time: a manual run while the scheduled one is still going would double-push."""
    lock = cfg.data_dir / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE:
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield True
    finally:
        lock.unlink(missing_ok=True)


def cmd_run(args) -> int:
    cfg = _cfg(args)
    log = _logger(cfg)
    # a dry run gets its own ledger so it never uses up the day or hides items from the real run
    ledger = Ledger(cfg.data_dir / ("ledger-dryrun.db" if args.dry_run else "ledger.db"))
    with _run_lock(cfg) as got:
        if not got:
            print("正在跑，稍后再看（另一个 attention-budget run 还没结束）")
            return 0
        try:
            return _run(cfg, ledger, args, log)
        except Exception as e:  # never fail silently (spec §7)
            log.exception("run failed")
            try:
                _emit(cfg, ledger, [Message(f"⚠️ 今天没跑成：{type(e).__name__}: {e}")], args.dry_run)
            except Exception:
                log.exception("could not send the failure message")
            return 1


def _run(cfg, ledger: Ledger, args, log) -> int:
    llm, bili, xhs_factory = make_runtime(cfg)
    try:
        with xhs_factory() as xhs:
            result = run_daily(cfg, ledger, llm, bili, xhs, datetime.now(), force=args.force)
            log.info("run %s: %s, %d picks", result.run_id, result.status, len(result.picks))
            if result.status != "failed":
                _send_result(cfg, ledger, result, args.dry_run)
            _offer_login(cfg, bili, xhs, result.source_status, args.dry_run)
    except AlreadyRanToday:
        run_id = ledger.latest_run(date.today())
        if run_id and not args.dry_run and not ledger.has_pushes(run_id):
            result = rebuild_result(cfg, ledger, run_id)  # finished but the push never went out: send it now
            if result.status != "failed":
                log.info("resending unsent run %s", run_id)
                _send_result(cfg, ledger, result, args.dry_run)
                return 0
        print("今天已经跑过了（要再跑一次请加 --force）")
        return 0
    except LLMError as e:
        log.exception("DeepSeek failed")
        _emit(cfg, ledger, [Message(f"⚠️ 今天没跑成：DeepSeek 出错（{e}）。今天手动再运行一次 run 会从断点接着跑。")],
              args.dry_run)
        return 1
    if result.status == "failed":
        reasons = "；".join(f"{SOURCE_NAME.get(k, k)}：{v}" for k, v in result.source_status.items()) or "没采到任何内容"
        _emit(cfg, ledger, [Message(f"⚠️ 今天没跑成：{reasons}")], args.dry_run)
        return 1
    return 0


def _send_result(cfg, ledger: Ledger, result, dry: bool) -> None:
    report = write_report(result, cfg.data_dir / ("reports-dryrun" if dry else "reports"))
    result.extra["report"] = str(report)
    _emit(cfg, ledger, format_daily(result), dry, run_id=result.run_id)


def _warn_old_build(cfg) -> None:
    exe = xhs_exe_path(cfg)
    if exe and exe == default_exe(cfg.data_dir) and not build_is_current(exe):
        print("⚠️ tools 里的 xiaohongshu-mcp 是旧的编译版本（还会用 leakless.exe），请先运行 attention-budget build-xhs-mcp")


def cmd_check_sources(args) -> int:
    cfg = _cfg(args)
    _warn_old_build(cfg)
    _, bili, xhs_factory = make_runtime(cfg)
    ok = True
    since = datetime.now() - timedelta(hours=24)
    with xhs_factory() as xhs:
        checks = [("B站 关注更新", lambda: bili.follow_updates(since, 20)),
                  ("B站 首页推荐", lambda: bili.recommend(20)),
                  ("B站 搜索", lambda: bili.search("DeepSeek", 5)),
                  ("小红书 首页推荐", lambda: xhs.recommend(20)),
                  ("小红书 全站搜索", lambda: xhs.search("AI", 5, scope="all")),
                  ("小红书 已关注范围搜索", lambda: xhs.search("AI", 5, scope="followed"))]
        if cfg.xhs.key_creators:
            checks.append(("小红书 重点博主", lambda: xhs.key_creators_updates(cfg.xhs.key_creators, 5)))
        for name, fn in checks:
            try:
                print(f"{name}：{len(fn())} 条")
            except SourceFailure as e:
                ok = False
                print(f"{name}：失败 —— {e.reason}")
    return 0 if ok else 1


def review_week_start(today: date, review_weekday: int) -> date:
    """On the review day, review this week; when catching up later, review the last full week."""
    monday = today - timedelta(days=today.weekday())
    return monday if today.weekday() >= review_weekday else monday - timedelta(days=7)


def cmd_review(args) -> int:
    cfg = _cfg(args)
    ledger = _ledger(cfg)
    llm, _, _ = make_runtime(cfg)
    week_start = review_week_start(date.today(), cfg.schedule.review_weekday)
    week, res = run_review(llm, ledger, cfg.data_dir / "profile", week_start)
    _emit(cfg, ledger, format_review(week, weekly_stats(ledger, week_start), res), args.dry_run)
    return 0


def cmd_apply_review(args) -> int:
    cfg = _cfg(args)
    path = apply_accepted(_ledger(cfg), cfg.data_dir / "profile", args.week)
    print(f"已更新：{path}" if path else "这一周没有被采纳的修改")
    return 0


def cmd_bot(args) -> int:
    cfg = _cfg(args)
    ledger = _ledger(cfg)

    def on_review(week: str) -> None:
        rv = ledger.get_review(week)
        if rv and len(rv["decisions"]) >= len(rv["proposals"]):
            apply_accepted(ledger, cfg.data_dir / "profile", week)

    send_messages(cfg.telegram.bot_token, cfg.telegram.chat_id, [], ledger)  # flush the outbox
    run_bot(cfg, ledger, on_review_done=on_review)
    return 0


def cmd_init(args) -> int:
    home = DEFAULT_HOME
    (home / "profile").mkdir(parents=True, exist_ok=True)
    cfg_file = home / "config.toml"
    if not cfg_file.exists():
        shutil.copyfile(TEMPLATES / "config.toml", cfg_file)
    interests = home / "profile" / "interests.md"
    if not interests.exists():
        shutil.copyfile(TEMPLATES / "interests.md", interests)
    print(f"已创建：\n  配置文件 {cfg_file}\n  兴趣说明 {interests}\n下一步：照 README 把这两个文件填好。")
    return 0


def _open_file(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — opening an image the user is expected to scan
    else:
        subprocess.run(["xdg-open" if sys.platform != "darwin" else "open", str(path)], check=False)


def cmd_login_bili(args) -> int:
    cfg = _cfg(args)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    bili = BiliCollector(cfg.data_dir / "bili_credential.json")
    png, poll = bili.login_qr()
    out = cfg.data_dir / "bili_login_qr.png"
    out.write_bytes(png)
    _open_file(out)
    print(f"用 B 站 App 扫描二维码（图片：{out}），确认登录……")
    try:
        ok = _wait(poll)
    except BiliLoginError as e:
        print(f"登录失败：{e}")
        return 1
    finally:
        out.unlink(missing_ok=True)
    if ok and not bili.logged_in():
        print("凭据已保存，但 B 站说它无效，请再运行一次 login-bili")
        return 1
    print("登录成功" if ok else "超时了，再运行一次 login-bili")
    return 0 if ok else 1


def cmd_build_xhs_mcp(args) -> int:
    cfg = _cfg(args)
    dest = default_exe(cfg.data_dir).parent
    try:
        exe = build_xhs_mcp(dest, tools_dir(cfg.data_dir) / "build")
    except RuntimeError as e:
        print(f"编译失败：{e}")
        return 1
    if cfg.xhs.exe_path and Path(cfg.xhs.exe_path) != exe:
        print(f"注意：config.toml 里 xhs.exe_path 还指向 {cfg.xhs.exe_path}；改成 {exe} 或删掉这一行，才会用新编译的版本")
    return 0


def cmd_login_xhs(args) -> int:
    cfg = _cfg(args)
    _warn_old_build(cfg)
    with XhsProcess(xhs_exe_path(cfg), cfg.xhs.port, log_path=cfg.data_dir / "logs" / "xiaohongshu-mcp.log",
                    headless=cfg.xhs.headless) as proc:
        xhs = XhsCollector(proc, cfg.xhs)
        if xhs.logged_in():
            print("小红书已经是登录状态")
            return 0
        out = cfg.data_dir / "xhs_login_qr.png"
        out.write_bytes(xhs.login_qr())
        _open_file(out)
        print(f"用小红书 App 扫描二维码（图片：{out}）……\n{XHS_LOGIN_HINT}")

        def check():
            xhs._logged_in = None
            return xhs.logged_in()

        ok = _wait(check)
    print("登录成功" if ok else "超时了，再运行一次 login-xhs")
    return 0 if ok else 1


TASKS = [("attention-budget-daily", "run", "daily"), ("attention-budget-review", "review", "weekly"),
         ("attention-budget-bot", "bot", "logon")]


def cmd_install_tasks(args) -> int:
    from .windows_tasks import task_xml

    if sys.platform != "win32":
        print("只在 Windows 上需要安装计划任务")
        return 1
    cfg = _cfg(args)
    # the windowless entry point keeps a console from popping up (and the bot from dying when one is closed)
    exe = shutil.which("attention-budget-bg") or shutil.which("attention-budget") or sys.argv[0]
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    times = {"daily": cfg.schedule.push_time, "weekly": cfg.schedule.review_time, "logon": None}
    failed = 0
    for name, sub, trig in TASKS:
        xml = task_xml(name, f'"{exe}" {sub}', trig, times[trig], weekday=cfg.schedule.review_weekday, user=user)
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(xml)
        r = subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", f.name, "/F"], capture_output=True, text=True)
        Path(f.name).unlink(missing_ok=True)
        if r.returncode == 0:
            print(f"已安装计划任务：{name}")
        else:
            failed += 1
            print(f"安装 {name} 失败：{(r.stderr or r.stdout).strip()}（可以试试用管理员身份运行）")
    return 1 if failed else 0


def cmd_uninstall_tasks(args) -> int:
    for name, _, _ in TASKS:
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="attention-budget", description="每天替你从 B 站和小红书里只挑几条")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--config", default=None, help="配置文件路径（默认 ~/.attention-budget/config.toml）")
        sp.set_defaults(fn=fn)
        return sp

    add("init", cmd_init, "创建配置文件和兴趣说明模板")
    add("login-bili", cmd_login_bili, "扫码登录 B 站")
    add("login-xhs", cmd_login_xhs, "扫码登录小红书（通过 xiaohongshu-mcp）")
    add("build-xhs-mcp", cmd_build_xhs_mcp, "编译修过 2978 端口问题的 xiaohongshu-mcp（放在数据目录 tools 下）")
    add("check-sources", cmd_check_sources, "检查各来源能不能拉到内容（不调 AI、不推送）")
    r = add("run", cmd_run, "跑今天这一轮")
    r.add_argument("--force", action="store_true", help="今天已经跑过也再跑一次")
    r.add_argument("--dry-run", action="store_true", help="结果打印到终端，不发 Telegram")
    rv = add("review", cmd_review, "生成并推送本周复盘")
    rv.add_argument("--dry-run", action="store_true")
    ap = add("apply-review", cmd_apply_review, "把某一周已采纳的修改写进 understanding.md")
    ap.add_argument("--week", required=True, help="例如 2026-W43")
    add("bot", cmd_bot, "常驻接收按钮和留言")
    add("install-tasks", cmd_install_tasks, "安装 Windows 计划任务")
    add("uninstall-tasks", cmd_uninstall_tasks, "删除 Windows 计划任务")
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
