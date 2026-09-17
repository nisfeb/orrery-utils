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


if __name__ == '__main__':
    unittest.main()
