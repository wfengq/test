"""Command line: attention-budget init | login-bili | login-xhs | check-sources | run | review | bot | install-tasks."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from .agent.review import apply_accepted, run_review, weekly_stats
from .bot.app import run_bot, send_messages, send_photo
from .bot.format import Message, format_daily, format_review
from .collectors.bilibili import BiliCollector
from .collectors.xhs import XhsCollector, XhsProcess
from .config import Config, load_config
from .ledger import AlreadyRanToday, Ledger
from .llm import DeepSeekLLM, LLMError
from .models import SourceFailure
from .pipeline import SOURCE_NAME, run_daily, write_report

DEFAULT_HOME = Path.home() / ".attention-budget"
TEMPLATES = Path(__file__).resolve().parent / "templates"
LOGIN_WAIT = 180
XHS_LOGIN_HINT = "登录后不要在电脑浏览器登录小红书网页版，否则会把它挤下线（手机 App 不受影响）"


class FailedXhs:
    """Stands in for the Xiaohongshu collector when xiaohongshu-mcp could not start."""

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
            proc = XhsProcess(cfg.xhs.exe_path, cfg.xhs.port).__enter__()
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


def cmd_run(args) -> int:
    cfg = _cfg(args)
    ledger = _ledger(cfg)
    llm, bili, xhs_factory = make_runtime(cfg)
    try:
        with xhs_factory() as xhs:
            result = run_daily(cfg, ledger, llm, bili, xhs, datetime.now(), force=args.force)
            _offer_login(cfg, bili, xhs, result.source_status, args.dry_run)
    except AlreadyRanToday:
        print("今天已经跑过了（要再跑一次请加 --force）")
        return 0
    except LLMError as e:
        _emit(cfg, ledger, [Message(f"⚠️ 今天没跑成：DeepSeek 出错（{e}）。下次运行会从断点接着跑。")], args.dry_run)
        return 1
    if result.status == "failed":
        reasons = "；".join(f"{SOURCE_NAME.get(k, k)}：{v}" for k, v in result.source_status.items()) or "没采到任何内容"
        _emit(cfg, ledger, [Message(f"⚠️ 今天没跑成：{reasons}")], args.dry_run)
        return 1
    report = write_report(result, cfg.data_dir / "reports")
    result.extra["report"] = str(report)
    _emit(cfg, ledger, format_daily(result), args.dry_run, run_id=result.run_id)
    return 0


def cmd_check_sources(args) -> int:
    cfg = _cfg(args)
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


def cmd_review(args) -> int:
    cfg = _cfg(args)
    ledger = _ledger(cfg)
    llm, _, _ = make_runtime(cfg)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
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
    bili = BiliCollector(cfg.data_dir / "bili_credential.json")
    png, poll = bili.login_qr()
    out = cfg.data_dir / "bili_login_qr.png"
    out.write_bytes(png)
    _open_file(out)
    print(f"用 B 站 App 扫描二维码（图片：{out}），确认登录……")
    ok = _wait(poll)
    print("登录成功" if ok else "超时了，再运行一次 login-bili")
    return 0 if ok else 1


def cmd_login_xhs(args) -> int:
    cfg = _cfg(args)
    with XhsProcess(cfg.xhs.exe_path, cfg.xhs.port) as proc:
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
    exe = shutil.which("attention-budget") or sys.argv[0]
    times = {"daily": cfg.schedule.push_time, "weekly": cfg.schedule.review_time, "logon": None}
    for name, sub, trig in TASKS:
        xml = task_xml(name, f'"{exe}" {sub}', trig, times[trig])
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(xml)
        subprocess.run(["schtasks", "/Create", "/TN", name, "/XML", f.name, "/F"], check=True)
        Path(f.name).unlink(missing_ok=True)
        print(f"已安装计划任务：{name}")
    return 0


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
