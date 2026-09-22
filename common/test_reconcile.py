"""The reconcile passes, checked on a made-up state: calendar series and
identical titles become one activity each with an observation per dated
occurrence, one-off situations and trips are left alone, and people who
are one person become a merge proposal with the person body as the target.

    cd common && python3 -m unittest test_reconcile
"""
import unittest

import reconcile


def sit(bid, name, started=None, ended=None, location=None, participants=()):
    attrs = {}
    if started:
        attrs['started'] = {'value': started}
    if ended:
        attrs['ended'] = {'value': ended}
    if location:
        attrs['location'] = {'value': location}
    if participants:
        attrs['participants'] = [{'value': {'ref': p}} for p in participants]
    return {'id': bid, 'kind': 'situation', 'name': name, 'aliases': [], 'attrs': attrs, 'created': '2026-09-01T00:00:00Z'}


UID = 'cal-c-0w1-94k9d-4787c8e7-9b34-44bf-a5e3-0b3ba3be492a'
UID2 = 'cal-c-0w1-94k9d-b014f34a-4f82-43bb-b54d-5c2228e22560'
STATE = {'me': 'person/me', 'bodies': [
    {'id': 'person/me', 'kind': 'person', 'name': 'me', 'aliases': ['I'], 'attrs': {}, 'created': '2026-01-01T00:00:00Z'},
    {'id': 'person/dana', 'kind': 'person', 'name': 'Dana', 'aliases': ['wife'], 'attrs': {'email': {'value': 'dana@example.com'}}, 'created': '2026-01-02T00:00:00Z'},
    {'id': 'org/dana-quill', 'kind': 'org', 'name': 'Dana Quill', 'aliases': [], 'attrs': {}, 'created': '2026-09-10T00:00:00Z'},
    {'id': 'person/dq', 'kind': 'person', 'name': 'D. Quill', 'aliases': [], 'attrs': {'email': {'value': 'Dana@Example.com'}}, 'created': '2026-09-11T00:00:00Z'},
    {'id': 'org/state-farm', 'kind': 'org', 'name': 'State Farm', 'aliases': [], 'attrs': {}, 'created': '2026-09-10T00:00:00Z'},
    {'id': 'org/kim-lee', 'kind': 'org', 'name': 'Kim Lee', 'aliases': [], 'attrs': {}, 'created': '2026-09-10T00:00:00Z'},
    sit('situation/' + UID, 'Nora- Pottery/Wheel', '2026-05-12T22:00:00Z', '2026-05-12T23:30:00Z', 'The studio', ['person/me']),
    sit('situation/' + UID + '-1', 'Nora- Pottery/Wheel', '2026-05-19T22:00:00Z', '2026-05-19T23:30:00Z'),
    sit('situation/' + UID + '-2', 'Nora- Pottery/Wheel'),
    sit('situation/' + UID + '-3', 'Reminder: Nora- Pottery/Wheel'),
    sit('situation/' + UID2, 'Pottery', '2026-05-05T22:00:00Z', '2026-05-05T23:30:00Z'),
    sit('situation/' + UID2 + '-1', 'Pottery'),
    sit('situation/' + UID2 + '-2', 'Pottery'),
    sit('situation/plain-1', 'Pottery', '2026-05-26T22:00:00Z'),
    sit('situation/2026-09-02-trip', 'Trip starting 2026-09-02', '2026-09-02T00:00:00Z'),
    sit('situation/2026-09-04-trip', 'Trip starting 2026-09-04', '2026-09-04T00:00:00Z'),
    sit('situation/2026-09-06-trip', 'Trip starting 2026-09-06', '2026-09-06T00:00:00Z'),
    sit('situation/meeting-a', 'Parent meeting'),
    sit('situation/meeting-b', 'Parent meeting'),
    sit('situation/2026-09-16-breakdown', 'The breakdown', '2026-09-16T22:00:00Z'),
], 'situations': [], 'actions': [], 'schema': {'kinds': {}}}


class Activities(unittest.TestCase):
    def setUp(self):
        self.plans = {p['activity']['id']: p for p in reconcile.plan_activities(STATE)}

    def test_two_series_from_calendar_ids_and_one_plain_title(self):
        self.assertEqual(sorted(self.plans), ['activity/nora-pottery-wheel', 'activity/pottery'])
        pottery = self.plans['activity/pottery']
        self.assertEqual(pottery['delete'], sorted(['situation/' + UID2, 'situation/' + UID2 + '-1', 'situation/' + UID2 + '-2', 'situation/plain-1']))
        self.assertEqual(pottery['occurrences'], 2)
        self.assertIn(UID2, pottery['activity']['aliases'])

    def test_activity_rows(self):
        nora = self.plans['activity/nora-pottery-wheel']
        self.assertEqual(nora['activity']['name'], 'Nora- Pottery/Wheel')
        self.assertIn('Reminder: Nora- Pottery/Wheel', nora['activity']['aliases'])
        rows = {(o['attr'], o.get('value') if not isinstance(o.get('value'), dict) else o['value']['ref']): o for o in nora['observations']}
        self.assertEqual(rows[('status', 'active')]['at'], '2026-05-12T22:00:00Z')
        self.assertEqual(rows[('location', 'The studio')]['source'], {'kind': 'reconcile', 'id': 'situation/' + UID})
        self.assertIn(('participants', 'person/me'), rows)
        last = [o for o in nora['observations'] if o['attr'] == 'last']
        self.assertEqual([(o['value'], o['at'], 'until' in o, o['source']['id']) for o in last],
                         [('2026-05-12T22:00:00Z', '2026-05-12T22:00:00Z', False, 'reconcile/situation/' + UID),
                          ('2026-05-19T22:00:00Z', '2026-05-19T22:00:00Z', False, 'reconcile/situation/' + UID + '-1')])
        self.assertEqual([o for o in nora['observations'] if o['attr'] == 'next'], [])
        self.assertEqual(len(nora['delete']), 4)

    def test_a_future_occurrence_is_next(self):
        state = {'bodies': [sit('situation/x-' + str(i), 'Choir', '2099-01-0%dT18:00:00Z' % (i + 1), '2099-01-0%dT19:00:00Z' % (i + 1)) for i in range(3)]}
        plan = reconcile.plan_activities(state)[0]
        nxt = [o for o in plan['observations'] if o['attr'] == 'next']
        self.assertEqual(len(nxt), 1)
        self.assertEqual((nxt[0]['value'], nxt[0]['until']), ('2099-01-01T18:00:00Z', '2099-01-01T19:00:00Z'))
        bare = {'bodies': [sit('situation/y-' + str(i), 'Choir', '2099-01-0%dT18:00:00Z' % (i + 1)) for i in range(3)]}
        n2 = [o for o in reconcile.plan_activities(bare)[0]['observations'] if o['attr'] == 'next']
        self.assertEqual(n2[0]['until'], '2099-01-02T18:00:00Z')
        self.assertEqual(len([o for o in plan['observations'] if o['attr'] == 'last']), 3)

    def test_one_offs_and_trips_stay(self):
        deleted = {d for p in self.plans.values() for d in p['delete']}
        for keep in ('situation/2026-09-02-trip', 'situation/meeting-a', 'situation/2026-09-16-breakdown'):
            self.assertNotIn(keep, deleted)

    def test_min_occurrences(self):
        plans = {p['activity']['id'] for p in reconcile.plan_activities(STATE, 2)}
        self.assertIn('activity/parent-meeting', plans)


class Decisions(unittest.TestCase):
    class Ship:
        def __init__(self):
            self.calls = []

        def call(self, method, path, body=None):
            self.calls.append((method, path, body))
            if path.startswith('/actions?'):
                acts = [
                    {'id': 'm1', 'kind': 'merge', 'status': 'dismissed', 'payload': {'from': 'person/b', 'into': 'person/a'}},
                    {'id': 'm2', 'kind': 'merge', 'status': 'done', 'payload': {'from': 'org/c', 'into': 'person/c'}},
                    {'id': 'm3', 'kind': 'merge', 'status': 'proposed', 'payload': {'from': 'person/d', 'into': 'person/e'}},
                    {'id': 't1', 'kind': 'task', 'status': 'done', 'payload': {}},
                ]
                return 200, [a for a in acts if a['status'] == 'proposed'] if 'status=open' in path else acts
            return 200, {'ok': True, 'id': 'x', 'status': 'proposed'}

    def test_remembered(self):
        ship = self.Ship()
        reconcile.propose_merges(ship, [
            {'from': 'person/a', 'into': 'person/b', 'why': 'x'},
            {'from': 'org/c', 'into': 'person/c', 'why': 'x'},
            {'from': 'person/d', 'into': 'person/e', 'why': 'x'},
            {'from': 'person/f', 'into': 'person/g', 'why': 'x'},
        ])
        posts = [(path, body) for m, path, body in ship.calls if m == 'POST']
        self.assertEqual(posts[0], ('/merge', {'from': 'org/c', 'into': 'person/c'}))
        titles = [b['title'] for path, b in posts if path == '/act']
        self.assertEqual(titles, ['Merge person/d into person/e', 'Merge person/f into person/g'])


class Participants(unittest.TestCase):
    def test_names_in_titles(self):
        n = reconcile.names_in
        self.assertEqual(n('Mira- Ballet/Tap'), (['Mira'], None))
        self.assertEqual(n('Theo and Juno- Opti Sail'), (['Theo', 'Juno'], None))
        self.assertEqual(n('Felix Birthday'), (['Felix'], None))
        self.assertEqual(n("Otto's birthday party"), (['Otto'], None))
        self.assertEqual(n('Felix Fencing Lesson'), ([], 'Felix'))
        self.assertEqual(n('Swan Lake rehearsal'), ([], 'Swan'))
        self.assertEqual(n('Ballet'), ([], 'Ballet'))
        self.assertEqual(n('gym'), ([], None))

    def test_people_out_of_titles(self):
        def act(bid, name, parts=()):
            b = sit(bid, name, participants=parts)
            b['kind'] = 'activity' if bid.startswith('activity/') else 'situation'
            return b
        state = {'bodies': [
            {'id': 'person/me', 'kind': 'person', 'name': 'dana', 'aliases': ['I'], 'attrs': {}, 'created': '2026-01-01T00:00:00Z'},
            {'id': 'person/juno', 'kind': 'person', 'name': 'Juno Quill', 'aliases': [], 'attrs': {}, 'created': '2026-01-01T00:00:00Z'},
            act('activity/mira-ballet-tap', 'Mira- Ballet/Tap', ['person/me']),
            act('activity/theo-and-juno-opti-sail', 'Theo and Juno- Opti Sail'),
            act('activity/felix-fencing-lesson', 'Felix Fencing Lesson'),
            act('situation/felix-birthday', 'Felix Birthday'),
            act('activity/swan-lake-rehearsal', 'Swan Lake rehearsal'),
            act('situation/dana-team-offsite', 'Dana Team Offsite'),
        ]}
        plan = reconcile.plan_participants(state)
        self.assertEqual([c['id'] for c in plan['creates']], ['person/felix', 'person/mira', 'person/theo'])
        rows = {(r['subject'], r['value']['ref']) for r in plan['rows']}
        self.assertEqual(rows, {('activity/mira-ballet-tap', 'person/mira'),
                                ('activity/theo-and-juno-opti-sail', 'person/theo'),
                                ('activity/theo-and-juno-opti-sail', 'person/juno'),
                                ('activity/felix-fencing-lesson', 'person/felix'),
                                ('situation/felix-birthday', 'person/felix'),
                                ('situation/dana-team-offsite', 'person/me')})
        self.assertEqual(plan['unsure'], ['Swan'])


class Times(unittest.TestCase):
    def test_future_facts_become_schedule(self):
        state = {'bodies': [{'id': 'situation/m', 'kind': 'situation', 'name': 'Meeting', 'aliases': [], 'attrs': {}, 'created': '2026-09-01T00:00:00Z'}]}

        def reader(method, path):
            return 200, {'observations': [
                {'id': 'o1', 'attr': 'ended', 'value': '2026-12-05T20:00:00Z', 'at': '2026-09-10T00:00:00Z', 'conf': 80, 'status': 'live'},
                {'id': 'o2', 'attr': 'started', 'value': '2026-09-01T10:00:00Z', 'at': '2026-09-01T10:00:00Z', 'conf': 90, 'status': 'live'},
                {'id': 'o3', 'attr': 'status', 'value': 'under way', 'at': '2026-09-10T00:00:00Z', 'status': 'live'},
                {'id': 'o4', 'attr': 'status', 'value': 'open', 'at': '2026-09-02T00:00:00Z', 'status': 'live'},
                {'id': 'o5', 'attr': 'starts', 'value': '2026-12-05T18:00:00Z', 'at': '2026-12-05T18:00:00Z', 'conf': 80, 'status': 'live'}]}
        retract, write = reconcile.plan_times(state, reader, '2026-09-18T12:00:00Z')
        self.assertEqual([r[0] for r in retract], ['o1', 'o3', 'o5'])
        self.assertEqual([(w['attr'], w['value'], w['at']) for w in write], [('ends', '2026-12-05T20:00:00Z', '2026-09-10T00:00:00Z'), ('starts', '2026-12-05T18:00:00Z', '2026-09-18T12:00:00Z')])

    def test_a_stale_next_is_replaced(self):
        state = {'bodies': [{'id': 'activity/gym', 'kind': 'activity', 'name': 'Gym', 'aliases': [], 'attrs': {}, 'created': '2026-09-01T00:00:00Z'}]}

        def reader(method, path):
            return 200, {'observations': [
                {'id': 'n1', 'attr': 'next', 'value': '2026-09-18T13:00:00Z', 'at': '2026-09-17T00:00:00Z', 'status': 'live'},
                {'id': 'l1', 'attr': 'last', 'value': '2026-09-18T13:00:00Z', 'at': '2026-09-18T13:00:00Z', 'status': 'live'},
                {'id': 'l2', 'attr': 'last', 'value': '2026-09-25T13:00:00Z', 'at': '2026-09-25T13:00:00Z', 'status': 'live'}]}
        retract, write = reconcile.plan_times(state, reader, '2026-09-18T14:00:00Z')
        self.assertEqual([r[0] for r in retract], ['n1'])
        self.assertEqual([(w['attr'], w['value'], w['until']) for w in write], [('next', '2026-09-25T13:00:00Z', '2026-09-26T13:00:00Z')])

    def test_retire_reads_the_schedule(self):
        b = sit('situation/past', 'Past thing')
        b['attrs']['ends'] = {'value': '2026-09-10T15:00:00Z'}
        plans = reconcile.plan_retire({'bodies': [b]}, None, 30, '2026-09-18T12:00:00Z')
        self.assertEqual([(p['id'], p['at']) for p in plans], [('situation/past', '2026-09-10T15:00:00Z')])



    NOW = '2026-09-18T12:00:00Z'

    def test_over_and_stale_close_and_the_rest_stay(self):
        state = {'bodies': [
            sit('situation/walk', 'Walk', '2026-09-10T14:00:00Z', '2026-09-10T15:00:00Z'),
            sit('situation/preop', 'PreOp appointment', '2026-09-01T13:00:00Z', '2026-09-01T14:00:00Z'),
            sit('situation/dinner', 'Dinner', '2026-09-19T23:00:00Z', '2026-09-20T01:00:00Z'),
            sit('situation/old-open', 'An old thing', '2026-07-01T00:00:00Z'),
            sit('situation/long-project', 'A long project', '2026-07-01T00:00:00Z', '2026-10-15T00:00:00Z'),
            sit('situation/fresh-open', 'A fresh thing', '2026-09-16T22:00:00Z'),
            {'id': 'situation/done', 'kind': 'situation', 'name': 'Done', 'aliases': [], 'created': '2026-08-01T00:00:00Z',
             'attrs': {'status': {'value': 'closed'}, 'ended': {'value': '2026-08-02T00:00:00Z'}}},
            {'id': 'activity/gym', 'kind': 'activity', 'name': 'Gym', 'aliases': [], 'created': '2026-08-01T00:00:00Z',
             'attrs': {'last': {'value': '2026-09-01T00:00:00Z'}}},
        ]}
        for b in state['bodies']:
            b['created'] = b['id'] in ('situation/old-open', 'situation/long-project') and '2026-07-01T00:00:00Z' or b.get('created', '2026-09-01T00:00:00Z')
        plans = {p['id']: p for p in reconcile.plan_retire(state, None, 30, self.NOW)}
        self.assertEqual(sorted(plans), ['situation/old-open', 'situation/preop', 'situation/walk'])
        self.assertEqual(plans['situation/walk']['at'], '2026-09-10T15:00:00Z')
        self.assertEqual(plans['situation/old-open']['at'], '2026-07-01T00:00:00Z')
        self.assertTrue(plans['situation/old-open']['why'].startswith('started 2026-07-01'))
        self.assertEqual(reconcile.plan_prune(state, None, 30, self.NOW), ['situation/done'])
        self.assertEqual(reconcile.plan_prune(state, None, 60, self.NOW), [])

    def test_trips_close_a_week_after_they_start(self):
        state = {'bodies': [sit('situation/2026-09-02-trip', 'Trip starting 2026-09-02', '2026-09-02T00:00:00Z'),
                            sit('situation/2026-09-14-trip', 'Trip starting 2026-09-14', '2026-09-14T00:00:00Z')]}
        plans = {p['id']: p for p in reconcile.plan_retire(state, None, 30, self.NOW)}
        self.assertEqual(list(plans), ['situation/2026-09-02-trip'])
        self.assertEqual(plans['situation/2026-09-02-trip']['at'], '2026-09-09T00:00:00Z')

    def test_a_close_lands_after_a_later_open_row(self):
        b = sit('situation/2026-01-01-trip', 'Trip starting 2026-01-01', '2026-01-01T00:00:00Z')
        b['attrs']['status'] = {'value': 'open', 'at': '2026-09-10T12:00:00Z'}
        plans = reconcile.plan_retire({'bodies': [b]}, None, 30, self.NOW)
        self.assertEqual(plans[0]['at'], '2026-09-10T12:00:01Z')

    def test_timeline_supplies_a_hidden_end(self):
        state = {'bodies': [sit('situation/x', 'X')]}
        state['bodies'][0]['created'] = '2026-09-01T00:00:00Z'

        def reader(method, path):
            return 200, {'observations': [{'attr': 'ended', 'value': '2026-09-15T10:00:00Z', 'status': 'live', 'seen': '2026-09-02T00:00:00Z'},
                                          {'attr': 'started', 'value': '2026-09-15T09:00:00Z', 'status': 'live', 'seen': '2026-09-02T00:00:00Z'}]}
        plans = reconcile.plan_retire(state, reader, 30, self.NOW)
        self.assertEqual([(p['id'], p['at']) for p in plans], [('situation/x', '2026-09-15T10:00:00Z')])


class People(unittest.TestCase):
    def test_proposals(self):
        props = {(p['from'], p['into']): p['why'] for p in reconcile.plan_people(STATE)}
        self.assertIn(('org/dana-quill', 'person/dana'), props)
        self.assertIn(('person/dq', 'person/dana'), props)
        self.assertTrue(props[('person/dq', 'person/dana')].startswith('same dana@example.com'))
        froms = {f for f, _ in props}
        self.assertNotIn('org/state-farm', froms)
        self.assertNotIn('org/kim-lee', froms)
        self.assertNotIn('person/me', froms)


if __name__ == '__main__':
    unittest.main()
