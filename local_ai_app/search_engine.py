"""Rebuildable disk index for JSON chat archives.

FTS5 is preferred, with a LIKE fallback when the Python SQLite build lacks FTS5.
Source-of-truth conversation data remains one JSON file per conversation. The
SQLite file is a disposable cache, not a second authoritative conversation store.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_TOKEN = re.compile(r'\w+', flags=re.UNICODE)


class KeywordIndex:
    def __init__(self, directory: Path):
        self.path = Path(directory) / 'keyword-index.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=4000')
        self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('CREATE TABLE IF NOT EXISTS entries ('
                        'item_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, '
                        'role TEXT NOT NULL, kind TEXT NOT NULL, timestamp TEXT NOT NULL, '
                        'origin TEXT NOT NULL, content TEXT NOT NULL)')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_convo ON entries(conversation_id)')
        self.db.execute('CREATE INDEX IF NOT EXISTS idx_user_time ON entries(role,kind,timestamp)')
        try:
            self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS indexed_text USING fts5(item_id UNINDEXED, content, origin)')
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def close(self):
        self.db.close()

    def rebuild(self, conversations):
        """Iterate lazily loaded JSON conversations. No model communication occurs."""
        with self.db:
            self.db.execute('DELETE FROM entries')
            if self.fts:
                self.db.execute('DELETE FROM indexed_text')
            for conv in conversations:
                self._insert_conversation(conv)

    def _insert_conversation(self, conv):
        records = [(x['id'], conv['id'], x['role'], x['kind'], x['timestamp'],
                    x.get('origin', ''), x['content']) for x in conv['items']]
        self.db.executemany('INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?)', records)
        if self.fts:
            self.db.executemany('INSERT INTO indexed_text(item_id, content, origin) VALUES (?, ?, ?)',
                                [(r[0], r[6], r[5]) for r in records])

    def refresh_conversation(self, conv):
        with self.db:
            old = [r[0] for r in self.db.execute('SELECT item_id FROM entries WHERE conversation_id=?', (conv['id'],))]
            self.db.execute('DELETE FROM entries WHERE conversation_id=?', (conv['id'],))
            if self.fts:
                self.db.executemany('DELETE FROM indexed_text WHERE item_id=?', [(x,) for x in old])
            self._insert_conversation(conv)

    def delete_conversation(self, cid):
        with self.db:
            old = [r[0] for r in self.db.execute('SELECT item_id FROM entries WHERE conversation_id=?', (cid,))]
            self.db.execute('DELETE FROM entries WHERE conversation_id=?', (cid,))
            if self.fts:
                self.db.executemany('DELETE FROM indexed_text WHERE item_id=?', [(x,) for x in old])

    def get_origin(self, item_id):
        row = self.db.execute('SELECT conversation_id FROM entries WHERE item_id=?', (item_id,)).fetchone()
        return row[0] if row else None

    def search(self, query: str, *, limit=100, mode='all', conversation_id=None, offset=0):
        if mode not in ('all', 'any', 'phrase'):
            raise ValueError('Invalid search mode.')
        tokens = _TOKEN.findall(query)[:20]
        limit = max(1, min(int(limit), 250))
        offset = max(0, min(int(offset), 200_000))
        if not tokens:
            return []
        if self.fts:
            if mode == 'phrase':
                expression = '"' + ' '.join(tokens).replace('"', '""') + '"'
            else:
                expression = (' OR ' if mode == 'any' else ' AND ').join('"' + t.replace('"', '""') + '"*' for t in tokens)
            # FTS MATCH binds the expression, so search strings cannot inject SQL.
            q = ('SELECT e.item_id, e.conversation_id, e.role, e.kind, e.timestamp, e.origin, '
                 'substr(e.content, 1, 220) AS snippet FROM indexed_text AS f '
                 'JOIN entries AS e ON f.item_id=e.item_id WHERE indexed_text MATCH ?')
            args = [expression]
        else:
            predicate = ' OR ' if mode == 'any' else ' AND '
            terms = [query] if mode == 'phrase' else tokens
            q = ('SELECT item_id, conversation_id, role, kind, timestamp, origin, '
                 'substr(content, 1, 220) AS snippet FROM entries WHERE (' +
                 predicate.join('(instr(lower(content || " " || origin), lower(?)) > 0)' for _ in terms) + ')')
            args = list(terms)
        if conversation_id:
            q += ' AND e.conversation_id=?' if self.fts else ' AND conversation_id=?'
            args.append(conversation_id)
        q += ' ORDER BY e.timestamp DESC, e.item_id DESC' if self.fts else ' ORDER BY timestamp DESC, item_id DESC'
        q += ' LIMIT ? OFFSET ?'
        args.extend([limit, offset])
        try:
            return [dict(row) for row in self.db.execute(q, args)]
        except sqlite3.OperationalError as exc:
            raise ValueError(f'Invalid keyword query: {exc}') from exc

    def last_user_timestamp(self):
        row=self.db.execute("SELECT timestamp FROM entries WHERE role='user' AND kind='message' ORDER BY timestamp DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def count(self):
        return self.db.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
