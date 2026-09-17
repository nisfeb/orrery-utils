"""The grammar and the executor, checked without Telegram or a ship:
fixtures/updates.json through fixtures/config.json produces exactly
fixtures/expected.json (one entry per update id, or a note), names are
resolved through a stub ship, and message actions go to the right chat.

    cd telegram && python3 -m unittest
"""
import json
import os
import unittest

import bot

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


def outcomes(cfg, ship=None):
    """update id -> the facts as json, or the first note when there are none."""
    out = {}
    for u in load('updates.json')['result']:
        msg = u.get('message')
        if not isinstance(msg, dict):
            out[str(u['update_id'])] = 'not a message'
            continue
        facts = bot.handle(msg, cfg, ship or bot.NoShip())
        out[str(u['update_id'])] = facts.as_json() if not facts.empty() else (facts.notes[0] if facts.notes else 'nothing')
    return out


class FakeShip(bot.NoShip):
    def resolve(self, q):
        return [{'id': 'person/sarah', 'kind': 'person', 'name': 'Sarah', 'match': 'exact'}] if q.lower() == 'sarah' else []


class Grammar(unittest.TestCase):
    def setUp(self):
        self.cfg = load('config.json')

    def test_every_update_matches_expected(self):
        self.assertEqual(outcomes(self.cfg), load('expected.json'))

    def test_who_is_ignored_and_why(self):
        out = outcomes(self.cfg)
        self.assertEqual(out['506'], 'sender 3003 is not in people: ignored')
        self.assertEqual(out['507'], 'chat 4004 is not in chats: ignored')
        self.assertEqual(out['508'], 'nothing')
        self.assertEqual(out['511'], 'not a message')

    def test_sender_is_the_subject_of_at_and_status(self):
        out = outcomes(self.cfg)
        me = out['501']['observations'][0]
        self.assertEqual((me['subject'], me['attr'], me['value']), ('person/me', 'status', 'stranded, waiting for a tow'))
        self.assertEqual(me['source'], {'kind': 'chat', 'id': 'telegram/1001/10'})
        self.assertEqual(me['at'], '2026-09-17T16:00:00Z')
        sarah = out['504']['observations'][0]
        self.assertEqual((sarah['subject'], sarah['value']), ('person/sarah', {'ref': 'place/home'}))
        self.assertEqual(out['510']['observations'][0]['value'], None)

    def test_obs_resolves_a_name_through_the_ship(self):
        self.assertIn('unknown: sarah', outcomes(self.cfg)['509'])
        out = outcomes(self.cfg, FakeShip())
        o = out['509']['observations'][0]
        self.assertEqual((o['subject'], o['attr'], o['value']), ('person/sarah', 'location', 'Lisbon'))

    def test_task_with_due(self):
        a = outcomes(self.cfg)['505']['actions'][0]
        self.assertEqual(a, {'kind': 'task', 'title': 'Call the shop about the Subaru', 'due': '2026-09-18T00:00:00Z'})

    def test_unknown_command_gets_the_list(self):
        self.assertIn('commands:', outcomes(self.cfg)['512'])

    def test_values(self):
        self.assertEqual(bot.parse_value('null'), None)
        self.assertEqual(bot.parse_value('42'), 42)
        self.assertEqual(bot.parse_value('2.5'), 2.5)
        self.assertEqual(bot.parse_value('place/home'), {'ref': 'place/home'})
        self.assertEqual(bot.parse_value('true'), True)
        self.assertEqual(bot.parse_value('Route 9'), 'Route 9')


class WithModel(unittest.TestCase):
    """Free text from a known person goes to the analyst; its answer lands
    with the message's pointer as the source."""

    class KnowingShip(bot.NoShip):
        def state(self):
            return {'me': 'person/me', 'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': ['I']},
                                                  {'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']}],
                    'schema': {'kinds': {'person': {'attrs': ['status', 'location']}}}}

    def setUp(self):
        bot.CONTEXT = None
        bot.MODEL = bot.analyze.FakeModel(json.dumps({
            'bodies': [{'id': 'place/johns-machine-shop', 'name': "John's Machine Shop"}],
            'observations': [{'subject': 'person/me', 'attr': 'location', 'value': {'ref': 'place/home'}, 'conf': 80,
                              'message': 'telegram/1001/13'}],
            'actions': []}))

    def tearDown(self):
        bot.MODEL = None
        bot.CONTEXT = None

    def test_free_text_becomes_facts(self):
        out = outcomes(load('config.json'), self.KnowingShip())
        facts = out['508']
        self.assertEqual([b['id'] for b in facts['bodies']], ['place/johns-machine-shop'])
        o = facts['observations'][0]
        self.assertEqual((o['subject'], o['attr'], o['value'], o['conf'], o['at']),
                         ('person/me', 'location', {'ref': 'place/home'}, 80, '2026-09-17T16:10:00Z'))
        self.assertEqual(o['source'], {'kind': 'chat', 'id': 'telegram/1001/13'})
        self.assertIn('home now, car is at the shop', bot.MODEL.asked[0])
        self.assertIn('person/sarah | Sarah | wife', bot.MODEL.asked[0])

    def test_commands_never_reach_the_model(self):
        outcomes(load('config.json'), self.KnowingShip())
        self.assertEqual(len(bot.MODEL.asked), 1)


class Actions(unittest.TestCase):
    class FakeShip(bot.NoShip):
        """The open list, the answer a claim gets, and what the read back
        sees: reads is one answer to actions('claimed') per read, and with
        none given every claim of ours lands at once."""

        def __init__(self, open_actions, claim=200, by='telegram', reads=None):
            self.open = open_actions
            self.claim = claim
            self.by = by
            self.reads = reads
            self.moves = []

        def actions(self, status):
            if status == 'open':
                return self.open
            if status != 'claimed':
                return []
            if self.reads is None:
                return [{'id': aid, 'history': [{'status': 'claimed', 'by': self.by}]}
                        for aid, st, _ in self.moves if st == 'claimed']
            return self.reads.pop(0) if self.reads else []

        def move(self, aid, status, note=''):
            self.moves.append((aid, status, note))
            code = self.claim if status == 'claimed' else 200
            return code, {'id': aid, 'status': status, 'by': self.by, 'ok': True}

    class FakeTelegram(bot.NoTelegram):
        def __init__(self):
            self.sent = []

        def send(self, chat_id, text):
            self.sent.append((chat_id, text))
            return True, ''

    def setUp(self):
        bot.CLAIM_PAUSE = 0
        bot.CLAIM_READS = 5

    def test_delivery(self):
        cfg = load('config.json')
        ship = self.FakeShip([
            {'id': 'a1', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': "The car is at John's"}},
            {'id': 'a2', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': 'person/nobody', 'text': 'hi'}},
            {'id': 'a3', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'sms', 'to': 'person/sarah', 'text': 'hi'}},
            {'id': 'a4', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': '-100200', 'text': ''}},
            {'id': 'a5', 'kind': 'task', 'status': 'approved', 'payload': {}},
            {'id': 'a6', 'kind': 'message', 'status': 'proposed', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': 'waiting for a human'}},
        ])
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, ship, tg, state)
        self.assertEqual(tg.sent, [('2002', "The car is at John's")])
        self.assertEqual([m[:2] for m in ship.moves],
                         [('a1', 'claimed'), ('a1', 'done'), ('a2', 'claimed'), ('a2', 'failed'),
                          ('a4', 'claimed'), ('a4', 'failed')])
        self.assertIn('nobody', ship.moves[3][2])
        self.assertEqual(state['executed'], ['a1', 'a2', 'a4'])
        bot.execute(cfg, ship, tg, state)
        self.assertEqual(len(tg.sent), 1)

    def test_a_refused_claim_leaves_the_action_alone(self):
        cfg = load('config.json')
        ship = self.FakeShip([
            {'id': 'a1', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': "The car is at John's"}},
        ], claim=409)
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, ship, tg, state)
        self.assertEqual(tg.sent, [])
        self.assertEqual([m[1] for m in ship.moves], ['claimed'])
        self.assertEqual(state['executed'], [])

    def test_a_claim_another_bot_won_is_not_sent(self):
        cfg = load('config.json')
        ship = self.FakeShip([
            {'id': 'a1', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': "The car is at John's"}},
        ], reads=[[{'id': 'a1', 'history': [{'status': 'claimed', 'by': 'other'}]}]])
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, ship, tg, state)
        self.assertEqual(tg.sent, [])
        self.assertEqual([m[1] for m in ship.moves], ['claimed'])
        self.assertEqual(state['executed'], [])

    def test_a_slow_writer_is_read_again_and_a_claim_that_never_lands_is_skipped(self):
        cfg = load('config.json')
        row = {'id': 'a1', 'kind': 'message', 'status': 'approved', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': "The car is at John's"}}
        slow = self.FakeShip([dict(row)], reads=[[], [{'id': 'a1', 'history': [{'status': 'claimed', 'by': 'telegram'}]}]])
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, slow, tg, state)
        self.assertEqual(tg.sent, [('2002', "The car is at John's")])
        self.assertEqual([m[:2] for m in slow.moves], [('a1', 'claimed'), ('a1', 'done')])
        self.assertEqual(state['executed'], ['a1'])
        never = self.FakeShip([dict(row)], reads=[[], [], [], [], []])
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, never, tg, state)
        self.assertEqual(tg.sent, [])
        self.assertEqual([m[1] for m in never.moves], ['claimed'])
        self.assertEqual(state['executed'], [])

    def test_an_abandoned_claim_is_taken_again(self):
        cfg = load('config.json')
        row = {'id': 'a1', 'kind': 'message', 'status': 'claimed', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': "The car is at John's"}}
        live = self.FakeShip([dict(row)], claim=409)
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, live, tg, state)
        self.assertEqual(tg.sent, [])
        self.assertEqual([m[1] for m in live.moves], ['claimed'])
        self.assertEqual(state['executed'], [])
        expired = self.FakeShip([dict(row)])
        tg = self.FakeTelegram()
        state = {}
        bot.execute(cfg, expired, tg, state)
        self.assertEqual(tg.sent, [('2002', "The car is at John's")])
        self.assertEqual([m[:2] for m in expired.moves], [('a1', 'claimed'), ('a1', 'done')])
        self.assertEqual(state['executed'], ['a1'])


if __name__ == '__main__':
    unittest.main()
