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
