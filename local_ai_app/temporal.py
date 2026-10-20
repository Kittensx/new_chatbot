"""Offline clock, exact elapsed-time accounting and fictional retrieval gating."""
from __future__ import annotations

import datetime as dt
import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value=None):
    return (value or utc_now()).astimezone(dt.timezone.utc).isoformat(timespec='seconds')


def parse_utc(value):
    try:
        output = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if output.tzinfo is None:
            output = output.replace(tzinfo=dt.timezone.utc)
        return output.astimezone(dt.timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None


def local_clock(now=None, zone='local'):
    now = now or utc_now()
    if zone == 'local':
        return now.astimezone()
    try:
        return now.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError as err:
        raise ValueError(f'Timezone data missing for {zone}. Use local system time or install tzdata.') from err


def last_user_gap(conversation, at=None):
    at = at or utc_now()
    for item in reversed(conversation['items']):
        if item['role'] == 'user' and item['kind'] == 'message':
            when = parse_utc(item.get('timestamp'))
            return max(0, int((at - when).total_seconds())) if when else None
    return None


def elapsed_text(seconds):
    if seconds is None:
        return 'unknown (no prior timestamp)'
    seconds = max(0, int(seconds))
    units = []
    for size, label in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        count, seconds = divmod(seconds, size)
        if count:
            units.append(f'{count} {label}' + ('s' if count != 1 else ''))
    if seconds or not units:
        units.append(f'{seconds} second' + ('s' if seconds != 1 else ''))
    return ', '.join(units)


def session_time_snapshot(conversation, *, at=None, zone='local', last_global_user_at=None):
    """Computes elapsed intervals before new message is stored; no model arithmetic.

    Conversation duration is based on first USER message, not file creation.
    Do not fabricate elapsed time when no user message is recorded yet.
    """
    at = at or utc_now()
    wall = local_clock(at, zone)
    user_times = [parse_utc(m.get('timestamp')) for m in conversation.get('items', [])
                  if m.get('kind') == 'message' and m.get('role') == 'user']
    user_times = [x for x in user_times if x is not None]
    first = min(user_times) if user_times else None
    previous = max(user_times) if user_times else None
    last_global = parse_utc(last_global_user_at)
    local_iso = lambda x: local_clock(x, zone).isoformat(timespec='seconds') if x else None
    def gap(x):
        return max(0, int((at - x).total_seconds())) if x else None
    return {
        'current_utc': iso_utc(at), 'current_local': wall.isoformat(timespec='seconds'),
        'timezone': str(wall.tzinfo), 'configured_zone': zone,
        'first_user_local': local_iso(first), 'previous_user_local': local_iso(previous),
        'current_message_local': local_iso(at),
        'conversation_elapsed_seconds': gap(first),
        'since_previous_user_seconds': gap(previous),
        'since_last_user_anywhere_seconds': gap(last_global),
        'previous_user_messages': len(user_times),
        'stored_messages': sum(x.get('kind') == 'message' for x in conversation.get('items', [])),
    }


def night_state(clock):
    return clock.hour >= 19 or clock.hour < 7


def clock_instructions(profile, conversation, at=None, zone='local', last_global_user_at=None):
    cfg = profile['time']
    if not cfg['enabled']:
        return '', None, False
    at = at or utc_now()
    clock = local_clock(at, zone)
    info = session_time_snapshot(conversation, at=at, zone=zone,
                                 last_global_user_at=last_global_user_at)
    night = night_state(clock)
    pieces = [
        'These values were calculated by the LOCAL application, not inferred by the model.',
        f'Current date/time: {clock.strftime("%Y-%m-%d %H:%M:%S")} ({info["current_local"]}; UTC {info["current_utc"]}).',
        f'Conversation first USER message: {info["first_user_local"] or "not yet recorded"}.',
        'Elapsed since the first user message in THIS conversation: '
        + elapsed_text(info['conversation_elapsed_seconds']) + '.',
        f'Previous USER messages: {info["previous_user_messages"]}; '
        f'prior saved chat messages: {info["stored_messages"]}.',
    ]
    gap = info['since_previous_user_seconds']
    if cfg['gap_tracking']:
        pieces.append(f'Elapsed since previous USER message in this conversation: {elapsed_text(gap)}.')
        if info['since_last_user_anywhere_seconds'] is not None:
            pieces.append('Elapsed since last USER message anywhere across the local archive: '
                          + elapsed_text(info['since_last_user_anywhere_seconds']) + '.')
        if gap is None or cfg['gap_mode'] == 'silent' or gap < cfg['minimum_gap_minutes'] * 60:
            pieces.append('Do not comment on elapsed time unless the user asks.')
        elif cfg['gap_mode'] == 'natural':
            pieces.append('Mention elapsed time only if relevant. Do not repeatedly greet the user.')
        else:
            pieces.append('You may naturally acknowledge the gap, without implying you were active meanwhile.')
    pieces.append('If asked for session duration, answer using the exact application-provided elapsed seconds. '
                  'Do not claim inability to access time/history when provided in this request. '
                  'Never claim to have perceived or performed activity while the user was away.')
    return '\n'.join(pieces), gap, night


def retain_fictional(item_id, *, seed, night, is_recent=False, age_days=0):
    """Deterministic fictional recall filter; archive and context selections stay intact."""
    if is_recent:
        return True
    digest = hashlib.sha256(f'{seed}:{item_id}:{int(night)}'.encode()).digest()
    fraction = int.from_bytes(digest[:8], 'big') / 2**64
    age_days = max(0, min(float(age_days), 36500))
    threshold = (0.35 if night else 0.75) / (1.0 + age_days / 30.0)
    return fraction < threshold
