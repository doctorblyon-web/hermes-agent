import json
import sqlite3
import time
import uuid
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS item(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
          source_text TEXT NOT NULL, details_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open', source_update_id TEXT NOT NULL,
          action_index INTEGER NOT NULL, created_at REAL NOT NULL,
          UNIQUE(source_update_id, action_index));
        CREATE TABLE IF NOT EXISTS daily_priority(
          position INTEGER PRIMARY KEY, text TEXT NOT NULL,
          source_update_id TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS intake(
          source_update_id TEXT PRIMARY KEY, source_message_id TEXT NOT NULL,
          source_text TEXT NOT NULL, proposal_json TEXT NOT NULL,
          result_json TEXT, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS action(
          id TEXT PRIMARY KEY, source_update_id TEXT NOT NULL,
          action_index INTEGER NOT NULL, kind TEXT NOT NULL,
          state TEXT NOT NULL, payload_json TEXT NOT NULL,
          result_json TEXT, updated_at REAL NOT NULL,
          UNIQUE(source_update_id, action_index));
        CREATE TABLE IF NOT EXISTS pending_clarification(
          id TEXT PRIMARY KEY, source_update_id TEXT NOT NULL,
          action_id TEXT NOT NULL, kind TEXT NOT NULL,
          question TEXT NOT NULL, context_json TEXT NOT NULL,
          state TEXT NOT NULL, reply_update_id TEXT,
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(intake)")}
        for name, definition in (
            ("processing_state", "TEXT NOT NULL DEFAULT 'received'"),
            ("source_timestamp", "TEXT"),
            ("pending_json", "TEXT"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE intake ADD COLUMN {name} {definition}")
        self.db.commit()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                candidate.chmod(0o600)

    def prior_result(self, update_id):
        row = self.db.execute("SELECT result_json FROM intake WHERE source_update_id=?", (str(update_id),)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def record_inbound(self, update_id, message_id, text, source_timestamp):
        self.db.execute(
            "INSERT OR IGNORE INTO intake(source_update_id,source_message_id,source_text,proposal_json,result_json,created_at,processing_state,source_timestamp,pending_json) VALUES(?,?,?,?,NULL,?,'received',?,NULL)",
            (str(update_id), str(message_id), text, "{}", time.time(), source_timestamp),
        )
        row = self.db.execute("SELECT source_message_id,source_text FROM intake WHERE source_update_id=?", (str(update_id),)).fetchone()
        if not row or row[0] != str(message_id) or row[1] != text:
            raise ValueError("Telegram update identity is already bound to different intake")
        self.db.commit()

    def set_proposal(self, update_id, proposal):
        self.db.execute(
            "UPDATE intake SET proposal_json=?,processing_state='processing' WHERE source_update_id=?",
            (json.dumps(proposal, sort_keys=True, ensure_ascii=False), str(update_id)),
        )
        self.db.commit()

    def begin(self, update_id, message_id, text, proposal):
        self.record_inbound(update_id, message_id, text, None)
        self.set_proposal(update_id, proposal)

    def put_action(self, update_id, index, kind, state, payload, result=None):
        action_id = f"act_{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT OR IGNORE INTO action VALUES(?,?,?,?,?,?,?,?)",
            (action_id, str(update_id), index, kind, state, json.dumps(payload, sort_keys=True, ensure_ascii=False), json.dumps(result, sort_keys=True, ensure_ascii=False) if result is not None else None, time.time()),
        )
        row = self.db.execute("SELECT id FROM action WHERE source_update_id=? AND action_index=?", (str(update_id), index)).fetchone()
        self.db.commit()
        return row[0]

    def update_action(self, action_id, state, result):
        self.db.execute("UPDATE action SET state=?,result_json=?,updated_at=? WHERE id=?", (state, json.dumps(result, sort_keys=True, ensure_ascii=False), time.time(), action_id))
        self.db.commit()

    def add_pending(self, update_id, action_id, kind, question, context):
        pending_id = f"clar_{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO pending_clarification VALUES(?,?,?,?,?,?,'pending',NULL,?,?)",
            (pending_id, str(update_id), action_id, kind, question, json.dumps(context, sort_keys=True, ensure_ascii=False), time.time(), time.time()),
        )
        self.db.commit()
        return pending_id

    def pending(self):
        row = self.db.execute("SELECT * FROM pending_clarification WHERE state='pending' ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def recent_source_containing(self, phrase):
        row = self.db.execute("SELECT source_text FROM intake WHERE lower(source_text) LIKE ? ORDER BY created_at DESC LIMIT 1", (f"%{phrase.lower()}%",)).fetchone()
        return row[0] if row else None

    def source_text(self, update_id):
        row = self.db.execute("SELECT source_text FROM intake WHERE source_update_id=?", (str(update_id),)).fetchone()
        return row[0] if row else None

    def latest_birthday_reminder(self):
        row = self.db.execute(
            "SELECT id,title,details_json FROM item WHERE kind='reminder' AND status='open' "
            "AND (lower(title) LIKE '%birthday%' OR lower(details_json) LIKE '%birthday%') "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return {"id": row[0], "title": row[1], **json.loads(row[2])} if row else None

    def latest_multi_action_summary(self):
        row = self.db.execute(
            "SELECT i.source_update_id,i.source_message_id,i.source_text FROM intake i WHERE (SELECT count(*) FROM action a WHERE a.source_update_id=i.source_update_id) > 1 ORDER BY i.created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        actions = self.db.execute("SELECT id,kind,state,payload_json,result_json FROM action WHERE source_update_id=? ORDER BY action_index", (row[0],))
        return {
            "source_update_id": row[0], "source_message_id": row[1], "source_text": row[2],
            "actions": [{"id": a[0], "kind": a[1], "state": a[2], "payload": json.loads(a[3]), "result": json.loads(a[4]) if a[4] else None} for a in actions],
        }

    def update_pending(self, pending_id, context, question, reply_update_id, state="pending"):
        self.db.execute(
            "UPDATE pending_clarification SET context_json=?,question=?,reply_update_id=?,state=?,updated_at=? WHERE id=?",
            (json.dumps(context, sort_keys=True, ensure_ascii=False), question, str(reply_update_id), state, time.time(), pending_id),
        )
        self.db.commit()

    def add_item(self, kind, title, source_text, details, update_id, index):
        item_id = f"{kind[:3]}_{uuid.uuid4().hex[:12]}"
        self.db.execute("INSERT OR IGNORE INTO item VALUES(?,?,?,?,?,'open',?,?,?)", (item_id, kind, title, source_text, json.dumps(details, sort_keys=True, ensure_ascii=False), str(update_id), index, time.time()))
        row = self.db.execute("SELECT id FROM item WHERE source_update_id=? AND action_index=?", (str(update_id), index)).fetchone()
        self.db.commit()
        return row[0]

    def update_item(self, item_id, details, status="open"):
        self.db.execute("UPDATE item SET details_json=?,status=? WHERE id=?", (json.dumps(details, sort_keys=True, ensure_ascii=False), status, item_id))
        self.db.commit()

    def set_priorities(self, values, update_id):
        with self.db:
            self.db.execute("DELETE FROM daily_priority")
            self.db.executemany("INSERT INTO daily_priority VALUES(?,?,?,?)", [(i + 1, value, str(update_id), time.time()) for i, value in enumerate(values)])

    def status(self, query):
        q = query.lower()
        result = {"source": "PA canonical state"}
        if "priorit" in q or "goal" in q:
            result["priorities"] = [r[0] for r in self.db.execute("SELECT text FROM daily_priority ORDER BY position")]
        kinds = ["obligation", "waiting"] if "waiting" in q or "owe" in q else ["task", "reminder", "obligation"]
        marks = ",".join("?" for _ in kinds)
        rows = self.db.execute(f"SELECT id,kind,title,details_json FROM item WHERE status='open' AND kind IN ({marks}) ORDER BY created_at DESC LIMIT 10", kinds)
        result["items"] = [{"id": r[0], "kind": r[1], "title": r[2], **json.loads(r[3])} for r in rows]
        return result

    def finish(self, update_id, result):
        self.db.execute("UPDATE intake SET result_json=?,processing_state='completed' WHERE source_update_id=?", (json.dumps(result, sort_keys=True, ensure_ascii=False), str(update_id)))
        self.db.commit()
