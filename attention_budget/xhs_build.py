"""Build a patched xiaohongshu-mcp that starts its browser without leakless.

Why: xiaohongshu-mcp launches Chromium through xpzouying/headless_browser → go-rod, whose launcher by default
wraps the browser in ysmood/leakless: it extracts a helper leakless.exe into TEMP and runs the browser through
it. On Windows that helper (1) takes a TCP lock on the hard-coded port 2978, which some machines exclude from
binding, and (2) is flagged by Windows Security ("file contains a virus or potentially unwanted software",
error 225). Disabling leakless (go-rod's Launcher.Leakless(false)) starts chrome.exe directly; cleaning up the
browser when xiaohongshu-mcp stops is done by attention-budget instead (a Windows Job Object, see
collectors/xhs.py ProcessTree). Everything (Go, module cache, sources, binaries) stays inside the data directory.
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
HB_MODULE = "github.com/xpzouying/headless_browser"
BUILD_ID = "no-leakless-3"  # bump when the patch changes; written to build-info.json next to the exe
GO_VERSION = "1.24.7"
GO_TOOLCHAIN = f"golang.org/toolchain@v0.0.1-go{GO_VERSION}.windows-amd64"
# sha256 of the module zip above, identical on every Go module proxy (verified against sum.golang.org)
GO_TOOLCHAIN_SHA256 = "615b342308b2ce49b2c1198070c39edbb897c02c943d573c2a88ae02ddfae337"
GO_PROXIES = ["https://goproxy.cn", "https://proxy.golang.org"]
EXE_NAME = "xiaohongshu-mcp.exe"
LOGIN_EXE_NAME = "xiaohongshu-login.exe"

_LAUNCH = "l := launcher.New()."


def patch_headless_browser(src: str) -> str:
    if "launcher.New().Leakless(false)." in src:
        return src
    if _LAUNCH not in src:
        raise RuntimeError("headless_browser 源码和预期不一致，无法打补丁（版本变了？）")
    return src.replace(_LAUNCH, "l := launcher.New().Leakless(false).", 1)


_WAIT_STABLE = "page.MustWaitStable()"
WAIT_PATCHED_FILES = ("search.go", "user_profile.go")


def patch_wait_for_dom(src: str, name: str) -> str:
    """Search / user-profile pages wait for "stable" = load + no network request in flight + DOM stable.
    The results page keeps background requests going, so it never gets there and hits the 60 s deadline
    ("context deadline exceeded"). The home feed (which works) waits for DOM stability only; do the same.
    The data itself is still awaited right after (window.__INITIAL_STATE__)."""
    if _WAIT_STABLE not in src:
        if "page.MustWaitDOMStable()" in src:
            return src
        raise RuntimeError(f"xiaohongshu-mcp 的 {name} 和预期不一致，无法打补丁（版本变了？）")
    return src.replace(_WAIT_STABLE, "page.MustWaitDOMStable()")


LOAD_PATCHED_FILES = ("login.go", "page_ready.go")
_READY_WAIT = "\tpage.MustWaitLoad()\n"
_READY_PATCHED = ("\t// attention-budget: no wait for the load event (one stalled sub-resource keeps it from ever firing);\n"
                  "\t// the container wait below bounds this instead\n")


def patch_load_waits(src: str, name: str) -> str:
    """The login check (login.go) and the note-detail page (page_ready.go) wait for the page's load event,
    which only fires once every image/ad/tracker has finished. One stalled sub-resource and the 30 s login
    check panics with "context deadline exceeded" although the page is usable. Wait for the DOM instead, as
    the working home feed does; the detail page already waits for its content container right after."""
    if name == "page_ready.go":
        if _READY_PATCHED in src:
            return src
        if _READY_WAIT not in src:
            raise RuntimeError(f"xiaohongshu-mcp 的 {name} 和预期不一致，无法打补丁（版本变了？）")
        return src.replace(_READY_WAIT, _READY_PATCHED, 1)
    if ".MustWaitLoad()" not in src:
        if ".MustWaitDOMStable()" in src:
            return src
        raise RuntimeError(f"xiaohongshu-mcp 的 {name} 和预期不一致，无法打补丁（版本变了？）")
    return src.replace(".MustWaitLoad()", ".MustWaitDOMStable()")


def tools_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "tools"


def default_exe(data_dir: Path) -> Path:
    return tools_dir(data_dir) / "xiaohongshu-mcp" / EXE_NAME


def build_is_current(exe: Path) -> bool:
    """True when exe was built by this version of build-xhs-mcp (leakless disabled)."""
    try:
        info = json.loads((Path(exe).parent / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return info.get("build_id") == BUILD_ID


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
            "GOPROXY": "|".join(GO_PROXIES) + "|direct", "CGO_ENABLED": "0"}


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

    for name in WAIT_PATCHED_FILES:
        f = src / "xiaohongshu" / name
        if f.exists():
            f.write_text(patch_wait_for_dom(f.read_text(encoding="utf-8"), name), encoding="utf-8")
    for name in LOAD_PATCHED_FILES:
        f = src / "xiaohongshu" / name
        if f.exists():
            f.write_text(patch_load_waits(f.read_text(encoding="utf-8"), name), encoding="utf-8")

    log("下载并修补 headless_browser（不再使用 leakless.exe）……")
    sh("mod", "download", HB_MODULE, cwd=src)
    hb_dir = sh("list", "-m", "-f", "{{.Dir}}", HB_MODULE, cwd=src).strip()
    hb = work / "headless_browser"
    shutil.rmtree(hb, ignore_errors=True)
    shutil.copytree(hb_dir, hb)
    _make_writable(hb)
    f = hb / "headless_browser.go"
    f.write_text(patch_headless_browser(f.read_text(encoding="utf-8")), encoding="utf-8")
    sh("mod", "edit", f"-replace={HB_MODULE}={hb.as_posix()}", cwd=src)

    dest.mkdir(parents=True, exist_ok=True)
    log("编译 xiaohongshu-mcp（几分钟）……")
    sh("build", "-o", str(dest / EXE_NAME), ".", cwd=src)
    sh("build", "-o", str(dest / LOGIN_EXE_NAME), "./cmd/login", cwd=src)
    (dest / "build-info.json").write_text(json.dumps({"build_id": BUILD_ID, "xiaohongshu_mcp": XHS_COMMIT}),
                                          encoding="utf-8")
    log(f"完成：{dest / EXE_NAME}")
    return dest / EXE_NAME


def _make_writable(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode | 0o200)
        except OSError:
            pass

