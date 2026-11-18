from __future__ import annotations

from pathlib import Path
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS ltm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_sessions(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,title TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_messages(
 pk INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,user_id TEXT NOT NULL,session_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL,role TEXT NOT NULL CHECK(role IN ('user','assistant')),body TEXT NOT NULL,
 created REAL NOT NULL,idem TEXT NOT NULL,turn_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'active',recycle_id TEXT,UNIQUE(user_id,idem),UNIQUE(session_id,ordinal));
CREATE INDEX IF NOT EXISTS ltm_messages_scope ON ltm_messages(user_id,status,session_id,ordinal);
CREATE INDEX IF NOT EXISTS ltm_messages_time ON ltm_messages(user_id,status,created);
CREATE VIRTUAL TABLE IF NOT EXISTS ltm_messages_fts USING fts5(terms,tokenize='unicode61');
CREATE TABLE IF NOT EXISTS ltm_attachments(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,user_id TEXT NOT NULL,
 relative_path TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,duration REAL,sha256 TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active',recognition TEXT,recognition_version TEXT);
CREATE INDEX IF NOT EXISTS ltm_attachment_message ON ltm_attachments(message_id);
CREATE TABLE IF NOT EXISTS ltm_revisions(id TEXT PRIMARY KEY,target_id TEXT NOT NULL,user_id TEXT NOT NULL,
 correction TEXT NOT NULL,kind TEXT NOT NULL,event_time TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,
 idem TEXT NOT NULL,user_message_id TEXT,turn_id TEXT NOT NULL,status TEXT NOT NULL,sources TEXT NOT NULL,
 epoch INTEGER NOT NULL,created REAL NOT NULL,UNIQUE(user_id,idem));
CREATE TABLE IF NOT EXISTS ltm_recycle(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,message_ids TEXT NOT NULL,
 created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL,seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_jobs(id TEXT PRIMARY KEY,message_id TEXT NOT NULL,revision INTEGER NOT NULL,
 run_id TEXT NOT NULL,seq INTEGER NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
 UNIQUE(message_id,revision));
CREATE TABLE IF NOT EXISTS ltm_chunks(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,
 source_ids TEXT NOT NULL,source_versions TEXT NOT NULL,body TEXT NOT NULL,generation TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_episodes(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,source_ids TEXT NOT NULL,
 source_versions TEXT NOT NULL,summary TEXT NOT NULL,event_time TEXT,created REAL NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ltm_products(id TEXT PRIMARY KEY,kind TEXT NOT NULL,user_id TEXT NOT NULL,
 source_ids TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
"""

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity(persona_id TEXT PRIMARY KEY,ledger_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS operations(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,
 target_ids TEXT NOT NULL,recycle_id TEXT NOT NULL,user_id TEXT NOT NULL,created REAL NOT NULL,
 expires REAL NOT NULL,applied INTEGER NOT NULL DEFAULT 0);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA secure_delete=ON")
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def terms(text: str) -> str:
    # Hex-encoded tokens prevent FTS query syntax injection and preserve CJK singles.
    text = text.casefold()
    tokens = set()
    for word in re.findall(r"[a-z0-9_]+|[^\W\d_]", text, re.UNICODE):
        tokens.add("w" + word.encode().hex())
    compact = re.sub(r"\s+", "", text)
    for a, b in zip(compact, compact[1:]):
        if not (a.isascii() and b.isascii()):
            tokens.add("b" + (a + b).encode().hex())
    return " ".join(sorted(tokens))


def match_query(text: str, relaxed: bool = False) -> str:
    values = terms(text).split()
    # Bounding query expansion is necessary for untrusted long search input.
    return (" OR " if relaxed else " AND ").join(values[:64])
