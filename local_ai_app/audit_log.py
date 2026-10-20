"""Local audit trail with no prompts, tool arguments, results or calendar details.

Audit is intentionally NOT a conversation transcript. All event fields are
strictly allowlisted; unknown/free-form values are never persisted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import secrets
import threading

from .memory_store import _atomic_json
from .temporal import utc_now

VALID_KINDS = {
    'chat.send', 'chat.complete', 'chat.error', 'profile.change',
    'tool.calculate', 'tool.clock', 'tool.calendar_read', 'tool.calendar_propose', 'tool.denied',
    'tool.thought_pin', 'tool.thought_resolve', 'tool.thought_list',
    'calendar.approve', 'calendar.dismiss', 'calendar.create', 'calendar.delete',
    'calendar.import', 'calendar.export', 'audit.settings', 'audit.export',
    'web.mode', 'web.request', 'web.approve', 'web.deny', 'web.fetch', 'web.revoke',
    'web.auto_approve', 'web.batch_approve', 'web.batch_deny', 'web.grant',
    'web.grant_pause', 'web.grant_resume', 'web.grant_revoke',
}
VALID_STATUS = {'ok', 'error', 'pending', 'denied'}


class AuditTrail:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.folder / 'settings.json'
        self.lock = threading.RLock()
        if self.settings_path.exists():
            raw = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if raw.get('schema') != 'local-ai-audit-1':
                raise ValueError('Unsupported audit settings; original data untouched.')
            self.enabled = bool(raw.get('enabled', True))
            self.retention_days = self._retention(raw.get('retention_days', 30))
            self.salt = raw.get('salt')
            if not isinstance(self.salt, str) or len(self.salt) < 24:
                raise ValueError('Invalid audit identifier salt.')
        else:
            self.enabled, self.retention_days = True, 30
            self.salt = secrets.token_hex(16)
            self.save_settings()
        self.prune()

    @staticmethod
    def _retention(value):
        if type(value) is not int or not 1 <= value <= 365:
            raise ValueError('Audit retention must be 1 to 365 days.')
        return value

    def save_settings(self):
        _atomic_json(self.settings_path, {'schema': 'local-ai-audit-1',
            'enabled': self.enabled, 'retention_days': self.retention_days, 'salt': self.salt})

    def configure(self, *, enabled, retention_days):
        if type(enabled) is not bool:
            raise ValueError('Audit enabled must be a boolean.')
        retention = self._retention(retention_days)
        with self.lock:
            self.enabled, self.retention_days = enabled, retention
            self.save_settings()
            self.prune()
        self.record('audit.settings')

    def _pseudonym(self, identifier):
        if not identifier:
            return None
        return hashlib.sha256((self.salt + str(identifier)).encode('utf-8')).hexdigest()[:16]

    def record(self, kind, *, status='ok', conversation_id=None):
        if kind not in VALID_KINDS or status not in VALID_STATUS:
            raise ValueError('Unknown audit event or status.')
        if not self.enabled:
            return
        stamp = utc_now()
        entry = {'utc': stamp.isoformat(timespec='seconds'), 'event': kind, 'status': status}
        if conversation_id:
            entry['conversation_ref'] = self._pseudonym(conversation_id)
        line = json.dumps(entry, separators=(',', ':'), ensure_ascii=True)
        with self.lock:
            path = self.folder / (stamp.strftime('%Y-%m-%d') + '.jsonl')
            # Bounded file size; do not append to unbounded audit files.
            if path.exists() and path.stat().st_size > 2_000_000:
                return
            with path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
            self.prune()

    def prune(self):
        with self.lock:
            oldest = utc_now().date() - dt.timedelta(days=self.retention_days - 1)
            for path in self.folder.glob('*.jsonl'):
                try:
                    day = dt.date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if day < oldest:
                    path.unlink()

    def recent(self, limit=200):
        entries = []
        requested = min(max(1, int(limit)), 500)
        with self.lock:
            for path in sorted(self.folder.glob('????-??-??.jsonl'), reverse=True):
                # Each daily file is capped at 2 MB; stop after enough recent
                # records rather than reading all retained days into memory.
                for line in reversed(path.read_text(encoding='utf-8').splitlines()):
                    try:
                        obj = json.loads(line)
                        if obj.get('event') in VALID_KINDS:
                            entries.append(obj)
                    except (ValueError, TypeError):
                        pass
                    if len(entries) >= requested:
                        break
                if len(entries) >= requested:
                    break
            return entries[:requested]

    def clear(self):
        with self.lock:
            for path in self.folder.glob('????-??-??.jsonl'):
                path.unlink()

    def export(self, target: Path):
        """Explicit user GUI action only. Copy redacted metadata; no settings salt."""
        _atomic_json(Path(target), {'schema': 'local-ai-audit-export-1', 'events': self.recent(500)})
        self.record('audit.export')
