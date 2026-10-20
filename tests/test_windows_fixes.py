"""Regression tests for the problems found during the first Windows install."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from attention_budget.collectors.bilibili import BiliCollector, BiliLoginError
from attention_budget.collectors.xhs import XhsCollector, XhsProcess
from attention_budget.config import XhsCfg
from attention_budget.models import SourceFailure
from attention_budget.xhs_build import (BUILD_ID, GO_TOOLCHAIN_SHA256, build_xhs_mcp, ensure_go,
                                        patch_headless_browser, xhs_exe_path)

# ---------- 1. B站 login saved only ac_time_value ----------


class FakeQr:
    def __init__(self, cred):
        self.cred = cred
        self.platform = None

    async def generate_qrcode(self):
        pass

    def get_qrcode_picture(self):
        return SimpleNamespace(content=b"png")

    async def check_state(self):
        return "DONE"

    def has_done(self):
        return True

    def get_credential(self):
        return self.cred


class LoginApi:
    def __init__(self, cred):
        self.qr = FakeQr(cred)
        self.channel = None

    def qr_login(self, channel="tv"):
        self.channel = channel
        return self.qr


def cred(**kw):
    base = dict(sessdata=None, bili_jct=None, buvid3=None, buvid4=None, dedeuserid=None, ac_time_value=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_login_uses_tv_channel(tmp_path):
    api = LoginApi(cred(sessdata="s", bili_jct="j", dedeuserid="1", ac_time_value="a"))
    BiliCollector(tmp_path / "c.json", api=api).login_qr()
    assert api.channel == "tv"


def test_incomplete_credential_is_rejected_and_not_saved(tmp_path):
    api = LoginApi(cred(ac_time_value="only-refresh-token"))
    _, poll = BiliCollector(tmp_path / "c.json", api=api).login_qr()
    with pytest.raises(BiliLoginError, match="sessdata"):
        poll()
    assert not (tmp_path / "c.json").exists()


def test_complete_credential_saved_with_required_fields(tmp_path):
    api = LoginApi(cred(sessdata="s", bili_jct="j", dedeuserid="1", ac_time_value="a"))
    _, poll = BiliCollector(tmp_path / "c.json", api=api).login_qr()
    assert poll() is True
    saved = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert {"sessdata", "bili_jct", "dedeuserid"} <= set(saved)


# ---------- 2. leakless lock port 2978 excluded on Windows ----------

class Proc:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1
        return self.t


class LocalServer:
    """A real HTTP server on 127.0.0.1 standing in for xiaohongshu-mcp."""

    def __init__(self, status_delay=False):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/v1/login/status" and outer.status_delay:
                    self.send_response(504)
                    self.end_headers()
                    return
                body = json.dumps({"success": True, "data": {"is_logged_in": True}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.status_delay = status_delay
        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def start(tmp_path, port, **kw):
    exe = tmp_path / "xiaohongshu-mcp.exe"
    exe.write_bytes(b"")
    seen = {}

    def popen(args, **k):
        seen["env"] = k.get("env") or {}
        return Proc()

    return XhsProcess(exe, port, popen=popen, clock=Clock(), sleep=lambda s: None, **kw), seen


def test_unexplained_5xx_says_so_instead_of_guessing(tmp_path):
    srv = LocalServer(status_delay=True)
    try:
        proc, _ = start(tmp_path, srv.port)
        with pytest.raises(SourceFailure) as e:
            with proc:
                pass
        assert "HTTP 504" in e.value.reason and "2978" not in e.value.reason
    finally:
        srv.close()


def test_patch_headless_browser_disables_leakless_and_is_idempotent():
    src = "func New(options ...Option) *Browser {\n\tl := launcher.New().\n\t\tHeadless(cfg.Headless).\n}\n"
    out = patch_headless_browser(src)
    assert "launcher.New().Leakless(false)." in out
    assert patch_headless_browser(out) == out


def test_patch_headless_browser_refuses_unknown_source():
    with pytest.raises(RuntimeError, match="headless_browser"):
        patch_headless_browser("package headless_browser\n")


def test_build_runs_go_with_local_caches_and_replace(tmp_path):
    calls = []

    def run(cmd, cwd=None, env=None, **kw):
        calls.append((cmd, cwd, env))
        if cmd[1:3] == ["mod", "download"] and "-json" in cmd:
            src = tmp_path / "modsrc"
            src.mkdir(exist_ok=True)
            (src / "go.mod").write_text("module x\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Dir": str(src)}), stderr="")
        if cmd[1:3] == ["list", "-m"]:
            hb = tmp_path / "hbsrc"
            hb.mkdir(exist_ok=True)
            (hb / "headless_browser.go").write_text("\tl := launcher.New().\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=str(hb), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    dest = tmp_path / "tools" / "xiaohongshu-mcp"
    build_xhs_mcp(dest, tmp_path / "work", go="go", run=run, log=lambda *a: None)
    env = calls[0][2]
    assert "goproxy.cn" in env["GOPROXY"] and str(tmp_path / "work") in env["GOMODCACHE"]
    assert env["GOPROXY"].split("|")[:2] == ["https://goproxy.cn", "https://proxy.golang.org"]  # fall back on any error
    assert env["CGO_ENABLED"] == "0"
    flat = [" ".join(c[0]) for c in calls]
    assert any("-replace=github.com/xpzouying/headless_browser=" in f for f in flat)
    assert not any("leakless" in f for f in flat)
    assert any("./cmd/login" in f for f in flat)
    patched = (tmp_path / "work" / "headless_browser" / "headless_browser.go").read_text(encoding="utf-8")
    assert "Leakless(false)" in patched
    assert json.loads((dest / "build-info.json").read_text(encoding="utf-8"))["build_id"] == BUILD_ID


def test_ensure_go_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr("attention_budget.xhs_build.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="校验"):
        ensure_go(tmp_path, fetch=lambda url: b"not a go toolchain", platform="win32")


def test_ensure_go_prefers_existing_go(tmp_path, monkeypatch):
    monkeypatch.setattr("attention_budget.xhs_build.shutil.which", lambda name: "/usr/bin/go")
    assert ensure_go(tmp_path, fetch=lambda url: b"") == "/usr/bin/go"


def test_go_toolchain_checksum_pinned():
    assert len(GO_TOOLCHAIN_SHA256) == 64


def test_built_exe_used_when_exe_path_not_set(tmp_path):
    cfg = SimpleNamespace(data_dir=tmp_path, xhs=XhsCfg())
    built = tmp_path / "tools" / "xiaohongshu-mcp" / "xiaohongshu-mcp.exe"
    built.parent.mkdir(parents=True)
    built.write_bytes(b"")
    assert xhs_exe_path(cfg) == built
    cfg.xhs.exe_path = tmp_path / "other.exe"
    assert xhs_exe_path(cfg) == tmp_path / "other.exe"


# ---------- 3. system proxy turns localhost calls into 502 ----------

def test_local_calls_ignore_proxy_settings(tmp_path, monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")  # nothing listens here: any proxied call fails
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    srv = LocalServer()
    try:
        proc, _ = start(tmp_path, srv.port)
        with proc as p:
            xc = XhsCollector(p, XhsCfg(), sleep=lambda s: None, ocr=lambda u, n: "")
            assert xc.logged_in() is True
    finally:
        srv.close()


# ---------- round 2: leakless.exe blocked by Windows Security; misleading 500 message ----------

import os
import signal
import subprocess
import sys
import time

from attention_budget.collectors.xhs import ProcessTree, explain_server_error

VIRUS_LINE = ('time="2026-10-20T20:50:12+08:00" level=error msg="服务器内部错误: fork/exec C:\\\\Users\\\\wei\\\\'
              'Desktop\\\\attention-budget\\\\.attention-budget\\\\runtime\\\\temp\\\\leakless-amd64-adb8\\\\leakless.exe: '
              'Operation did not complete successfully because the file contains a virus or potentially unwanted '
              'software., path: /api/v1/login/status"\n')


def test_virus_block_is_explained_without_blaming_port_2978():
    msg = explain_server_error(500, {"error": "服务器内部错误", "code": "INTERNAL_ERROR"}, VIRUS_LINE)
    assert "Windows 安全中心" in msg and "leakless.exe" in msg and "build-xhs-mcp" in msg
    assert "2978" not in msg


def test_real_error_is_shown_and_secrets_are_stripped():
    log = ('time="x" level=error msg="服务器内部错误: navigate https://www.xiaohongshu.com/explore/1?xsec_token=SECRETTOKEN123 '
           'failed: net::ERR_PROXY_CONNECTION_FAILED Authorization: Bearer SECRETBEARER web_session=SECRETSESSION, path: /x"\n')
    msg = explain_server_error(500, {"error": "服务器内部错误"}, log)
    assert "ERR_PROXY_CONNECTION_FAILED" in msg
    for secret in ("SECRETTOKEN123", "SECRETBEARER", "SECRETSESSION"):
        assert secret not in msg
    assert "2978" not in msg


def test_body_error_used_when_log_has_nothing():
    msg = explain_server_error(500, {"error": "获取Feeds列表失败", "details": "context deadline exceeded"}, "")
    assert "获取Feeds列表失败" in msg and "context deadline exceeded" in msg


def test_500_on_startup_reports_the_real_reason(tmp_path):
    srv = LocalServer(status_delay=True)  # answers 500-class for login/status
    log = tmp_path / "xhs.log"
    try:
        proc, _ = start(tmp_path, srv.port, log_path=log)
        log.write_text(VIRUS_LINE, encoding="utf-8")
        with pytest.raises(SourceFailure) as e:
            with proc:
                pass
        assert "Windows 安全中心" in e.value.reason and "2978" not in e.value.reason
    finally:
        srv.close()


def test_child_output_goes_to_log_file(tmp_path):
    srv = LocalServer()
    log = tmp_path / "logs" / "xiaohongshu-mcp.log"
    try:
        exe = tmp_path / "xiaohongshu-mcp.exe"
        exe.write_bytes(b"")
        seen = {}

        def popen(args, **k):
            seen.update(k)
            return Proc()

        with XhsProcess(exe, srv.port, popen=popen, clock=Clock(), sleep=lambda s: None, log_path=log):
            pass
        assert seen["stdout"] is not subprocess.DEVNULL and seen["stderr"] == subprocess.STDOUT
        assert log.exists()
    finally:
        srv.close()


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); treat it as dead
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return False


def test_stopping_kills_the_whole_process_tree(tmp_path):
    """Stands in for xiaohongshu-mcp -> chrome.exe: the grandchild must die with the parent."""
    pidfile = tmp_path / "child.pid"
    script = (f"import subprocess,sys,time;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
              f"open(r'{pidfile}','w').write(str(p.pid));time.sleep(300)")
    tree = ProcessTree.spawn([sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    child = int(pidfile.read_text())
    assert _alive(child)
    tree.kill()
    for _ in range(100):
        if not _alive(child):
            break
        time.sleep(0.05)
    assert not _alive(child) and tree.poll() is not None


def test_old_build_is_detected(tmp_path):
    from attention_budget.xhs_build import build_is_current
    exe = tmp_path / "xiaohongshu-mcp.exe"
    exe.write_bytes(b"")
    assert build_is_current(exe) is False  # last round's build wrote no build-info.json
    (tmp_path / "build-info.json").write_text(json.dumps({"build_id": BUILD_ID}), encoding="utf-8")
    assert build_is_current(exe) is True


# ---------- round 3: search / profile pages never reach network idle ----------

from attention_budget.xhs_build import patch_wait_for_dom


def test_wait_stable_replaced_by_dom_stable():
    src = ("\tpage.MustNavigate(searchURL)\n\tpage.MustWaitStable()\n"
           "\tpage.MustWait(`() => window.__INITIAL_STATE__ !== undefined`)\n")
    out = patch_wait_for_dom(src, "search.go")
    assert "page.MustWaitDOMStable()" in out and "MustWaitStable()" not in out
    assert "__INITIAL_STATE__" in out  # still waits for the data itself
    assert patch_wait_for_dom(out, "search.go") == out


def test_wait_patch_refuses_unknown_source():
    with pytest.raises(RuntimeError, match="search.go"):
        patch_wait_for_dom("package xiaohongshu\n", "search.go")


def test_build_patches_search_and_profile(tmp_path):
    calls = []

    def run(cmd, cwd=None, env=None, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["mod", "download"] and "-json" in cmd:
            src = tmp_path / "modsrc"
            (src / "xiaohongshu").mkdir(parents=True, exist_ok=True)
            (src / "go.mod").write_text("module x\n", encoding="utf-8")
            for f in ("search.go", "user_profile.go"):
                (src / "xiaohongshu" / f).write_text("\tpage.MustWaitStable()\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Dir": str(src)}), stderr="")
        if cmd[1:3] == ["list", "-m"]:
            hb = tmp_path / "hbsrc"
            hb.mkdir(exist_ok=True)
            (hb / "headless_browser.go").write_text("\tl := launcher.New().\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=str(hb), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    build_xhs_mcp(tmp_path / "out", tmp_path / "work", go="go", run=run, log=lambda *a: None)
    for f in ("search.go", "user_profile.go"):
        assert "MustWaitDOMStable()" in (tmp_path / "work" / "src" / "xiaohongshu" / f).read_text(encoding="utf-8")


def test_cold_start_timeout_on_login_status_is_retried_once(tmp_path):
    hits = {"n": 0}

    class Http:
        def get(self, url, timeout=None, headers=None):
            if url.endswith("/health"):
                return httpx.Response(200, json={})
            hits["n"] += 1
            if hits["n"] == 1:
                return httpx.Response(500, json={"error": "服务器内部错误", "details": "context deadline exceeded"})
            return httpx.Response(200, json={"success": True, "data": {"is_logged_in": True}})

    exe = tmp_path / "x.exe"
    exe.write_bytes(b"")
    with XhsProcess(exe, 18060, popen=lambda a, **k: Proc(), http=Http(), clock=Clock(), sleep=lambda s: None):
        pass
    assert hits["n"] == 2


def test_headless_can_be_turned_off_for_diagnosis(tmp_path):
    srv = LocalServer()
    try:
        exe = tmp_path / "x.exe"
        exe.write_bytes(b"")
        seen = {}

        def popen(args, **k):
            seen["args"] = args
            return Proc()

        with XhsProcess(exe, srv.port, popen=popen, clock=Clock(), sleep=lambda s: None, headless=False):
            pass
        assert "-headless=false" in seen["args"]
    finally:
        srv.close()


# ---------- round 4: login check panics in MustWaitLoad (login.go:23) ----------

from attention_budget.xhs_build import patch_load_waits

LOGIN_SRC = ('func (a *LoginAction) CheckLoginStatus(ctx context.Context) (bool, error) {\n'
             '\tpp := a.page.Context(ctx).Timeout(30 * time.Second)\n'
             '\tpp.MustNavigate("https://www.xiaohongshu.com/explore").MustWaitLoad()\n'
             '\ttime.Sleep(1 * time.Second)\n'
             '\texists, _, err := pp.Has(`.main-container .user .link-wrapper .channel`)\n}\n'
             'func (a *LoginAction) Login(ctx context.Context) error {\n'
             '\tpp.MustNavigate("https://www.xiaohongshu.com/explore").MustWaitLoad()\n}\n')
READY_SRC = ('func waitFeedPageReady(page *rod.Page) {\n\tpage.MustWaitLoad()\n\n'
             '\tif _, err := page.Timeout(feedReadyTimeout).Element(feedReadySelectors); err != nil {\n\t}\n}\n')


def test_login_waits_for_dom_not_the_load_event():
    out = patch_load_waits(LOGIN_SRC, "login.go")
    assert "MustWaitLoad" not in out and out.count(".MustWaitDOMStable()") == 2
    assert ".main-container .user .link-wrapper .channel" in out  # same login check afterwards
    assert patch_load_waits(out, "login.go") == out


def test_feed_detail_drops_the_load_wait_but_keeps_the_container_wait():
    out = patch_load_waits(READY_SRC, "page_ready.go")
    assert "MustWaitLoad" not in out and "Element(feedReadySelectors)" in out
    assert patch_load_waits(out, "page_ready.go") == out


def test_load_patch_refuses_unknown_source():
    with pytest.raises(RuntimeError, match="login.go"):
        patch_load_waits("package xiaohongshu\n", "login.go")


def test_build_patches_login_and_detail(tmp_path):
    def run(cmd, cwd=None, env=None, **kw):
        if cmd[1:3] == ["mod", "download"] and "-json" in cmd:
            src = tmp_path / "modsrc"
            (src / "xiaohongshu").mkdir(parents=True, exist_ok=True)
            (src / "go.mod").write_text("module x\n", encoding="utf-8")
            (src / "xiaohongshu" / "login.go").write_text(LOGIN_SRC, encoding="utf-8")
            (src / "xiaohongshu" / "page_ready.go").write_text(READY_SRC, encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Dir": str(src)}), stderr="")
        if cmd[1:3] == ["list", "-m"]:
            hb = tmp_path / "hbsrc"
            hb.mkdir(exist_ok=True)
            (hb / "headless_browser.go").write_text("\tl := launcher.New().\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=str(hb), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    build_xhs_mcp(tmp_path / "out", tmp_path / "work", go="go", run=run, log=lambda *a: None)
    for f in ("login.go", "page_ready.go"):
        assert "MustWaitLoad" not in (tmp_path / "work" / "src" / "xiaohongshu" / f).read_text(encoding="utf-8")
    assert json.loads((tmp_path / "out" / "build-info.json").read_text(encoding="utf-8"))["build_id"] == BUILD_ID
    assert BUILD_ID == "no-leakless-3"
