#!/usr/bin/env python3
"""home-assistant: a Home Assistant client for orrery.

Both directions. It reads the entities the config maps, turns their state
changes into observations, and sends them to orrery with a scoped key. It
also executes approved actions of kind "home" by calling Home Assistant
services, within the allowlist in the config, and reports done or failed.

    python3 client.py --config config.json --dry-run                 # print, send nothing
    python3 client.py --config config.json                           # one pass: read, send, execute
    python3 client.py --config config.json --loop 60                 # every minute
    python3 client.py --dry-run --config fixtures/mapping.json --states fixtures/states.json

Standard library only. See README.md for the mapping and the scope.
"""
import argparse
import fnmatch
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

SOURCE = 'home-assistant'
MAX_BODIES = 50
MAX_OBS = 200
DEAD = ('unavailable', 'unknown', '', None)


# ==  http

def request(url, token, method='GET', body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'Bearer ' + token)
    if data is not None:
        req.add_header('content-type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except ValueError:
                return resp.status, raw.decode(errors='replace')
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors='replace')
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e)


class Ship:
    """orrery's HTTP API through a key."""

    def __init__(self, url, token):
        self.api = url.rstrip('/') + '/apps/orrery/api'
        self.token = token

    def observe(self, bodies, observations):
        return request(self.api + '/observe', self.token, 'POST', {'bodies': bodies, 'observations': observations})

    def actions(self, status):
        code, d = request(self.api + '/actions?status=' + status, self.token)
        return d if code == 200 and isinstance(d, list) else []

    def move(self, aid, status, note=''):
        body = {'status': status}
        if note:
            body['note'] = note[:500]
        return request(self.api + '/actions/' + aid, self.token, 'POST', body)


class NoShip:
    """A dry run: writes print, reads answer nothing."""

    def observe(self, bodies, observations):
        print(json.dumps({'observe': {'bodies': bodies, 'observations': observations}}, indent=1))
        return 200, None

    def actions(self, status):
        return []

    def move(self, aid, status, note=''):
        print(json.dumps({'move': {'id': aid, 'status': status, 'note': note}}))
        return 200, None


class Hass:
    """Home Assistant's REST API with a long-lived access token."""

    def __init__(self, url, token):
        self.api = url.rstrip('/') + '/api'
        self.token = token

    def states(self):
        code, d = request(self.api + '/states', self.token)
        if code != 200 or not isinstance(d, list):
            raise SystemExit('cannot read states: %s %s' % (code, str(d)[:200]))
        return d

    def call(self, domain, service, data):
        code, d = request('%s/services/%s/%s' % (self.api, domain, service), self.token, 'POST', data)
        return code == 200, '' if code == 200 else 'home assistant answered %s: %s' % (code, str(d)[:300])


class NoHass:
    """A dry run never calls a service."""

    def call(self, domain, service, data):
        print(json.dumps({'call': {'service': domain + '.' + service, 'data': data}}))
        return True, ''


# ==  helpers

def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(s):
    """Home Assistant's ISO 8601 with an offset, as a UTC datetime."""
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def slug(text, limit=64):
    s = re.sub(r'[^a-z0-9-]+', '-', str(text).lower()).strip('-')
    return re.sub(r'-{2,}', '-', s)[:limit].strip('-') or 'x'


def short(text, limit=200):
    text = str(text)
    return text if len(text.encode()) <= limit else text.encode()[:limit].decode(errors='ignore')


class Facts:
    def __init__(self):
        self.bodies = []
        self.observations = []
        self.notes = []

    def body(self, bid, name):
        if not any(b['id'] == bid for b in self.bodies):
            self.bodies.append({'id': bid, 'name': short(name)})

    def obs(self, entity, changed, subject, attr, value, at, until=None, conf=90):
        o = {'subject': subject, 'attr': attr, 'value': value, 'at': iso(at), 'conf': conf,
             'source': {'kind': SOURCE, 'id': entity + '@' + changed}}
        if until is not None:
            o['until'] = iso(until)
        self.observations.append(o)

    def empty(self):
        return not (self.bodies or self.observations)

    def as_json(self):
        return {'bodies': self.bodies, 'observations': self.observations}


# ==  the mapping
#
#  Each entry in the config's "map" names one entity and one of three kinds:
#    presence  a device_tracker or person: home, away, or a zone -> <body>.location
#    attr      any entity: its state -> <body>.<attr>, with optional value translation and expiry
#    alarm     a binary sensor: on opens a situation, off closes it

def map_states(cfg, states, state):
    """Facts for every mapped entity whose last_changed moved since the cursor,
    and the cursor to save once they land."""
    facts = Facts()
    seen = dict(state.get('seen', {}))
    alarms = dict(state.get('alarms', {}))
    home = cfg.get('home', 'place/home')
    by_id = {s.get('entity_id'): s for s in states if isinstance(s, dict)}
    for entry in cfg.get('map', []):
        entity = entry.get('entity', '')
        s = by_id.get(entity)
        if not s:
            facts.notes.append(entity + ': not in home assistant')
            continue
        changed = str(s.get('last_changed', ''))
        if seen.get(entity) == changed:
            continue
        seen[entity] = changed
        value = s.get('state')
        attrs = s.get('attributes') or {}
        if value in DEAD:
            facts.notes.append(entity + ': ' + str(value) + ', nothing written')
            continue
        at = parse_time(changed)
        kind = entry.get('kind', 'attr')
        name = entry.get('name') or attrs.get('friendly_name') or entity
        if kind == 'presence':
            body = entry.get('body', 'person/me')
            if value == 'home':
                facts.obs(entity, changed, body, 'location', {'ref': home}, at)
            elif value == 'not_home':
                facts.obs(entity, changed, body, 'location', None, at)
            else:
                facts.obs(entity, changed, body, 'location', str(value), at)
        elif kind == 'alarm':
            if value == 'on':
                sid = 'situation/' + at.strftime('%Y-%m-%d') + '-' + slug(name)
                alarms[entity] = sid
                facts.body(sid, name)
                facts.obs(entity, changed, sid, 'status', 'open', at)
                facts.obs(entity, changed, sid, 'started', iso(at), at)
                facts.obs(entity, changed, sid, 'participants', {'ref': 'person/me'}, at)
                if entry.get('location'):
                    facts.obs(entity, changed, sid, 'location', entry['location'], at)
            elif value == 'off' and entity in alarms:
                sid = alarms.pop(entity)
                facts.body(sid, name)
                facts.obs(entity, changed, sid, 'status', 'closed', at)
                facts.obs(entity, changed, sid, 'ended', iso(at), at)
        else:
            body = entry.get('body')
            attr = entry.get('attr')
            if not body or not attr:
                facts.notes.append(entity + ': an attr entry needs body and attr')
                continue
            if value in entry.get('ignore', []):
                continue
            out = entry.get('values', {}).get(str(value), value)
            if entry.get('numeric'):
                try:
                    out = float(value)
                    out = int(out) if out == int(out) else out
                except (TypeError, ValueError):
                    facts.notes.append(entity + ': not a number: ' + str(value))
                    continue
            until = None
            minutes = attrs.get(entry.get('until_minutes_attr', ''))
            try:
                if minutes is not None and float(minutes) > 0:
                    until = at + timedelta(minutes=float(minutes))
            except (TypeError, ValueError):
                pass
            facts.body(body, name)
            facts.obs(entity, changed, body, attr, out, at, until)
    return facts, {'seen': seen, 'alarms': alarms}


# ==  the executor

SERVICE_RE = re.compile(r'^[a-z_]+\.[a-z_]+$')
ENTITY_RE = re.compile(r'^[a-z_]+\.[a-z0-9_]+$')


def check_payload(payload, allow):
    """Why this home action may not run, or None."""
    if not isinstance(payload, dict):
        return 'payload must be an object with service and entity_id'
    service = str(payload.get('service', ''))
    entity = str(payload.get('entity_id', ''))
    if not SERVICE_RE.match(service):
        return 'service must look like domain.service'
    if not ENTITY_RE.match(entity):
        return 'entity_id must look like domain.object'
    if not any(fnmatch.fnmatch(service + ' ' + entity, pat) for pat in allow):
        return 'not allowed: add "%s %s" to allow in config.json' % (service, entity)
    data = payload.get('data', {})
    if data is not None and not isinstance(data, dict):
        return 'data must be an object'
    return None


def execute(cfg, ship, hass, state):
    """Run every approved home action once and report on each."""
    done = list(state.get('executed', []))
    for a in ship.actions('approved'):
        if not isinstance(a, dict) or a.get('kind') != 'home' or a.get('id') in done:
            continue
        aid = str(a.get('id'))
        payload = a.get('payload')
        why = check_payload(payload, cfg.get('allow', []))
        if why:
            ship.move(aid, 'failed', why)
            done.append(aid)
            continue
        domain, service = payload['service'].split('.', 1)
        data = dict(payload.get('data') or {})
        data['entity_id'] = payload['entity_id']
        ok, err = hass.call(domain, service, data)
        ship.move(aid, 'done' if ok else 'failed', err)
        done.append(aid)
    state['executed'] = done[-500:]


# ==  sending and the cursor

def send(ship, facts):
    ok = True
    bodies, observations = list(facts.bodies), list(facts.observations)
    while bodies or observations:
        bs, os_ = bodies[:MAX_BODIES], observations[:MAX_OBS]
        bodies, observations = bodies[MAX_BODIES:], observations[MAX_OBS:]
        code, d = ship.observe(bs, os_)
        if code != 200:
            print('observe refused', code, d, file=sys.stderr)
            ok = False
            continue
        for key in ('bodies', 'observations'):
            for r in (d or {}).get(key, []):
                if not r.get('ok'):
                    print(key, 'item refused:', r.get('error'), file=sys.stderr)
    return ok


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def one_pass(cfg, ship, hass, states, state_path, dry):
    state = load_state(state_path)
    facts, cursor = map_states(cfg, states, state)
    for n in facts.notes:
        print('#', n)
    if facts.empty():
        print('# nothing changed')
    elif not send(ship, facts):
        print('# a batch was refused whole: the cursor stays', file=sys.stderr)
        return
    state.update(cursor)
    execute(cfg, ship, hass, state)
    if not dry:
        save_state(state_path, state)


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--dry-run', action='store_true', help='print batches and calls, send nothing, keep the cursor')
    ap.add_argument('--states', help='read entity states from this file instead of home assistant')
    ap.add_argument('--loop', type=int, default=0, help='repeat every N seconds')
    args = ap.parse_args(argv)
    with open(args.config) as f:
        cfg = json.load(f)
    if args.dry_run:
        ship, hass = NoShip(), NoHass()
    else:
        token = os.environ.get(cfg['orrery'].get('token_env', 'ORRERY_TOKEN'), '')
        htok = os.environ.get(cfg['home_assistant'].get('token_env', 'HASS_TOKEN'), '')
        if not token or not htok:
            raise SystemExit('set both tokens in the environment (see config.example.json)')
        ship, hass = Ship(cfg['orrery']['url'], token), Hass(cfg['home_assistant']['url'], htok)
    reader = None
    if not args.states:
        htok = os.environ.get(cfg.get('home_assistant', {}).get('token_env', 'HASS_TOKEN'), '')
        reader = Hass(cfg['home_assistant']['url'], htok)
    state_path = cfg.get('state', 'state.json')
    while True:
        if args.states:
            with open(args.states) as f:
                states = json.load(f)
        else:
            states = reader.states()
        one_pass(cfg, ship, hass, states, state_path, args.dry_run)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == '__main__':
    sys.exit(run())
