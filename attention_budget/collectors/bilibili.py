"""Bilibili: follow updates, home recommendations, search and read-only deep-read tools."""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from ..models import Item, SourceFailure

SUBTITLE_MAX = 8000
TAG_RE = re.compile(r"<[^>]+>")


class AuthExpired(Exception):
    pass


def parse_count(v) -> int:
    """'1.2万' -> 12000, '300' -> 300, 5000 -> 5000."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "")
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10_000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100_000_000)
        return int(float(s))
    except ValueError:
        return 0


def _ts(v) -> datetime | None:
    return datetime.fromtimestamp(int(v), tz=timezone.utc) if v else None


def _video_url(bvid: str) -> str:
    return f"https://www.bilibili.com/video/{bvid}"


class RealBiliApi:
    """Thin sync facade over bilibili_api; returns raw dicts."""

    def __init__(self):
        import bilibili_api  # imported lazily so tests never touch the network library

        self.b = bilibili_api

    def _run(self, coro):
        try:
            return asyncio.run(coro)
        except self.b.exceptions.ResponseCodeException as e:
            if e.code == -101:
                raise AuthExpired(str(e)) from e
            raise

    def check_valid(self, cred) -> bool:
        return bool(self._run(cred.check_valid()))

    def dynamic_page(self, cred, offset):
        return self._run(self.b.dynamic.get_dynamic_page_info(
            credential=cred, _type=self.b.dynamic.DynamicType.VIDEO, offset=offset))

    def homepage(self, cred):
        return self._run(self.b.homepage.get_videos(credential=cred))

    def search(self, keyword, page):
        return self._run(self.b.search.search_by_type(
            keyword, search_type=self.b.search.SearchObjectType.VIDEO, page=page))

    def video_info(self, bvid, cred):
        return self._run(self.b.video.Video(bvid=bvid, credential=cred).get_info())

    def subtitle_info(self, bvid, cid, cred):
        return self._run(self.b.video.Video(bvid=bvid, credential=cred).get_subtitle(cid=cid))

    def fetch_json(self, url):
        return httpx.get(url, timeout=15).json()

    def comments(self, aid, cred):
        c = self.b.comment
        return self._run(c.get_comments(aid, c.CommentResourceType.VIDEO, order=c.OrderType.LIKE, credential=cred))

    def user_videos(self, mid, n, cred):
        return self._run(self.b.user.User(int(mid), credential=cred).get_videos(ps=n))

    def make_credential(self, d: dict):
        return self.b.Credential(**d)

    def qr_login(self):
        from bilibili_api import login_v2
        return login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)


class BiliCollector:
    def __init__(self, credential_path: Path, api=None):
        self.path = Path(credential_path)
        self.api = api or RealBiliApi()
        self._cred = None
        self._valid: bool | None = None

    # ---- credential ----
    def _credential(self):
        if self._cred is None:
            if not self.path.exists():
                raise SourceFailure("bili", "登录过期（还没扫码登录过 B 站）")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            make = getattr(self.api, "make_credential", None)
            self._cred = make(data) if make else data
        return self._cred

    def logged_in(self) -> bool:
        try:
            cred = self._credential()
        except SourceFailure:
            return False
        if self._valid is None:
            try:
                self._valid = self.api.check_valid(cred)
            except AuthExpired:
                self._valid = False
        return self._valid

    def _call(self, fn: Callable, *args):
        if not self.logged_in():
            raise SourceFailure("bili", "登录过期")
        try:
            return fn(*args)
        except AuthExpired as e:
            raise SourceFailure("bili", "登录过期") from e
        except SourceFailure:
            raise
        except Exception as e:
            raise SourceFailure("bili", f"接口出错：{e}") from e

    # ---- collection ----
    def follow_updates(self, since: datetime, limit: int) -> list[Item]:
        if since.tzinfo is None:
            since = since.astimezone()
        out, offset = [], None
        while len(out) < limit:
            page = self._call(self.api.dynamic_page, self._credential(), offset)
            stop = False
            for it in page.get("items", []):
                mods = it.get("modules", {})
                author = mods.get("module_author", {})
                arc = (mods.get("module_dynamic", {}).get("major") or {}).get("archive")
                published = _ts(author.get("pub_ts"))
                if not arc or not arc.get("bvid"):
                    continue
                if published and published < since:
                    stop = True
                    break
                out.append(Item(id=f"bili:{arc['bvid']}", platform="bili", channel="follow",
                                author=author.get("name", ""), author_id=str(author.get("mid", "")),
                                title=arc.get("title", ""), snippet=arc.get("desc", ""),
                                url=_video_url(arc["bvid"]), published_at=published,
                                metrics={"play": parse_count((arc.get("stat") or {}).get("play", 0))}))
                if len(out) >= limit:
                    break
            if stop or not page.get("has_more"):
                break
            offset = page.get("offset")
        return out

    def recommend(self, limit: int) -> list[Item]:
        data = self._call(self.api.homepage, self._credential())
        out = []
        for v in data.get("item", []):
            if not v.get("bvid"):
                continue
            owner = v.get("owner") or {}
            out.append(Item(id=f"bili:{v['bvid']}", platform="bili", channel="recommend",
                            author=owner.get("name", ""), author_id=str(owner.get("mid", "")),
                            title=v.get("title", ""), snippet=v.get("desc", "") or "",
                            url=_video_url(v["bvid"]), published_at=_ts(v.get("pubdate")),
                            metrics={"play": parse_count((v.get("stat") or {}).get("view", 0))}))
        return out[:limit]

    def search(self, query: str, limit: int) -> list[Item]:
        out, page = [], 1
        while len(out) < limit and page <= 3:
            data = self._call(self.api.search, query, page)
            results = data.get("result") or []
            for v in results:
                if not v.get("bvid"):
                    continue
                out.append(Item(id=f"bili:{v['bvid']}", platform="bili", channel="search",
                                author=v.get("author", ""), author_id=str(v.get("mid", "")),
                                title=TAG_RE.sub("", v.get("title", "")), snippet=v.get("description", ""),
                                url=_video_url(v["bvid"]), published_at=_ts(v.get("pubdate")),
                                metrics={"play": parse_count(v.get("play", 0))}, query=query))
            if not results:
                break
            page += 1
        return out[:limit]

    # ---- deep-read tools (read only) ----
    def _subtitle(self, bvid: str) -> str:
        cred = self._credential()
        info = self.api.video_info(bvid, cred)
        subs = (self.api.subtitle_info(bvid, info["cid"], cred) or {}).get("subtitles") or []
        if not subs:
            return "无字幕"
        url = subs[0]["subtitle_url"]
        url = "https:" + url if url.startswith("//") else url
        body = self.api.fetch_json(url).get("body") or []
        text = "\n".join(line.get("content", "") for line in body).strip()
        return text[:SUBTITLE_MAX] or "无字幕"

    def _comments(self, bvid: str, n: int = 20) -> str:
        info = self.api.video_info(bvid, self._credential())
        replies = (self.api.comments(info["aid"], self._credential()) or {}).get("replies") or []
        lines = [f"- {r['member']['uname']}（{r.get('like', 0)} 赞）：{r['content']['message']}" for r in replies[:n]]
        return "\n".join(lines) or "没有评论"

    def _up_recent(self, mid: str, n: int = 5) -> str:
        data = self.api.user_videos(mid, n, self._credential())
        vlist = ((data or {}).get("list") or {}).get("vlist") or []
        return "\n".join(f"- {v['title']}" for v in vlist[:n]) or "没有找到近期视频"

    def tools(self) -> dict[str, Callable[..., str]]:
        return {"subtitle": self._subtitle, "comments": self._comments, "up_recent": self._up_recent}

    # ---- login ----
    def login_qr(self) -> tuple[bytes, Callable[[], bool]]:
        qr = self.api.qr_login()
        asyncio.run(qr.generate_qrcode())
        png = qr.get_qrcode_picture().content

        def poll() -> bool:
            asyncio.run(qr.check_state())
            if not qr.has_done():
                return False
            c = qr.get_credential()
            data = {k: getattr(c, k) for k in ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid",
                                               "ac_time_value") if getattr(c, k, None)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data), encoding="utf-8")
            self._cred, self._valid = None, None
            return True

        return png, poll
