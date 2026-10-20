"""SQLite ledger: every item seen, every judgment, every press of a button."""
import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from .models import Item

CHANNEL_RANK = {"follow": 0, "search": 1, "recommend": 2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, date TEXT, stage TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, source_status TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0, capped INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, data TEXT, first_seen_run TEXT);
CREATE TABLE IF NOT EXISTS run_items (
  run_id TEXT, item_id TEXT, ord INTEGER, PRIMARY KEY (run_id, item_id));
CREATE TABLE IF NOT EXISTS verdicts (
  run_id TEXT, item_id TEXT, stage TEXT, score INTEGER, decision TEXT, reason TEXT,
  note_to_user TEXT, no_source INTEGER, serendipity INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (
  run_id TEXT, item_id TEXT, tool TEXT, args TEXT, ok INTEGER, error TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS pushes (
  run_id TEXT, item_id TEXT, slot TEXT, telegram_message_id INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS feedback (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, run_id TEXT, value TEXT, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS notes (
  text TEXT, at TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS reviews (
  week TEXT PRIMARY KEY, proposals TEXT, decisions TEXT, self_reported_minutes INTEGER, saved_at TEXT);
CREATE TABLE IF NOT EXISTS run_state (
  run_id TEXT, key TEXT, value TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT);
"""


class AlreadyRanToday(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _x(self, sql: str, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    # ---- runs ----
    def start_run(self, day: date, force: bool = False) -> tuple[str, bool]:
        rows = self._x("SELECT run_id, finished_at FROM runs WHERE date=? ORDER BY started_at DESC",
                       (day.isoformat(),)).fetchall()
        for r in rows:
            if r["finished_at"] is None:
                return r["run_id"], True
        if rows and not force:
            raise AlreadyRanToday(day.isoformat())
        run_id = f"{day.isoformat()}-{uuid.uuid4().hex[:6]}"
        self._x("INSERT INTO runs (run_id, date, stage, started_at) VALUES (?,?,?,?)",
                (run_id, day.isoformat(), None, _now()))
        return run_id, False

    def set_stage(self, run_id: str, stage: str) -> None:
        self._x("UPDATE runs SET stage=? WHERE run_id=?", (stage, run_id))

    def get_stage(self, run_id: str) -> str | None:
        r = self._x("SELECT stage FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return r["stage"] if r else None

    def finish_run(self, run_id: str, status: Literal["ok", "partial", "failed"], source_status: dict,
                   capped: bool) -> None:
        self._x("UPDATE runs SET finished_at=?, status=?, source_status=?, capped=? WHERE run_id=?",
                (_now(), status, json.dumps(source_status, ensure_ascii=False), int(capped), run_id))

    def set_state(self, run_id: str, key: str, value) -> None:
        self._x("INSERT OR REPLACE INTO run_state VALUES (?,?,?)", (run_id, key, json.dumps(value, ensure_ascii=False)))

    def get_state(self, run_id: str, key: str, default=None):
        r = self._x("SELECT value FROM run_state WHERE run_id=? AND key=?", (run_id, key)).fetchone()
        return json.loads(r["value"]) if r else default

    def last_run_started_at(self) -> datetime | None:
        r = self._x("SELECT started_at FROM runs WHERE finished_at IS NOT NULL "
                    "ORDER BY started_at DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(r["started_at"]) if r else None

    # ---- items ----
    def save_items(self, run_id: str, items: list[Item]) -> list[Item]:
        merged: dict[str, Item] = {}
        for it in items:
            prev = merged.get(it.id)
            if prev is None or CHANNEL_RANK[it.channel] < CHANNEL_RANK[prev.channel]:
                merged[it.id] = it
        new = []
        start = self._x("SELECT COUNT(*) FROM run_items WHERE run_id=?", (run_id,)).fetchone()[0]
        for it in merged.values():
            if self._x("SELECT 1 FROM items WHERE id=?", (it.id,)).fetchone():
                continue
            self._x("INSERT INTO items (id, data, first_seen_run) VALUES (?,?,?)",
                    (it.id, it.model_dump_json(), run_id))
            self._x("INSERT OR IGNORE INTO run_items (run_id, item_id, ord) VALUES (?,?,?)",
                    (run_id, it.id, start + len(new)))
            new.append(it)
        return new

    def run_items(self, run_id: str) -> list[Item]:
        rows = self._x("SELECT i.data FROM run_items r JOIN items i ON i.id=r.item_id "
                       "WHERE r.run_id=? ORDER BY r.ord", (run_id,)).fetchall()
        return [Item.model_validate_json(r["data"]) for r in rows]

    def get_item(self, item_id: str) -> Item | None:
        r = self._x("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
        return Item.model_validate_json(r["data"]) if r else None

    # ---- verdicts / tools / usage ----
    def add_verdict(self, run_id: str, item_id: str, stage: Literal["coarse", "deep", "final"],
                    score: int | None, decision: str | None, reason: str, note_to_user: str = "",
                    no_source: bool = False, serendipity: bool = False) -> None:
        self._x("INSERT INTO verdicts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, item_id, stage, score, decision, reason, note_to_user, int(no_source),
                 int(serendipity), _now()))

    def verdicts(self, run_id: str, stage: str) -> list[dict]:
        rows = self._x("SELECT * FROM verdicts WHERE run_id=? AND stage=? ORDER BY rowid",
                       (run_id, stage)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["no_source"] = bool(d["no_source"])
            d["serendipity"] = bool(d["serendipity"])
            out.append(d)
        return out

    def add_tool_call(self, run_id: str, item_id: str | None, tool: str, args: dict, ok: bool,
                      error: str = "") -> None:
        self._x("INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?)",
                (run_id, item_id, tool, json.dumps(args, ensure_ascii=False), int(ok), error, _now()))
        self._x("UPDATE runs SET tool_calls = tool_calls + 1 WHERE run_id=?", (run_id,))

    def add_usage(self, run_id: str, tokens_in: int, tokens_out: int) -> None:
        self._x("UPDATE runs SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE run_id=?",
                (tokens_in, tokens_out, run_id))

    def usage(self, run_id: str) -> tuple[int, int, int]:
        r = self._x("SELECT tokens_in, tokens_out, tool_calls FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return (r["tokens_in"], r["tokens_out"], r["tool_calls"]) if r else (0, 0, 0)

    # ---- pushes ----
    def add_push(self, run_id: str, item_id: str, slot: Literal["main", "serendipity", "missed"],
                 message_id: int | None) -> None:
        self._x("INSERT INTO pushes VALUES (?,?,?,?,?)", (run_id, item_id, slot, message_id, _now()))

    def pushed_ids(self) -> set[str]:
        return {r[0] for r in self._x("SELECT item_id FROM pushes WHERE slot != 'missed'").fetchall()}

    def pushes(self, since: datetime | None = None) -> list[dict]:
        rows = self._x("SELECT * FROM pushes" + (" WHERE at >= ?" if since else ""),
                       (since.isoformat(),) if since else ()).fetchall()
        return [dict(r) for r in rows]

    # ---- feedback ----
    def add_feedback(self, item_id: str, run_id: str, value: Literal["useful", "not_useful"],
                     reason: str | None) -> None:
        self._x("INSERT INTO feedback (item_id, run_id, value, reason, at) VALUES (?,?,?,?,?)",
                (item_id, run_id, value, reason, _now()))

    def feedback_count(self, item_id: str) -> int:
        return self._x("SELECT COUNT(*) FROM feedback WHERE item_id=?", (item_id,)).fetchone()[0]

    def latest_feedback_by_item(self, since: datetime | None = None) -> dict[str, dict]:
        rows = self._x("SELECT * FROM feedback" + (" WHERE at >= ?" if since else "") + " ORDER BY seq",
                       (since.isoformat(),) if since else ()).fetchall()
        latest: dict[str, dict] = {}
        for r in rows:
            latest[r["item_id"]] = dict(r)
        return latest

    def latest_feedback(self, days: int, n_each: int) -> tuple[list[dict], list[dict]]:
        latest = self.latest_feedback_by_item(datetime.now() - timedelta(days=days))
        useful, not_useful = [], []
        for fb in sorted(latest.values(), key=lambda d: d["seq"], reverse=True):
            item = self.get_item(fb["item_id"])
            fb = {**fb, "title": item.title if item else "", "platform": item.platform if item else ""}
            (useful if fb["value"] == "useful" else not_useful).append(fb)
        return useful[:n_each], not_useful[:n_each]

    # ---- notes ----
    def add_note(self, text: str, expires_at: datetime) -> None:
        self._x("INSERT INTO notes VALUES (?,?,?)", (text, _now(), expires_at.isoformat()))

    def active_notes(self, now: datetime) -> list[str]:
        rows = self._x("SELECT text FROM notes WHERE expires_at > ? ORDER BY at", (now.isoformat(),)).fetchall()
        return [r["text"] for r in rows]

    # ---- outbox ----
    def outbox_put(self, payload: dict) -> int:
        return self._x("INSERT INTO outbox (payload) VALUES (?)", (json.dumps(payload, ensure_ascii=False),)).lastrowid

    def outbox_all(self) -> list[tuple[int, dict]]:
        return [(r["id"], json.loads(r["payload"])) for r in self._x("SELECT * FROM outbox ORDER BY id")]

    def outbox_delete(self, id: int) -> None:
        self._x("DELETE FROM outbox WHERE id=?", (id,))

    # ---- reviews ----
    def save_review(self, week: str, proposals: list[dict]) -> None:
        self._x("INSERT OR REPLACE INTO reviews (week, proposals, decisions, saved_at) VALUES (?,?,?,?)",
                (week, json.dumps(proposals, ensure_ascii=False), "{}", _now()))

    def get_review(self, week: str) -> dict | None:
        r = self._x("SELECT * FROM reviews WHERE week=?", (week,)).fetchone()
        if not r:
            return None
        return {"week": r["week"], "proposals": json.loads(r["proposals"]), "decisions": json.loads(r["decisions"]),
                "self_reported_minutes": r["self_reported_minutes"], "saved_at": r["saved_at"]}

    def decide_proposal(self, week: str, index: int, accepted: bool) -> None:
        rv = self.get_review(week)
        if rv is None:
            return
        rv["decisions"][str(index)] = accepted
        self._x("UPDATE reviews SET decisions=? WHERE week=?", (json.dumps(rv["decisions"]), week))

    def set_self_reported_minutes(self, week: str, minutes: int) -> None:
        self._x("UPDATE reviews SET self_reported_minutes=? WHERE week=?", (minutes, week))

    def awaiting_minutes(self, now: datetime) -> str | None:
        r = self._x("SELECT week FROM reviews WHERE self_reported_minutes IS NULL AND saved_at >= ? "
                    "ORDER BY saved_at DESC LIMIT 1", ((now - timedelta(hours=48)).isoformat(),)).fetchone()
        return r["week"] if r else None
