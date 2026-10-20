"""Build a patched xiaohongshu-mcp whose leakless lock port is configurable.

Why: xiaohongshu-mcp launches its browser through go-rod, which uses ysmood/leakless. leakless v0.9.0 serialises
extracting its helper binary with a TCP "lock" on 127.0.0.1:2978 and retries forever when it cannot bind.
Some Windows machines exclude 2978 from binding (WinError 10013), so the browser never starts and every
xiaohongshu-mcp call hangs. The port is hard-coded, so the only fix is a rebuild with a small patch that reads
LEAKLESS_LOCK_PORT. Everything (Go, module cache, sources, binaries) stays inside the data directory.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Callable

import httpx

XHS_MODULE = "github.com/xpzouying/xiaohongshu-mcp"
XHS_COMMIT = "7797fd375aba02dcd33afe143f6bee13e5870859"  # 2026-10-09, the version the collector was written for
LEAKLESS_MODULE = "github.com/ysmood/leakless"
GO_VERSION = "1.24.7"
GO_TOOLCHAIN = f"golang.org/toolchain@v0.0.1-go{GO_VERSION}.windows-amd64"
# sha256 of the module zip above, identical on every Go module proxy (verified against sum.golang.org)
GO_TOOLCHAIN_SHA256 = "615b342308b2ce49b2c1198070c39edbb897c02c943d573c2a88ae02ddfae337"
GO_PROXIES = ["https://goproxy.cn", "https://proxy.golang.org"]
EXE_NAME = "xiaohongshu-mcp.exe"
LOGIN_EXE_NAME = "xiaohongshu-login.exe"

_PATCH_FUNC = '''
// lockPort lets the cross-process lock port be moved off 2978, which some Windows
// machines exclude from binding (WinError 10013). Patched by attention-budget.
func lockPort() int {
	if v, err := strconv.Atoi(os.Getenv("LEAKLESS_LOCK_PORT")); err == nil && v > 0 && v < 65536 {
		return v
	}
	return 2978
}
'''


def patch_leakless(src: str) -> str:
    if "func lockPort() int" in src:
        return src
    if "Lock: 2978," not in src or '\t"runtime"\n' not in src:
        raise RuntimeError("leakless 源码和预期不一致，无法打补丁（版本变了？）")
    src = src.replace("Lock: 2978,", "Lock: lockPort(),", 1)
    if '\t"strconv"\n' not in src:
        src = src.replace('\t"runtime"\n', '\t"runtime"\n\t"strconv"\n', 1)
    return src + _PATCH_FUNC


def tools_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "tools"


def default_exe(data_dir: Path) -> Path:
    return tools_dir(data_dir) / "xiaohongshu-mcp" / EXE_NAME


def xhs_exe_path(cfg) -> Path | None:
    """config's xhs.exe_path wins; otherwise the patched build inside the data directory."""
    if cfg.xhs.exe_path:
        return Path(cfg.xhs.exe_path)
    built = default_exe(cfg.data_dir)
    return built if built.exists() else None


def _fetch(url: str) -> bytes:
    return httpx.get(url, timeout=600, follow_redirects=True, trust_env=True).content


def ensure_go(work: Path, fetch: Callable[[str], bytes] = _fetch, platform: str = sys.platform) -> str:
    """Use a go already on PATH; otherwise unpack a pinned, checksum-verified portable Go into work/."""
    if found := shutil.which("go"):
        return found
    local = Path(work) / "go" / "bin" / ("go.exe" if platform == "win32" else "go")
    if local.exists():
        return str(local)
    if platform != "win32":
        raise RuntimeError("这台机器上没有 go，请先安装 Go 1.24+")
    last = ""
    for proxy in GO_PROXIES:
        url = f"{proxy}/golang.org/toolchain/@v/v0.0.1-go{GO_VERSION}.windows-amd64.zip"
        try:
            data = fetch(url)
        except Exception as e:  # try the next mirror
            last = f"{url}：{e}"
            continue
        if hashlib.sha256(data).hexdigest() != GO_TOOLCHAIN_SHA256:
            last = f"{url}：校验值不对"
            continue
        prefix = f"{GO_TOOLCHAIN}/"
        dest = Path(work) / "go"
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                target = dest / info.filename[len(prefix):]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(info))
        return str(local)
    raise RuntimeError(f"下载 Go 失败或校验不通过（{last}）")


def _go_env(work: Path) -> dict:
    work = Path(work)
    return {**os.environ,
            "GOPATH": str(work / "gopath"), "GOMODCACHE": str(work / "gopath" / "mod"),
            "GOCACHE": str(work / "gocache"), "GOFLAGS": "-modcacherw", "GOTOOLCHAIN": "local",
            "GOPROXY": ",".join(GO_PROXIES) + ",direct", "CGO_ENABLED": "0"}


def build_xhs_mcp(dest: Path, work: Path, go: str | None = None, run=subprocess.run,
                  log: Callable[..., None] = print) -> Path:
    dest, work = Path(dest), Path(work)
    work.mkdir(parents=True, exist_ok=True)
    go = go or ensure_go(work)
    env = _go_env(work)

    def sh(*args, cwd):
        r = run([go, *args], cwd=str(cwd), env=env, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"go {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[-800:]}")
        return r.stdout

    stub = work / "stub"
    stub.mkdir(exist_ok=True)
    if not (stub / "go.mod").exists():
        (stub / "go.mod").write_text("module stub\n\ngo 1.24\n", encoding="utf-8")
    log(f"下载 xiaohongshu-mcp 源码（{XHS_COMMIT[:7]}）……")
    info = json.loads(sh("mod", "download", "-json", f"{XHS_MODULE}@{XHS_COMMIT}", cwd=stub))
    src = work / "src"
    shutil.rmtree(src, ignore_errors=True)
    shutil.copytree(info["Dir"], src)
    _make_writable(src)

    log("下载并修补 leakless……")
    sh("mod", "download", LEAKLESS_MODULE, cwd=src)
    ll_dir = sh("list", "-m", "-f", "{{.Dir}}", LEAKLESS_MODULE, cwd=src).strip()
    ll = work / "leakless"
    shutil.rmtree(ll, ignore_errors=True)
    shutil.copytree(ll_dir, ll)
    _make_writable(ll)
    f = ll / "leakless.go"
    f.write_text(patch_leakless(f.read_text(encoding="utf-8")), encoding="utf-8")
    sh("mod", "edit", f"-replace={LEAKLESS_MODULE}={ll.as_posix()}", cwd=src)

    dest.mkdir(parents=True, exist_ok=True)
    log("编译 xiaohongshu-mcp（几分钟）……")
    sh("build", "-o", str(dest / EXE_NAME), ".", cwd=src)
    sh("build", "-o", str(dest / LOGIN_EXE_NAME), "./cmd/login", cwd=src)
    log(f"完成：{dest / EXE_NAME}")
    return dest / EXE_NAME


def _make_writable(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode | 0o200)
        except OSError:
            pass

