#!/usr/bin/env python3
"""mail: an email reader for orrery.

Reads what arrived in a mailbox since its cursor, decides with rules what
each message says about the world, and sends the facts to orrery with a
scoped key. The message itself never leaves this program: the ship gets a
source pointer (kind "mail", id the Message-ID) and the facts.

    python3 reader.py --config config.json --dry-run          # print, send nothing
    python3 reader.py --config config.json                    # read, send, advance the cursor
    python3 reader.py --dry-run --eml fixtures/shipped.eml    # run the rules on a file

Standard library only. See README.md for the mapping table and the scope.
"""
import argparse
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SOURCE = 'mail'
MAX_BODIES = 50
MAX_OBS = 200
TEXT_LIMIT = 20000


# ==  the ship

class Ship:
    """orrery's HTTP API through a key. Every call answers (status, json)."""

    def __init__(self, url, token):
        self.api = url.rstrip('/') + '/apps/orrery/api'
        self.token = token

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header('Authorization', 'Bearer ' + self.token)
        if data is not None:
            req.add_header('content-type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors='replace')

    def resolve(self, q):
        code, d = self.call('GET', '/resolve?' + urllib.parse.urlencode({'q': q}))
        return d if code == 200 and isinstance(d, list) else []

    def body(self, bid):
        code, d = self.call('GET', '/body/' + bid)
        return d if code == 200 and isinstance(d, dict) else None

    def observe(self, bodies, observations):
        return self.call('POST', '/observe', {'bodies': bodies, 'observations': observations})

    def act(self, action):
        return self.call('POST', '/act', action)


class NoShip:
    """A dry run never contacts the ship: reads answer nothing, writes print."""

    def resolve(self, q):
        return []

    def body(self, bid):
        return None

    def observe(self, bodies, observations):
        print(json.dumps({'observe': {'bodies': bodies, 'observations': observations}}, indent=1))
        return 200, None

    def act(self, action):
        print(json.dumps({'act': action}, indent=1))
        return 200, None


# ==  messages

class Msg:
    def __init__(self, mid, date, from_name, from_addr, subject, text, bulk, calendar):
        self.id = mid
        self.date = date
        self.from_name = from_name
        self.from_addr = from_addr
        self.subject = subject
        self.text = text
        self.bulk = bulk
        self.calendar = calendar

    @property
    def haystack(self):
        return self.subject + '\n' + self.text


def parse(raw):
    """Parse raw RFC 5322 bytes into a Msg. Attachments are ignored."""
    m = email.message_from_bytes(raw, policy=email.policy.default)
    mid = (m.get('Message-ID') or '').strip()
    if not mid:
        mid = '<' + hashlib.sha256(raw).hexdigest()[:32] + '@no-message-id>'
    date = None
    try:
        date = email.utils.parsedate_to_datetime(m.get('Date', ''))
    except (TypeError, ValueError):
        date = None
    if date is None:
        date = datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    from_name, from_addr = email.utils.parseaddr(m.get('From', ''))
    subject = str(m.get('Subject', '')).strip()
    text = ''
    calendar = False
    for part in m.walk():
        if part.get_content_type() == 'text/calendar':
            calendar = True
    body = m.get_body(preferencelist=('plain', 'html'))
    if body is not None:
        try:
            text = body.get_content()
        except (KeyError, LookupError, UnicodeDecodeError):
            text = ''
        if body.get_content_type() == 'text/html':
            text = html.unescape(re.sub(r'<[^>]+>', ' ', text))
    text = re.sub(r'[ \t]+', ' ', text)[:TEXT_LIMIT]
    bulk = bool(m.get('List-Unsubscribe')) or str(m.get('Precedence', '')).lower() in ('bulk', 'list')
    return Msg(mid, date.astimezone(timezone.utc), from_name.strip(), from_addr.strip().lower(),
               subject, text, bulk, calendar)


# ==  small helpers

def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def slug(text, limit=64):
    s = re.sub(r'[^a-z0-9-]+', '-', text.lower()).strip('-')
    s = re.sub(r'-{2,}', '-', s)
    return s[:limit].strip('-') or 'x'


def short(text, limit=200):
    return text if len(text.encode()) <= limit else text.encode()[:limit].decode(errors='ignore')


MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
DATE_RE = re.compile(
    r'(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|'
    r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s+(?P<year>\d{4}))?'
    r'|(?P<iso>\d{4}-\d{2}-\d{2})'
    r'|(?P<num>\d{1,2}/\d{1,2}/\d{2,4})', re.I)


def find_date(text, base):
    """The first date in text as a UTC midnight datetime, or None. A date with
    no year takes the message's year, or the next one when it would fall more
    than a month before the message."""
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        if m.group('iso'):
            d = datetime.strptime(m.group('iso'), '%Y-%m-%d')
        elif m.group('num'):
            mo, da, yr = m.group('num').split('/')
            yr = int(yr)
            yr = yr + 2000 if yr < 100 else yr
            d = datetime(yr, int(mo), int(da))
        else:
            mon = MONTHS[m.group('mon')[:3].lower()]
            yr = int(m.group('year')) if m.group('year') else base.year
            d = datetime(yr, mon, int(m.group('day')))
            if not m.group('year') and d.replace(tzinfo=timezone.utc) < base - timedelta(days=31):
                d = datetime(yr + 1, mon, int(m.group('day')))
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc)


def window(text, pattern, span=80):
    """The text that follows the first match of pattern, at most span chars."""
    m = re.search(pattern, text, re.I)
    return text[m.end():m.end() + span] if m else ''


AMOUNT_RE = re.compile(r'(?:\$|€|£|USD|EUR|GBP)\s?\d[\d,]*(?:\.\d{2})?')


def source(msg):
    return {'kind': SOURCE, 'id': msg.id}


def obs(msg, subject, attr, value, at=None, until=None, conf=100):
    o = {'subject': subject, 'attr': attr, 'value': value, 'at': iso(at or msg.date),
         'conf': conf, 'source': source(msg)}
    if until is not None:
        o['until'] = iso(until)
    return o


class Facts:
    def __init__(self):
        self.bodies = []
        self.observations = []
        self.actions = []
        self.notes = []

    def body(self, bid, name, aliases=()):
        if not any(b['id'] == bid for b in self.bodies):
            b = {'id': bid, 'name': short(name)}
            if aliases:
                b['aliases'] = list(aliases)
            self.bodies.append(b)

    def empty(self):
        return not (self.bodies or self.observations or self.actions)

    def as_json(self):
        return {'bodies': self.bodies, 'observations': self.observations, 'actions': self.actions}


# ==  the rules
#
#  Each rule reads a Msg and adds to Facts. A rule that returns True claims
#  the message: the rules after it do not run. The order below is the
#  mapping table in README.md.

def skip_calendar(msg, facts, ship):
    if msg.calendar or re.match(r'(updated )?invitation:', msg.subject, re.I):
        facts.notes.append('calendar invitation: the calendar integration owns it')
        return True
    return False


SHIP_RE = re.compile(r'\b(has (?:been )?shipped|is on its way|on the way to you|out for delivery|'
                     r'(?:has been|was) delivered)\b', re.I)
ORDER_RE = re.compile(r'\border\s*(?:#|number|no\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})', re.I)


def shipping(msg, facts, ship):
    m = SHIP_RE.search(msg.haystack)
    if not m:
        return False
    phrase = m.group(1).lower()
    order = ORDER_RE.search(msg.haystack)
    number = order.group(1) if order else None
    bid = 'thing/order-' + (slug(number) if number else hashlib.sha256(msg.id.encode()).hexdigest()[:8])
    facts.body(bid, 'Order ' + number if number else 'An order', ['order ' + number] if number else ())
    if 'delivered' in phrase:
        facts.observations.append(obs(msg, bid, 'status', 'delivered', conf=90))
        return True
    status = 'out for delivery' if 'out for delivery' in phrase else 'shipped'
    arrival = find_date(window(msg.haystack, r'\b(arriv\w*|deliver\w* (?:by|on)|expected|estimated)\b'), msg.date)
    until = arrival + timedelta(days=1) if arrival else None
    facts.observations.append(obs(msg, bid, 'status', status, until=until, conf=90))
    facts.observations.append(obs(msg, bid, 'location', 'in transit', until=until, conf=90))
    return True


BILL_RE = re.compile(r'\b(invoice|bill|payment due|amount due|balance due)\b', re.I)


def invoice(msg, facts, ship):
    if not BILL_RE.search(msg.haystack):
        return False
    amount = AMOUNT_RE.search(msg.haystack)
    if not amount:
        return False
    org = msg.from_name or msg.from_addr.split('@')[-1].split('.')[0]
    bid = 'org/' + slug(org)
    facts.body(bid, org)
    due = find_date(window(msg.haystack, r'\bdue\b(?:\s+(?:on|by|date))?[:\s]*', 60), msg.date)
    action = {'kind': 'task', 'title': short('Pay ' + org + ' ' + amount.group(0).replace(' ', '')),
              'about': [bid]}
    if due:
        action['due'] = iso(due)
    facts.actions.append(action)
    return True


TRIP_RE = re.compile(r'\b(itinerary|booking confirm\w*|reservation (?:is )?confirmed|your flight|'
                     r'your hotel|your stay|check-in)\b', re.I)


def trip(msg, facts, ship):
    if not TRIP_RE.search(msg.haystack):
        return False
    start = find_date(msg.haystack, msg.date)
    if not start:
        return False
    day = start.strftime('%Y-%m-%d')
    bid = 'situation/' + day + '-trip'
    facts.body(bid, 'Trip starting ' + day)
    facts.observations.append(obs(msg, bid, 'status', 'open', conf=80))
    facts.observations.append(obs(msg, bid, 'participants', {'ref': 'person/me'}, conf=80))
    facts.observations.append(obs(msg, bid, 'started', day, conf=80))
    return True


def skip_bulk(msg, facts, ship):
    if msg.bulk:
        facts.notes.append('bulk mail: nothing to say about the world')
        return True
    return False


def known_person(msg, facts, ship):
    """A person the ship knows, writing from an address it does not have."""
    if not msg.from_name or not msg.from_addr:
        return False
    hits = [h for h in ship.resolve(msg.from_name)
            if isinstance(h, dict) and h.get('kind') == 'person'
            and str(h.get('name', '')).lower() == msg.from_name.lower()]
    if len(hits) != 1:
        if isinstance(ship, NoShip):
            facts.notes.append('known-person rule needs the ship: skipped in a dry run')
        return False
    bid = hits[0]['id']
    view = ship.body(bid) or {}
    have = (view.get('attrs') or {}).get('email') or {}
    if str(have.get('value', '')).lower() == msg.from_addr:
        return False
    facts.observations.append(obs(msg, bid, 'email', msg.from_addr, conf=80))
    return False


def classify_with_model(msg, facts, ship):
    """Where a local model plugs in. It may add bodies, observations and
    actions to facts the same way the rules do. Money and health facts go to
    the attributes income and health and nowhere else, so the owner's
    sensitive list keeps them from every key."""
    return False


RULES = [skip_calendar, shipping, invoice, trip, skip_bulk, known_person, classify_with_model]


def classify(msg, ship):
    facts = Facts()
    for rule in RULES:
        if rule(msg, facts, ship):
            break
    return facts


# ==  the mailbox and the cursor

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


def fetch_new(cfg, state, limit):
    """Yield (uid, raw) for messages after the cursor, oldest first, and the
    new cursor state. Messages are read with BODY.PEEK so nothing is marked
    seen."""
    im = cfg['imap']
    password = os.environ.get(im.get('password_env', 'MAIL_PASSWORD'), '')
    conn = imaplib.IMAP4_SSL(im['host'], int(im.get('port', 993)))
    conn.login(im['user'], password)
    folder = im.get('folder', 'INBOX')
    ok, data = conn.select(folder, readonly=True)
    if ok != 'OK':
        raise SystemExit('cannot open folder ' + folder)
    ok, validity = conn.response('UIDVALIDITY')
    validity = int(validity[0]) if validity and validity[0] else 0
    key = folder
    cur = state.get(key, {})
    last = int(cur.get('last_uid', 0)) if cur.get('uidvalidity') == validity else 0
    ok, data = conn.uid('search', None, 'UID %d:*' % (last + 1))
    uids = [int(u) for u in (data[0].split() if data and data[0] else []) if int(u) > last]
    uids.sort()
    out = []
    for uid in uids[:limit]:
        ok, parts = conn.uid('fetch', str(uid), '(BODY.PEEK[])')
        raw = None
        for p in parts:
            if isinstance(p, tuple) and len(p) > 1:
                raw = p[1]
        if raw:
            out.append((uid, raw))
    conn.logout()
    return out, key, validity


# ==  sending

def send(ship, facts):
    """Send one message's facts. Answers True when every item landed."""
    ok = True
    bodies, observations = list(facts.bodies), list(facts.observations)
    while bodies or observations:
        bs, observations_now = bodies[:MAX_BODIES], observations[:MAX_OBS]
        bodies, observations = bodies[MAX_BODIES:], observations[MAX_OBS:]
        code, d = ship.observe(bs, observations_now)
        if code != 200:
            print('observe refused', code, d, file=sys.stderr)
            ok = False
            continue
        for key in ('bodies', 'observations'):
            for r in (d or {}).get(key, []):
                if not r.get('ok'):
                    print(key, 'item refused:', r.get('error'), file=sys.stderr)
                    ok = False
    for a in facts.actions:
        code, d = ship.act(a)
        if code != 200:
            print('act refused', code, d, file=sys.stderr)
            ok = False
    return ok


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--dry-run', action='store_true', help='print the batches, send nothing, keep the cursor')
    ap.add_argument('--eml', nargs='*', help='run the rules on these files instead of the mailbox')
    ap.add_argument('--limit', type=int, default=200, help='messages per run')
    args = ap.parse_args(argv)

    cfg = {}
    if not args.eml or not args.dry_run:
        with open(args.config) as f:
            cfg = json.load(f)
    if args.dry_run:
        ship = NoShip()
    else:
        token = os.environ.get(cfg['orrery'].get('token_env', 'ORRERY_TOKEN'), '')
        if not token:
            raise SystemExit('no token: set ' + cfg['orrery'].get('token_env', 'ORRERY_TOKEN'))
        ship = Ship(cfg['orrery']['url'], token)

    if args.eml:
        for path in args.eml:
            with open(path, 'rb') as f:
                msg = parse(f.read())
            facts = classify(msg, ship)
            print('#', path, msg.id, '|', ' ; '.join(facts.notes) or ('nothing' if facts.empty() else 'facts'))
            if not facts.empty():
                send(ship, facts)
        return 0

    state_path = cfg.get('state', 'state.json')
    state = load_state(state_path)
    msgs, key, validity = fetch_new(cfg, state, args.limit)
    print('%d new message(s) in %s' % (len(msgs), key))
    for uid, raw in msgs:
        msg = parse(raw)
        facts = classify(msg, ship)
        print('#', uid, msg.id, '|', ' ; '.join(facts.notes) or ('nothing' if facts.empty() else 'facts'))
        if not facts.empty() and not send(ship, facts):
            print('stopping before uid %d so the next run retries it' % uid, file=sys.stderr)
            break
        if not args.dry_run:
            state[key] = {'uidvalidity': validity, 'last_uid': uid}
            save_state(state_path, state)
    return 0


if __name__ == '__main__':
    sys.exit(run())
