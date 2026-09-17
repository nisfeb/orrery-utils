"""The mapping, checked without a ship: every fixture in fixtures/ produces
exactly the batch fixtures/expected.json records for it, and the rule that
needs the ship behaves with a stub.

    cd mail && python3 -m unittest
"""
import json
import os
import unittest

import reader

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')


def facts_for(name, ship=None):
    with open(os.path.join(FIX, name + '.eml'), 'rb') as f:
        msg = reader.parse(f.read())
    return reader.classify(msg, ship or reader.NoShip())


class FakeShip(reader.NoShip):
    """A ship that knows Sarah with an old address."""

    def resolve(self, q):
        return [{'id': 'person/sarah', 'kind': 'person', 'name': 'Sarah'}] if q.lower() == 'sarah' else []

    def body(self, bid):
        return {'id': bid, 'attrs': {'email': {'value': 'sarah@oldmail.example'}}}


class Mapping(unittest.TestCase):
    def test_every_fixture_matches_expected(self):
        with open(os.path.join(FIX, 'expected.json')) as f:
            expected = json.load(f)
        for name, want in expected.items():
            with self.subTest(fixture=name):
                self.assertEqual(facts_for(name).as_json(), want)

    def test_skips_say_why(self):
        self.assertIn('calendar', facts_for('invitation').notes[0])
        self.assertIn('bulk', facts_for('newsletter').notes[0])
        self.assertIn('needs the ship', facts_for('personal').notes[0])

    def test_known_person_new_address(self):
        facts = facts_for('personal', FakeShip())
        self.assertEqual(len(facts.observations), 1)
        o = facts.observations[0]
        self.assertEqual((o['subject'], o['attr'], o['value'], o['conf']),
                         ('person/sarah', 'email', 'sarah@newmail.example', 80))
        self.assertEqual(o['source'], {'kind': 'mail', 'id': '<p1@newmail.example>'})

    def test_known_person_same_address_is_silent(self):
        class Same(FakeShip):
            def body(self, bid):
                return {'id': bid, 'attrs': {'email': {'value': 'sarah@newmail.example'}}}
        self.assertTrue(facts_for('personal', Same()).empty())

    def test_dates(self):
        base = reader.parse(b'Date: Wed, 17 Sep 2026 14:02:00 +0000\n\nx').date
        d = reader.find_date
        self.assertEqual(d('arriving Friday, September 19', base).strftime('%Y-%m-%d'), '2026-09-19')
        self.assertEqual(d('due January 5', base).strftime('%Y-%m-%d'), '2027-01-05')
        self.assertEqual(d('on 2026-10-03', base).strftime('%Y-%m-%d'), '2026-10-03')
        self.assertEqual(d('by 9/30/2026', base).strftime('%Y-%m-%d'), '2026-09-30')
        self.assertIsNone(d('February 30, 2026', base))
        self.assertIsNone(d('no date here', base))

    def test_replay_is_the_same_batch(self):
        self.assertEqual(facts_for('shipped').as_json(), facts_for('shipped').as_json())


class Filters(unittest.TestCase):
    """The owner's skip lists and allowlist, applied after the transactional rules."""

    def tearDown(self):
        for k in reader.FILTERS:
            reader.FILTERS[k] = []

    def test_sender_and_subject_lists(self):
        reader.FILTERS['from'] = ['newmail.example']
        self.assertIn('sender matches newmail.example', facts_for('personal').notes[0])
        reader.FILTERS['from'] = []
        reader.FILTERS['subject'] = ['friday']
        self.assertIn('subject matches friday', facts_for('personal').notes[0])

    def test_allowlist(self):
        reader.FILTERS['only_from'] = ['sarah@']
        self.assertIn('needs the ship', facts_for('personal').notes[0])
        reader.FILTERS['only_from'] = ['boss@work.example']
        self.assertIn('not on only_from', facts_for('personal').notes[0])

    def test_transactional_rules_come_first(self):
        reader.FILTERS['from'] = ['shop.example']
        reader.FILTERS['only_from'] = ['nobody']
        facts = facts_for('shipped')
        self.assertEqual(facts.observations[0]['attr'], 'status')


class WithModel(unittest.TestCase):
    """The model hook: free text the rules leave alone goes to the analyst,
    whose answer lands as facts with the mail's Message-ID as the source."""

    class KnowingShip(reader.NoShip):
        def state(self):
            return {'me': 'person/me', 'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': ['I']},
                                                  {'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']}],
                    'schema': {'kinds': {'person': {'attrs': ['status', 'location']}}}}

    def setUp(self):
        reader.CONTEXT = None
        reader.MODEL = reader.analyze.FakeModel(json.dumps({
            'bodies': [{'id': 'place/the-bar', 'name': 'the bar'}],
            'observations': [{'subject': 'person/sarah', 'attr': 'status', 'value': 'running late', 'conf': 80,
                              'until': '2026-09-17T20:00:00Z', 'message': '<p1@newmail.example>'}],
            'actions': []}))

    def tearDown(self):
        reader.MODEL = None
        reader.CONTEXT = None

    def test_free_text_becomes_facts(self):
        facts = facts_for('personal', self.KnowingShip())
        self.assertEqual([b['id'] for b in facts.bodies], ['place/the-bar'])
        o = facts.observations[0]
        self.assertEqual((o['subject'], o['attr'], o['value'], o['conf'], o['until']),
                         ('person/sarah', 'status', 'running late', 80, '2026-09-17T20:00:00Z'))
        self.assertEqual(o['source'], {'kind': 'mail', 'id': '<p1@newmail.example>'})
        self.assertEqual(o['at'], '2026-09-17T19:05:00Z')
        self.assertIn('person/sarah | Sarah | wife', reader.MODEL.asked[0])
        self.assertIn('Running late, see you at 8.', reader.MODEL.asked[0])

    def test_the_rules_still_come_first(self):
        facts = facts_for('shipped', self.KnowingShip())
        self.assertEqual(reader.MODEL.asked, [])
        self.assertEqual(facts.observations[0]['attr'], 'status')

    def test_a_created_body_joins_the_context(self):
        facts_for('personal', self.KnowingShip())
        self.assertIn('place/the-bar', [b['id'] for b in reader.CONTEXT['bodies']])


if __name__ == '__main__':
    unittest.main()
