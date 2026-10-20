"""Xiaohongshu via a locally run xiaohongshu-mcp: read-only endpoints only."""
import base64
import random
import re
import secrets
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


class XhsApiError(Exception):
    pass


class XhsProcess:
    """Starts xiaohongshu-mcp headless for one round and stops it afterwards."""

    def __init__(self, exe_path: Path | None, port: int, popen=subprocess.Popen, http=httpx,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.exe_path, self.port = exe_path, port
        self.popen, self.http, self.clock, self.sleep = popen, http, clock, sleep
        self.base_url = f"http://127.0.0.1:{port}"
        self.token = ""
        self.proc = None

    def __enter__(self) -> "XhsProcess":
        if not self.exe_path or not Path(self.exe_path).exists():
            raise SourceFailure("xhs", "没找到 xiaohongshu-mcp，请在 config.toml 里填 xhs.exe_path")
        self.token = secrets.token_urlsafe(24)
        args = [str(self.exe_path), "-headless=true", f"-port=:{self.port}", f"-token={self.token}"]
        try:
            self.proc = self.popen(args, cwd=str(Path(self.exe_path).parent), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            raise SourceFailure("xhs", f"xiaohongshu-mcp 没起来：{e}") from e
        deadline = self.clock() + HEALTH_TIMEOUT
        last = "无响应"
        while self.clock() < deadline:
            try:
                if self.http.get(f"{self.base_url}/health", timeout=2).status_code == 200:
                    return self
                last = "健康检查返回异常"
            except Exception as e:
                last = str(e)
            self.sleep(1)
        self._stop()
        raise SourceFailure("xhs", f"xiaohongshu-mcp 没起来：30 秒内健康检查不通过（{last}；"
                                   "可能是端口被占用，或首次运行还在下载浏览器）")

    def _stop(self) -> None:
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
            self.proc = None

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
        self.client = client or httpx.Client(base_url=proc.base_url, timeout=120)
        self._requests = 0
        self._broken: dict[tuple[str, str], XhsApiError] = {}
        self._tokens: dict[str, str] = {}
        self._details = 0
        self._logged_in: bool | None = None

    # ---- transport ----
    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        key = (method, path)
        if key not in ALLOWED:
            raise PermissionError(f"不允许调用 {method} {path}（只读白名单之外）")
        if key in self._broken:
            raise self._broken[key]
        if self._requests:
            self.sleep(self.rand(self.cfg.delay_min, self.cfg.delay_max))
        self._requests += 1
        try:
            r = self.client.request(method, path, json=json, headers={"Authorization": f"Bearer {self.proc.token}"})
            body = r.json()
            if r.status_code != 200 or not body.get("success"):
                raise XhsApiError(f"{path} 返回 {r.status_code}：{body.get('message') or body.get('error') or ''}")
        except XhsApiError as e:
            self._broken[key] = e
            raise
        except Exception as e:
            err = XhsApiError(f"{path} 请求失败：{e}")
            self._broken[key] = err
            raise err from e
        return body

    def logged_in(self) -> bool:
        if self._logged_in is None:
            try:
                self._logged_in = bool(self._request("GET", "/api/v1/login/status")["data"].get("is_logged_in"))
            except XhsApiError:
                self._logged_in = False
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
        out = []
        for link in links:
            user_id, token = parse_profile_link(link)
            out += self._collect("POST", "/api/v1/user/profile",
                                 {"user_id": user_id, "xsec_token": token, "tab": "note"}, "follow", None, per_creator)
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
