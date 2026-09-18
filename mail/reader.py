#!/usr/bin/env python3
"""mail: an email reader for orrery.

Reads what arrived in a mailbox since its cursor, decides with rules what
each message says about the world, and sends the facts to orrery with a
scoped key. The message itself never leaves this program: the ship gets a
source pointer (kind "mail", id the Message-ID) and the facts.

    python3 reader.py --config config.json --dry-run          # print, send nothing
    python3 reader.py --config config.json                    # read, send, advance the cursor
    python3 reader.py --config config.json --months 6         # backfill: everything since six months ago
    python3 reader.py --dry-run --eml fixtures/shipped.eml    # run the rules on a file

With a "model" block in the config, messages the rules do not claim go to
a local model (LM Studio's OpenAI-compatible server) through ../common/analyze.py.

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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import analyze  # noqa: E402

SOURCE = 'mail'
MAX_BODIES = 50
MAX_OBS = 200
TEXT_LIMIT = 20000

#  the analyst, set from the config at the start of a run; None means rules only
MODEL = None
CONTEXT = None
CONFIG = {}


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

    def state(self):
        code, d = self.call('GET', '/state')
        return d if code == 200 and isinstance(d, dict) else {}

    def observe(self, bodies, observations):
        return self.call('POST', '/observe', {'bodies': bodies, 'observations': observations})

    def act(self, action):
        return self.call('POST', '/act', action)


class NoShip:
    """A dry run: writes print, reads go to the ship when one is given. Reading
    is harmless and a dry run that cannot read validates against an empty
    vocabulary, so it prints bodies and attributes a real run would drop."""

    def __init__(self, reads=None):
        self.reads = reads

    def resolve(self, q):
        return self.reads.resolve(q) if self.reads else []

    def body(self, bid):
        return self.reads.body(bid) if self.reads else None

    def state(self):
        return self.reads.state() if self.reads else {}

    def observe(self, bodies, observations):
        print(json.dumps({'observe': {'bodies': bodies, 'observations': observations}}, indent=1))
        return 200, None

    def act(self, action):
        print(json.dumps({'act': action}, indent=1))
        return 200, None


def dry_ship(cfg):
    """The ship a dry run talks to. With a key in the environment its reads are
    the real ship's, so what a dry run prints is judged against the same bodies
    and schema a real run sees; without one it says so, because the difference
    is silent otherwise."""
    orrery = cfg.get('orrery') or {}
    token = os.environ.get(orrery.get('token_env', 'ORRERY_TOKEN'), '')
    if orrery.get('url') and token:
        return NoShip(Ship(orrery['url'], token))
    print('# dry run without a key: the ship is not read, so its bodies and schema are unknown '
          'and this prints more than a real run would send', file=sys.stderr)
    return NoShip()


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


def order_number(text):
    """An order number with a digit in it, or None. Prose after the word
    order ("order from", "order your") is not a number."""
    for m in ORDER_RE.finditer(text):
        token = m.group(1)
        if any(c.isdigit() for c in token):
            return token
    return None


def shipping(msg, facts, ship):
    m = SHIP_RE.search(msg.haystack)
    if not m:
        return False
    phrase = m.group(1).lower()
    number = order_number(msg.haystack)
    bid = 'thing/order-' + (slug(number) if number else hashlib.sha256(msg.id.encode()).hexdigest()[:8])
    facts.body(bid, 'Order ' + number if number else 'An order', ['order ' + number] if number else ())
    if 'delivered' in phrase:
        facts.observations.append(obs(msg, bid, 'status', 'delivered', conf=90))
        facts.observations.append(obs(msg, bid, 'location', None, conf=90))
        return True
    status = 'out for delivery' if 'out for delivery' in phrase else 'shipped'
    arrival = find_date(window(msg.haystack, r'\b(arriv\w*|deliver\w* (?:by|on)|expected|estimated)\b'), msg.date)
    until = arrival + timedelta(days=1) if arrival else None
    facts.observations.append(obs(msg, bid, 'status', status, until=until, conf=90))
    facts.observations.append(obs(msg, bid, 'location', 'in transit', until=until, conf=90))
    return True


BILL_RE = re.compile(r'\b(invoice|bill|payment due|amount due|balance due)\b', re.I)
PAID_RE = re.compile(r'\b(thank you for your payment|payment (?:received|confirmation|successful)|receipt|'
                     r'has been paid|you paid|paid on)\b', re.I)
ORG_WORDS = {'inc', 'llc', 'ltd', 'co', 'corp', 'company', 'bank', 'club', 'church', 'school', 'storage',
             'support', 'services', 'service', 'group', 'team', 'billing', 'insurance', 'store', 'shop',
             'market', 'office', 'dept', 'department', 'associates', 'partners', 'clinic', 'center', 'centre'}


def looks_like_person(name):
    """Two or three capitalised words with no company word among them."""
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", name)
    return (2 <= len(words) <= 3 and '@' not in name and all(w[0].isupper() for w in words)
            and not any(w.lower().strip('.') in ORG_WORDS for w in words))


def invoice(msg, facts, ship):
    if not BILL_RE.search(msg.haystack):
        return False
    if PAID_RE.search(msg.haystack) and not re.search(r'\bdue\b', msg.haystack, re.I):
        facts.notes.append('a receipt: nothing to pay')
        return True
    amount = AMOUNT_RE.search(msg.haystack)
    if not amount:
        return False
    payee = msg.from_name or msg.from_addr
    if '@' in payee:
        payee = payee.split('@')[-1].split('.')[0]
    due = find_date(window(msg.haystack, r'\bdue\b(?:\s+(?:on|by|date))?[:\s]*', 60), msg.date)
    action = {'kind': 'task', 'title': short('Pay ' + payee + ' ' + amount.group(0).replace(' ', ''))}
    if looks_like_person(payee):
        #  a person's request: the task is about the person the ship knows, never an org body
        hits = [h for h in ship.resolve(payee) if isinstance(h, dict) and h.get('kind') == 'person']
        if len(hits) == 1:
            action['about'] = [hits[0]['id']]
        else:
            facts.notes.append('payee looks like a person the ship does not know: no body made')
    else:
        bid = 'org/' + slug(payee)
        facts.body(bid, payee)
        action['about'] = [bid]
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


#  the config's "filters": substrings matched case-insensitively against the
#  sender (name and address) and the subject, and an optional allowlist of
#  senders; set from the config at the start of a run
FILTERS = {'from': [], 'subject': [], 'only_from': []}


def skip_filtered(msg, facts, ship):
    """Mail the owner said to ignore: a sender or subject on the skip lists,
    or a sender off the allowlist when there is one. Runs after the
    transactional rules, so a shop's shipping notice still lands even when
    the shop is on the list."""
    sender = (msg.from_name + ' ' + msg.from_addr).lower()
    for s in FILTERS.get('from') or []:
        if s.lower() in sender:
            facts.notes.append('skipped: sender matches ' + s)
            return True
    for s in FILTERS.get('subject') or []:
        if s.lower() in msg.subject.lower():
            facts.notes.append('skipped: subject matches ' + s)
            return True
    only = FILTERS.get('only_from') or []
    if only and not any(s.lower() in sender for s in only):
        facts.notes.append('skipped: sender is not on only_from')
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


CONTEXT_EVERY = 50
CONTEXT_USES = [0]


def context_for(ship):
    """The analyst's view of the ship, read at the start and again every
    CONTEXT_EVERY messages the model sees, so a long backfill learns about
    bodies made meanwhile (a consolidation, another reader), and grown with
    the bodies this run creates in between."""
    global CONTEXT
    CONTEXT_USES[0] += 1
    if CONTEXT is None or CONTEXT_USES[0] % CONTEXT_EVERY == 0:
        CONTEXT = analyze.context_from_state(ship.state(), SOURCE, sensitive_write=bool((CONFIG.get('orrery') or {}).get('sensitive_write')))
    return CONTEXT


def classify_with_model(msg, facts, ship):
    """A message the rules did not claim goes to the local model, which may
    add bodies, observations and actions. The model sees the text; the ship
    gets the facts and the Message-ID. Money and health facts belong to the
    attributes income and health and nowhere else, so the owner's sensitive
    list keeps them from every key."""
    if MODEL is None:
        return False
    ctx = context_for(ship)
    who = (msg.from_name + ' <' + msg.from_addr + '>').strip() if msg.from_addr else msg.from_name
    text = (msg.subject + '\n\n' + msg.text).strip()[:analyze.MAX_TEXT]
    got = analyze.analyze(MODEL, [{'id': msg.id, 'at': iso(msg.date), 'who': who, 'text': text}], ctx)
    facts.notes.extend(got['notes'])
    bodies, observations, actions = analyze.to_batch(got, SOURCE)
    for b in bodies:
        facts.body(b['id'], b['name'], b.get('aliases', ()))
        ctx['bodies'].append({'id': b['id'], 'name': b['name'], 'aliases': list(b.get('aliases', ()))})
    facts.observations.extend(observations)
    facts.actions.extend(actions)
    return True


RULES = [skip_calendar, shipping, invoice, trip, skip_bulk, skip_filtered, known_person, classify_with_model]


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


def fetch_new(cfg, state, limit, since=None):
    """Yield (messages, folder, uidvalidity) a folder at a time, oldest first,
    where messages are (uid, raw) after that folder's cursor. The config's
    "folders" list is read in order; "folder" is the fallback for one. Each
    folder keeps its own cursor under its own name, so they advance apart and
    limit applies to each. With since (a date), a backfill: every message from
    that day on that the backfill has not handled yet, tracked apart from the
    live cursor. Messages are read with BODY.PEEK so nothing is marked seen."""
    im = cfg['imap']
    password = os.environ.get(im.get('password_env', 'MAIL_PASSWORD'), '')
    conn = imaplib.IMAP4_SSL(im['host'], int(im.get('port', 993)))
    conn.login(im['user'], password)
    folders = im.get('folders') or [im.get('folder', 'INBOX')]
    for folder in folders:
        #  quoted: names like "Real Estate" and "Finance/Receipts" are not atoms
        ok, data = conn.select('"%s"' % folder, readonly=True)
        if ok != 'OK':
            print('cannot open folder %s, skipping it' % folder, file=sys.stderr)
            continue
        yield _folder_batch(conn, state, limit, since, folder)
    conn.logout()


def _folder_batch(conn, state, limit, since, folder):
    """The selected folder's share of the run: its cursor, its new uids, their
    bytes. Split out only so fetch_new stays a readable loop over folders."""
    ok, validity = conn.response('UIDVALIDITY')
    validity = int(validity[0]) if validity and validity[0] else 0
    key = folder
    if since is None:
        cur = state.get(key, {})
        last = int(cur.get('last_uid', 0)) if cur.get('uidvalidity') == validity else 0
        ok, data = conn.uid('search', None, 'UID %d:*' % (last + 1))
    else:
        cur = state.get('backfill', {}).get(key, {})
        same = cur.get('uidvalidity') == validity and cur.get('since') == since.strftime('%Y-%m-%d')
        last = int(cur.get('last_uid', 0)) if same else 0
        ok, data = conn.uid('search', None, 'SINCE %s' % since.strftime('%d-%b-%Y'))
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
    ap.add_argument('--since', help='backfill: every message from this day (YYYY-MM-DD) on')
    ap.add_argument('--months', type=int, help='backfill: every message from this many months ago on')
    ap.add_argument('--no-model', action='store_true', help='rules only, even with a model in the config')
    args = ap.parse_args(argv)

    cfg = {}
    if not args.eml or not args.dry_run or os.path.exists(args.config):
        try:
            with open(args.config) as f:
                cfg = json.load(f)
        except OSError:
            if not (args.eml and args.dry_run):
                raise
    if args.dry_run:
        ship = dry_ship(cfg)
    else:
        token = os.environ.get(cfg['orrery'].get('token_env', 'ORRERY_TOKEN'), '')
        if not token:
            raise SystemExit('no token: set ' + cfg['orrery'].get('token_env', 'ORRERY_TOKEN'))
        ship = Ship(cfg['orrery']['url'], token)
    global MODEL, CONTEXT, CONFIG
    MODEL, CONTEXT, CONFIG = None, None, cfg
    for k in FILTERS:
        FILTERS[k] = list((cfg.get('filters') or {}).get(k) or [])
    mc = cfg.get('model')
    if mc and not args.no_model and mc.get('enabled', True):
        MODEL = analyze.Model(mc.get('url', analyze.DEFAULT_URL), mc.get('name'), int(mc.get('timeout', 180)))
        try:
            print('# model:', MODEL.model_name(), 'at', MODEL.url)
        except RuntimeError as e:
            raise SystemExit(str(e) + ' (start the server, or run with --no-model)')
    since = None
    if args.since:
        since = datetime.strptime(args.since, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    elif args.months:
        since = (datetime.now(timezone.utc) - timedelta(days=30 * args.months)).replace(hour=0, minute=0, second=0, microsecond=0)

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
    for msgs, key, validity in fetch_new(cfg, state, args.limit, since):
        print('%d %s message(s) in %s' % (len(msgs), 'backfill' if since else 'new', key))
        refused = False
        for n, (uid, raw) in enumerate(msgs, 1):
            msg = parse(raw)
            facts = classify(msg, ship)
            print('#', '%d/%d' % (n, len(msgs)), uid, msg.date.strftime('%Y-%m-%d'), msg.id, '|',
                  ' ; '.join(facts.notes) or ('nothing' if facts.empty() else 'facts'))
            if not facts.empty() and not send(ship, facts):
                print('stopping before uid %d so the next run retries it' % uid, file=sys.stderr)
                refused = True
                break
            if not args.dry_run:
                cur = state.get(key, {})
                live = int(cur.get('last_uid', 0)) if cur.get('uidvalidity') == validity else 0
                if since is None:
                    state[key] = {'uidvalidity': validity, 'last_uid': uid}
                else:
                    #  a backfill keeps its own place and only ever moves the live cursor forward
                    state.setdefault('backfill', {})[key] = {'uidvalidity': validity, 'since': since.strftime('%Y-%m-%d'), 'last_uid': uid}
                    if uid > live:
                        state[key] = {'uidvalidity': validity, 'last_uid': uid}
                save_state(state_path, state)
        if refused:
            #  the ship refused: stop the whole run, not just this folder
            break
    return 0


if __name__ == '__main__':
    sys.exit(run())
