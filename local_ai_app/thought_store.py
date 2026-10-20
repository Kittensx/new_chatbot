"""Durable, user-controlled *open loops*, distinct from fact memory and chat history.

Thoughts are short, model/user-authored ideas, questions and experiments. They are
not raw reasoning traces, validated factual memories, or permission to run tasks.
The canonical data is local JSON; no internet access or background execution.
"""
from __future__ import annotations

import datetime as dt
import json
import functools
import threading
from pathlib import Path
import re

from .history_store import new_id
from .memory_store import _atomic_json
from .temporal import utc_now, parse_utc, iso_utc

SCHEMA = 'local-ai-thoughts-1'
WORD = re.compile(r'\w+', re.UNICODE)
MAX_RECORDS = 800
MAX_RETRIEVAL = 4
CATEGORIES = {'question', 'idea', 'experiment', 'intention', 'creative'}
STATUS = {'active', 'resolved', 'dismissed'}


def _string(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError('Invalid or oversized thought field.')
    return value.strip()


def _ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Invalid thought identifier.')
    return value


def _synchronized(fn):
    @functools.wraps(fn)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapped


class ThoughtStore:
    def __init__(self, directory: Path):
        self._lock = threading.RLock()
        self.directory = Path(directory)
        self.path = self.directory / 'thoughts.json'
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (data.get('schema') != SCHEMA or not isinstance(data.get('items'), list)
                    or not isinstance(data.get('settings'), dict) or len(data['items']) > MAX_RECORDS):
                raise ValueError('Unrecognized thought archive. Original file was preserved.')
            self.items = data['items']
            self.settings = data['settings']
            for item in self.items:
                self._validate_record(item)
            self._validate_settings(self.settings)
        else:
            self.items = []
            self.settings = {'allow_ai_pinning': False, 'automatic_recall': True,
                             'max_active_ai_pins': 25}
            self.save()

    @staticmethod
    def _validate_settings(settings):
        if (type(settings.get('allow_ai_pinning')) is not bool
                or type(settings.get('automatic_recall')) is not bool
                or type(settings.get('max_active_ai_pins')) is not int
                or not 1 <= settings['max_active_ai_pins'] <= 100):
            raise ValueError('Invalid thought autonomy settings.')

    @staticmethod
    def _validate_record(item):
        if not isinstance(item, dict):
            raise ValueError('Malformed thought entry.')
        _ident(item.get('id'))
        _string(item.get('title'), 120, required=True)
        _string(item.get('detail'), 700, required=True)
        _string(item.get('why', ''), 300)
        _string(item.get('conversation_id', ''), 32)
        _string(item.get('persona_id', ''), 100)
        if item.get('category') not in CATEGORIES or item.get('status') not in STATUS:
            raise ValueError('Invalid thought category or status.')
        if item.get('owner') not in ('agent', 'user') or type(item.get('pinned')) is not bool:
            raise ValueError('Invalid thought owner or pin state.')
        if parse_utc(item.get('created_at')) is None:
            raise ValueError('Invalid thought timestamp.')

    @_synchronized
    def save(self):
        _atomic_json(self.path, {'schema': SCHEMA, 'version': 1,
                                 'settings': self.settings, 'items': self.items})

    @_synchronized
    def configure(self, *, allow_ai_pinning, automatic_recall, max_active_ai_pins):
        candidate = {'allow_ai_pinning': allow_ai_pinning,
                     'automatic_recall': automatic_recall,
                     'max_active_ai_pins': max_active_ai_pins}
        self._validate_settings(candidate)
        if self.active_agent_count() > candidate['max_active_ai_pins']:
            raise ValueError('Resolve or dismiss active AI pins before reducing this limit.')
        self.settings = candidate
        self.save()

    @_synchronized
    def active_agent_count(self):
        return sum(1 for i in self.items if i['owner'] == 'agent'
                   and i['pinned'] and i['status'] == 'active')

    @_synchronized
    def pin(self, *, title, detail, why='', category='idea', owner='user',
            conversation_id='', persona_id=''):
        if owner not in ('user', 'agent') or category not in CATEGORIES:
            raise ValueError('Invalid thought owner or category.')
        title, detail = _string(title, 120, required=True), _string(detail, 700, required=True)
        why = _string(why, 300)
        conversation_id = _string(conversation_id, 32)
        persona_id = _string(persona_id, 100)
        if conversation_id:
            _ident(conversation_id)
        if owner == 'agent' and not self.settings['allow_ai_pinning']:
            raise ValueError('AI thought pinning is disabled in your settings.')
        if owner == 'agent' and self.active_agent_count() >= self.settings['max_active_ai_pins']:
            raise ValueError('AI active thought pin limit reached.')
        if len(self.items) >= MAX_RECORDS:
            raise ValueError('Thought archive full: resolve, dismiss, or delete entries.')
        stamp = iso_utc()
        item = {'id': new_id(), 'title': title, 'detail': detail, 'why': why,
                'category': category, 'owner': owner, 'pinned': True,
                'status': 'active', 'conversation_id': conversation_id,
                'persona_id': persona_id, 'created_at': stamp, 'updated_at': stamp}
        self.items.append(item)
        self.save()
        return dict(item)

    @_synchronized
    def get(self, thought_id):
        tid = _ident(thought_id)
        return next((x for x in self.items if x['id'] == tid), None) or self._missing()

    @staticmethod
    def _missing():
        raise ValueError('Thought no longer exists.')

    @_synchronized
    def modify(self, thought_id, *, status=None, pinned=None, title=None, detail=None):
        item = self.get(thought_id)
        if status is not None:
            if status not in STATUS:
                raise ValueError('Invalid thought status.')
            item['status'] = status
        if pinned is not None:
            if type(pinned) is not bool:
                raise ValueError('Invalid pin value.')
            if pinned and item['owner'] == 'agent' and not item['pinned']:
                if self.active_agent_count() >= self.settings['max_active_ai_pins']:
                    raise ValueError('AI pin limit reached.')
            item['pinned'] = pinned
        if title is not None:
            item['title'] = _string(title, 120, required=True)
        if detail is not None:
            item['detail'] = _string(detail, 700, required=True)
        item['updated_at'] = iso_utc()
        self.save()
        return dict(item)

    @_synchronized
    def agent_resolve(self, thought_id, *, persona_id, conversation_id):
        item = self.get(thought_id)
        if (item['owner'] != 'agent' or item['status'] != 'active'
                or item['persona_id'] != persona_id):
            raise ValueError('Agent may only resolve its own active thoughts for this persona.')
        return self.modify(thought_id, status='resolved', pinned=False)

    @_synchronized
    def remove(self, thought_id):
        item = self.get(thought_id)
        self.items.remove(item)
        self.save()

    @_synchronized
    def export_json(self, path):
        _atomic_json(Path(path), {'schema': SCHEMA, 'version': 1,
                                  'items': self.items, 'settings': self.settings})

    @_synchronized
    def import_json(self, path):
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        if (raw.get('schema') != SCHEMA or not isinstance(raw.get('items'), list)
                or len(raw['items']) > MAX_RECORDS):
            raise ValueError('Invalid thought backup. No changes made.')
        imported = [dict(x) for x in raw['items']]
        for item in imported:
            self._validate_record(item)
        # Import does not grant tool permissions or override local settings.
        if len(imported) + len(self.items) > MAX_RECORDS:
            raise ValueError('Too many ideas to import.')
        existing = {x['id'] for x in self.items}
        for item in imported:
            if item['id'] in existing:
                item['id'] = new_id()
            existing.add(item['id'])
        self.items.extend(imported)
        self.save()
        return len(imported)

    @_synchronized
    def clear(self):
        self.items = []
        self.save()

    @_synchronized
    def purge_conversation(self, conversation_id):
        old = len(self.items)
        self.items = [x for x in self.items if x.get('conversation_id') != conversation_id]
        if len(self.items) != old:
            self.save()
        return old - len(self.items)

    @_synchronized
    def clear_conversation_links(self, conversation_ids):
        """Delete thought links to deleted conversations; leave global user pins intact."""
        ids = set(conversation_ids)
        before = len(self.items)
        self.items = [i for i in self.items if i.get('conversation_id') not in ids]
        if len(self.items) != before:
            self.save()
        return before - len(self.items)

    @_synchronized
    def relevant(self, query, *, persona_id='', conversation_id='', limit=MAX_RETRIEVAL):
        """Only expose pins from this persona and eligible conversation, never global archive text."""
        if not self.settings['automatic_recall']:
            return []
        terms = set(WORD.findall(str(query).lower())) - {
            'please','about','would','could','what','some','with','that','this','have','just','your'}
        if not terms:
            return []
        scored = []
        for item in self.items:
            if item['status'] != 'active' or not item['pinned']:
                continue
            if item['persona_id'] not in ('', persona_id):
                continue
            # Pinning explicitly promotes a brief idea to the persona's
            # cross-session retrieval scope. The original chat remains private;
            # only this pinned summary may be retrieved elsewhere.
            indexed = set(WORD.findall((item['title']+' '+item['detail']+' '+item['category']).lower()))
            match = len(terms & indexed)
            if match:
                scored.append((match, item['updated_at'], item))
        scored.sort(key=lambda z: (z[0], z[1]), reverse=True)
        return [dict(v[2]) for v in scored[:min(int(limit), MAX_RETRIEVAL)]]

    @_synchronized
    def visible(self, *, persona_id='', conversation_id=None, include_archived=True):
        return [dict(x) for x in self.items if
                x['persona_id'] in ('', persona_id) and
                (conversation_id is None or x['conversation_id'] in ('', conversation_id)
                 or x['persona_id'] == persona_id) and
                (include_archived or x['status'] == 'active')]
