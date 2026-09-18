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
    {"id": "<source id>", "at": "<ISO UTC>", "who": "<body id or name>", "text": "...",
     "context": true}   context: an earlier message handled before, shown so the new ones read
                        right; no fact is taken from it
context: what the model may refer to
    {"bodies": [{"id", "name", "aliases"}], "attrs": {"person": [...], ...},
     "me": "person/me", "channel": "mail", "action_kinds": ["task"]}
facts: {"bodies": [...], "observations": [...], "actions": [...], "notes": [...]}
    observations carry "message" (the id of the message that supports them),
    "at" (ISO UTC) and "conf"; actions carry "message" too.

Standard library only.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_URL = 'http://localhost:1234/v1'
MAX_TEXT = 8000
MAX_BODIES_IN_CONTEXT = 300
BID_RE = re.compile(r'^[a-z0-9-]{1,24}/[a-z0-9-]{1,64}$')
#  the kinds a body may have. BID_RE alone matches the prompt's own
#  "kind/slug" placeholder, which a model copies verbatim often enough to matter
KINDS = ('person', 'place', 'thing', 'org', 'situation', 'note', 'activity')
#  where a health or money fact belongs. A ship that lists these in its schema
#  takes them; one whose policy marks them sensitive redacts them from a key's
#  schema, and then the rule below drops them like any other unlisted name.
#  Named here only so the note says which of the two happened.
SENSITIVE_ATTRS = ('health', 'income')
#  a key minted with sensitive: write (orrery version 13) sees these names in its
#  schema view and so may write them; a key without it does not, and the rule
#  below drops them like any other unlisted name
#  attributes the prompt offers as a sink for feelings; never sent
SINK_ATTRS = ('mood', 'feeling', 'feelings', 'emotion')
ATTR_RE = re.compile(r'^[a-z0-9-]{1,48}$')

#  the system prompt lives in analyst-prompt.md beside this file, so a client
#  in any language reads the same text; edit it there
PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'analyst-prompt.md')


def load_prompt(path=PROMPT_PATH):
    with open(path, encoding='utf-8') as f:
        return f.read().strip()


SYSTEM = load_prompt()


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

CLOSED_DAYS = 30


def closed_before(body, cutoff):
    """A situation whose status is closed and whose end (or its close) is
    before cutoff."""
    attrs = body.get('attrs') or {}
    status = attrs.get('status') if isinstance(attrs.get('status'), dict) else None
    if not status or status.get('value') != 'closed':
        return False
    ended = attrs.get('ended') if isinstance(attrs.get('ended'), dict) else None
    when = (ended or {}).get('value') or status.get('at') or ''
    return isinstance(when, str) and bool(when) and when < cutoff


def context_from_state(state, channel, action_kinds=('task',)):
    """The context block from a state view: the bodies (id, name, aliases),
    the schema's attribute names per kind, and person/me."""
    bodies = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CLOSED_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for b in (state or {}).get('bodies', []):
        if not isinstance(b, dict) or not b.get('id'):
            continue
        if b['id'].startswith('situation/') and closed_before(b, cutoff):
            #  a situation long over is not something a new message refers to
            continue
        bodies.append({'id': b['id'], 'name': b.get('name', ''), 'aliases': list(b.get('aliases') or [])})
    attrs = {}
    notes = {}
    for kind, spec in ((state or {}).get('schema') or {}).get('kinds', {}).items():
        if isinstance(spec, dict):
            attrs[kind] = list(spec.get('attrs') or [])
            if isinstance(spec.get('notes'), dict):
                notes[kind] = {str(k): str(v) for k, v in spec['notes'].items() if isinstance(v, str)}
    return {'bodies': bodies[:MAX_BODIES_IN_CONTEXT], 'attrs': attrs, 'notes': notes, 'me': (state or {}).get('me', 'person/me'),
            'channel': channel, 'action_kinds': list(action_kinds)}


def prompt(messages, context):
    lines = ['Channel: ' + str(context.get('channel', '')), 'The owner is ' + str(context.get('me', 'person/me')) + '.']
    if context.get('attrs'):
        lines.append('Attribute names by kind:')
        for kind, names in context['attrs'].items():
            lines.append('  %s: %s' % (kind, ', '.join(names)))
    if context.get('notes'):
        lines.append('What the attributes mean:')
        for kind, notes in context['notes'].items():
            for attr, text in notes.items():
                lines.append('  %s.%s: %s' % (kind, attr, text))
    if context.get('action_kinds'):
        lines.append('Action kinds you may propose: ' + ', '.join(context['action_kinds']))
    lines.append('Existing bodies (id | name | aliases):')
    for b in context.get('bodies', []):
        lines.append('  %s | %s | %s' % (b['id'], b.get('name', ''), ', '.join(b.get('aliases') or [])))
    if not context.get('bodies'):
        lines.append('  (none known)')
    lines.append('')
    earlier = [m for m in messages if m.get('context')]
    fresh = [m for m in messages if not m.get('context')]
    if earlier:
        lines.append('Earlier messages, context only, oldest first (write no facts from these):')
        for m in earlier:
            lines.append('--- context %s | %s | from %s' % (m['id'], m.get('at', ''), m.get('who', '')))
            lines.append(str(m.get('text', ''))[:MAX_TEXT])
        lines.append('New messages, oldest first:')
    else:
        lines.append('Messages, oldest first:')
    for m in fresh:
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
    and weekdays, punctuation and case. "Reminder: Pottery @ Thu May 14, 6:00pm"
    and "Pottery" normalise to the same key; "Robin- Pottery/Wheel" stays its own."""
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
    name is a first name, not a role: "dana" and "dana quill" are one
    person, "dana" and "daniel quill" are not, and "wife" names nobody."""
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
        hits = [b for b in pool if b['id'].startswith('person/')
                and (same_person(body.get('name'), b.get('name'))
                     or any(same_person(body.get('name'), a) for a in (b.get('aliases') or [])))]
        if len(hits) == 1:
            return hits[0]['id']
        #  several candidates: the one whose name is word for word the same wins,
        #  so "owner" folds into the body named owner and not into "owner wife"
        def flat(n):
            return re.sub(r'\s+', ' ', str(n or '').strip().lower())
        exact = [b['id'] for b in hits if flat(b.get('name')) == flat(body.get('name'))
                 or flat(body.get('name')) in [flat(a) for a in (b.get('aliases') or [])]]
        return exact[0] if len(exact) == 1 else None
    return None


def validate(answer, messages, context):
    """The model's answer as facts orrery will take, with notes on what was dropped."""
    notes = []
    known = {b['id'] for b in context.get('bodies', [])}
    ids = [m['id'] for m in messages if not m.get('context')]
    context_ids = {m['id'] for m in messages if m.get('context')}
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
        if bid.split('/', 1)[0] not in KINDS:
            notes.append('dropped body of an unknown kind: ' + bid)
            continue
        aliases = [str(a).strip()[:100] for a in (b.get('aliases') or []) if str(a).strip()][:32]
        if bid in known:
            #  a body the ship has, named by new words: send the aliases only, so
            #  the ship unions them and keeps its own name. That is how "next door"
            #  becomes a name of place/neighbors without anyone typing it in
            have = next((set(x.get('aliases') or []) | {x.get('name')} for x in context.get('bodies', []) if x['id'] == bid), set())
            fresh = [a for a in aliases if a not in have]
            if fresh:
                bodies.append({'id': bid, 'aliases': fresh})
            continue
        name = str(b.get('name') or bid.split('/', 1)[1].replace('-', ' ')).strip()[:200]
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
        if attr in SINK_ATTRS:
            #  the prompt offers these so a feeling has somewhere to go that is not
            #  status; they are thrown away here, quietly
            continue
        if str(o.get('message', '')) in context_ids:
            #  a fact re-derived from a message handled before: its facts exist already
            continue
        kind = subject.split('/', 1)[0]
        listed = (context.get('attrs') or {}).get(kind) or []
        if listed and attr not in listed:
            #  a kind the schema speaks for keeps to its vocabulary, so a health or
            #  money fact cannot land on an invented name the owner's policy never sees
            why = ("the owner's policy keeps it from keys" if attr in SENSITIVE_ATTRS
                   else 'not an attribute of ' + kind)
            notes.append('dropped %s.%s: %s' % (subject, attr, why))
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
        if str(a.get('message', '')) in context_ids:
            continue
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
    if not [m for m in messages if not m.get('context')]:
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
