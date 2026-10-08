from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

_lock = threading.Lock()
Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
_conn = sqlite3.connect(Path(settings.data_dir) / ("tmerge-demo.sqlite" if settings.demo else "tmerge.sqlite"), check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript(
    """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL, uploaded_by TEXT NOT NULL,
  uploaded_at TEXT NOT NULL, size INTEGER NOT NULL, info TEXT NOT NULL, blob BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, template_id TEXT NOT NULL, template_name TEXT NOT NULL,
  source_name TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT, source_blob BLOB,
  provider TEXT, status TEXT NOT NULL, error TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  finished_at TEXT, title TEXT, report TEXT, preview TEXT, output BLOB
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
"""
)
_conn.execute("UPDATE jobs SET status = 'error', error = 'Interrupted by a server restart. Run it again.' WHERE status = 'running'")
_conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _q(sql: str, args=()):
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


# ---------- templates ----------

def add_template(name: str, filename: str, user: str, blob: bytes, info: dict) -> str:
    tid = uuid.uuid4().hex[:12]
    _q("INSERT INTO templates VALUES (?,?,?,?,?,?,?,?)", (tid, name, filename, user, now(), len(blob), json.dumps(info), blob))
    return tid


def list_templates() -> list[dict]:
    rows = _q("SELECT id, name, filename, uploaded_by, uploaded_at, size, info FROM templates ORDER BY uploaded_at DESC").fetchall()
    out = []
    for r in rows:
        info = json.loads(r["info"])
        out.append({**{k: r[k] for k in ("id", "name", "filename", "uploaded_by", "uploaded_at", "size")},
                    "sections": len(info["sections"]), "fields": len(info["fields"])})
    return out


def get_template(tid: str, with_blob=False) -> dict | None:
    r = _q("SELECT * FROM templates WHERE id = ?", (tid,)).fetchone()
    if not r:
        return None
    d = {k: r[k] for k in r.keys() if k != "blob"}
    d["info"] = json.loads(d["info"])
    if with_blob:
        d["blob"] = r["blob"]
    return d


def delete_template(tid: str) -> None:
    _q("DELETE FROM templates WHERE id = ?", (tid,))


# ---------- jobs ----------

def add_job(template: dict, source_name: str, kind: str, ref: str | None, blob: bytes | None, provider: str | None, user: str) -> str:
    jid = uuid.uuid4().hex[:12]
    _q(
        "INSERT INTO jobs (id, template_id, template_name, source_name, source_kind, source_ref, source_blob, provider, status, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (jid, template["id"], template["name"], source_name, kind, ref, blob, provider, "running", user, now()),
    )
    return jid


def finish_job(jid: str, *, title=None, report=None, preview=None, output=None, error=None) -> None:
    _q(
        "UPDATE jobs SET status = ?, finished_at = ?, title = ?, report = ?, preview = ?, output = ?, error = ? WHERE id = ?",
        ("error" if error else "complete", now(), title, json.dumps(report) if report else None,
         json.dumps(preview) if preview else None, output, error, jid),
    )


def _job_row(r, full=False) -> dict:
    d = {k: r[k] for k in ("id", "template_id", "template_name", "source_name", "source_kind", "source_ref", "provider",
                           "status", "error", "created_by", "created_at", "finished_at", "title")}
    rep = json.loads(r["report"]) if r["report"] else None
    if full:
        d["report"] = rep
        d["preview"] = json.loads(r["preview"]) if r["preview"] else None
    elif rep:
        d["summary"] = {k: rep[k] for k in ("sections_total", "sections_filled", "sections_missing", "placed_by_ai", "fields_total", "fields_filled", "extra_pieces")}
    return d


def list_jobs(limit=200) -> list[dict]:
    cols = "id, template_id, template_name, source_name, source_kind, source_ref, provider, status, error, created_by, created_at, finished_at, title, report"
    return [_job_row(r) for r in _q(f"SELECT {cols} FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]


def get_job(jid: str) -> dict | None:
    r = _q("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
    return _job_row(r, full=True) if r else None


def get_job_output(jid: str) -> tuple[str, bytes] | None:
    r = _q("SELECT title, source_name, output FROM jobs WHERE id = ?", (jid,)).fetchone()
    if not r or not r["output"]:
        return None
    return (r["title"] or r["source_name"]), r["output"]


def get_job_source(jid: str) -> dict | None:
    r = _q("SELECT template_id, source_name, source_kind, source_ref, source_blob, provider FROM jobs WHERE id = ?", (jid,)).fetchone()
    return dict(r) if r else None


def delete_job(jid: str) -> None:
    _q("DELETE FROM jobs WHERE id = ?", (jid,))
