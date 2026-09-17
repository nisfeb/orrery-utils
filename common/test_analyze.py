"""The analyst, checked without a model: a canned answer is validated into
facts, bad items are dropped with a note, message ids become source
pointers, and a broken answer yields nothing but a note.

    cd common && python3 -m unittest
"""
import json
import unittest

import analyze

CONTEXT = {
    'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': ['me', 'I']},
               {'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']},
               {'id': 'thing/subaru', 'name': 'the Subaru', 'aliases': ['the car']}],
    'attrs': {'person': ['status', 'location'], 'thing': ['status', 'location'],
              'situation': ['status', 'participants', 'location', 'started', 'ended']},
    'me': 'person/me', 'channel': 'chat', 'action_kinds': ['task'],
}
MESSAGES = [
    {'id': 'telegram/1001/10', 'at': '2026-09-16T22:05:00Z', 'who': 'person/me',
     'text': 'car died on route 9, stranded waiting for a tow'},
    {'id': 'telegram/1001/11', 'at': '2026-09-16T23:40:00Z', 'who': 'person/me',
     'text': "tow guy is here, taking it to john's machine shop"},
]
ANSWER = {
    'bodies': [{'id': 'place/johns-machine-shop', 'name': "John's Machine Shop", 'aliases': ["John's", 'the shop']},
               {'id': 'situation/2026-09-16-breakdown', 'name': 'The breakdown'},
               {'id': 'person/sarah', 'name': 'Sarah'},
               {'id': 'Bad Id', 'name': 'x'}],
    'observations': [
        {'subject': 'person/me', 'attr': 'status', 'value': 'stranded, waiting for a tow', 'conf': 90,
         'until': '2026-09-17T02:00:00Z', 'message': 'telegram/1001/10'},
        {'subject': 'thing/subaru', 'attr': 'location', 'value': 'Route 9', 'at': '2026-09-16T22:00:00Z',
         'conf': 85, 'message': 'telegram/1001/10'},
        {'subject': 'thing/subaru', 'attr': 'status', 'value': "being towed to John's Machine Shop",
         'message': 'telegram/1001/11'},
        {'subject': 'situation/2026-09-16-breakdown', 'attr': 'participants', 'value': {'ref': 'person/me'},
         'message': 'telegram/1001/10'},
        {'subject': 'situation/2026-09-16-breakdown', 'attr': 'participants', 'value': {'ref': 'nope'},
         'message': 'telegram/1001/10'},
        {'subject': 'person/nobody', 'attr': 'status', 'value': 'x', 'message': 'telegram/1001/10'},
        {'subject': 'person/me', 'attr': 'Bad Attr', 'value': 'x', 'message': 'telegram/1001/10'},
        {'subject': 'person/me', 'attr': 'mood', 'value': 'tired', 'conf': 'high', 'message': 'not-a-message'},
    ],
    'actions': [
        {'kind': 'task', 'title': 'Call the shop about the Subaru', 'about': ['thing/subaru', 'place/johns-machine-shop', 'org/nobody'],
         'due': '2026-09-17', 'message': 'telegram/1001/11'},
        {'kind': 'message', 'title': 'Tell Sarah', 'message': 'telegram/1001/11'},
        {'kind': 'task', 'title': ''},
    ],
}


class Validation(unittest.TestCase):
    def setUp(self):
        self.facts = analyze.analyze(analyze.FakeModel(json.dumps(ANSWER)), MESSAGES, CONTEXT)

    def test_new_bodies_only(self):
        self.assertEqual([b['id'] for b in self.facts['bodies']],
                         ['place/johns-machine-shop', 'situation/2026-09-16-breakdown'])
        self.assertEqual(self.facts['bodies'][0]['aliases'], ["John's", 'the shop'])
        self.assertIn('dropped body with a bad id: bad id', self.facts['notes'])

    def test_observations(self):
        rows = {(o['subject'], o['attr']): o for o in self.facts['observations']}
        self.assertEqual(len(rows), 5)
        me = rows[('person/me', 'status')]
        self.assertEqual((me['at'], me['until'], me['conf'], me['message']),
                         ('2026-09-16T22:05:00Z', '2026-09-17T02:00:00Z', 90, 'telegram/1001/10'))
        self.assertEqual(rows[('thing/subaru', 'location')]['at'], '2026-09-16T22:00:00Z')
        self.assertEqual(rows[('thing/subaru', 'status')]['conf'], 70)
        self.assertEqual(rows[('thing/subaru', 'status')]['at'], '2026-09-16T23:40:00Z')
        self.assertEqual(rows[('situation/2026-09-16-breakdown', 'participants')]['value'], {'ref': 'person/me'})
        mood = rows[('person/me', 'mood')]
        self.assertEqual((mood['conf'], mood['message'], mood['at']), (70, 'telegram/1001/11', '2026-09-16T23:40:00Z'))
        notes = ' '.join(self.facts['notes'])
        self.assertIn('unknown body: person/nobody', notes)
        self.assertIn('bad attr: bad attr', notes)
        self.assertIn('ref is not a body id: nope', notes)

    def test_actions(self):
        self.assertEqual(len(self.facts['actions']), 1)
        a = self.facts['actions'][0]
        self.assertEqual((a['title'], a['about'], a['due'], a['message']),
                         ('Call the shop about the Subaru', ['thing/subaru', 'place/johns-machine-shop'],
                          '2026-09-17T00:00:00Z', 'telegram/1001/11'))
        self.assertIn('dropped action: Tell Sarah', self.facts['notes'])

    def test_batch_carries_source_pointers(self):
        bodies, observations, actions = analyze.to_batch(self.facts, 'chat')
        self.assertEqual(observations[0]['source'], {'kind': 'chat', 'id': 'telegram/1001/10'})
        self.assertNotIn('message', observations[0])
        self.assertNotIn('message', actions[0])
        self.assertEqual(len(bodies), 2)


class Association(unittest.TestCase):
    """Twins the model makes anyway are folded into the bodies the ship has."""

    def test_normalised_titles(self):
        n = analyze.normalize_title
        self.assertEqual(n('Reminder: Ballet @ Wed Sep 16, 2026 4:45pm'), 'ballet')
        self.assertEqual(n('Ballet'), 'ballet')
        self.assertEqual(n('Adelaide- Ballet/Tap'), 'adelaide ballet/tap')
        self.assertNotEqual(n('Adelaide- Ballet/Tap'), n('Ballet'))
        self.assertEqual(n('Invitation: Confession 2026-09-20'), 'confession')

    def test_same_person(self):
        self.assertTrue(analyze.same_person('Andrea', 'Andrea Egan'))
        self.assertTrue(analyze.same_person('andrea egan', 'Andrea'))
        self.assertFalse(analyze.same_person('Andrea', 'Andrew Egan'))
        self.assertFalse(analyze.same_person('', 'Andrea'))
        self.assertFalse(analyze.same_person('wife', 'jackson wife'))
        self.assertFalse(analyze.same_person('Egan', 'Andrea Egan'))
        self.assertTrue(analyze.same_person('Andrea Egan', 'Andrea O Egan'))

    def test_twins_fold_into_existing_bodies(self):
        context = {'bodies': [{'id': 'person/andrea', 'name': 'Andrea', 'aliases': ['wife']},
                              {'id': 'activity/ballet', 'name': 'Ballet', 'aliases': []},
                              {'id': 'person/me', 'name': 'me', 'aliases': []}],
                   'attrs': {}, 'me': 'person/me', 'channel': 'mail', 'action_kinds': ['task']}
        answer = {'bodies': [{'id': 'person/andrea-egan', 'name': 'Andrea Egan'},
                             {'id': 'situation/ballet-sep-16', 'name': 'Reminder: Ballet @ Sep 16, 4:45pm'},
                             {'id': 'situation/ballet-sep-18', 'name': 'Ballet'},
                             {'id': 'activity/confession', 'name': 'Confession'},
                             {'id': 'situation/confession-2', 'name': 'Reminder: Confession'}],
                  'observations': [{'subject': 'person/andrea-egan', 'attr': 'email', 'value': 'a@x.example', 'message': 'm1'},
                                   {'subject': 'situation/ballet-sep-16', 'attr': 'last', 'value': '2026-09-16T20:45:00Z', 'message': 'm1'},
                                   {'subject': 'situation/confession-2', 'attr': 'last', 'value': '2026-09-19T14:00:00Z', 'message': 'm1'},
                                   {'subject': 'person/me', 'attr': 'spouse', 'value': {'ref': 'person/andrea-egan'}, 'message': 'm1'}],
                  'actions': [{'kind': 'task', 'title': 'Pay Andrea', 'about': ['person/andrea-egan', 'person/andrea'], 'message': 'm1'}]}
        facts = analyze.validate(answer, [{'id': 'm1', 'at': '2026-09-16T12:00:00Z', 'who': 'x', 'text': ''}], context)
        self.assertEqual([b['id'] for b in facts['bodies']], ['activity/confession'])
        subjects = [(o['subject'], o['attr']) for o in facts['observations']]
        self.assertEqual(subjects, [('person/andrea', 'email'), ('activity/ballet', 'last'),
                                    ('activity/confession', 'last'), ('person/me', 'spouse')])
        self.assertEqual(facts['observations'][3]['value'], {'ref': 'person/andrea'})
        self.assertEqual(facts['actions'][0]['about'], ['person/andrea'])
        self.assertIn('person/andrea-egan is person/andrea', facts['notes'])
        self.assertIn('situation/ballet-sep-18 is activity/ballet', facts['notes'])


class Answers(unittest.TestCase):
    def test_fenced_json_is_read(self):
        facts = analyze.analyze(analyze.FakeModel('Sure.\n```json\n' + json.dumps(ANSWER) + '\n```'), MESSAGES, CONTEXT)
        self.assertEqual(len(facts['observations']), 5)

    def test_broken_answer_is_a_note(self):
        facts = analyze.analyze(analyze.FakeModel('I cannot help with that.'), MESSAGES, CONTEXT)
        self.assertEqual((facts['bodies'], facts['observations'], facts['actions']), ([], [], []))
        self.assertTrue(facts['notes'][0].startswith('model: '))

    def test_no_messages_no_call(self):
        m = analyze.FakeModel('{}')
        self.assertEqual(analyze.analyze(m, [], CONTEXT)['notes'], [])
        self.assertEqual(m.asked, [])

    def test_prompt_names_the_bodies_and_the_messages(self):
        m = analyze.FakeModel('{}')
        analyze.analyze(m, MESSAGES, CONTEXT)
        self.assertIn('person/sarah | Sarah | wife', m.asked[0])
        self.assertIn('--- message telegram/1001/11 | 2026-09-16T23:40:00Z | from person/me', m.asked[0])

    def test_context_from_state(self):
        state = {'me': 'person/me', 'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': ['I']}],
                 'schema': {'kinds': {'person': {'attrs': ['status']}}}}
        c = analyze.context_from_state(state, 'mail')
        self.assertEqual(c['bodies'], [{'id': 'person/me', 'name': 'me', 'aliases': ['I']}])
        self.assertEqual(c['attrs'], {'person': ['status']})
        self.assertEqual((c['channel'], c['action_kinds']), ('mail', ['task']))


if __name__ == '__main__':
    unittest.main()
