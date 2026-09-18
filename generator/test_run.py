"""The generator, checked without a ship or a model: the prompt it builds,
the phase it reads off a situation's times, and what it keeps and drops of
a model's answer.

    cd generator && python3 -m unittest
"""
import json
import unittest

import run

NOW = '2026-09-18T12:00:00Z'
STATE = {
    'me': 'person/me', 'rev': 1,
    'bodies': [
        {'id': 'person/me', 'kind': 'person', 'name': 'owner', 'aliases': [], 'attrs': {'timezone': {'value': 'America/New_York'}}},
        {'id': 'person/alice', 'kind': 'person', 'name': 'Alice', 'aliases': ['wife'], 'attrs': {'status': {'value': 'on jury duty'}}},
        {'id': 'thing/subaru', 'kind': 'thing', 'name': 'the Subaru', 'aliases': [], 'attrs': {'status': {'value': 'at the shop, awaiting diagnosis'}, 'location': {'value': {'ref': 'place/johns-machine-shop'}}}},
        {'id': 'place/johns-machine-shop', 'kind': 'place', 'name': "John's Machine Shop", 'aliases': [], 'attrs': {}},
        {'id': 'situation/2026-09-16-breakdown', 'kind': 'situation', 'name': 'The breakdown', 'aliases': [], 'attrs': {'started': {'value': '2026-09-16T22:00:00Z'}, 'participants': [{'value': {'ref': 'person/me'}}]}},
        {'id': 'situation/2026-12-05-meeting', 'kind': 'situation', 'name': 'Parent meeting', 'aliases': [], 'attrs': {'starts': {'value': '2026-12-05T19:00:00Z'}, 'ends': {'value': '2026-12-05T20:00:00Z'}}},
        {'id': 'situation/2026-09-10-walk', 'kind': 'situation', 'name': 'Walk', 'aliases': [], 'attrs': {'status': {'value': 'closed'}, 'ended': {'value': '2026-09-10T15:00:00Z'}}},
        {'id': 'activity/ballet', 'kind': 'activity', 'name': 'Ballet', 'aliases': [], 'attrs': {'last': {'value': '2026-09-16T20:45:00Z'}, 'next': {'value': '2026-09-18T20:45:00Z'}}},
    ],
    'situations': ['situation/2026-09-16-breakdown', 'situation/2026-12-05-meeting'],
    'actions': [{'id': 'a1', 'kind': 'task', 'title': 'Call the shop about the Subaru', 'about': ['thing/subaru'], 'status': 'approved'}],
    'schema': {'kinds': {}, 'actions': ['task', 'note', 'message', 'home'],
               'payloads': {'message': {'via': 'required: telegram', 'to': 'required: a body id', 'text': 'required'},
                            'home': {'service': 'required', 'entity_id': 'required', 'data': 'optional'}}},
}
DECIDED = [{'kind': 'task', 'title': 'Pay Utility Co $142.50', 'status': 'done'},
           {'kind': 'message', 'title': 'Tell Alice the car is at the shop', 'status': 'dismissed'}]


class Prompt(unittest.TestCase):
    def test_phase(self):
        p = {b['id']: run.phase(b, NOW) for b in STATE['bodies'] if b['kind'] == 'situation'}
        self.assertEqual(p, {'situation/2026-09-16-breakdown': 'under way', 'situation/2026-12-05-meeting': 'upcoming', 'situation/2026-09-10-walk': 'closed'})

    def test_build(self):
        text = run.build(STATE, DECIDED, NOW, 'America/New_York', 5)
        self.assertIn('Payload shapes:', text)
        self.assertIn('"via": "required: telegram"', text)
        self.assertIn('situation/2026-12-05-meeting | Parent meeting | upcoming', text)
        self.assertNotIn('situation/2026-09-10-walk', text)
        self.assertIn('activities:\n  activity/ballet | Ballet | last=2026-09-16T20:45:00Z; next=2026-09-18T20:45:00Z', text)
        self.assertIn('location=place/johns-machine-shop', text)
        self.assertIn('Open actions', text)
        self.assertIn('task | Call the shop about the Subaru', text)
        self.assertIn('dismissed | message | Tell Alice the car is at the shop', text)
        self.assertIn('timezone America/New_York', text)


class Answers(unittest.TestCase):
    def test_validation(self):
        answer = {'actions': [
            {'kind': 'task', 'title': 'Ask the shop for a diagnosis estimate', 'about': ['thing/subaru', 'place/johns-machine-shop'], 'due': '2026-09-19T13:00:00Z', 'why': 'the car has sat two days'},
            {'kind': 'task', 'title': 'Call the shop about the Subaru', 'about': ['thing/subaru']},
            {'kind': 'message', 'title': 'Tell Alice the car is at the shop', 'payload': {'via': 'telegram', 'to': 'person/alice', 'text': 'x'}},
            {'kind': 'message', 'title': 'Wish Alice luck at jury duty', 'about': ['person/alice'], 'payload': {'via': 'telegram', 'to': 'person/alice', 'text': 'Good luck today'}},
            {'kind': 'message', 'title': 'Ping the mechanic', 'payload': {'to': 'person/mechanic'}},
            {'kind': 'email', 'title': 'Email the shop'},
            {'kind': 'task', 'title': 'Buy a new car', 'about': ['thing/tesla']},
            {'kind': 'home', 'title': 'Porch light on', 'payload': {'service': 'light.turn_on', 'entity_id': 'light.porch'}},
        ], 'notes': ['the breakdown situation has no ended']}
        out, notes = run.validate(answer, STATE, DECIDED, 5)
        self.assertEqual([a['title'] for a in out], ['Ask the shop for a diagnosis estimate', 'Wish Alice luck at jury duty', 'Porch light on'])
        self.assertEqual(out[0]['payload'], {'why': 'the car has sat two days'})
        self.assertEqual(out[0]['due'], '2026-09-19T13:00:00Z')
        self.assertEqual(out[1]['payload']['to'], 'person/alice')
        joined = ' '.join(notes)
        self.assertIn('already open or decided: Call the shop about the Subaru', joined)
        self.assertIn('already open or decided: Tell Alice the car is at the shop', joined)
        self.assertIn('payload lacks via, text', joined)
        self.assertIn('kind email', joined)
        self.assertIn('thing/tesla', joined)
        self.assertIn('model note: the breakdown situation has no ended', joined)

    def test_limit(self):
        answer = {'actions': [{'kind': 'task', 'title': 'Task %d' % i} for i in range(9)]}
        out, notes = run.validate(answer, STATE, [], 3)
        self.assertEqual(len(out), 3)

    def test_same_title(self):
        self.assertTrue(run.same_title('Call the shop about the Subaru', 'call the shop about the subaru.'))
        self.assertTrue(run.same_title('Call John\'s shop about the Subaru', 'Call the shop about the Subaru'))
        self.assertFalse(run.same_title('Pay the electricity bill', 'Call the shop about the Subaru'))


if __name__ == '__main__':
    unittest.main()
