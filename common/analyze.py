#!/usr/bin/env python3
"""The analyst: a local model turns messages into orrery facts.

Talks to an OpenAI-compatible chat endpoint (LM Studio at
http://localhost:1234/v1 by default) and asks it for bodies, observations
and actions in orrery's own shapes. Every answer is validated here before
it goes anywhere near a ship: ids well formed, subjects known, values
bounded, times parseable. The model sees the message text; the ship never
does.

    from analyze import Model, analyze
    model = Model(url, name)              # name None: the first model the server lists
    facts = analyze(model, messages, context)

messages: a window of one conversation, oldest first, each
    {"id": "<source id>", "at": "<ISO UTC>", "who": "<body id or name>", "text": "..."}
context: what the model may refer to
    {"bodies": [{"id", "name", "aliases"}], "attrs": {"person": [...], ...},
     "me": "person/me", "channel": "mail", "action_kinds": ["task"]}
facts: {"bodies": [...], "observations": [...], "actions": [...], "notes": [...]}
    observations carry "message" (the id of the message that supports them),
    "at" (ISO UTC) and "conf"; actions carry "message" too.

Standard library only.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = 'http://localhost:1234/v1'
MAX_TEXT = 8000
MAX_BODIES_IN_CONTEXT = 300
BID_RE = re.compile(r'^[a-z0-9-]{1,24}/[a-z0-9-]{1,64}$')
ATTR_RE = re.compile(r'^[a-z0-9-]{1,48}$')

SYSTEM = """You turn messages into facts for orrery, a model of one person's world.
Three shapes exist.
A body is something that exists: a person, place, thing, org, situation or note. Its id is kind/slug, lowercase letters, digits and hyphens, for example person/sarah, place/johns-machine-shop, thing/subaru, situation/2026-09-16-breakdown.
An observation is one claim about one body: subject.attr = value, with when it became true. Values are a short string, a number, true or false, null (which clears the attribute), or {"ref": "kind/slug"} pointing at another body.
An action is something to do: a task with a title, the bodies it is about, and an optional due time.
Rules.
Only state what the messages say or clearly imply. Never invent. When unsure, leave it out or lower the confidence.
Use the existing bodies by id whenever a message refers to one of them, by name or alias. Create a new body only for a named person, place, thing or org, or for a situation (an event with participants) the messages describe.
Use the attribute names listed for each kind when one fits; otherwise a short lowercase name.
A situation body carries status ("open" or "closed"), participants (one observation per participant, value {"ref": ...}), location, started and ended. A situation happens once: a breakdown, a birthday, a delivery.
An activity is something that repeats: a class, a practice, a standing appointment, confession every Saturday. It is one body of kind activity, with schedule ("Tue/Thu 16:45"), cadence ("weekly"), location, participants and organizer. An occurrence of an activity is never a new body: write the activity's "last" = the start of that occurrence, with "at" = that start, and "next" = the start of the following one when the message says it. A calendar reminder or notification for a repeating event is an occurrence of an activity, not a situation.
A person is never an org. A payment request, a reminder or a note from a person names a person body; reuse the existing person when the name or the address matches, even when only the first name is on record.
"at" is when the fact became true, ISO 8601 UTC, and defaults to the message's time; set it only when the message says otherwise. "until" is when it will stop being true, when the message says so.
"conf" is 0 to 100: 90 for a plain statement, 60 for an inference, 40 for a guess.
Each observation and action names the "message" id it comes from.
Answer with one JSON object and nothing else:
{"bodies": [{"id": "kind/slug", "name": "...", "aliases": ["..."]}],
 "observations": [{"subject": "kind/slug", "attr": "...", "value": ..., "at": "...", "until": "...", "conf": 90, "message": "..."}],
 "actions": [{"kind": "task", "title": "...", "about": ["kind/slug"], "due": "...", "message": "..."}]}
Empty lists are fine. Small talk, greetings and things already known produce nothing."""


class Model:
    """An OpenAI-compatible chat endpoint."""

    def __init__(self, url=DEFAULT_URL, name=None, timeout=180, temperature=0.0):
        self.url = (url or DEFAULT_URL).rstrip('/')
        self.name = name
        self.timeout = timeout
        self.temperature = temperature

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method='POST' if data else 'GET')
        req.add_header('content-type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError('model answered %s: %s' % (e.code, e.read().decode(errors='replace')[:300]))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError('model unreachable at %s: %s' % (self.url, e))

    def model_name(self):
        if not self.name:
            d = self.request('/models')
            names = [m.get('id') for m in d.get('data', []) if isinstance(m, dict) and m.get('id')]
            if not names:
                raise RuntimeError('the server lists no model')
            self.name = names[0]
        return self.name

    def chat(self, system, user):
        d = self.request('/chat/completions', {
            'model': self.model_name(), 'temperature': self.temperature, 'max_tokens': 2000,
            'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]})
        try:
            return d['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError):
            raise RuntimeError('model answered without content: ' + json.dumps(d)[:300])


class FakeModel:
    """Answers a canned string; tests and dry runs without a server."""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def chat(self, system, user):
        self.asked.append(user)
        return self.answer


# ==  the prompt

def context_from_state(state, channel, action_kinds=('task',)):
    """The context block from a state view: the bodies (id, name, aliases),
    the schema's attribute names per kind, and person/me."""
    bodies = []
    for b in (state or {}).get('bodies', []):
        if isinstance(b, dict) and b.get('id'):
            bodies.append({'id': b['id'], 'name': b.get('name', ''), 'aliases': list(b.get('aliases') or [])})
    attrs = {}
    for kind, spec in ((state or {}).get('schema') or {}).get('kinds', {}).items():
        if isinstance(spec, dict):
            attrs[kind] = list(spec.get('attrs') or [])
    return {'bodies': bodies[:MAX_BODIES_IN_CONTEXT], 'attrs': attrs, 'me': (state or {}).get('me', 'person/me'),
            'channel': channel, 'action_kinds': list(action_kinds)}


def prompt(messages, context):
    lines = ['Channel: ' + str(context.get('channel', '')), 'The owner is ' + str(context.get('me', 'person/me')) + '.']
    if context.get('attrs'):
        lines.append('Attribute names by kind:')
        for kind, names in context['attrs'].items():
            lines.append('  %s: %s' % (kind, ', '.join(names)))
    if context.get('action_kinds'):
        lines.append('Action kinds you may propose: ' + ', '.join(context['action_kinds']))
    lines.append('Existing bodies (id | name | aliases):')
    for b in context.get('bodies', []):
        lines.append('  %s | %s | %s' % (b['id'], b.get('name', ''), ', '.join(b.get('aliases') or [])))
    if not context.get('bodies'):
        lines.append('  (none known)')
    lines.append('')
    lines.append('Messages, oldest first:')
    for m in messages:
        lines.append('--- message %s | %s | from %s' % (m['id'], m.get('at', ''), m.get('who', '')))
        lines.append(str(m.get('text', ''))[:MAX_TEXT])
    lines.append('---')
    lines.append('Answer with the JSON object.')
    return '\n'.join(lines)


# ==  the answer

def parse_json(text):
    """The first JSON object in a model's answer, fences and chatter stripped."""
    t = text.strip()
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    start, end = t.find('{'), t.rfind('}')
    if start < 0 or end < start:
        raise ValueError('no JSON object in the answer')
    return json.loads(t[start:end + 1])


def iso_or_none(value):
    """An ISO 8601 time as orrery writes it, or None."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', v):
        v += 'T00:00:00Z'
    try:
        dt = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def clean_value(v, known):
    """A value orrery accepts, or a refusal string starting with '!'."""
    if v is None or isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        return v.strip()[:2000]
    if isinstance(v, dict) and set(v.keys()) == {'ref'}:
        ref = str(v['ref'])
        if not BID_RE.match(ref):
            return '!ref is not a body id: ' + ref
        return {'ref': ref}
    if isinstance(v, (dict, list)):
        s = json.dumps(v)
        return v if len(s) <= 2000 else '!value over 2000 bytes'
    return '!value of an unknown type'


NOISE_RE = re.compile(r'^(?:reminder|invitation|updated invitation|fwd|fw|re|notification)\s*:\s*', re.I)
DATEISH_RE = re.compile(r'\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|'
                        r'\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|'
                        r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?\b', re.I)


def normalize_title(text):
    """A title with its noise stripped: prefixes like Reminder:, dates, times
    and weekdays, punctuation and case. "Reminder: Ballet @ Wed Sep 16, 4:45pm"
    and "Ballet" normalise to the same key; "Adelaide- Ballet/Tap" stays its own."""
    t = NOISE_RE.sub('', str(text or '').strip())
    t = NOISE_RE.sub('', t)
    t = DATEISH_RE.sub(' ', t)
    t = re.sub(r'[^a-z0-9/ ]+', ' ', t.lower())
    return re.sub(r'\s+', ' ', t).strip()


def slug(text, limit=64):
    """A body slug: lowercase letters, digits and hyphens."""
    s = re.sub(r'[^a-z0-9-]+', '-', str(text or '').lower()).strip('-')
    return re.sub(r'-{2,}', '-', s)[:limit].strip('-') or 'x'


def person_key(name):
    """The lowercase word set of a person's name."""
    return frozenset(w for w in re.split(r'[^a-z0-9]+', str(name or '').lower()) if w)


ROLE_WORDS = {'me', 'i', 'wife', 'husband', 'mom', 'mum', 'dad', 'mother', 'father', 'son', 'daughter',
              'brother', 'sister', 'boss', 'friend', 'partner', 'mr', 'mrs', 'ms', 'dr', 'the'}


def same_person(a, b):
    """Every word of the shorter name is in the longer one, and a one-word
    name is a first name, not a role: "andrea" and "andrea egan" are one
    person, "andrea" and "andrew egan" are not, and "wife" names nobody."""
    ka, kb = person_key(a) - ROLE_WORDS, person_key(b) - ROLE_WORDS
    if not ka or not kb:
        return False
    short_, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if not short_ <= long_:
        return False
    if len(short_) == 1:
        first = [w for w in re.split(r'[^a-z0-9]+', str(a if short_ == ka else b).lower()) if w and w not in ROLE_WORDS]
        other = [w for w in re.split(r'[^a-z0-9]+', str(b if short_ == ka else a).lower()) if w and w not in ROLE_WORDS]
        return bool(first) and bool(other) and first[0] == other[0]
    return True


def existing_for(body, context, made):
    """The id of an existing or already-made body this new one duplicates:
    a situation or activity with the same normalised title, or a person the
    same words name. None when it is new."""
    kind = body['id'].split('/', 1)[0]
    pool = list(context.get('bodies', [])) + made
    if kind in ('situation', 'activity'):
        key = normalize_title(body.get('name'))
        if not key:
            return None
        for b in pool:
            if b['id'].split('/', 1)[0] in ('situation', 'activity') and normalize_title(b.get('name')) == key:
                return b['id']
        return None
    if kind == 'person':
        hits = [b['id'] for b in pool if b['id'].startswith('person/')
                and (same_person(body.get('name'), b.get('name'))
                     or any(same_person(body.get('name'), a) for a in (b.get('aliases') or [])))]
        return hits[0] if len(hits) == 1 else None
    return None


def validate(answer, messages, context):
    """The model's answer as facts orrery will take, with notes on what was dropped."""
    notes = []
    known = {b['id'] for b in context.get('bodies', [])}
    ids = [m['id'] for m in messages]
    at_of = {m['id']: m.get('at') for m in messages}
    last = ids[-1] if ids else ''
    bodies = []
    alias_of = {}
    for b in answer.get('bodies') or []:
        if not isinstance(b, dict):
            continue
        bid = str(b.get('id', '')).strip().lower()
        if not BID_RE.match(bid):
            notes.append('dropped body with a bad id: ' + bid)
            continue
        if bid in known:
            continue
        name = str(b.get('name') or bid.split('/', 1)[1].replace('-', ' ')).strip()[:200]
        aliases = [str(a).strip()[:100] for a in (b.get('aliases') or []) if str(a).strip()][:32]
        row = {'id': bid, 'name': name, 'aliases': aliases} if aliases else {'id': bid, 'name': name}
        twin = existing_for(row, context, bodies)
        if twin:
            alias_of[bid] = twin
            notes.append('%s is %s' % (bid, twin))
            continue
        bodies.append(row)
        known.add(bid)

    def canon(bid):
        return alias_of.get(bid, bid)

    observations = []
    for o in answer.get('observations') or []:
        if not isinstance(o, dict):
            continue
        subject = canon(str(o.get('subject', '')).strip().lower())
        attr = str(o.get('attr', '')).strip().lower()
        if isinstance(o.get('value'), dict) and set(o['value'].keys()) == {'ref'}:
            o = dict(o, value={'ref': canon(str(o['value']['ref']).strip().lower())})
        if subject not in known:
            notes.append('dropped observation on an unknown body: ' + subject)
            continue
        if not ATTR_RE.match(attr):
            notes.append('dropped observation with a bad attr: ' + attr)
            continue
        value = clean_value(o.get('value'), known)
        if isinstance(value, str) and value.startswith('!'):
            notes.append('dropped %s.%s: %s' % (subject, attr, value[1:]))
            continue
        msg = str(o.get('message', '')) if str(o.get('message', '')) in ids else last
        at = iso_or_none(o.get('at')) or at_of.get(msg)
        try:
            conf = max(0, min(100, int(o.get('conf', 70))))
        except (TypeError, ValueError):
            conf = 70
        row = {'subject': subject, 'attr': attr, 'value': value, 'at': at, 'conf': conf, 'message': msg}
        until = iso_or_none(o.get('until'))
        if until:
            row['until'] = until
        observations.append(row)
    actions = []
    kinds = set(context.get('action_kinds') or ['task'])
    for a in answer.get('actions') or []:
        if not isinstance(a, dict):
            continue
        kind = str(a.get('kind', 'task')).strip().lower() or 'task'
        title = str(a.get('title', '')).strip()[:200]
        if kind not in kinds or not title:
            notes.append('dropped action: ' + (title or '(no title)'))
            continue
        about = [canon(str(x).strip().lower()) for x in (a.get('about') or [])]
        about = list(dict.fromkeys(x for x in about if x in known))[:20]
        row = {'kind': kind, 'title': title, 'about': about,
               'message': str(a.get('message', '')) if str(a.get('message', '')) in ids else last}
        due = iso_or_none(a.get('due'))
        if due:
            row['due'] = due
        actions.append(row)
    return {'bodies': bodies, 'observations': observations, 'actions': actions, 'notes': notes}


def analyze(model, messages, context):
    """Facts for one window of messages, or empty facts with a note on why."""
    if not messages:
        return {'bodies': [], 'observations': [], 'actions': [], 'notes': []}
    try:
        raw = model.chat(SYSTEM, prompt(messages, context))
        answer = parse_json(raw)
    except (RuntimeError, ValueError) as e:
        return {'bodies': [], 'observations': [], 'actions': [], 'notes': ['model: ' + str(e)[:200]]}
    if not isinstance(answer, dict):
        return {'bodies': [], 'observations': [], 'actions': [], 'notes': ['model: the answer is not an object']}
    return validate(answer, messages, context)


def to_batch(facts, source_kind, by_default=None):
    """The observe batch and the actions for a ship: each row gets its
    source pointer from its message id; the model's bookkeeping keys go."""
    bodies = [dict(b) for b in facts['bodies']]
    observations = []
    for o in facts['observations']:
        row = {k: v for k, v in o.items() if k != 'message'}
        row['source'] = {'kind': source_kind, 'id': o['message']}
        observations.append(row)
    actions = [{k: v for k, v in a.items() if k != 'message'} for a in facts['actions']]
    return bodies, observations, actions
