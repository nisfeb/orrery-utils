#!/usr/bin/env python3
"""generator: a frontier model reads the state and proposes actions.

Reads the state view and the recent decisions with a scoped key, builds one
prompt, asks the model for actions, validates every proposal against the
schema (kinds, payload shapes, bodies named), drops what was already done or
dismissed, and files the rest with POST /act. The owner approves or
dismisses on the page; the executors do the rest.

    python3 run.py --config config.json --dry-run       # print the prompt's summary and the proposals, file nothing
    python3 run.py --config config.json                 # one pass
    python3 run.py --config config.json --loop 3600     # every hour

The model is any OpenAI-compatible chat endpoint (provider "openai", LM
Studio or a hosted API) or Anthropic's Messages API (provider "anthropic",
key in the environment). Standard library only. The prompt is
../common/generator-prompt.md.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import analyze  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common', 'generator-prompt.md')
RECENT = 60          # decided actions shown to the model as "not again"
MAX_BODIES = 300


# ==  the ship

class Ship:
    def __init__(self, url, token):
        self.api = url.rstrip('/') + '/apps/orrery/api'
        self.h = {'Authorization': 'Bearer ' + token}

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method=method)
        for k, v in self.h.items():
            req.add_header(k, v)
        if data is not None:
            req.add_header('content-type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors='replace')
        except (urllib.error.URLError, OSError) as e:
            return 0, str(e)

    def state(self):
        code, d = self.call('GET', '/state')
        if code != 200 or not isinstance(d, dict):
            raise SystemExit('cannot read the state: %s %s' % (code, str(d)[:200]))
        return d

    def actions(self, status):
        code, d = self.call('GET', '/actions?status=' + status)
        return d if code == 200 and isinstance(d, list) else []

    def act(self, action):
        return self.call('POST', '/act', action)


class NoShip(Ship):
    """A dry run reads the ship and files nothing."""

    def act(self, action):
        print(json.dumps({'act': action}, indent=1))
        return 200, {'id': 'dry', 'status': 'proposed', 'existing': False}


# ==  the models

class Anthropic:
    def __init__(self, name, key, timeout=180, max_tokens=2000):
        self.name, self.key, self.timeout, self.max_tokens = name, key, timeout, max_tokens

    def chat(self, system, user):
        body = {'model': self.name, 'max_tokens': self.max_tokens, 'system': system,
                'messages': [{'role': 'user', 'content': user}]}
        req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps(body).encode(), method='POST')
        req.add_header('x-api-key', self.key)
        req.add_header('anthropic-version', '2023-06-01')
        req.add_header('content-type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                d = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError('anthropic answered %s: %s' % (e.code, e.read().decode(errors='replace')[:300]))
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError('anthropic unreachable: %s' % e)
        parts = [c.get('text', '') for c in d.get('content', []) if isinstance(c, dict) and c.get('type') == 'text']
        if not parts:
            raise RuntimeError('anthropic answered without text: ' + json.dumps(d)[:300])
        return ''.join(parts)


def model_from(cfg):
    m = cfg.get('model') or {}
    provider = m.get('provider', 'openai')
    if provider == 'anthropic':
        key = os.environ.get(m.get('key_env', 'ANTHROPIC_API_KEY'), '')
        if not key:
            raise SystemExit('set ' + m.get('key_env', 'ANTHROPIC_API_KEY'))
        return Anthropic(m.get('name', 'claude-sonnet-5'), key, int(m.get('timeout', 180)))
    return analyze.Model(m.get('url', analyze.DEFAULT_URL), m.get('name'), int(m.get('timeout', 180)))


# ==  the prompt

def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def val(attrs, name):
    v = (attrs or {}).get(name)
    if isinstance(v, dict):
        return v.get('value')
    if isinstance(v, list):
        return [x.get('value') for x in v if isinstance(x, dict)]
    return None


def phase(b, now):
    a = b.get('attrs') or {}
    st = val(a, 'status')
    if st in ('closed', 'cancelled'):
        return st
    end = val(a, 'ended') or val(a, 'ends')
    start = val(a, 'started') or val(a, 'starts')
    if isinstance(end, str) and end <= now:
        return 'over'
    if isinstance(start, str) and start <= now:
        return 'under way'
    if isinstance(start, str):
        return 'upcoming'
    return st or 'open'


def line(b, now):
    a = b.get('attrs') or {}
    bits = []
    for k in sorted(a):
        v = val(a, k)
        if v is None:
            continue
        if isinstance(v, dict) and 'ref' in v:
            v = v['ref']
        if isinstance(v, list):
            v = ', '.join(x['ref'] if isinstance(x, dict) and 'ref' in x else str(x) for x in v)
        bits.append('%s=%s' % (k, ' '.join(str(v).split())[:120]))
    head = '%s | %s' % (b['id'], b.get('name', ''))
    if b['id'].startswith('situation/'):
        head += ' | ' + phase(b, now)
    return head + (' | ' + '; '.join(bits) if bits else '')


def build(state, decided, now, tz, limit):
    lines = ['Now: %s. The owner is %s, timezone %s. Propose at most %d actions.' % (now, state.get('me', 'person/me'), tz or 'unknown', limit)]
    schema = state.get('schema') or {}
    lines.append('Action kinds: ' + ', '.join(schema.get('actions') or ['task', 'note']))
    payloads = schema.get('payloads') or {}
    if payloads:
        lines.append('Payload shapes:')
        for k, shape in payloads.items():
            lines.append('  %s: %s' % (k, json.dumps(shape)))
    bodies = [b for b in state.get('bodies', []) if isinstance(b, dict)][:MAX_BODIES]
    hidden = {b['id'] for b in bodies if b['id'].startswith('situation/') and phase(b, now) in ('closed', 'cancelled', 'over')}
    for kind in ('situation', 'activity', 'person', 'thing', 'place', 'org', 'note'):
        rows = [b for b in bodies if b.get('kind') == kind and b['id'] not in hidden]
        if rows:
            lines.append(('activities' if kind == 'activity' else kind + 's') + ':')
            for b in rows:
                lines.append('  ' + line(b, now))
    lines.append('Open actions (proposed or approved, do not duplicate):')
    for a in state.get('actions') or []:
        lines.append('  %s | %s | about %s' % (a.get('kind'), a.get('title'), ', '.join(a.get('about') or [])))
    lines.append('Recent decisions (do not propose these again):')
    for a in decided[-RECENT:]:
        lines.append('  %s | %s | %s' % (a.get('status'), a.get('kind'), a.get('title')))
    lines.append('Answer with the JSON object.')
    return '\n'.join(lines)


# ==  the answer

def norm(title):
    return re.sub(r'[^a-z0-9 ]+', ' ', str(title or '').lower()).split()


def same_title(a, b):
    ka, kb = set(norm(a)), set(norm(b))
    if not ka or not kb:
        return False
    return ka == kb or (len(ka & kb) >= max(2, int(0.8 * min(len(ka), len(kb)))))


def validate(answer, state, decided, limit):
    notes = []
    schema = state.get('schema') or {}
    kinds = set(schema.get('actions') or ['task', 'note'])
    payloads = schema.get('payloads') or {}
    known = {b['id'] for b in state.get('bodies', []) if isinstance(b, dict)}
    taken = [a.get('title') for a in (state.get('actions') or [])] + [a.get('title') for a in decided]
    out = []
    for a in (answer.get('actions') or [])[:limit * 2]:
        if not isinstance(a, dict):
            continue
        kind = str(a.get('kind', 'task')).strip().lower()
        title = str(a.get('title', '')).strip()[:200]
        if kind not in kinds or not title:
            notes.append('dropped: kind %s or no title (%s)' % (kind, title[:40]))
            continue
        if any(same_title(title, t) for t in taken):
            notes.append('dropped as already open or decided: ' + title)
            continue
        about = [str(x).strip().lower() for x in (a.get('about') or [])]
        bad = [x for x in about if x not in known]
        if bad:
            notes.append('dropped %s: names bodies that do not exist: %s' % (title, ', '.join(bad)))
            continue
        payload = a.get('payload') if isinstance(a.get('payload'), dict) else {}
        shape = payloads.get(kind)
        if isinstance(shape, dict):
            missing = [k for k, v in shape.items() if isinstance(v, str) and v.startswith('required') and k not in payload]
            if missing:
                notes.append('dropped %s: payload lacks %s' % (title, ', '.join(missing)))
                continue
        row = {'kind': kind, 'title': title, 'about': about[:20], 'payload': payload}
        due = analyze.iso_or_none(a.get('due'))
        if due:
            row['due'] = due
        if a.get('why'):
            row['payload'] = dict(payload, why=str(a['why'])[:300])
        out.append(row)
        taken.append(title)
        if len(out) >= limit:
            break
    for n in (answer.get('notes') or [])[:10]:
        notes.append('model note: ' + str(n)[:200])
    return out, notes


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--dry-run', action='store_true', help='ask the model, print, file nothing')
    ap.add_argument('--loop', type=int, default=0, help='repeat every N seconds')
    ap.add_argument('--show-prompt', action='store_true', help='print the user prompt before asking')
    ap.add_argument('--no-model', action='store_true', help='build and print the prompt, ask nothing, file nothing')
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = json.load(f)
    #  a .env beside the config (git-ignored) supplies the keys without them
    #  ever appearing on a command line or in a shell history
    env_path = os.path.join(os.path.dirname(os.path.abspath(args.config)), '.env')
    if os.path.exists(env_path):
        with open(env_path) as f:
            for row in f:
                k, _, v = row.strip().partition('=')
                if k and v and k not in os.environ:
                    os.environ[k] = v
    token = os.environ.get(cfg['orrery'].get('token_env', 'ORRERY_TOKEN'), '')
    if not token:
        raise SystemExit('set ' + cfg['orrery'].get('token_env', 'ORRERY_TOKEN'))
    ship = (NoShip if args.dry_run or args.no_model else Ship)(cfg['orrery']['url'], token)
    model = None if args.no_model else model_from(cfg)
    limit = int(cfg.get('max_actions', 5))
    with open(PROMPT_PATH, encoding='utf-8') as f:
        system = f.read().strip()
    while True:
        state = ship.state()
        decided = [a for a in ship.actions('all') if a.get('status') in ('done', 'dismissed', 'failed')]
        decided.sort(key=lambda a: str(a.get('proposed', '')))
        me = next((b for b in state.get('bodies', []) if b.get('id') == state.get('me', 'person/me')), {})
        tz = val(me.get('attrs') or {}, 'timezone') or cfg.get('timezone')
        user = build(state, decided, iso_now(), tz, limit)
        if args.show_prompt or args.no_model:
            print(user)
        if args.no_model:
            return 0
        try:
            raw = model.chat(system, user)
            answer = analyze.parse_json(raw)
        except (RuntimeError, ValueError) as e:
            print('model:', str(e)[:300], file=sys.stderr)
            answer = {}
        proposals, notes = validate(answer if isinstance(answer, dict) else {}, state, decided, limit)
        for n in notes:
            print('#', n)
        for p in proposals:
            code, d = ship.act(p)
            print('filed' if code == 200 else 'refused', p['kind'], '|', p['title'], '|', (d or {}).get('status', '') if code == 200 else str(d)[:120])
        print('# %d proposal(s) filed' % len(proposals))
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == '__main__':
    sys.exit(run())
