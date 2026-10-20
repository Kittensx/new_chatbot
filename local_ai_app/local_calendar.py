"""Portable, offline, confirmation-gated personal calendar.

Event creation from a model is PROPOSAL ONLY; only the GUI/controller's
explicit approve call can move a proposal to the actual calendar.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import threading
import uuid

from .memory_store import _atomic_json
from .temporal import local_clock, utc_now

SCHEMA = 'local-ai-calendar-1'
MAX_EVENTS = 20000
MAX_PENDING = 250


def _text(value, maximum, *, required=False):
    if not isinstance(value, str):
        raise ValueError('Expected text.')
    result = value.strip()
    if len(result) > maximum or (required and not result):
        raise ValueError(f'Text must be 1-{maximum} characters.' if required else 'Text too long.')
    return result


def parse_event_datetime(value, timezone='local'):
    """Return UTC datetime; local wall-time input uses configured timezone.

    Ambiguous DST wall times can be represented unambiguously with an explicit
    UTC offset. We never interpret untrusted strings as commands.
    """
    text = _text(value, 40, required=True)
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Use YYYY-MM-DD HH:MM or an ISO timestamp with offset.') from exc
    if parsed.tzinfo is None:
        if timezone == 'local':
            parsed = parsed.astimezone()
        else:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
            except ZoneInfoNotFoundError as exc:
                raise ValueError('Unrecognized timezone or missing tzdata.') from exc
    return parsed.astimezone(dt.timezone.utc)


class LocalCalendar:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / 'calendar.json'
        self.lock = threading.RLock()
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if raw.get('schema') != SCHEMA or not isinstance(raw.get('events'), list) or not isinstance(raw.get('pending'), list):
                raise ValueError('Unknown calendar format. No existing calendar was changed.')
            self.data = raw
        else:
            self.data = {'schema': SCHEMA, 'events': [], 'pending': []}
            self._save()

    def _save(self):
        _atomic_json(self.path, self.data)

    def _draft(self, title, start, end=None, details='', *, timezone='local'):
        name = _text(title, 150, required=True)
        note = _text(details, 1500)
        beginning = parse_event_datetime(start, timezone)
        finish = parse_event_datetime(end, timezone) if end else beginning + dt.timedelta(hours=1)
        if finish <= beginning:
            raise ValueError('Event end must be after event start.')
        if finish - beginning > dt.timedelta(days=31):
            raise ValueError('Individual events cannot exceed 31 days.')
        return {'id': uuid.uuid4().hex, 'title': name, 'start_utc': beginning.isoformat(timespec='seconds'),
                'end_utc': finish.isoformat(timespec='seconds'), 'details': note,
                'created_utc': utc_now().isoformat(timespec='seconds')}

    def add(self, title, start, end=None, details='', *, timezone='local'):
        """Only call this after a direct user GUI action, not from model tools."""
        event = self._draft(title, start, end, details, timezone=timezone)
        with self.lock:
            if len(self.data['events']) >= MAX_EVENTS:
                raise ValueError('Calendar event limit reached.')
            self.data['events'].append(event)
            self._save()
        return copy.deepcopy(event)

    def propose(self, title, start, end=None, details='', *, timezone='local'):
        """Model request: record pending proposal, never an active event."""
        event = self._draft(title, start, end, details, timezone=timezone)
        with self.lock:
            if len(self.data['pending']) >= MAX_PENDING:
                raise ValueError('Please review pending calendar requests before making more.')
            self.data['pending'].append(event)
            self._save()
        return copy.deepcopy(event)

    def resolve(self, proposal_id, approve=False):
        with self.lock:
            match = next((e for e in self.data['pending'] if e['id'] == proposal_id), None)
            if match is None:
                raise ValueError('Calendar proposal no longer exists.')
            if approve and len(self.data['events']) >= MAX_EVENTS:
                raise ValueError('Calendar event limit reached.')
            self.data['pending'] = [e for e in self.data['pending'] if e['id'] != proposal_id]
            if approve:
                self.data['events'].append(match)
            self._save()
        return copy.deepcopy(match)

    def delete(self, event_id):
        with self.lock:
            previous = len(self.data['events'])
            self.data['events'] = [e for e in self.data['events'] if e['id'] != event_id]
            if len(self.data['events']) == previous:
                raise ValueError('Calendar event not found.')
            self._save()

    def events(self, *, start=None, days=31, timezone='local', limit=200):
        if type(days) is not int or not 1 <= days <= 366:
            raise ValueError('Days must be between 1 and 366.')
        if start is None:
            beginning = utc_now()
        else:
            beginning = parse_event_datetime(start, timezone)
        ending = beginning + dt.timedelta(days=days)
        with self.lock:
            selected = [copy.deepcopy(e) for e in self.data['events']
                        if dt.datetime.fromisoformat(e['start_utc']) < ending and
                        dt.datetime.fromisoformat(e['end_utc']) >= beginning]
        selected.sort(key=lambda e: e['start_utc'])
        return selected[:max(1, min(int(limit), 300))]

    def on_date(self, day, timezone='local'):
        if not isinstance(day, dt.date):
            raise ValueError('Expected date.')
        start = dt.datetime.combine(day, dt.time.min).isoformat()
        next_day = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min).isoformat()
        beginning = parse_event_datetime(start, timezone)
        ending = parse_event_datetime(next_day, timezone)
        with self.lock:
            return sorted([copy.deepcopy(e) for e in self.data['events']
                           if dt.datetime.fromisoformat(e['start_utc']) < ending and
                           dt.datetime.fromisoformat(e['end_utc']) > beginning],
                          key=lambda e: e['start_utc'])

    def pending(self):
        with self.lock:
            return copy.deepcopy(self.data['pending'])

    def counts_for_month(self, year, month, timezone='local'):
        import calendar
        counts = {}
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            counts[day] = len(self.on_date(dt.date(year, month, day), timezone))
        return counts

    def export_json(self, destination: Path):
        with self.lock:
            _atomic_json(Path(destination), {'schema': SCHEMA, 'events': self.data['events'], 'pending': []})

    def import_json(self, source: Path):
        """Call only after a GUI confirmation; imported events are normal user-approved events."""
        file = Path(source)
        if file.stat().st_size > 8_000_000:
            raise ValueError('Calendar import too large.')
        source_data = json.loads(file.read_text(encoding='utf-8'))
        if source_data.get('schema') != SCHEMA or not isinstance(source_data.get('events'), list):
            raise ValueError('Not a recognized local calendar backup.')
        imports = []
        for event in source_data['events']:
            imports.append(self._draft(event.get('title'), event.get('start_utc'),
                                       event.get('end_utc'), event.get('details', ''), timezone='UTC'))
        with self.lock:
            if len(self.data['events']) + len(imports) > MAX_EVENTS:
                raise ValueError('Calendar import exceeds event limit.')
            self.data['events'].extend(imports)
            self._save()
        return len(imports)
