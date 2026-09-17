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
    sit('situation/' + UID, 'Kid- Ballet/Tap', '2026-09-02T20:45:00Z', '2026-09-02T22:45:00Z', 'The dance school', ['person/me']),
    sit('situation/' + UID + '-1', 'Kid- Ballet/Tap', '2026-09-09T20:45:00Z', '2026-09-09T22:45:00Z'),
    sit('situation/' + UID + '-2', 'Kid- Ballet/Tap'),
    sit('situation/' + UID + '-3', 'Reminder: Kid- Ballet/Tap'),
    sit('situation/' + UID2, 'Ballet', '2026-08-26T20:45:00Z', '2026-08-26T22:45:00Z'),
    sit('situation/' + UID2 + '-1', 'Ballet'),
    sit('situation/' + UID2 + '-2', 'Ballet'),
    sit('situation/plain-1', 'Ballet', '2026-09-16T20:45:00Z'),
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
        self.assertEqual(sorted(self.plans), ['activity/ballet', 'activity/kid-ballet-tap'])
        ballet = self.plans['activity/ballet']
        self.assertEqual(ballet['delete'], sorted(['situation/' + UID2, 'situation/' + UID2 + '-1', 'situation/' + UID2 + '-2', 'situation/plain-1']))
        self.assertEqual(ballet['occurrences'], 2)
        self.assertIn(UID2, ballet['activity']['aliases'])

    def test_activity_rows(self):
        kid = self.plans['activity/kid-ballet-tap']
        self.assertEqual(kid['activity']['name'], 'Kid- Ballet/Tap')
        self.assertIn('Reminder: Kid- Ballet/Tap', kid['activity']['aliases'])
        rows = {(o['attr'], o.get('value') if not isinstance(o.get('value'), dict) else o['value']['ref']): o for o in kid['observations']}
        self.assertEqual(rows[('status', 'active')]['at'], '2026-09-02T20:45:00Z')
        self.assertEqual(rows[('location', 'The dance school')]['source'], {'kind': 'reconcile', 'id': 'situation/' + UID})
        self.assertIn(('participants', 'person/me'), rows)
        last = [o for o in kid['observations'] if o['attr'] == 'last']
        self.assertEqual([(o['value'], o['at'], 'until' in o, o['source']['id']) for o in last],
                         [('2026-09-02T20:45:00Z', '2026-09-02T20:45:00Z', False, 'reconcile/situation/' + UID),
                          ('2026-09-09T20:45:00Z', '2026-09-09T20:45:00Z', False, 'reconcile/situation/' + UID + '-1')])
        self.assertEqual([o for o in kid['observations'] if o['attr'] == 'next'], [])
        self.assertEqual(len(kid['delete']), 4)

    def test_a_future_occurrence_is_next(self):
        state = {'bodies': [sit('situation/x-' + str(i), 'Choir', '2099-01-0%dT18:00:00Z' % (i + 1), '2099-01-0%dT19:00:00Z' % (i + 1)) for i in range(3)]}
        plan = reconcile.plan_activities(state)[0]
        nxt = [o for o in plan['observations'] if o['attr'] == 'next']
        self.assertEqual(len(nxt), 1)
        self.assertEqual((nxt[0]['value'], nxt[0]['until']), ('2099-01-01T18:00:00Z', '2099-01-01T19:00:00Z'))
        self.assertEqual(len([o for o in plan['observations'] if o['attr'] == 'last']), 3)

    def test_one_offs_and_trips_stay(self):
        deleted = {d for p in self.plans.values() for d in p['delete']}
        for keep in ('situation/2026-09-02-trip', 'situation/meeting-a', 'situation/2026-09-16-breakdown'):
            self.assertNotIn(keep, deleted)

    def test_min_occurrences(self):
        plans = {p['activity']['id'] for p in reconcile.plan_activities(STATE, 2)}
        self.assertIn('activity/parent-meeting', plans)


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
