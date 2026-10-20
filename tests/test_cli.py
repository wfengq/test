from contextlib import contextmanager

import pytest

import attention_budget.cli as cli
from attention_budget.cli import main
from attention_budget.windows_tasks import task_xml
from tests.conftest import make_item
from tests.fakes import FakeCollector, FakeLLM

IDS = ["bili:BV1", "bili:BV2", "bili:BV3", "xhs:a", "xhs:b"]


class ReadyEnv:
    def __init__(self, tmp_path, monkeypatch):
        data = tmp_path / "data"
        self.config = tmp_path / "config.toml"
        self.config.write_text('data_dir = "' + data.as_posix() + '"\n[deepseek]\napi_key="k"\n'
                               '[telegram]\nbot_token="t"\nchat_id=42\n', encoding="utf-8")
        self.bili = FakeCollector("bili", {"follow_updates": [make_item("bili:BV1", "follow")],
                                           "recommend": [make_item("bili:BV2", "recommend")],
                                           "search": [make_item("bili:BV3", "search")]})
        self.xhs = FakeCollector("xhs", {"recommend": [make_item("xhs:a", "recommend")],
                                         "search": [make_item("xhs:b", "search")]})
        self.llm = FakeLLM({
            "SearchPlan": [{"queries": [{"platform": "bili", "query": "q"}, {"platform": "xhs", "query": "q2"}]}],
            "CoarseResult": [{"scores": [{"id": i, "score": 60, "reason": "r"} for i in IDS], "rescue": []}],
            "DeepStep": [{"action": "verdict", "decision": "pick", "reason": "好", "note_to_user": "给你"}],
            "FinalResult": [{"picks": [{"id": i, "rank": n} for n, i in enumerate(IDS)], "serendipity": None,
                             "reject_summary": []}],
        })

        @contextmanager
        def xhs_factory():
            yield self.xhs

        monkeypatch.setattr(cli, "make_runtime", lambda cfg: (self.llm, self.bili, xhs_factory))
        self.sent = []
        monkeypatch.setattr(cli, "send_messages", lambda *a, **k: self.sent.append(a))


@pytest.fixture
def ready_env(tmp_path, monkeypatch):
    return ReadyEnv(tmp_path, monkeypatch)


def test_dry_run_prints_and_does_not_send(ready_env, capsys):
    assert main(["run", "--dry-run", "--config", str(ready_env.config)]) == 0
    out = capsys.readouterr().out
    assert ready_env.sent == [] and "今天的 5 条" in out and "今日账单" in out


def test_real_run_sends(ready_env):
    assert main(["run", "--config", str(ready_env.config)]) == 0
    assert len(ready_env.sent) == 1


def test_already_ran_today_exits_zero(ready_env, capsys):
    main(["run", "--dry-run", "--config", str(ready_env.config)])
    assert main(["run", "--dry-run", "--config", str(ready_env.config)]) == 0
    assert "今天已经跑过" in capsys.readouterr().out


def test_both_sources_down_reports_failure(ready_env, capsys):
    ready_env.bili.fail_all("登录过期")
    ready_env.xhs.fail_all("未登录")
    assert main(["run", "--dry-run", "--config", str(ready_env.config)]) == 1
    assert "今天没跑成" in capsys.readouterr().out


def test_check_sources_prints_counts(ready_env, capsys):
    assert main(["check-sources", "--config", str(ready_env.config)]) == 0
    out = capsys.readouterr().out
    assert "B站 关注更新：1 条" in out and "小红书 首页推荐：1 条" in out


def test_init_writes_config_and_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_HOME", tmp_path / "home")
    assert main(["init"]) == 0
    assert (tmp_path / "home" / "config.toml").exists()
    assert "一律不要的内容" in (tmp_path / "home" / "profile" / "interests.md").read_text(encoding="utf-8")


def test_task_xml_catches_up_missed_runs():
    xml = task_xml("attention-budget-daily", r"C:\x\attention-budget.exe run", "daily", "08:00")
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml and "T08:00:00" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<Command>C:\\x\\attention-budget.exe</Command>" in xml and "<Arguments>run</Arguments>" in xml


def test_task_xml_weekly_and_logon():
    assert "<Sunday />" in task_xml("w", "a.exe review", "weekly", "20:00")
    assert "<LogonTrigger>" in task_xml("b", "a.exe bot", "logon", None)


# ---- final review fixes ----

def test_i2_crash_before_send_is_resent_next_start(ready_env, monkeypatch):
    calls = []

    def flaky(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("console closed")

    monkeypatch.setattr(cli, "send_messages", flaky)
    assert main(["run", "--config", str(ready_env.config)]) == 1
    assert main(["run", "--config", str(ready_env.config)]) == 0
    # call 1 crashed, call 2 was the "没跑成" notice, call 3 is the resend of the real push
    assert "今天没跑成" in calls[1][2][0].text and any("今天的 5 条" in m.text for m in calls[-1][2])


def test_i5_unexpected_error_sends_failure_and_logs(ready_env, monkeypatch):
    def boom(*a, **k):
        raise ValueError("坏了")

    monkeypatch.setattr(cli, "run_daily", boom)
    assert main(["run", "--config", str(ready_env.config)]) == 1
    texts = [m.text for call in ready_env.sent for m in call[2]]
    assert any("今天没跑成" in t and "ValueError" in t for t in texts)
    logs = list((ready_env.config.parent / "data" / "logs").glob("*.log"))
    assert logs and "坏了" in logs[0].read_text(encoding="utf-8")


def test_i6_dry_run_does_not_use_up_the_day(ready_env, capsys):
    assert main(["run", "--dry-run", "--config", str(ready_env.config)]) == 0
    assert main(["run", "--config", str(ready_env.config)]) == 0
    assert len(ready_env.sent) == 1 and any("今天的 5 条" in m.text for m in ready_env.sent[0][2])


def test_i8_concurrent_start_is_refused(ready_env, capsys):
    lock = ready_env.config.parent / "data" / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345", encoding="utf-8")
    assert main(["run", "--config", str(ready_env.config)]) == 0
    assert "正在跑" in capsys.readouterr().out and ready_env.sent == []


def test_i7_review_week_on_review_day_and_after():
    from datetime import date
    from attention_budget.cli import review_week_start
    sunday, monday = date(2026, 10, 25), date(2026, 10, 26)
    assert review_week_start(sunday, review_weekday=6) == date(2026, 10, 19)
    assert review_week_start(monday, review_weekday=6) == date(2026, 10, 19)


def test_i7_task_xml_uses_configured_weekday():
    assert "<Saturday />" in task_xml("w", "a.exe review", "weekly", "20:00", weekday=5)


def test_i10_task_xml_runs_as_given_user():
    xml = task_xml("b", "a.exe bot", "logon", None, user="PC\\me")
    assert xml.count("<UserId>PC\\me</UserId>") == 2


# ---- Windows install fixes ----

def test_login_bili_reports_incomplete_credential(tmp_path, monkeypatch, capsys):
    from attention_budget.collectors.bilibili import BiliLoginError
    cfgf = tmp_path / "config.toml"
    cfgf.write_text('data_dir = "' + (tmp_path / "d").as_posix() + '"\n[deepseek]\napi_key="k"\n'
                    '[telegram]\nbot_token="t"\nchat_id=42\n', encoding="utf-8")

    def login_qr(self):
        def poll():
            raise BiliLoginError("B站返回的登录信息不完整（缺少 sessdata），没有保存，请重新扫码")
        return b"png", poll

    monkeypatch.setattr(cli.BiliCollector, "login_qr", login_qr)
    monkeypatch.setattr(cli, "_open_file", lambda p: None)
    assert main(["login-bili", "--config", str(cfgf)]) == 1
    out = capsys.readouterr().out
    assert "不完整" in out and "登录成功" not in out


def test_build_xhs_mcp_command_builds_into_data_dir(tmp_path, monkeypatch, capsys):
    cfgf = tmp_path / "config.toml"
    data = tmp_path / "d"
    cfgf.write_text('data_dir = "' + data.as_posix() + '"\n[deepseek]\napi_key="k"\n'
                    '[telegram]\nbot_token="t"\nchat_id=42\n', encoding="utf-8")
    seen = {}

    def fake_build(dest, work, **kw):
        seen["dest"], seen["work"] = dest, work
        return dest / "xiaohongshu-mcp.exe"

    monkeypatch.setattr(cli, "build_xhs_mcp", fake_build)
    assert main(["build-xhs-mcp", "--config", str(cfgf)]) == 0
    assert seen["dest"] == data / "tools" / "xiaohongshu-mcp" and seen["work"] == data / "tools" / "build"
