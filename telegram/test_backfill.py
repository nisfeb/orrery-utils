"""The export reader, checked without a model or a ship: the fixture export
yields the right messages per chat (mapped senders, real messages, inside
the window, past the place), windows split as promised, and the analyst's
facts carry telegram/<chat>/<message> source pointers.

    cd telegram && python3 -m unittest test_backfill
"""
import json
import os
import unittest
from datetime import datetime, timezone

import backfill  # noqa: F401  (puts ../common on the path)
import analyze  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class Export(unittest.TestCase):
    def setUp(self):
        self.cfg = load('config.json')
        self.chats = {c['name']: c for c in backfill.chats_in(load('export.json'))}
        self.since = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def test_chats_are_found(self):
        self.assertEqual(sorted(self.chats), ['Deals Channel', 'Sarah', 'family'])
        single = backfill.chats_in(self.chats['Sarah'])
        self.assertEqual(len(single), 1)

    def test_messages_worth_reading(self):
        msgs = backfill.messages_of(self.chats['Sarah'], self.cfg, self.since, 0)
        self.assertEqual([m['mid'] for m in msgs], [201, 202, 203, 205])
        self.assertEqual(msgs[1]['who'], 'person/sarah')
        self.assertEqual(msgs[1]['text'], 'oh no. want me to come get you? https://maps.example/route9')
        self.assertEqual(msgs[0]['at'].strftime('%Y-%m-%dT%H:%M:%SZ'), '2026-09-16T22:05:00Z')

    def test_place_and_window_and_strangers(self):
        self.assertEqual([m['mid'] for m in backfill.messages_of(self.chats['Sarah'], self.cfg, self.since, 203)], [205])
        self.assertEqual([m['mid'] for m in backfill.messages_of(self.chats['Sarah'], self.cfg, None, 0)][0], 100)
        fam = backfill.messages_of(self.chats['family'], self.cfg, self.since, 0)
        self.assertEqual([m['mid'] for m in fam], [41])
        self.assertEqual(backfill.messages_of(self.chats['Deals Channel'], self.cfg, self.since, 0), [])

    def test_windows_split_by_count_and_size(self):
        msgs = [{'mid': i, 'at': self.since, 'who': 'person/me', 'text': 'x' * 100} for i in range(30)]
        runs = list(backfill.windows(msgs))
        self.assertEqual([len(r) for r in runs], [12, 12, 6])
        big = [{'mid': i, 'at': self.since, 'who': 'person/me', 'text': 'y' * 2000} for i in range(3)]
        self.assertEqual([len(r) for r in backfill.windows(big)], [1, 1, 1])
        self.assertEqual(list(backfill.windows([])), [])

    def test_facts_carry_source_pointers(self):
        backfill.MODEL = analyze.FakeModel(json.dumps({
            'bodies': [{'id': 'place/johns-machine-shop', 'name': "John's Machine Shop", 'aliases': ["john's"]}],
            'observations': [
                {'subject': 'person/me', 'attr': 'status', 'value': 'stranded, waiting for a tow', 'conf': 90, 'message': 'telegram/2002/201'},
                {'subject': 'thing/subaru', 'attr': 'location', 'value': {'ref': 'place/johns-machine-shop'}, 'conf': 85, 'message': 'telegram/2002/205'}],
            'actions': [{'kind': 'task', 'title': 'Call the shop about the Subaru', 'about': ['thing/subaru'], 'message': 'telegram/2002/205'}]}))
        try:
            context = {'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': []}, {'id': 'person/sarah', 'name': 'Sarah', 'aliases': []},
                                  {'id': 'thing/subaru', 'name': 'the Subaru', 'aliases': ['the car']}],
                       'attrs': {}, 'me': 'person/me', 'channel': 'chat', 'action_kinds': ['task']}
            window = backfill.messages_of(self.chats['Sarah'], self.cfg, self.since, 0)
            facts = backfill.facts_for(window, '2002', context)
        finally:
            backfill.MODEL = None
        self.assertEqual([b['id'] for b in facts.bodies], ['place/johns-machine-shop'])
        self.assertEqual(facts.observations[0]['source'], {'kind': 'chat', 'id': 'telegram/2002/201'})
        self.assertEqual(facts.observations[0]['at'], '2026-09-16T22:05:00Z')
        self.assertEqual(facts.observations[1]['at'], '2026-09-17T02:10:00Z')
        self.assertEqual(facts.actions[0]['title'], 'Call the shop about the Subaru')
        self.assertIn('place/johns-machine-shop', [b['id'] for b in context['bodies']])


if __name__ == '__main__':
    unittest.main()
