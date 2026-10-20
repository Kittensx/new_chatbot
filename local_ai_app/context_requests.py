"""Approval inbox for local keyword suggestions. Index results NEVER go to the model automatically."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .history_store import new_id, now
from .memory_store import _atomic_json

STOPWORDS = frozenset(('what', 'about', 'when', 'where', 'which', 'then', 'this', 'that', 'there',
                       'their', 'have', 'your', 'please', 'could', 'would', 'should', 'from',
                       'with', 'into', 'been', 'were', 'does', 'just', 'want', 'know', 'again',
                       'these', 'those', 'how', 'why', 'the', 'and', 'for', 'can', 'did', 'was',
                       'are', 'let', 'you', 'our', 'its', 'new', 'recent', 'model', 'chat'))
MARKER = re.compile(r'\[\[CONTEXT_SEARCH_REQUEST:\s*([^\]\n]{3,140})\s*\]\]', re.IGNORECASE)


def extract_terms(text, limit=8):
    tokens = re.findall(r'[^\W_]+', (text or '').casefold(), flags=re.UNICODE)
    seen = set()
    output = []
    for word in tokens:
        if len(word) >= 4 and word not in STOPWORDS and word not in seen:
            output.append(word)
            seen.add(word)
        if len(output) >= limit:
            break
    return ' '.join(output)


class ContextInbox:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'context_requests.json'
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.data, dict) or self.data.get('schema') != 'local-ai-context-inbox-1' or not isinstance(self.data.get('requests'), list):
                raise ValueError('Unknown context inbox format; left unchanged.')
        else:
            self.data = {'schema': 'local-ai-context-inbox-1', 'requests': []}
            self.save()

    def save(self):
        _atomic_json(self.path, self.data)

    def pending(self, conversation_id=None):
        result = [r for r in self.data['requests'] if r['status'] == 'pending']
        if conversation_id:
            result = [r for r in result if r['conversation_id'] == conversation_id]
        return list(reversed(result))

    def get(self, request_id):
        return next((r for r in self.data['requests'] if r['id'] == request_id), None)

    def propose(self, vault, query, *, cid=None, from_model=False):
        cid = cid or vault.active['id']
        original = query.strip()[:140]
        words = extract_terms(original)
        if not words:
            return None
        selected = set(vault.get_conversation(cid)['selected_ids'])
        hits = vault.search(words, limit=60, mode='all') + vault.search(words, limit=200, mode='any')
        # Rank by count of matching query terms; do NOT read full archive blobs.
        tokens = set(words.split())
        candidates = []
        seen_hits = set()
        for hit in hits:
            if hit['item_id'] in seen_hits or hit['item_id'] in selected:
                continue
            seen_hits.add(hit['item_id'])
            frequency = len(tokens & set(re.findall(r'\w+', (hit['snippet'] + ' ' + hit['conversation_name']).casefold())))
            if frequency < 1:
                continue
            candidates.append((frequency, hit))
        candidates.sort(key=lambda pair: (-pair[0], pair[1]['timestamp']), reverse=False)
        items = [h['item_id'] for _, h in candidates[:8]]
        if not items:
            return None
        duplicate = next((r for r in self.pending(cid) if r['item_ids'] == items and r['query'] == original), None)
        if duplicate:
            return duplicate
        request = {'id': new_id(), 'created_at': now(), 'conversation_id': cid,
                   'query': original, 'from_model': bool(from_model), 'status': 'pending',
                   'item_ids': items}
        self.data['requests'].append(request)
        # Keep a bounded actionable inbox. Completed requests aren't part of history.
        self.data['requests'] = self.data['requests'][-1000:]
        self.save()
        return request

    def resolve(self, vault, request_id, *, action, selected_ids=None):
        req = self.get(request_id)
        if req is None or req['status'] != 'pending':
            raise ValueError('Context request is no longer pending.')
        if action not in ('approve', 'dismiss'):
            raise ValueError('Unknown inbox action.')
        if action == 'approve':
            chosen = list(selected_ids if selected_ids is not None else req['item_ids'])
            if not set(chosen).issubset(set(req['item_ids'])):
                raise ValueError('Only entries presented for approval can be admitted.')
            # Set active for only this operation; never leave another chat selected.
            old = vault.data['active_id']
            try:
                vault.set_active(req['conversation_id'])
                vault.set_selected(chosen, True)
            finally:
                if any(m['id'] == old for m in vault.conversations):
                    vault.set_active(old)
            req['approved_ids'] = chosen
        req['status'] = 'approved' if action == 'approve' else 'dismissed'
        req['resolved_at'] = now()
        self.save()
        return req

    def purge_deleted(self, removed_item_ids=None, removed_conversation_ids=None):
        ids = set(removed_item_ids or [])
        chats = set(removed_conversation_ids or [])
        new = []
        for request in self.data['requests']:
            if request['conversation_id'] in chats:
                continue
            request['item_ids'] = [i for i in request['item_ids'] if i not in ids]
            if request['item_ids']:
                new.append(request)
        if new != self.data['requests']:
            self.data['requests'] = new
            self.save()

    def clear(self):
        self.data['requests'] = []
        self.save()


def parse_model_request(reply):
    match = MARKER.search(reply or '')
    return match.group(1).strip() if match else None
