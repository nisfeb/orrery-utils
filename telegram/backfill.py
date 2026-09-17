#!/usr/bin/env python3
"""telegram/backfill.py: build state from a Telegram Desktop export.

A bot cannot read your past conversations, so the past comes from the
export Telegram Desktop makes (Settings, Advanced, Export Telegram data,
JSON). This reads that file, keeps the messages from the people the config
maps and the window of time you ask for, and hands them to the local model
in runs of consecutive messages per chat, so a reply is read against what
it answers. The facts go to orrery with the same key the bot uses.

    python3 backfill.py --config config.json --export ~/Downloads/Telegram\\ Desktop/DataExport/result.json --months 6 --dry-run
    python3 backfill.py --config config.json --export result.json --months 6
    python3 backfill.py --config config.json --export result.json --since 2026-01-01 --chat Sarah --chat family

Standard library only. The message text goes to the model on your machine
and nowhere else; the ship gets the facts and telegram/<chat id>/<message id>.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import analyze  # noqa: E402
import bot  # noqa: E402

SOURCE = 'chat'
PLATFORM = 'telegram'
WINDOW_MESSAGES = 12
WINDOW_CHARS = 3000

MODEL = None


# ==  the export

def chats_in(export):
    """Every chat in an export, whole-account or single-chat."""
    if isinstance(export, dict) and isinstance(export.get('chats'), dict):
        return [c for c in export['chats'].get('list', []) if isinstance(c, dict)]
    if isinstance(export, dict) and isinstance(export.get('messages'), list):
        return [export]
    return []


def text_of(m):
    """The text of an exported message: a string, or a list of strings and
    typed pieces (links, mentions, bold) that join back into one."""
    t = m.get('text', '')
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return ''.join(p if isinstance(p, str) else str(p.get('text', '')) for p in t)
    return ''


def when(m):
    """The message's time as UTC: date_unixtime when the export has it, else
    the local-time date field read as if it were UTC."""
    try:
        return datetime.fromtimestamp(int(m['date_unixtime']), timezone.utc)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(m.get('date', ''))).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def user_id(m):
    """The numeric sender id from an export's from_id ("user1001"), or ''."""
    f = str(m.get('from_id', ''))
    return f[4:] if f.startswith('user') and f[4:].isdigit() else ''


def messages_of(chat, cfg, since, done_after):
    """The chat's messages worth reading, oldest first: real messages with
    text, from people in the config, at or after since, with ids above the
    backfill's place in this chat."""
    people = cfg.get('people', {})
    out = []
    for m in chat.get('messages', []):
        if not isinstance(m, dict) or m.get('type') != 'message':
            continue
        mid = m.get('id')
        if not isinstance(mid, int) or mid <= done_after:
            continue
        at = when(m)
        if at is None or (since is not None and at < since):
            continue
        who = people.get(user_id(m))
        text = text_of(m).strip()
        if not who or not text:
            continue
        out.append({'mid': mid, 'at': at, 'who': who, 'text': text})
    out.sort(key=lambda x: x['mid'])
    return out


def windows(msgs):
    """Runs of consecutive messages, each at most WINDOW_MESSAGES long and
    about WINDOW_CHARS of text."""
    run, size = [], 0
    for m in msgs:
        if run and (len(run) >= WINDOW_MESSAGES or size + len(m['text']) > WINDOW_CHARS):
            yield run
            run, size = [], 0
        run.append(m)
        size += len(m['text'])
    if run:
        yield run


# ==  the run

def facts_for(window, chat_id, context):
    """The analyst's facts for one window, as bot.Facts, with source pointers."""
    facts = bot.Facts()
    msgs = [{'id': '%s/%s/%s' % (PLATFORM, chat_id, m['mid']), 'at': bot.iso(m['at']), 'who': m['who'], 'text': m['text']}
            for m in window]
    got = analyze.analyze(MODEL, msgs, context)
    facts.notes.extend(got['notes'])
    bodies, observations, actions = analyze.to_batch(got, SOURCE)
    for b in bodies:
        facts.bodies.append(b)
        context['bodies'].append({'id': b['id'], 'name': b['name'], 'aliases': list(b.get('aliases', ()))})
    facts.observations.extend(observations)
    facts.actions.extend(actions)
    return facts


def run(argv=None):
    global MODEL
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--export', required=True, help='result.json from Telegram Desktop, or one chat\'s export')
    ap.add_argument('--since', help='from this day (YYYY-MM-DD) on')
    ap.add_argument('--months', type=int, help='from this many months ago on')
    ap.add_argument('--chat', action='append', help='only chats with this name; repeatable')
    ap.add_argument('--dry-run', action='store_true', help='print the batches, send nothing, keep no place')
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = json.load(f)
    with open(args.export) as f:
        export = json.load(f)
    since = None
    if args.since:
        since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    elif args.months:
        since = (datetime.now(timezone.utc) - timedelta(days=30 * args.months)).replace(hour=0, minute=0, second=0, microsecond=0)
    mc = cfg.get('model') or {}
    if MODEL is None:
        MODEL = analyze.Model(mc.get('url', analyze.DEFAULT_URL), mc.get('name'), int(mc.get('timeout', 180)))
        try:
            print('# model:', MODEL.model_name(), 'at', MODEL.url)
        except RuntimeError as e:
            raise SystemExit(str(e))
    if args.dry_run:
        ship = bot.NoShip()
    else:
        token = os.environ.get(cfg['orrery'].get('token_env', 'ORRERY_TOKEN'), '')
        if not token:
            raise SystemExit('no token: set ' + cfg['orrery'].get('token_env', 'ORRERY_TOKEN'))
        ship = bot.Ship(cfg['orrery']['url'], token)
    state_path = cfg.get('state', 'state.json')
    state = bot.load_state(state_path)
    place = state.setdefault('backfill', {})
    context = analyze.context_from_state(ship.state() if hasattr(ship, 'state') else {}, SOURCE)
    for chat in chats_in(export):
        name = str(chat.get('name') or chat.get('id') or '')
        if args.chat and name not in args.chat:
            continue
        chat_id = str(chat.get('id', ''))
        msgs = messages_of(chat, cfg, since, int(place.get(chat_id, 0)))
        print('# chat %s (%s): %d message(s) to read' % (name, chat_id, len(msgs)))
        for n, window in enumerate(windows(msgs)):
            if n and n % 10 == 0:
                context = analyze.context_from_state(ship.state() if hasattr(ship, 'state') else {}, SOURCE)
            facts = facts_for(window, chat_id, context)
            span = '%s..%s' % (window[0]['at'].strftime('%Y-%m-%d'), window[-1]['at'].strftime('%Y-%m-%d'))
            print('#  ', span, '%d msg' % len(window), '|', ' ; '.join(facts.notes) or ('nothing' if facts.empty() else
                  '%d bodies, %d observations, %d actions' % (len(facts.bodies), len(facts.observations), len(facts.actions))))
            if not facts.empty() and not bot.send(ship, facts):
                print('stopping in chat %s so the next run retries this window' % name, file=sys.stderr)
                return 1
            if not args.dry_run:
                place[chat_id] = window[-1]['mid']
                bot.save_state(state_path, state)
    return 0


if __name__ == '__main__':
    sys.exit(run())
