#!/usr/bin/env python3
"""telegram: a Telegram bot for orrery.

Both directions. People the config names tell the bot facts in a short
command grammar (in a DM with the bot or in a group it sits in), and the
bot sends them to orrery with a scoped key. It also delivers approved
actions of kind "message" whose payload says via "telegram", to the people
the config maps, and reports done or failed. Connected to your account with
Telegram Business, it also reads your private chats as they come in.

    python3 bot.py --config config.json --dry-run                     # print, send nothing, confirm nothing
    python3 bot.py --config config.json                               # one pass over pending updates
    python3 bot.py --config config.json --loop                        # long poll for ever
    python3 bot.py --dry-run --config fixtures/config.json --updates fixtures/updates.json

Standard library only. See README.md for the grammar and the scope.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import analyze  # noqa: E402

#  the analyst, set from the config at the start of a run; None means the grammar only
MODEL = None
CONTEXT = None
#  the gate: a decision model in front of the analyst says whether a
#  message carries a fact at all; below the threshold the analyst is not
#  asked. Conservative on purpose: it skips only what it is sure of.
DECIDER = None
GATE_THRESHOLD = 0.3
#  Jev picks the bodies the analyst sees, from those scored at or above KEEP;
#  off until the owner has run the check. The status check runs whenever the
#  decider does.
RELEVANCE = False
KEEP = 0.5
#  how many earlier messages of a chat the model sees, as context, with each new one
RECENT = 4

SOURCE = 'chat'
PLATFORM = 'telegram'
MAX_BODIES = 50
MAX_OBS = 200


# ==  http

def request(url, method='GET', body=None, headers=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
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
        self.h = {'Authorization': 'Bearer ' + token}

    def resolve(self, q):
        code, d = request(self.api + '/resolve?' + urllib.parse.urlencode({'q': q}), headers=self.h)
        return d if code == 200 and isinstance(d, list) else []

    def state(self):
        code, d = request(self.api + '/state', headers=self.h)
        return d if code == 200 and isinstance(d, dict) else {}

    def observe(self, bodies, observations):
        return request(self.api + '/observe', 'POST', {'bodies': bodies, 'observations': observations}, self.h)

    def act(self, action):
        return request(self.api + '/act', 'POST', action, self.h)

    def actions(self, status):
        code, d = request(self.api + '/actions?status=' + status, headers=self.h)
        return d if code == 200 and isinstance(d, list) else []

    def move(self, aid, status, note=''):
        body = {'status': status}
        if note:
            body['note'] = note[:500]
        return request(self.api + '/actions/' + aid, 'POST', body, self.h)


class NoShip:
    """A dry run: writes print, reads go to the ship when one is given. Reading
    is harmless and a dry run that cannot read validates against an empty
    vocabulary, so it prints bodies and attributes a real run would drop."""

    def __init__(self, reads=None):
        self.reads = reads

    def resolve(self, q):
        return self.reads.resolve(q) if self.reads else []

    def state(self):
        return self.reads.state() if self.reads else {}

    def observe(self, bodies, observations):
        print(json.dumps({'observe': {'bodies': bodies, 'observations': observations}}, indent=1))
        return 200, None

    def act(self, action):
        print(json.dumps({'act': action}, indent=1))
        return 200, None

    def actions(self, status):
        return []

    def move(self, aid, status, note=''):
        print(json.dumps({'move': {'id': aid, 'status': status, 'note': note}}))
        return 200, None


def dry_ship(cfg):
    """The ship a dry run talks to. With a key (orrery.token, or its variable) its reads are
    the real ship's, so what a dry run prints is judged against the same bodies
    and schema a real run sees; without one it says so, because the difference
    is silent otherwise."""
    orrery = cfg.get('orrery') or {}
    token = analyze.secret(orrery, 'token', 'ORRERY_TOKEN')
    if orrery.get('url') and token:
        return NoShip(Ship(orrery['url'], token))
    print('# dry run without a key: the ship is not read, so its bodies and schema are unknown '
          'and this prints more than a real run would send', file=sys.stderr)
    return NoShip()


#  a long poll's connection drops now and then (reset by a router or by
#  Telegram, a timeout, a moment without network): nothing was received or
#  confirmed, so the bot waits this long and asks again rather than exiting
RETRY_PAUSE = 5


class Telegram:
    """The Bot API with long polling."""

    def __init__(self, token, api='https://api.telegram.org'):
        self.base = api.rstrip('/') + '/bot' + token
        self.owners = {}

    def updates(self, offset, timeout=30):
        q = urllib.parse.urlencode({'offset': offset, 'timeout': timeout,
                                    'allowed_updates': '["message", "business_message"]'})
        code, d = request(self.base + '/getUpdates?' + q, timeout=timeout + 15)
        if code == 0 or code == 429 or code >= 500:
            print('getUpdates: %s %s; asking again in %ds' % (code, str(d)[:120], RETRY_PAUSE), file=sys.stderr)
            time.sleep(RETRY_PAUSE)
            return []
        if code != 200 or not isinstance(d, dict) or not d.get('ok'):
            #  a bad token or another poller on the same one: asking again will not help
            raise SystemExit('getUpdates failed: %s %s' % (code, str(d)[:200]))
        return d.get('result', [])

    def send(self, chat_id, text):
        code, d = request(self.base + '/sendMessage', 'POST', {'chat_id': chat_id, 'text': text[:4096]})
        ok = code == 200 and isinstance(d, dict) and d.get('ok')
        return bool(ok), '' if ok else 'telegram answered %s: %s' % (code, str(d)[:300])

    def business_owner(self, conn_id):
        """The user id of the account a business connection belongs to, or ''
        when Telegram does not say or the connection is off."""
        if conn_id not in self.owners:
            code, d = request(self.base + '/getBusinessConnection?' + urllib.parse.urlencode({'business_connection_id': conn_id}))
            c = d.get('result') if code == 200 and isinstance(d, dict) and d.get('ok') else None
            if not isinstance(c, dict) or not c.get('is_enabled'):
                return ''
            self.owners[conn_id] = str((c.get('user') or {}).get('id', ''))
        return self.owners[conn_id]


class NoTelegram:
    """A dry run never sends; it reads through telegram when it has one."""

    def __init__(self, reads=None):
        self.reads = reads

    def send(self, chat_id, text):
        print(json.dumps({'send': {'chat_id': chat_id, 'text': text}}))
        return True, ''

    def business_owner(self, conn_id):
        return self.reads.business_owner(conn_id) if self.reads else ''


# ==  helpers

def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def short(text, limit=200):
    text = str(text)
    return text if len(text.encode()) <= limit else text.encode()[:limit].decode(errors='ignore')


BID_RE = re.compile(r'^[a-z0-9-]{1,24}/[a-z0-9-]{1,64}$')
ATTR_RE = re.compile(r'^[a-z0-9-]{1,48}$')
NUM_RE = re.compile(r'^-?\d+(\.\d+)?$')


def parse_value(text):
    """null clears, a number is a number, a body id is a ref, the rest is text."""
    t = text.strip()
    if t.lower() == 'null':
        return None
    if NUM_RE.match(t):
        return float(t) if '.' in t else int(t)
    if BID_RE.match(t):
        return {'ref': t}
    if t.lower() in ('true', 'false'):
        return t.lower() == 'true'
    return t


class Facts:
    def __init__(self):
        self.bodies = []
        self.observations = []
        self.actions = []
        self.notes = []
        self.reply = None

    def obs(self, src, subject, attr, value, at, conf=100):
        self.observations.append({'subject': subject, 'attr': attr, 'value': value, 'at': iso(at),
                                  'conf': conf, 'source': src})

    def refuse(self, why):
        self.notes.append(why)
        self.reply = why

    def empty(self):
        return not (self.bodies or self.observations or self.actions)

    def as_json(self):
        return {'bodies': self.bodies, 'observations': self.observations, 'actions': self.actions}


# ==  the grammar
#
#  /at <place>                      the sender's location
#  /status <text>                   the sender's status ("-" clears it)
#  /obs <subject> <attr> <value>    any fact; subject is a body id, a name the ship resolves, or "me"
#  /task <title> [due YYYY-MM-DD]   an action
#  anything else                    the model, when the config has one, held to grounded() below

def subject_of(word, sender_body, ship, facts):
    if word.lower() == 'me':
        return sender_body
    if BID_RE.match(word):
        return word
    hits = [h for h in ship.resolve(word) if isinstance(h, dict) and str(h.get('name', '')).lower() == word.lower()]
    if len(hits) == 1:
        return hits[0]['id']
    facts.refuse('unknown: ' + word + (' (a dry run without a key resolves nothing)' if isinstance(ship, NoShip) and not ship.reads else ''))
    return None


def handle(msg, cfg, ship, recent=None):
    """Facts for one Telegram message, or notes on why there are none. recent
    is the chat's last few free-text messages, shown to the model as context."""
    facts = Facts()
    chat = msg.get('chat') or {}
    sender = msg.get('from') or {}
    text = str(msg.get('text') or '').strip()
    chat_id = str(chat.get('id', ''))
    user_id = str(sender.get('id', ''))
    if chat_id not in [str(c) for c in cfg.get('chats', [])]:
        facts.notes.append('chat %s is not in chats: ignored' % chat_id)
        return facts
    sender_body = cfg.get('people', {}).get(user_id)
    if not sender_body:
        facts.notes.append('sender %s is not in people: ignored' % user_id)
        return facts
    if not text:
        facts.notes.append('no text: ignored')
        return facts
    at = datetime.fromtimestamp(int(msg.get('date', 0) or 0), timezone.utc)
    src = {'kind': SOURCE, 'id': '%s/%s/%s' % (PLATFORM, chat_id, msg.get('message_id', ''))}
    cmd, _, rest = text.partition(' ')
    cmd = cmd.lower().split('@')[0]
    rest = rest.strip()
    if cmd == '/at':
        if not rest:
            facts.refuse('usage: /at <place>')
        else:
            facts.obs(src, sender_body, 'location', parse_value(rest), at)
    elif cmd == '/status':
        if not rest:
            facts.refuse('usage: /status <text>, or /status - to clear')
        else:
            facts.obs(src, sender_body, 'status', None if rest == '-' else rest, at)
    elif cmd == '/obs':
        parts = rest.split(None, 2)
        if len(parts) < 3:
            facts.refuse('usage: /obs <subject> <attr> <value>')
        elif not ATTR_RE.match(parts[1].lower()):
            facts.refuse('attr must be lowercase letters, digits and hyphens')
        else:
            subject = subject_of(parts[0], sender_body, ship, facts)
            if subject:
                facts.obs(src, subject, parts[1].lower(), parse_value(parts[2]), at)
    elif cmd == '/task':
        m = re.match(r'^(.*?)(?:\s+due\s+(\d{4}-\d{2}-\d{2}))?$', rest, re.S)
        title = (m.group(1) if m else rest).strip()
        if not title:
            facts.refuse('usage: /task <title> [due YYYY-MM-DD]')
        else:
            action = {'kind': 'task', 'title': short(title)}
            if m and m.group(2):
                action['due'] = m.group(2) + 'T00:00:00Z'
            facts.actions.append(action)
    elif cmd.startswith('/'):
        facts.refuse('commands: /at, /status, /obs, /task')
    else:
        classify_with_model(msg, sender_body, src, at, facts, ship, recent or [])
    return facts


#  a live bot runs for days, so its view of the ship is read again this often
#  and sees the bodies the other readers made since
CONTEXT_TTL = 600
CONTEXT_AT = 0.0


def context_for(ship):
    """The analyst's view of the ship, grown with the bodies this run creates
    and read again every CONTEXT_TTL seconds."""
    global CONTEXT, CONTEXT_AT
    if CONTEXT is None or time.time() - CONTEXT_AT > CONTEXT_TTL:
        CONTEXT, CONTEXT_AT = analyze.context_from_state(ship.state(), SOURCE), time.time()
    return CONTEXT


#  a small model writes what it remembers as readily as what it read, so a chat
#  fact has to be traceable to its message; the same rules as Talon's extractor
WORD_RE = re.compile(r"[a-z0-9']+")


def words(text):
    return ' %s ' % ' '.join(WORD_RE.findall(str(text).lower()))


def named_in(text, bodies):
    """The ids of the bodies a text names: a name or an alias as whole words,
    or a person's first name; three letters at least, so "me" names nobody."""
    said, hits = words(text), set()
    for b in bodies:
        names = [b.get('name', '')] + list(b.get('aliases') or [])
        if b['id'].startswith('person/'):
            names.append(str(b.get('name', '')).split(' ')[0])
        if any(len(n.strip()) >= 3 and words(n) in said for n in names if words(n).strip()):
            hits.add(b['id'])
    return hits


def shares_a_word(value, text):
    """Whether a paraphrase could be of this text: a word of four letters or more in common."""
    return bool({w for w in words(value).split() if len(w) >= 4} & set(words(text).split()))


#  words that put a message in its author's mouth, and words that point at
#  someone else: a message with the second and none of the first is about
#  someone else, whoever sent it ("grandpa's flight got cancelled", from Sarah)
FIRST_PERSON = {'i', "i'm", 'im', "i've", "i'll", "i'd", 'me', 'my', 'mine', 'myself',
                'we', "we're", "we've", "we'll", 'us', 'our', 'ours'}
SOMEONE_ELSE = {'he', "he's", 'him', 'his', 'she', "she's", 'her', 'hers', 'they', "they're", 'them', 'their',
                'grandma', 'grandpa', 'granny', 'nana', 'mom', 'mum', 'mother', 'dad', 'father', 'wife', 'husband',
                'son', 'daughter', 'brother', 'sister', 'aunt', 'uncle', 'cousin', 'baby', 'kids', 'boss', 'friend'}
#  a status naming a diagnosis is a medical fact, which goes under health, out
#  of every key's sight, and nowhere else (docs/writing-a-client.md, rule 7):
#  moved there when the key's schema lists health, dropped when it does not
#  ponytail: a word list, so "positive vibes" moves too; widen it as leaks show up
MEDICAL = {'covid', 'flu', 'cancer', 'positive', 'diagnosed', 'diagnosis', 'infection', 'fever', 'surgery',
           'chemo', 'pregnant', 'hospital', 'hospitalized', 'medication'}
#  attributes whose value is a paraphrase by design, held to sharing a word
#  with what was said rather than to being quoted from it
PARAPHRASED = ('status', 'health')


def grounded(got, window, ctx):
    """The model's facts that its messages bear out: about the author, when the
    message is theirs to speak for, or a body the message names; a value other
    than a status or health found in the message's words; a status or health
    not read from the earlier messages instead; a status naming a diagnosis
    moved to health; nothing from a question. The rest is dropped with a note,
    and so is a new body no fact kept is about."""
    by_id = {m['id']: m for m in window}
    earlier = [m['text'] for m in window if m.get('context')]
    keep = []
    for o in got['observations']:
        m = by_id.get(o.get('message'), {})
        text = str(m.get('text', ''))
        #  a possessive points as surely as the word: "grandpa's flight" is grandpa's
        said = {w for t in words(text).split() for w in (t, t[:-2] if t.endswith("'s") else t)}
        named = named_in(text, list(ctx.get('bodies', [])) + got['bodies'])
        others = {b for b in named if b.startswith('person/')} - {m.get('who')}
        value = o.get('value')
        medical = o['attr'] == 'status' and isinstance(value, str) and bool(set(words(value).split()) & MEDICAL)
        if text.rstrip().endswith('?'):
            why = 'a question states nothing'
        elif o['subject'] != m.get('who') and o['subject'] not in named:
            why = 'not the author and not named in the message'
        elif o['subject'] == m.get('who') and o['subject'] not in named and (said & SOMEONE_ELSE or others) \
                and not said & FIRST_PERSON:
            why = 'the message is about someone else'
        elif medical and 'health' not in (ctx.get('attrs') or {}).get(o['subject'].split('/', 1)[0], []):
            why = 'a medical fact goes under health, which this key may not write'
        elif o['attr'] not in PARAPHRASED and isinstance(value, dict) and value.get('ref') not in named:
            why = 'the message does not name ' + str(value.get('ref'))
        elif o['attr'] not in PARAPHRASED and isinstance(value, (str, int, float)) and not isinstance(value, bool) \
                and str(value).lower() not in text.lower():
            why = 'the value is not in the message'
        elif o['attr'] in PARAPHRASED and isinstance(value, str) and not shares_a_word(value, text) \
                and any(shares_a_word(value, e) for e in earlier):
            why = 'read from the earlier messages'
        else:
            #  a chat fact is true from its message, not from midnight: a bare
            #  date would lose the fold to anything said earlier that day
            if str(o.get('at', '')).endswith('T00:00:00Z') and str(o['at'])[:10] == str(m.get('at', ''))[:10]:
                o['at'] = m['at']
            if medical:
                o['attr'] = 'health'
                got['notes'].append('moved %s.status to health: a medical fact' % o['subject'])
            keep.append(o)
            continue
        got['notes'].append('dropped %s.%s: %s' % (o['subject'], o['attr'], why))
    got['observations'] = keep
    used = {o['subject'] for o in keep} | {o['value']['ref'] for o in keep if isinstance(o.get('value'), dict)}
    used |= {x for a in got['actions'] for x in a.get('about', [])}
    known = {b['id'] for b in ctx.get('bodies', [])}
    said = words(' '.join(str(m.get('text', '')) for m in window if not m.get('context')))
    kept = []
    for b in got['bodies']:
        if b['id'] in known:
            #  new names for a body the ship has: only words the messages use
            aliases = [a for a in b.get('aliases', []) if words(a).strip() and words(a) in said]
            if aliases:
                kept.append(dict(b, aliases=aliases))
            else:
                got['notes'].append('dropped new names for %s: not in the message' % b['id'])
        elif b['id'] in used:
            kept.append(b)
        else:
            got['notes'].append('dropped body %s: no fact is about it' % b['id'])
    got['bodies'] = kept
    return got


def classify_with_model(msg, sender_body, src, at, facts, ship, recent=()):
    """Free text from a known person goes to the model with the
    sender's body, the source pointer, the message time and the chat's last
    few messages as context. The model sees the text; the ship gets the
    facts and the pointer, and only for the new message."""
    if MODEL is None:
        return None
    if str(msg.get('text') or '').rstrip().endswith('?'):
        #  a question states nothing; it still rides along as the next one's context
        return None
    ctx = context_for(ship)
    window = [dict(m, context=True) for m in recent] + [{'id': src['id'], 'at': iso(at), 'who': sender_body, 'text': str(msg.get('text') or '')}]
    if DECIDER is not None:
        try:
            p = analyze.gate(DECIDER, window, ctx)
        except RuntimeError as e:
            #  a gate that cannot answer lets everything through
            facts.notes.append('gate unavailable, analyst asked: ' + str(e)[:120])
            p = 1.0
        if p < GATE_THRESHOLD:
            facts.notes.append('gate: %.2f that this carries a fact, below %.2f: not read' % (p, GATE_THRESHOLD))
            return None
        facts.notes.append('gate: %.2f, read' % p)
    shown = None
    if DECIDER is not None and RELEVANCE:
        picked = analyze.relevance_pick(DECIDER, window, ctx)
        facts.notes.append(picked['note'])
        shown = analyze.chosen(ctx, window, picked, KEEP, sender_body)
    got = grounded(analyze.analyze(MODEL, window, ctx, shown), window, ctx)
    if DECIDER is not None and got.get('observations'):
        #  rule 8, held by a model that cannot answer outside the set: a
        #  status that is a feeling never reaches the ship
        got['observations'], said = analyze.status_check(DECIDER, window, got['observations'])
        facts.notes.extend(said)
    facts.notes.extend(got['notes'])
    bodies, observations, actions = analyze.to_batch(got, SOURCE)
    facts.bodies.extend(bodies)
    analyze.remember(ctx, bodies)
    facts.observations.extend(observations)
    facts.actions.extend(actions)
    return None


# ==  the executor: message actions via telegram

#  a claim answers before the writer applies it, so the claim is read
#  back a few times before it is given up on
CLAIM_READS = 5
CLAIM_PAUSE = 0.2
#  how often the executor asks the ship for open actions. Every request
#  into a grubbery app costs the ship about a second of its single
#  thread, and the long poll returns every thirty seconds whether or not
#  a message came, so asking on every pass was a heartbeat of requests
#  (a fifth of ricsul's busy time, measured 2026-09-20). An approved
#  message now goes out within this many seconds instead.
EXECUTE_EVERY = 300


def claimant(a):
    """The by of the last claimed step in an action's history, or ''."""
    who = ''
    for h in (a.get('history') or []):
        if isinstance(h, dict) and h.get('status') == 'claimed':
            who = str(h.get('by') or '')
    return who


def confirm_claim(ship, aid, mine):
    """'' when the action is ours to act on, else why it is not."""
    if not mine:
        return 'the claim answered no by'
    for n in range(CLAIM_READS):
        if n:
            time.sleep(CLAIM_PAUSE)
        for a in ship.actions('claimed'):
            if isinstance(a, dict) and str(a.get('id')) == aid:
                who = claimant(a)
                return '' if who == mine else 'claimed by ' + who
    return 'the claim did not land in %d reads' % CLAIM_READS


def chat_for(to, cfg):
    """The chat id a message action's "to" names, or None."""
    to = str(to or '')
    for uid, body in cfg.get('people', {}).items():
        if body == to:
            return uid
    if to in [str(c) for c in cfg.get('chats', [])] or to in cfg.get('people', {}):
        return to
    return None


def execute(cfg, ship, tg, state):
    """Claim and deliver every open message action addressed via telegram, once.

    Open, not approved: an action another bot claimed and abandoned is
    claimable again once its lease runs out, and the ship, not the clock
    here, decides that.
    """
    done = list(state.get('executed', []))
    for a in ship.actions('open'):
        if not isinstance(a, dict) or a.get('kind') != 'message' or a.get('id') in done:
            continue
        if a.get('status') not in ('approved', 'claimed'):
            continue
        payload = a.get('payload') if isinstance(a.get('payload'), dict) else {}
        if payload.get('via') != PLATFORM:
            continue
        aid = str(a.get('id'))
        code, d = ship.move(aid, 'claimed')
        if code != 200:
            print('claim refused, skipping', aid, code, d, file=sys.stderr)
            continue
        why = confirm_claim(ship, aid, str((d or {}).get('by') or ''))
        if why:
            print('claim not confirmed, skipping', aid, why, file=sys.stderr)
            continue
        text = str(payload.get('text') or '').strip()
        chat = chat_for(payload.get('to'), cfg)
        if not text:
            ship.move(aid, 'failed', 'payload.text is empty')
        elif not chat:
            ship.move(aid, 'failed', 'payload.to names nobody in people or chats: ' + str(payload.get('to')))
        else:
            ok, why = tg.send(chat, text)
            ship.move(aid, 'done' if ok else 'failed', why)
        done.append(aid)
    state['executed'] = done[-500:]


# ==  sending, the cursor, the loop

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
                    facts.reply = 'refused: ' + str(r.get('error'))
    for a in facts.actions:
        code, d = ship.act(a)
        if code != 200:
            print('act refused', code, d, file=sys.stderr)
            facts.reply = 'refused: ' + str(d)[:200]
            ok = False
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


def remember(state, msg, sender_body):
    """Keep a chat's last RECENT free-text messages in the state, as the
    context the next one is read with."""
    chat_id = str((msg.get('chat') or {}).get('id', ''))
    text = str(msg.get('text') or '').strip()
    if not text or text.startswith('/') or not sender_body:
        return
    ring = state.setdefault('recent', {}).setdefault(chat_id, [])
    ring.append({'id': '%s/%s/%s' % (PLATFORM, chat_id, msg.get('message_id', '')),
                 'at': iso(datetime.fromtimestamp(int(msg.get('date', 0) or 0), timezone.utc)),
                 'who': sender_body, 'text': text[:2000]})
    del ring[:-RECENT]


EXECUTED_AT = 0.0


def one_pass(cfg, ship, tg, updates, state, state_path, dry):
    global EXECUTED_AT
    for u in updates:
        uid = u.get('update_id')
        #  a business message is one of the owner's own private chats, read
        #  through the Telegram Business connection: never replied to, since a
        #  reply would land in the other person's chat with the bot
        business = 'business_message' in u
        msg = u.get('business_message') if business else u.get('message')
        if isinstance(msg, dict):
            chat_id = str((msg.get('chat') or {}).get('id', ''))
            sender_body = cfg.get('people', {}).get(str((msg.get('from') or {}).get('id', '')))
            stranger = business and tg.business_owner(str(msg.get('business_connection_id', ''))) not in cfg.get('people', {})
            if stranger:
                facts = Facts()
                facts.notes.append('business connection of an account not in people: ignored')
            else:
                facts = handle(msg, cfg, ship, state.get('recent', {}).get(chat_id, []))
            print('#', uid, '|', 'business' if business else 'chat', chat_id, 'from', sender_body or 'someone', '|',
                  ' ; '.join(facts.notes) or ('nothing' if facts.empty() else 'facts'))
            #  live messages have no export behind them, so one the model never
            #  read is kept for the next pass rather than confirmed and lost
            if analyze.model_down(facts.notes):
                print('stopping before update %s until the model answers' % uid, file=sys.stderr)
                return False
            if not facts.empty() and not send(ship, facts):
                print('stopping before update %s so the next pass retries it' % uid, file=sys.stderr)
                return False
            #  remembered once it is handled, so a retry does not show it to the model
            #  as its own context, and only from chats the bot reads
            if not stranger and chat_id in [str(c) for c in cfg.get('chats', [])]:
                remember(state, msg, sender_body)
            if facts.reply and cfg.get('reply_errors', True) and not business:
                tg.send(msg.get('chat', {}).get('id'), facts.reply)
        if uid is not None:
            state['offset'] = int(uid) + 1
            if not dry:
                save_state(state_path, state)
    if time.time() - EXECUTED_AT >= cfg.get('execute_every', EXECUTE_EVERY):
        execute(cfg, ship, tg, state)
        EXECUTED_AT = time.time()
    if not dry:
        save_state(state_path, state)
    return True


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--dry-run', action='store_true', help='print, send nothing, confirm no update')
    ap.add_argument('--updates', help='read updates from this file instead of telegram')
    ap.add_argument('--loop', action='store_true', help='long poll for ever')
    args = ap.parse_args(argv)
    cfg = analyze.load_config(args.config)
    global MODEL, CONTEXT, DECIDER, GATE_THRESHOLD
    MODEL, CONTEXT = None, None
    mc = cfg.get('model')
    if mc and mc.get('enabled', True):
        MODEL = analyze.Model.from_config(mc)
        try:
            print('# model:', MODEL.model_name(), 'at', MODEL.url)
        except RuntimeError as e:
            raise SystemExit(str(e) + ' (start the server, or remove the model block)')
    global RELEVANCE, KEEP
    DECIDER = analyze.Decider.from_config(cfg) if MODEL is not None else None
    if DECIDER is not None:
        dc = cfg.get('decide') or {}
        GATE_THRESHOLD = float(dc.get('threshold', GATE_THRESHOLD))
        RELEVANCE, KEEP = bool(dc.get('relevance', False)), float(dc.get('keep', KEEP))
        print('# gate:', DECIDER.model, 'below', GATE_THRESHOLD, 'the analyst is not asked;', 'it sees the bodies Jev picks at %.2f' % KEEP if RELEVANCE else 'it sees every body', '; statuses are checked')
    reader = None
    if not args.updates:
        btok = analyze.secret(cfg['telegram'], 'token', 'TELEGRAM_TOKEN')
        if not btok:
            raise SystemExit('no bot token: put it in telegram.token, or set the variable telegram.token_env names')
        reader = Telegram(btok, cfg['telegram'].get('api', 'https://api.telegram.org'))
    if args.dry_run:
        ship, tg = dry_ship(cfg), NoTelegram(reader)
    else:
        token = analyze.secret(cfg['orrery'], 'token', 'ORRERY_TOKEN')
        if not token or reader is None:
            raise SystemExit('a real run needs both tokens and telegram (no --updates)')
        ship, tg = Ship(cfg['orrery']['url'], token), reader
    state_path = cfg.get('state', 'state.json')
    state = load_state(state_path)
    while True:
        if args.updates:
            with open(args.updates) as f:
                updates = json.load(f)
            updates = updates.get('result', updates) if isinstance(updates, dict) else updates
        else:
            updates = reader.updates(int(state.get('offset', 0)), timeout=30 if args.loop else 0)
        if not one_pass(cfg, ship, tg, updates, state, state_path, args.dry_run):
            return 1
        if not args.loop or args.updates:
            return 0
        if not updates:
            time.sleep(1)


if __name__ == '__main__':
    sys.exit(run())
