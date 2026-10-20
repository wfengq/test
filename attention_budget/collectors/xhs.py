"""Xiaohongshu via a locally run xiaohongshu-mcp: read-only endpoints only."""
import base64
import random
import re
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from ..models import Item, SourceFailure
from ..ocr import image_text
from .bilibili import parse_count

ALLOWED = {
    ("GET", "/api/v1/login/status"),
    ("GET", "/api/v1/login/qrcode"),
    ("GET", "/api/v1/feeds/list"),
    ("POST", "/api/v1/feeds/search"),
    ("POST", "/api/v1/feeds/detail"),
    ("POST", "/api/v1/user/profile"),
}
HEALTH_TIMEOUT = 30
SUBTITLE_MAX = 8000
SRT_TIME = re.compile(r"^\d+$|-->")
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


LOG_MAX = 5_000_000
STARTUP_TIMEOUT = 90
SECRET_RE = re.compile(r"(?i)\b(xsec_token|access_token|token|web_session|session|a1|webid|sessdata|bili_jct|cookies?)"
                       r"\s*[=:]\s*(?:bearer\s+)?[^\s,&;\"']+")
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
ERROR_LINE_RE = re.compile(r'level=error msg="(.*)"\s*$')


def _sanitize(text: str) -> str:
    text = BEARER_RE.sub("Bearer ***", text)
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return text.replace("\\\\", "\\")


def _last_server_error(log_tail: str, path: str | None = None) -> str:
    """Latest error line in xiaohongshu-mcp's log; with path, only a line about that request."""
    for line in reversed(log_tail.splitlines()):
        m = ERROR_LINE_RE.search(line)
        if not m:
            continue
        msg = m.group(1)
        if re.fullmatch(r"(GET|POST|PUT|DELETE) \S+ \d{3}", msg):  # the access-log line, not the cause
            continue
        if path and not re.search(rf",\s*path:\s*{re.escape(path)}$", msg):
            continue
        msg = re.sub(r"^服务器内部错误:\s*", "", msg)
        return re.sub(r",\s*path:\s*\S+$", "", msg)
    return ""


def explain_server_error(status: int, body: dict | None, log_tail: str, path: str | None = None) -> str:
    """Turn a 5xx from xiaohongshu-mcp into the actual reason, with credentials stripped."""
    reason = _last_server_error(log_tail, path)
    if not reason and body:
        parts = [str(body.get("error") or "")]
        details = body.get("details")
        if details and details != {}:
            parts.append(details if isinstance(details, str) else str(details))
        reason = "：".join(p for p in parts if p)
    reason = _sanitize(reason)[:400]
    low = reason.lower()
    if "virus" in low or "potentially unwanted" in low or "error 225" in low:
        blocked = re.search(r"[^\s\\/:]+\.exe", reason)
        name = blocked.group(0) if blocked else "一个程序"
        hint = ("这是旧的 xiaohongshu-mcp 编译版本，它还在用 leakless.exe；请重新运行 attention-budget build-xhs-mcp"
                if "leakless" in low else "请检查这个文件来自哪里")
        return f"xiaohongshu-mcp 启动浏览器时，{name} 被 Windows 安全中心拦截了（错误 225）。{hint}。原始错误：{reason}"
    if reason:
        return f"xiaohongshu-mcp 出错（HTTP {status}）：{reason}"
    return f"xiaohongshu-mcp 出错（HTTP {status}），没有拿到具体原因"


class ProcessTree:
    """A child process plus everything it starts (xiaohongshu-mcp → chrome.exe), stopped together.

    Windows: the child goes into a Job Object with KILL_ON_JOB_CLOSE, so its browsers die with it — also when
    this Python process itself dies, because the OS closes the job handle then. This replaces what go-rod's
    leakless.exe used to do. POSIX: the child leads a new session and the whole process group is signalled.
    """

    def __init__(self, popen: subprocess.Popen, job=None):
        self.p, self.job = popen, job

    @classmethod
    def spawn(cls, args, **kw) -> "ProcessTree":
        if sys.platform == "win32":
            p = subprocess.Popen(args, **kw)
            try:
                job = _win_job_for(p)
            except OSError:
                p.kill()
                raise
            return cls(p, job)
        kw.pop("creationflags", None)
        return cls(subprocess.Popen(args, start_new_session=True, **kw))

    def poll(self):
        return self.p.poll()

    def wait(self, timeout=None):
        return self.p.wait(timeout=timeout)

    def _signal_group(self, sig) -> None:
        try:
            os.killpg(self.p.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def terminate(self) -> None:
        if self.job is not None:
            self.job.terminate()
        else:
            self._signal_group(signal.SIGTERM)

    def kill(self) -> None:
        if self.job is not None:
            self.job.terminate()
        else:
            self._signal_group(signal.SIGKILL)
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()

    def close(self) -> None:
        """Make sure nothing in the tree survives, then release the job."""
        self.kill()
        if self.job is not None:
            self.job.close()
            self.job = None


class _WinJob:
    def __init__(self, handle, k32):
        self.handle, self.k32 = handle, k32

    def terminate(self) -> None:
        if self.handle:
            self.k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None


def _win_job_for(p: subprocess.Popen) -> _WinJob:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = EXTENDED()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(p._handle))):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    return _WinJob(job, k32)


class XhsApiError(Exception):
    def __init__(self, msg: str, transient: bool = False):
        super().__init__(msg)
        self.transient = transient


TRANSIENT_RETRIES = 2       # extra attempts per call for a timeout-type failure
TRANSIENT_BREAK_AFTER = 2   # calls that stayed failing after retries before an endpoint is given up for the round


def _is_transient(reason: str) -> bool:
    low = reason.lower()
    return "deadline exceeded" in low or "timeout" in low or "timed out" in low


class XhsProcess:
    """Starts xiaohongshu-mcp headless for one round and stops it (and its browsers) afterwards."""

    def __init__(self, exe_path: Path | None, port: int, popen=ProcessTree.spawn, http=None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 log_path: Path | None = None, headless: bool = True):
        self.exe_path, self.port, self.headless = exe_path, port, headless
        # local calls must never go through a (system) proxy: it answers 502 for 127.0.0.1
        self.popen, self.http, self.clock, self.sleep = popen, http or httpx.Client(trust_env=False), clock, sleep
        self.base_url = f"http://127.0.0.1:{port}"
        self.token = ""
        self.proc = None
        self.log_path = Path(log_path) if log_path else None
        self._log = None
        self.login_state: bool | None = None  # what login/status said at startup; the collector reuses it

    def _open_log(self):
        if not self.log_path:
            return subprocess.DEVNULL
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX:
            self.log_path.replace(self.log_path.with_suffix(".log.1"))
        self._log = open(self.log_path, "ab")
        return self._log

    def log_tail(self, size: int = 16_000) -> str:
        if not self.log_path or not self.log_path.exists():
            return ""
        with open(self.log_path, "rb") as f:
            f.seek(max(0, self.log_path.stat().st_size - size))
            return f.read().decode("utf-8", errors="replace")

    def explain(self, status: int, body: dict | None, path: str | None = None) -> str:
        return explain_server_error(status, body, self.log_tail(), path=path)

    def __enter__(self) -> "XhsProcess":
        if not self.exe_path or not Path(self.exe_path).exists():
            raise SourceFailure("xhs", "没找到 xiaohongshu-mcp，请运行 attention-budget build-xhs-mcp，"
                                       "或在 config.toml 里填 xhs.exe_path")
        self.token = secrets.token_urlsafe(24)
        # bind to localhost only; pass the token through the environment so it is not on the command line
        args = [str(self.exe_path), f"-headless={'true' if self.headless else 'false'}", f"-port=127.0.0.1:{self.port}"]
        out = self._open_log()
        try:
            self.proc = self.popen(args, cwd=str(Path(self.exe_path).parent), stdout=out,
                                   stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
                                   env={**os.environ, "AUTH_TOKEN": self.token})
        except OSError as e:
            self._close_log()
            raise SourceFailure("xhs", f"xiaohongshu-mcp 没起来：{e}") from e
        deadline = self.clock() + HEALTH_TIMEOUT
        last = "无响应"
        while self.clock() < deadline:
            if self.proc.poll() is not None:
                self._stop()
                reason = _sanitize(_last_server_error(self.log_tail()) or "")
                raise SourceFailure("xhs", "xiaohongshu-mcp 启动后马上退出了（可能端口 "
                                           f"{self.port} 被占用，或程序文件损坏）" + (f"：{reason}" if reason else ""))
            try:
                if self.http.get(f"{self.base_url}/health", timeout=2).status_code == 200:
                    self._confirm_ours()
                    return self
                last = "健康检查返回异常"
            except SourceFailure:
                raise
            except Exception as e:
                last = str(e)
            self.sleep(1)
        self._stop()
        raise SourceFailure("xhs", f"xiaohongshu-mcp 没起来：30 秒内健康检查不通过（{last}；"
                                   "可能是端口被占用，或首次运行还在下载浏览器）")

    def _confirm_ours(self) -> None:
        """/health is unauthenticated; make sure the server answering is the one we just started,
        and that it can actually start its browser (login/status needs one)."""
        for attempt in range(2):
            try:
                r = self.http.get(f"{self.base_url}/api/v1/login/status", timeout=STARTUP_TIMEOUT,
                                  headers={"Authorization": f"Bearer {self.token}"})
            except httpx.TimeoutException:
                self._stop()
                raise SourceFailure("xhs", f"xiaohongshu-mcp 启动浏览器超时（{STARTUP_TIMEOUT} 秒没响应）"
                                           + (f"，日志：{self.log_path}" if self.log_path else "")) from None
            if r.status_code == 401:
                self._stop()
                raise SourceFailure("xhs", f"端口 {self.port} 被别的程序占用（可能是上次没关掉的 xiaohongshu-mcp，"
                                           "重启电脑或在任务管理器里结束它即可）")
            if r.status_code < 500:
                try:
                    self.login_state = bool(r.json()["data"]["is_logged_in"])
                except (ValueError, KeyError, TypeError):
                    self.login_state = None
                return
            try:
                body = r.json()
            except ValueError:
                body = None
            reason = self.explain(r.status_code, body, path="/api/v1/login/status")
            # the very first browser start (new profile, first page load) can overrun xiaohongshu-mcp's own
            # deadline once; a second try reuses the warmed-up cache
            if attempt == 0 and "deadline exceeded" in reason:
                continue
            self._stop()
            raise SourceFailure("xhs", reason)

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def _stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            close = getattr(self.proc, "close", None)
            if close:
                close()  # kill anything left in the tree (browsers) and release the job
            self.proc = None
        self._close_log()

    def __exit__(self, *exc) -> None:
        self._stop()


def _note_url(note_id: str, token: str) -> str:
    return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_feed"


def _feed_to_item(f: dict, channel: str, query: str | None) -> Item | None:
    if f.get("modelType") != "note" or not f.get("id"):
        return None
    card = f.get("noteCard") or {}
    user = card.get("user") or {}
    token = f.get("xsecToken", "")
    return Item(id=f"xhs:{f['id']}", platform="xhs", channel=channel,
                author=user.get("nickname") or user.get("nickName") or "", author_id=user.get("userId", ""),
                title=card.get("displayTitle", ""), snippet="", url=_note_url(f["id"], token), published_at=None,
                metrics={"likes": parse_count((card.get("interactInfo") or {}).get("likedCount", 0))},
                query=query, xsec_token=token)


def _flatten(feeds) -> list[dict]:
    out = []
    for f in feeds or []:
        out.extend(_flatten(f) if isinstance(f, list) else [f])
    return out


def parse_feeds(payload: dict, channel: str, query: str | None = None) -> list[Item]:
    data = payload.get("data") or {}
    if "feeds" not in data and isinstance(data.get("data"), dict):  # user/profile wraps once more
        data = data["data"]
    return [it for f in _flatten(data.get("feeds")) if (it := _feed_to_item(f, channel, query))]


def parse_profile_link(url: str) -> tuple[str, str]:
    u = urlparse(url)
    m = re.search(r"/user/profile/([^/?#]+)", u.path)
    if not m:
        raise ValueError(f"不是小红书用户主页链接：{url}")
    token = parse_qs(u.query).get("xsec_token", [""])[0]
    if not token:
        raise ValueError("主页链接里缺 xsec_token，请从小红书里重新复制分享链接")
    return m.group(1), token


def _srt_text(srt: str) -> str:
    lines = [ln.strip() for ln in srt.splitlines() if ln.strip() and not SRT_TIME.search(ln.strip())]
    return "\n".join(lines)


class XhsCollector:
    def __init__(self, proc, cfg, sleep=time.sleep, rand=random.uniform, client: httpx.Client | None = None,
                 ocr=image_text):
        self.proc, self.cfg = proc, cfg
        self.sleep, self.rand, self.ocr = sleep, rand, ocr
        self.client = client or httpx.Client(base_url=proc.base_url, timeout=120, trust_env=False)
        self._requests = 0
        self._broken: dict[tuple[str, str], XhsApiError] = {}
        self._timeouts: dict[tuple[str, str], int] = {}
        self._tokens: dict[str, str] = {}
        self._details = 0
        self._logged_in: bool | None = None
        self.warnings: list[str] = []

    # ---- transport ----
    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        """One API call. Timeout-type failures (xiaohongshu-mcp starts a browser per request, which can overrun
        its own deadline) are retried; an endpoint is only given up for the round after a permanent error or
        after TRANSIENT_BREAK_AFTER calls that kept timing out."""
        key = (method, path)
        if key not in ALLOWED:
            raise PermissionError(f"不允许调用 {method} {path}（只读白名单之外）")
        if key in self._broken:
            raise self._broken[key]
        err = None
        for _ in range(TRANSIENT_RETRIES + 1):
            try:
                body = self._send(method, path, json)
                self._timeouts.pop(key, None)
                return body
            except XhsApiError as e:
                err = e
                if not e.transient:
                    self._broken[key] = e
                    raise
        self._timeouts[key] = self._timeouts.get(key, 0) + 1
        if self._timeouts[key] >= TRANSIENT_BREAK_AFTER:
            self._broken[key] = err
        raise err

    def _send(self, method: str, path: str, json: dict | None) -> dict:
        if self._requests:
            self.sleep(self.rand(self.cfg.delay_min, self.cfg.delay_max))
        self._requests += 1
        try:
            r = self.client.request(method, path, json=json, headers={"Authorization": f"Bearer {self.proc.token}"})
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise XhsApiError(f"{path} 请求超时或连不上：{e}", transient=True) from e
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 500:
            reason = (self.proc.explain(r.status_code, body, path=path) if hasattr(self.proc, "explain")
                      else f"HTTP {r.status_code}")
            raise XhsApiError(f"{path}：{reason}", transient=_is_transient(reason))
        if r.status_code != 200 or not body or not body.get("success"):
            msg = _sanitize(str((body or {}).get("message") or (body or {}).get("error") or ""))
            raise XhsApiError(f"{path} 返回 {r.status_code}：{msg}")
        return body

    def logged_in(self, refresh: bool = False) -> bool:
        """Reuse what XhsProcess saw at startup; ask xiaohongshu-mcp again only when refresh is requested
        (each login/status call makes it launch a whole browser)."""
        if refresh:
            self._logged_in = None
        elif self._logged_in is None and getattr(self.proc, "login_state", None) is not None:
            self._logged_in = self.proc.login_state
        if self._logged_in is None:
            try:
                self._logged_in = bool(self._request("GET", "/api/v1/login/status")["data"].get("is_logged_in"))
            except XhsApiError as e:
                raise SourceFailure("xhs", f"xiaohongshu-mcp 出错：{e}") from e
        return self._logged_in

    def login_qr(self) -> bytes:
        img = (self._request("GET", "/api/v1/login/qrcode").get("data") or {}).get("img", "")
        return base64.b64decode(img.split(",", 1)[1] if img.startswith("data:") else img)

    def _collect(self, method: str, path: str, json: dict | None, channel: str, query: str | None,
                 limit: int) -> list[Item]:
        if not self.logged_in():
            raise SourceFailure("xhs", "未登录或被挤下线")
        try:
            items = parse_feeds(self._request(method, path, json), channel, query)
        except XhsApiError as e:
            raise SourceFailure("xhs", str(e)) from e
        if not items:
            raise SourceFailure("xhs", f"{path} 返回 0 条")
        return items[:limit]

    # ---- collection ----
    def recommend(self, limit: int) -> list[Item]:
        return self._collect("GET", "/api/v1/feeds/list", None, "recommend", None, limit)

    def search(self, query: str, limit: int, scope: Literal["all", "followed"]) -> list[Item]:
        body = {"keyword": query}
        if scope == "followed":
            body["filters"] = {"search_scope": "已关注", "publish_time": "一周内"}
        return self._collect("POST", "/api/v1/feeds/search", body, "follow" if scope == "followed" else "search",
                             query, limit)

    def key_creators_updates(self, links: list[str], per_creator: int) -> list[Item]:
        """Each creator is isolated: a bad link or an empty page skips that creator only."""
        out = []
        for link in links:
            try:
                user_id, token = parse_profile_link(link)
                out += self._collect("POST", "/api/v1/user/profile",
                                     {"user_id": user_id, "xsec_token": token, "tab": "note"}, "follow", None,
                                     per_creator)
            except (ValueError, SourceFailure) as e:
                self.warnings.append(f"重点博主 {link[:60]}：{e}")
                if isinstance(e, SourceFailure) and e.reason.startswith("xiaohongshu-mcp 出错"):
                    raise
        return out

    def remember(self, items: list[Item]) -> None:
        for it in items:
            if it.platform == "xhs" and it.xsec_token:
                self._tokens[it.id] = it.xsec_token

    # ---- deep-read tools (read only) ----
    def _note_detail(self, item_id: str) -> str:
        if self._details >= self.cfg.detail_cap:
            return "今天小红书详情额度已用完，请根据已有信息下结论"
        token = self._tokens.get(item_id)
        if not token:
            return "找不到这条笔记的访问参数，无法打开详情"
        self._details += 1
        note_id = item_id.split(":", 1)[1]
        body = self._request("POST", "/api/v1/feeds/detail", {"feed_id": note_id, "xsec_token": token})
        data = (body.get("data") or {}).get("data") or {}
        note = data.get("note") or {}
        parts = [f"【标题】{note.get('title', '')}", f"【正文】{note.get('desc', '') or '（无）'}"]
        if note.get("time"):
            parts.append(f"【发布时间】{datetime.fromtimestamp(note['time'] / 1000, tz=timezone.utc).date()}")
        urls = [img.get("urlDefault") or img.get("urlPre") for img in note.get("imageList") or []]
        if urls := [u for u in urls if u]:
            parts.append("【图片里的文字】\n" + (self.ocr(urls, self.cfg.ocr_images) or "（图片里没有识别出文字）"))
        subs = ((note.get("video") or {}).get("subtitles") or {})
        sub_urls = [s.get("url") for lang in subs.values() if isinstance(lang, list) for s in lang if s.get("url")]
        if sub_urls:
            try:
                parts.append("【字幕】\n" + _srt_text(httpx.get(sub_urls[0], timeout=15).text)[:SUBTITLE_MAX])
            except Exception:
                parts.append("【字幕】下载失败")
        comments = (data.get("comments") or {}).get("list") or []
        lines = [f"- {(c.get('userInfo') or {}).get('nickname', '')}（{c.get('likeCount', '0')} 赞）：{c.get('content', '')}"
                 for c in comments[:10]]
        parts.append("【评论】\n" + ("\n".join(lines) or "没有评论"))
        return "\n".join(parts)

    def _author_recent(self, user_id: str, xsec_token: str) -> str:
        body = self._request("POST", "/api/v1/user/profile", {"user_id": user_id, "xsec_token": xsec_token, "tab": "note"})
        items = parse_feeds(body, "follow")[:5]
        return "\n".join(f"- {it.title}" for it in items) or "没有找到近期笔记"

    def tools(self) -> dict[str, Callable[..., str]]:
        return {"note_detail": self._note_detail, "author_recent": self._author_recent}
