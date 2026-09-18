#!/usr/bin/env python3
"""reconcile: associate what the readers left apart.

Two passes over a ship's state.

  activities   situations that are occurrences of one repeating event become
               one activity body each, with an observation per occurrence,
               and the occurrence bodies are deleted (owner cookie needed).
  people       bodies that name the same person (an org made from a person's
               name, two persons whose names or addresses match) become merge
               proposals: actions of kind "merge" for the owner to approve,
               applied with --apply through the ship's merge op.
  retire       situations that are over (ended in the past, or started long
               ago with nothing said since) are closed with status = closed
               written at the time they ended; --prune DAYS also deletes
               closed situations that ended more than DAYS ago.

    python3 reconcile.py --ship https://your-ship.example --jar jar activities --dry-run
    python3 reconcile.py --ship https://your-ship.example --jar jar activities
    python3 reconcile.py --ship https://your-ship.example --jar jar people            # propose
    python3 reconcile.py --ship https://your-ship.example --jar jar people --apply    # run the approved merges
    python3 reconcile.py --ship https://your-ship.example --jar jar retire --dry-run
    python3 reconcile.py --ship https://your-ship.example --jar jar retire --prune 90
    python3 reconcile.py --state saved-state.json activities --dry-run                # from a file, no ship

Standard library only. Runs with the owner cookie (a curl jar), because
deleting and merging bodies are the owner's ops.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze  # noqa: E402

CAL_RE = re.compile(r'^situation/(cal-[a-z0-9-]*?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:-[a-z0-9]+)?$')


# ==  the ship, as the owner

class Owner:
    """The owner's cookie, read straight from a curl jar (Netscape format):
    the last non-comment line's name and value."""

    def __init__(self, url, jar_path):
        self.api = url.rstrip('/') + '/apps/orrery/api'
        self.cookie = ''
        with open(jar_path) as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) >= 7 and not line.startswith('#'):
                    self.cookie = parts[5] + '=' + parts[6]
        if not self.cookie:
            raise SystemExit('no cookie in ' + jar_path)

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header('Cookie', self.cookie)
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

    def state(self):
        code, d = self.call('GET', '/state')
        if code != 200:
            raise SystemExit('cannot read the state: %s %s' % (code, str(d)[:200]))
        return d


class Printer:
    """A dry run: every write prints."""

    def __init__(self, state):
        self._state = state

    def state(self):
        return self._state

    def call(self, method, path, body=None):
        print(json.dumps({method: path, 'body': body}, indent=None)[:600])
        return 200, {'ok': True, 'bodies': [], 'observations': [], 'id': 'dry', 'status': 'proposed'}


# ==  activities

def series_key(body):
    """What groups occurrences: the calendar UID when the id carries one,
    else the normalised title."""
    m = CAL_RE.match(body['id'])
    if m:
        return 'uid:' + m.group(1)
    return 'title:' + analyze.normalize_title(body.get('name'))


def value_of(attrs, name):
    v = attrs.get(name)
    if isinstance(v, dict):
        return v.get('value')
    if isinstance(v, list):
        return [x.get('value') for x in v if isinstance(x, dict)]
    return None


TRIP_RE = re.compile(r'^situation/\d{4}-\d{2}-\d{2}-trip$')


def strict_key(name):
    """A title with only its prefix noise and case removed: two occurrences
    with no calendar id must be named alike, so "Trip starting 2026-09-02"
    and "Trip starting 2026-09-04" stay two situations."""
    t = analyze.NOISE_RE.sub('', str(name or '').strip())
    return re.sub(r'\s+', ' ', t.lower()).strip()


def common_title(members):
    """The title most of a group's members carry, ties to the shortest."""
    counts = defaultdict(int)
    for m in members:
        n = str(m.get('name') or '').strip()
        if n:
            counts[n] += 1
    if not counts:
        return ''
    return sorted(counts.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]


def plan_activities(state, min_occurrences=3, reader=None):
    """Groups of situations that are one activity each, with the activity
    body and observations to write and the occurrence ids to delete. A
    calendar id groups occurrences whatever they were called; without one,
    only identical titles group. Groups whose common title normalises alike
    are one activity."""
    groups = defaultdict(list)
    for b in state.get('bodies', []):
        if b.get('kind') != 'situation' or TRIP_RE.match(b['id']):
            continue
        key = series_key(b)
        if not key.startswith('uid:'):
            key = 'title:' + strict_key(b.get('name'))
        groups[key].append(b)
    by_title = defaultdict(list)
    for key, members in groups.items():
        by_title[analyze.normalize_title(common_title(members))].extend(members)
    plans = []
    existing = {b['id'] for b in state.get('bodies', [])}
    for title, members in sorted(by_title.items()):
        if not title or len(members) < min_occurrences:
            continue
        name = common_title(members)
        others = sorted({str(m.get('name') or '').strip() for m in members if str(m.get('name') or '').strip() and str(m.get('name') or '').strip() != name})
        aid = 'activity/' + analyze.slug(title)
        aliases = others + sorted({series_key(m)[4:] for m in members if series_key(m).startswith('uid:')})
        body = {'id': aid, 'name': name}
        if aliases:
            body['aliases'] = aliases[:32]
        obs = []
        starts = []
        location = None
        participants = []
        now = datetime.now(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
        ahead = []
        for m in members:
            attrs = m.get('attrs') or {}
            started = value_of(attrs, 'started')
            ended = value_of(attrs, 'ended')
            if not started and reader is not None:
                #  an occurrence still ahead has its started row in the future, so the
                #  fold hides it; the timeline has it
                code, view = reader('GET', '/body/' + m['id'])
                rows = [o for o in (view or {}).get('observations', []) if isinstance(view, dict)
                        and o.get('status') != 'retracted' and isinstance(o.get('value'), str)]
                for o in rows:
                    if o['attr'] == 'started' and not started:
                        started = o['value']
                    if o['attr'] == 'ended' and not ended:
                        ended = o['value']
            if isinstance(started, str) and analyze.iso_or_none(started):
                at = analyze.iso_or_none(started)
                starts.append(at)
                src = {'kind': 'reconcile', 'id': 'reconcile/' + m['id']}
                #  last: when it happened; a future occurrence's row folds in once
                #  its time has passed, so last keeps itself current
                obs.append({'subject': aid, 'attr': 'last', 'value': at, 'at': at, 'conf': 90, 'source': src})
                if at > now:
                    ahead.append((at, analyze.iso_or_none(ended) if isinstance(ended, str) else None, src))
            loc = value_of(attrs, 'location')
            if isinstance(loc, str) and loc and not location:
                location = loc
            for p in value_of(attrs, 'participants') or []:
                if isinstance(p, dict) and p.get('ref') and p['ref'] not in participants:
                    participants.append(p['ref'])
        if ahead:
            #  next: the nearest occurrence still ahead, one row, gone once it has ended
            at, ended, src = min(ahead)
            row = {'subject': aid, 'attr': 'next', 'value': at, 'at': now, 'conf': 90, 'source': src}
            if ended:
                row['until'] = ended
            obs.append(row)
        first = min(starts) if starts else None
        base_at = first or now
        src = {'kind': 'reconcile', 'id': members[0]['id']}
        obs.insert(0, {'subject': aid, 'attr': 'status', 'value': 'active', 'at': base_at, 'conf': 90, 'source': src})
        if location:
            obs.append({'subject': aid, 'attr': 'location', 'value': location, 'at': base_at, 'conf': 90, 'source': src})
        for p in participants:
            obs.append({'subject': aid, 'attr': 'participants', 'value': {'ref': p}, 'at': base_at, 'conf': 90, 'source': src})
        plans.append({'activity': body, 'exists': aid in existing, 'observations': obs,
                      'delete': sorted(m['id'] for m in members), 'occurrences': len(starts)})
    return plans


def apply_activities(ship, plans):
    for p in plans:
        bodies = [] if p['exists'] else [p['activity']]
        obs = list(p['observations'])
        while bodies or obs:
            code, d = ship.call('POST', '/observe', {'bodies': bodies, 'observations': obs[:200]})
            bodies, obs = [], obs[200:]
            if code != 200:
                print('observe refused for', p['activity']['id'], code, str(d)[:200], file=sys.stderr)
                return False
            for r in (d or {}).get('observations', []):
                if not r.get('ok'):
                    print('row refused:', r.get('error'), file=sys.stderr)
        for bid in p['delete']:
            code, d = ship.call('DELETE', '/body/' + bid)
            if code not in (200, 404):
                print('delete refused for', bid, code, str(d)[:200], file=sys.stderr)
                return False
    return True


# ==  people

PERSON_ATTRS = ('email', 'phone')


def identity_values(body):
    out = set()
    attrs = body.get('attrs') or {}
    for a in PERSON_ATTRS:
        v = value_of(attrs, a)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, str) and x.strip():
                out.add(x.strip().lower())
    return out


def person_named(body):
    """An org whose name reads like a person's name, or a person."""
    if body.get('kind') == 'person':
        return True
    if body.get('kind') != 'org':
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", str(body.get('name') or ''))
    org_words = {'inc', 'llc', 'ltd', 'co', 'corp', 'company', 'bank', 'club', 'church', 'school', 'storage',
                 'support', 'services', 'service', 'group', 'team', 'billing', 'insurance', 'store', 'shop',
                 'market', 'office', 'dept', 'department', 'associates', 'partners', 'clinic', 'center', 'centre'}
    return 2 <= len(words) <= 3 and all(w[0].isupper() for w in words) and not any(w.lower() in org_words for w in words)


def plan_people(state):
    """Merge proposals: (from, into, why), the surer body as into. A person
    body wins over an org; an older body wins over a newer one."""
    people = [b for b in state.get('bodies', []) if person_named(b)]
    proposals = []
    seen = set()
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            why = None
            if identity_values(a) & identity_values(b):
                why = 'same ' + ', '.join(sorted(identity_values(a) & identity_values(b)))
            elif analyze.same_person(a.get('name'), b.get('name')) or \
                    any(analyze.same_person(a.get('name'), x) for x in (b.get('aliases') or [])) or \
                    any(analyze.same_person(b.get('name'), x) for x in (a.get('aliases') or [])):
                why = 'the names match: %s and %s' % (a.get('name'), b.get('name'))
            if not why:
                continue
            into, frm = a, b
            if a.get('kind') != 'person' and b.get('kind') == 'person':
                into, frm = b, a
            elif a.get('kind') == b.get('kind') and str(b.get('created', '')) < str(a.get('created', '')):
                into, frm = b, a
            if frm['id'] == 'person/me':
                into, frm = frm, into
            key = (frm['id'], into['id'])
            if key in seen:
                continue
            seen.add(key)
            proposals.append({'from': frm['id'], 'into': into['id'], 'why': why})
    return proposals


def decided(ship):
    """What the owner already decided about merge pairs: (from, into) to the
    status of the latest merge action about it, done, dismissed, failed or open."""
    code, acts = ship.call('GET', '/actions?status=all')
    out = {}
    if code != 200 or not isinstance(acts, list):
        return out
    seen = defaultdict(set)
    for a in acts:
        if a.get('kind') != 'merge':
            continue
        p = a.get('payload') or {}
        status = a.get('status')
        seen[frozenset((p.get('from'), p.get('into')))].add('open' if status in ('proposed', 'approved', 'claimed') else status)
    for key, statuses in seen.items():
        #  a merge that ran wins; a dismissal sticks over an open re-proposal
        out[key] = 'done' if 'done' in statuses else 'dismissed' if 'dismissed' in statuses else 'open' if 'open' in statuses else 'failed'
    return out


def dismiss_open(ship, pair, why):
    """Dismiss any open merge action about this pair."""
    code, acts = ship.call('GET', '/actions?status=open')
    for a in acts if code == 200 and isinstance(acts, list) else []:
        p = a.get('payload') or {}
        if a.get('kind') == 'merge' and frozenset((p.get('from'), p.get('into'))) == frozenset(pair):
            ship.call('POST', '/actions/' + a['id'], {'status': 'dismissed', 'by': 'reconcile', 'note': why})


def drop_stale(ship, state):
    """Dismiss open merge proposals about a body that no longer exists."""
    ids = {b['id'] for b in state.get('bodies', [])}
    code, acts = ship.call('GET', '/actions?status=open')
    for a in acts if code == 200 and isinstance(acts, list) else []:
        p = a.get('payload') or {}
        if a.get('kind') == 'merge' and (p.get('from') not in ids or p.get('into') not in ids):
            ship.call('POST', '/actions/' + a['id'], {'status': 'dismissed', 'by': 'reconcile', 'note': 'reconcile: a body in this pair is gone'})


def propose_merges(ship, proposals):
    """File a proposal per pair, except pairs the owner dismissed before (left
    alone) and pairs the owner merged before (merged again at once, since a
    reader recreated the duplicate)."""
    past = decided(ship)
    for p in proposals:
        was = past.get(frozenset((p['from'], p['into'])))
        if was == 'dismissed':
            dismiss_open(ship, (p['from'], p['into']), 'reconcile: dismissed before')
            print('skipped (dismissed before)', p['from'], '->', p['into'])
            continue
        if was == 'done':
            code, d = ship.call('POST', '/merge', {'from': p['from'], 'into': p['into']})
            print('merged again (approved before)' if code == 200 else 'merge refused', p['from'], '->', p['into'], '' if code == 200 else str(d)[:120])
            dismiss_open(ship, (p['from'], p['into']), 'reconcile: merged again as approved before')
            continue
        action = {'kind': 'merge', 'title': 'Merge %s into %s' % (p['from'], p['into']),
                  'about': [p['from'], p['into']], 'payload': p, 'by': 'reconcile'}
        code, d = ship.call('POST', '/act', action)
        print('proposed' if code == 200 else 'refused', action['title'], '' if code == 200 else str(d)[:200])


def apply_merges(ship):
    """Run every approved merge action once: claim, merge, report."""
    code, acts = ship.call('GET', '/actions?status=open')
    if code != 200:
        raise SystemExit('cannot list actions')
    for a in acts:
        if a.get('kind') != 'merge' or a.get('status') not in ('approved', 'claimed'):
            continue
        p = a.get('payload') or {}
        code, d = ship.call('POST', '/actions/' + a['id'], {'status': 'claimed', 'by': 'reconcile'})
        if code != 200:
            print('claim refused for', a['title'], str(d)[:120])
            continue
        code, d = ship.call('POST', '/merge', {'from': p.get('from'), 'into': p.get('into')})
        ok = code == 200
        ship.call('POST', '/actions/' + a['id'], {'status': 'done' if ok else 'failed', 'by': 'reconcile',
                                                  'note': '' if ok else str(d)[:400]})
        print('merged' if ok else 'failed', a['title'], '' if ok else str(d)[:200])


# ==  retire: close what is over

def last_word(body, reader):
    """(started, ended, status, latest) for a situation: the folded values,
    the timeline's when the fold hides a future-dated row, and the time of
    the newest observation of any kind."""
    attrs = body.get('attrs') or {}
    started = value_of(attrs, 'started')
    ended = value_of(attrs, 'ended')
    status = value_of(attrs, 'status')
    st = attrs.get('status') if isinstance(attrs.get('status'), dict) else {}
    status_at = st.get('at') if isinstance(st, dict) else None
    latest = None
    if reader is not None:
        code, view = reader('GET', '/body/' + body['id'])
        rows = [o for o in (view or {}).get('observations', []) if isinstance(view, dict) and o.get('status') != 'retracted']
        for o in rows:
            if o['attr'] == 'started' and not started and isinstance(o.get('value'), str):
                started = o['value']
            if o['attr'] == 'ended' and not ended and isinstance(o.get('value'), str):
                ended = o['value']
            if isinstance(o.get('seen'), str) and (latest is None or o['seen'] > latest):
                latest = o['seen']
    return (analyze.iso_or_none(started) if isinstance(started, str) else None,
            analyze.iso_or_none(ended) if isinstance(ended, str) else None,
            status, latest or body.get('created'), analyze.iso_or_none(status_at) if isinstance(status_at, str) else None)


def after(at, status_at):
    """A close time that wins the fold: at, or one second past the live
    status row when that row is later (a reminder mail can say "open" after
    the event ended)."""
    if status_at and status_at >= at:
        return (datetime.fromisoformat(status_at.replace('Z', '+00:00')) + timedelta(seconds=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
    return at


TRIP_DAYS = 7


def trip_end(started):
    return (datetime.fromisoformat(started.replace('Z', '+00:00')) + timedelta(days=TRIP_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')


def plan_retire(state, reader=None, stale_days=30, now=None):
    """The situations to close, each with the time to close it at: one whose
    end has passed closes at its end; one with a start but no end, started
    more than stale_days ago with nothing observed since, closes at its
    newest observation. Activities and open situations still running are
    left alone."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    stale = (datetime.fromisoformat(now.replace('Z', '+00:00')) - timedelta(days=stale_days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    plans = []
    for b in state.get('bodies', []):
        if b.get('kind') != 'situation':
            continue
        started, ended, status, latest, status_at = last_word(b, reader)
        if status == 'closed':
            continue
        if ended and ended < now:
            plans.append({'id': b['id'], 'name': b.get('name', ''), 'at': after(ended, status_at), 'why': 'ended ' + ended})
        elif started and not ended and TRIP_RE.match(b['id']) and trip_end(started) < now:
            #  a trip the mail never gave an end: over a week after it started
            plans.append({'id': b['id'], 'name': b.get('name', ''), 'at': after(trip_end(started), status_at), 'why': 'a trip started ' + started + ' with no end'})
        elif started and started < stale and (not latest or latest < stale):
            plans.append({'id': b['id'], 'name': b.get('name', ''), 'at': after(latest or started, status_at),
                          'why': 'started %s, nothing since %s' % (started, latest or started)})
    return plans


def apply_retire(ship, plans):
    rows = [{'subject': p['id'], 'attr': 'status', 'value': 'closed', 'at': p['at'], 'conf': 90,
             'source': {'kind': 'reconcile', 'id': 'retire/' + p['id']}} for p in plans]
    while rows:
        code, d = ship.call('POST', '/observe', {'bodies': [], 'observations': rows[:200]})
        rows = rows[200:]
        if code != 200:
            print('observe refused', code, str(d)[:200], file=sys.stderr)
            return False
        for r in (d or {}).get('observations', []):
            if not r.get('ok'):
                print('row refused:', r.get('error'), file=sys.stderr)
    return True


def plan_prune(state, reader, days, now=None):
    """Closed situations that ended more than days ago, to delete."""
    now = now or datetime.now(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    cut = (datetime.fromisoformat(now.replace('Z', '+00:00')) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    out = []
    for b in state.get('bodies', []):
        if b.get('kind') != 'situation':
            continue
        started, ended, status, latest, status_at = last_word(b, reader)
        if status == 'closed' and (ended or latest) and (ended or latest) < cut:
            out.append(b['id'])
    return out


# ==  the schema

def ensure_activity_kind(ship, dry):
    code, schema = ship.call('GET', '/schema')
    if code != 200 or not isinstance(schema, dict):
        print('cannot read the schema; the activity kind is advisory, continuing', file=sys.stderr)
        return
    kinds = schema.setdefault('kinds', {})
    if 'activity' in kinds:
        return
    kinds['activity'] = {'attrs': ['status', 'schedule', 'cadence', 'location', 'participants', 'organizer', 'last', 'next']}
    if dry:
        print('would add the activity kind to the schema')
    else:
        code, d = ship.call('PUT', '/schema', schema)
        print('schema:', 'activity kind added' if code == 200 else 'refused %s %s' % (code, str(d)[:100]))


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--ship', help='the ship URL')
    ap.add_argument('--jar', help='a curl cookie jar with the owner cookie')
    ap.add_argument('--state', help='a saved state view instead of a ship (dry run only)')
    ap.add_argument('pass_', choices=['activities', 'people', 'retire'], metavar='PASS')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--apply', action='store_true', help='people: run the approved merges instead of proposing')
    ap.add_argument('--min', type=int, default=3, help='activities: occurrences needed to make an activity')
    ap.add_argument('--stale', type=int, default=30, help='retire: days after which a situation with no end and nothing new is closed')
    ap.add_argument('--prune', type=int, help='retire: also delete closed situations that ended more than this many days ago')
    args = ap.parse_args(argv)
    if args.state:
        with open(args.state) as f:
            ship = Printer(json.load(f))
        args.dry_run = True
    elif args.ship and args.jar:
        ship = Owner(args.ship, args.jar)
    else:
        raise SystemExit('give --ship and --jar, or --state')
    state = ship.state()
    if args.pass_ == 'activities':
        plans = plan_activities(state, args.min, None if args.state else ship.call)
        for p in plans:
            print('%s <- %d situations (%d dated): %s' % (p['activity']['id'], len(p['delete']), p['occurrences'],
                                                         p['activity']['name']))
        print('%d activities, %d situations to delete' % (len(plans), sum(len(p['delete']) for p in plans)))
        if args.dry_run:
            return 0
        ensure_activity_kind(ship, False)
        return 0 if apply_activities(ship, plans) else 1
    if args.pass_ == 'retire':
        reader = None if args.state else ship.call
        plans = plan_retire(state, reader, args.stale)
        for p in plans:
            print('close %s (%s): %s' % (p['id'], p['name'], p['why']))
        print('%d situation(s) to close' % len(plans))
        if not args.dry_run and plans and not apply_retire(ship, plans):
            return 1
        if args.prune:
            gone = plan_prune(state, reader, args.prune)
            print('%d closed situation(s) older than %d days to delete' % (len(gone), args.prune))
            if not args.dry_run:
                for bid in gone:
                    code, d = ship.call('DELETE', '/body/' + bid)
                    if code not in (200, 404):
                        print('delete refused for', bid, code, str(d)[:120], file=sys.stderr)
        return 0
    if args.apply:
        if args.dry_run:
            raise SystemExit('--apply is a real run')
        apply_merges(ship)
        return 0
    proposals = plan_people(state)
    for p in proposals:
        print('merge %s into %s: %s' % (p['from'], p['into'], p['why']))
    print('%d proposal(s)' % len(proposals))
    if not args.dry_run:
        drop_stale(ship, state)
        propose_merges(ship, proposals)
    return 0


if __name__ == '__main__':
    sys.exit(run())
