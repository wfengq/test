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
from attention_budget.xhs_build import (GO_TOOLCHAIN_SHA256, build_xhs_mcp, ensure_go, patch_leakless,
                                        xhs_exe_path)

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


def test_leakless_lock_port_moved_off_2978(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAKLESS_LOCK_PORT", raising=False)
    srv = LocalServer()
    try:
        proc, seen = start(tmp_path, srv.port)
        with proc:
            port = int(seen["env"]["LEAKLESS_LOCK_PORT"])
        assert port != 2978 and not (2930 <= port <= 3029)
    finally:
        srv.close()


def test_preset_leakless_port_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAKLESS_LOCK_PORT", "45678")
    srv = LocalServer()
    try:
        proc, seen = start(tmp_path, srv.port)
        with proc:
            assert seen["env"]["LEAKLESS_LOCK_PORT"] == "45678"
    finally:
        srv.close()


def test_browser_stuck_is_reported_as_such(tmp_path):
    srv = LocalServer(status_delay=True)
    try:
        proc, _ = start(tmp_path, srv.port)
        with pytest.raises(SourceFailure, match="浏览器"):
            with proc:
                pass
    finally:
        srv.close()


def test_patch_leakless_reads_env_and_is_idempotent():
    src = ('package leakless\n\nimport (\n\t"os"\n\t"runtime"\n)\n\nfunc New() *Launcher {\n'
           '\treturn &Launcher{\n\t\tLock: 2978,\n\t\tpid:  make(chan int),\n\t}\n}\n')
    out = patch_leakless(src)
    assert "Lock: lockPort()," in out and 'os.Getenv("LEAKLESS_LOCK_PORT")' in out and '"strconv"' in out
    assert patch_leakless(out) == out


def test_patch_leakless_refuses_unknown_source():
    with pytest.raises(RuntimeError, match="leakless"):
        patch_leakless("package leakless\n")


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
            ll = tmp_path / "llsrc"
            ll.mkdir(exist_ok=True)
            (ll / "leakless.go").write_text('import (\n\t"os"\n\t"runtime"\n)\n\t\tLock: 2978,\n', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=str(ll), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    dest = tmp_path / "tools" / "xiaohongshu-mcp"
    build_xhs_mcp(dest, tmp_path / "work", go="go", run=run, log=lambda *a: None)
    env = calls[0][2]
    assert "goproxy.cn" in env["GOPROXY"] and str(tmp_path / "work") in env["GOMODCACHE"]
    assert env["CGO_ENABLED"] == "0"
    flat = [" ".join(c[0]) for c in calls]
    assert any("-replace=github.com/ysmood/leakless=" in f for f in flat)
    assert any(f.endswith(". ") or f.endswith(" .") for f in flat if " build " in f)
    assert any("./cmd/login" in f for f in flat)
    assert "Lock: lockPort()," in (tmp_path / "work" / "leakless" / "leakless.go").read_text(encoding="utf-8")


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
