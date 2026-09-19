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
                                                  {'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']},
                                                  {'id': 'place/home', 'name': 'Home'}],
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
        #  the answer names a shop the message never names, and no fact is about it
        self.assertEqual(facts['bodies'], [])
        o = facts['observations'][0]
        self.assertEqual((o['subject'], o['attr'], o['value'], o['conf'], o['at']),
                         ('person/me', 'location', {'ref': 'place/home'}, 80, '2026-09-17T16:10:00Z'))
        self.assertEqual(o['source'], {'kind': 'chat', 'id': 'telegram/1001/13'})
        self.assertIn('home now, car is at the shop', bot.MODEL.asked[0])
        self.assertIn('person/sarah | Sarah | wife', bot.MODEL.asked[0])

    def test_the_last_messages_of_a_chat_ride_along_as_context(self):
        cfg = load('config.json')
        state = {}
        ups = [{'update_id': 1, 'message': {'message_id': 1, 'date': 1789660800, 'chat': {'id': 1001}, 'from': {'id': 1001}, 'text': 'jury duty tomorrow'}},
               {'update_id': 2, 'message': {'message_id': 2, 'date': 1789660860, 'chat': {'id': 1001}, 'from': {'id': 1001}, 'text': '/status waiting'}},
               {'update_id': 3, 'message': {'message_id': 3, 'date': 1789660920, 'chat': {'id': 1001}, 'from': {'id': 1001}, 'text': 'still here, want to scream'}}]
        bot.one_pass(cfg, self.KnowingShip(), bot.NoTelegram(), ups, state, '/dev/null', True)
        self.assertEqual(len(bot.MODEL.asked), 2)
        self.assertNotIn('context', bot.MODEL.asked[0])
        self.assertIn('--- context telegram/1001/1', bot.MODEL.asked[1])
        self.assertIn('jury duty tomorrow', bot.MODEL.asked[1])
        self.assertNotIn('/status waiting', bot.MODEL.asked[1])
        self.assertEqual([m['id'] for m in state['recent']['1001']], ['telegram/1001/1', 'telegram/1001/3'])

    def test_the_gate_keeps_chatter_from_the_model(self):
        bot.DECIDER = bot.analyze.FakeDecider({'worth_reading': {'type': 'noul', 'noul': 0.05}})
        try:
            note = outcomes(load('config.json'), self.KnowingShip())['508']
            self.assertEqual(bot.MODEL.asked, [])
            #  nothing written: the outcome is the note itself
            self.assertTrue(note.startswith('gate: 0.05') and 'not read' in note, note)
            state, questions = bot.DECIDER.asked[0]
            self.assertIn('worth_reading', questions)
            self.assertIn('home now, car is at the shop', state['message'])
            self.assertIn('person/sarah | Sarah | wife', state['known_bodies'])
            bot.DECIDER = bot.analyze.FakeDecider({'worth_reading': {'type': 'noul', 'noul': 0.8}})
            facts = outcomes(load('config.json'), self.KnowingShip())['508']
            self.assertEqual(len(bot.MODEL.asked), 1)
            self.assertEqual(facts['observations'][0]['attr'], 'location')
        finally:
            bot.DECIDER = None

    def test_commands_never_reach_the_model(self):
        outcomes(load('config.json'), self.KnowingShip())
        self.assertEqual(len(bot.MODEL.asked), 1)

    def answer(self, text, observations, recent=()):
        """The facts for one message from Sarah in the family group, the model answering observations."""
        bot.MODEL = bot.analyze.FakeModel(json.dumps({'observations': observations}))
        msg = {'message_id': 50, 'date': 1789660800, 'chat': {'id': -100200}, 'from': {'id': 2002}, 'text': text}
        return bot.handle(msg, load('config.json'), self.KnowingShip(), list(recent))

    def test_a_question_never_reaches_the_model(self):
        facts = self.answer('Bot?', [{'subject': 'person/me', 'attr': 'status', 'value': 'Bot?', 'message': 'telegram/-100200/50'}])
        self.assertEqual(bot.MODEL.asked, [])
        self.assertTrue(facts.empty())

    def test_a_fact_is_about_the_author_or_someone_named(self):
        src = 'telegram/-100200/50'
        facts = self.answer('stuck at the dentist for another hour', [
            {'subject': 'person/me', 'attr': 'status', 'value': 'idle', 'message': src},
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'at the dentist', 'message': src}])
        self.assertEqual([(o['subject'], o['value']) for o in facts.observations], [('person/sarah', 'at the dentist')])
        self.assertIn('dropped person/me.status: not the author and not named in the message', facts.notes)

    def test_a_message_about_someone_else_says_nothing_of_its_author(self):
        bot.MODEL = bot.analyze.FakeModel(json.dumps({
            'bodies': [{'id': 'situation/cancelled-flight'}],
            'observations': [{'subject': 'person/sarah', 'attr': 'status', 'value': 'flight cancelled', 'at': '2026-09-17',
                              'message': 'telegram/-100200/50'}]}))
        msg = {'message_id': 50, 'date': 1789660800, 'chat': {'id': -100200}, 'from': {'id': 2002},
               'text': "grandpa's flight got cancelled"}
        facts = bot.handle(msg, load('config.json'), self.KnowingShip())
        self.assertTrue(facts.empty())
        self.assertIn('dropped person/sarah.status: the message is about someone else', facts.notes)
        self.assertIn('dropped body situation/cancelled-flight: no fact is about it', facts.notes)
        facts = self.answer("she's fine, I'm stuck at the dentist", [
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'stuck at the dentist', 'at': '2026-09-17',
             'message': 'telegram/-100200/50'}])
        self.assertEqual(facts.observations[0]['at'], '2026-09-17T16:00:00Z')

    def test_a_diagnosis_is_never_a_status(self):
        facts = self.answer('I tested positive for strep', [
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'strep positive', 'message': 'telegram/-100200/50'}])
        self.assertTrue(facts.empty())
        self.assertIn('dropped person/sarah.status: a medical fact goes under health, which this key may not write', facts.notes)

    def test_a_diagnosis_moves_to_health_when_the_key_may_write_it(self):
        class Writes(self.KnowingShip):
            def state(self):
                s = super().state()
                s['schema']['kinds']['person']['attrs'].append('health')
                return s
        self.KnowingShip = Writes
        facts = self.answer('I tested positive for strep', [
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'strep positive', 'message': 'telegram/-100200/50'}])
        self.assertEqual([(o['attr'], o['value']) for o in facts.observations], [('health', 'strep positive')])

    def test_new_names_for_a_body_the_ship_has_are_kept_when_the_message_uses_them(self):
        class Neighbors(self.KnowingShip):
            def state(self):
                s = super().state()
                s['bodies'].append({'id': 'place/neighbors', 'name': 'the neighbors'})
                return s
        src = 'telegram/-100200/50'
        answer = {'bodies': [{'id': 'place/neighbors', 'aliases': ['next door', 'the green house']}],
                  'observations': [{'subject': 'person/sarah', 'attr': 'location', 'value': {'ref': 'place/neighbors'}, 'message': src}]}
        bot.MODEL = bot.analyze.FakeModel(json.dumps(answer))
        msg = {'message_id': 50, 'date': 1789660800, 'chat': {'id': -100200}, 'from': {'id': 2002}, 'text': "I'm next door"}
        facts = bot.handle(msg, load('config.json'), Neighbors())
        self.assertEqual(facts.bodies, [{'id': 'place/neighbors', 'aliases': ['next door']}])
        self.assertEqual([(o['subject'], o['value']) for o in facts.observations], [('person/sarah', {'ref': 'place/neighbors'})])
        self.assertIn('next door', [b for b in bot.CONTEXT['bodies'] if b['id'] == 'place/neighbors'][0]['aliases'])

    def test_a_value_other_than_a_status_is_in_the_words(self):
        src = 'telegram/-100200/50'
        facts = self.answer('back home, the car is at the shop', [
            {'subject': 'person/sarah', 'attr': 'location', 'value': {'ref': 'place/home'}, 'message': src},
            {'subject': 'person/sarah', 'attr': 'location', 'value': 'Lisbon', 'message': src}])
        self.assertEqual([o['value'] for o in facts.observations], [{'ref': 'place/home'}])
        self.assertIn('dropped person/sarah.location: the value is not in the message', facts.notes)

    def test_a_status_read_from_the_earlier_messages_is_dropped(self):
        recent = [{'id': 'telegram/-100200/49', 'at': '2026-09-17T15:59:00Z', 'who': 'person/sarah', 'text': 'jury duty all week'}]
        facts = self.answer('ugh', [{'subject': 'person/sarah', 'attr': 'status', 'value': 'on jury duty',
                                     'message': 'telegram/-100200/50'}], recent)
        self.assertTrue(facts.empty())
        facts = self.answer('still on jury duty', [{'subject': 'person/sarah', 'attr': 'status', 'value': 'on jury duty',
                                                    'message': 'telegram/-100200/50'}], recent)
        self.assertEqual(facts.observations[0]['value'], 'on jury duty')

    def test_a_message_the_model_could_not_read_is_kept(self):
        class Down:
            def chat(self, system, user):
                raise RuntimeError('model unreachable at http://localhost:1234/v1: refused')
        bot.MODEL = Down()
        updates = [u for u in load('updates.json')['result'] if u['update_id'] in (507, 508)]
        state = {}
        self.assertFalse(bot.one_pass(load('config.json'), self.KnowingShip(), bot.NoTelegram(), updates, state, None, True))
        self.assertEqual(state['offset'], 508)


class Business(unittest.TestCase):
    """The owner's own private chats, read through a Telegram Business
    connection: a message lands like any other, only when the connection
    belongs to an account in people, and never gets a reply."""

    class Tg(bot.NoTelegram):
        def __init__(self, owner):
            self.owner, self.sent = owner, []

        def business_owner(self, conn_id):
            return self.owner if conn_id == 'c1' else ''

        def send(self, chat_id, text):
            self.sent.append((chat_id, text))
            return True, ''

    class Ship(bot.NoShip):
        def __init__(self):
            self.observed = []

        def observe(self, bodies, observations):
            self.observed.extend(observations)
            return 200, {}

    def run_one(self, owner, text):
        cfg = load('config.json')
        cfg['chats'].append(2002)
        ship, tg, state = self.Ship(), self.Tg(owner), {}
        u = {'update_id': 900, 'business_message': {
            'business_connection_id': 'c1', 'message_id': 77, 'date': 1789660800,
            'chat': {'id': 2002, 'type': 'private'}, 'from': {'id': 1001}, 'text': text}}
        bot.one_pass(cfg, ship, tg, [u], state, None, True)
        self.assertEqual(state['offset'], 901)
        return ship, tg

    def test_the_owners_chat_lands_with_its_pointer(self):
        ship, _ = self.run_one('1001', '/at place/home')
        o = ship.observed[0]
        self.assertEqual((o['subject'], o['value']), ('person/me', {'ref': 'place/home'}))
        self.assertEqual(o['source'], {'kind': 'chat', 'id': 'telegram/2002/77'})

    def test_no_reply_in_a_business_chat(self):
        _, tg = self.run_one('1001', '/nope')
        self.assertEqual(tg.sent, [])

    def test_a_stranger_connecting_the_bot_is_ignored(self):
        ship, _ = self.run_one('5555', '/at place/home')
        self.assertEqual(ship.observed, [])



class Polling(unittest.TestCase):
    def test_a_dropped_connection_is_asked_again_and_a_bad_token_is_not(self):
        answers = []
        real_request, real_sleep = bot.request, bot.time.sleep
        bot.request = lambda url, **kw: answers.pop(0)
        bot.time.sleep = lambda s: None
        try:
            tg = bot.Telegram('t', 'https://api.example')
            for transient in ((0, '<urlopen error [Errno 104] Connection reset by peer>'), (502, 'Bad Gateway'), (429, {'ok': False})):
                answers.append(transient)
                self.assertEqual(tg.updates(5), [], transient)
            answers.append((200, {'ok': True, 'result': [{'update_id': 7}]}))
            self.assertEqual(tg.updates(5), [{'update_id': 7}])
            for fatal in ((401, {'ok': False, 'description': 'Unauthorized'}), (409, {'ok': False, 'description': 'Conflict'})):
                answers.append(fatal)
                with self.assertRaises(SystemExit):
                    tg.updates(5)
        finally:
            bot.request, bot.time.sleep = real_request, real_sleep

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
