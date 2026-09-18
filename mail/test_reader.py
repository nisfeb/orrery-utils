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

    def test_an_order_is_named_for_what_it_is_or_skipped(self):
        def mail(subject, body, sender='Amazon.com'):
            raw = ('From: %s <ship@amazon.example>\nTo: me@example.com\nSubject: %s\n'
                   'Date: Wed, 17 Sep 2026 14:02:00 +0000\nMessage-ID: <%s@amazon.example>\n'
                   'Content-Type: text/plain; charset=utf-8\n\n%s\n') % (sender, subject, abs(hash(subject)), body)
            return reader.parse(raw.encode())
        amazon = reader.classify(mail('Shipped: \u201cAnker 6-Outlet Surge Protector\u201d and 1 more item',
                                      'Your package is on its way. Order # 112-4471-9901'), reader.NoShip())
        self.assertEqual(amazon.bodies, [{'id': 'thing/order-112-4471-9901', 'name': 'Anker 6-Outlet Surge Protector from Amazon.com',
                                          'aliases': ['order 112-4471-9901']}])
        shopify = reader.classify(mail('A shipment from order #1002 is on the way', 'Items in this shipment\nOak side table \u00d7 1\n',
                                       sender='Oakworks'), reader.NoShip())
        self.assertEqual(shopify.bodies[0]['name'], 'Oak side table from Oakworks')
        bare = mail('Your order 5520 has shipped', 'Your order 5520 is on its way.', sender='Some Store')
        facts = reader.classify(bare, reader.NoShip())
        self.assertTrue(facts.empty())
        self.assertIn('order 5520 names no product, no arrival date and no tracking number: skipped', facts.notes)
        dated = reader.classify(mail('Your order 5521 has shipped', 'Estimated delivery: Saturday, September 19.',
                                     sender='Some Store'), reader.NoShip())
        self.assertEqual(dated.bodies[0]['name'], 'Order 5521 from Some Store')
        self.assertEqual(dated.observations[0]['until'], '2026-09-20T00:00:00Z')
        tracked = reader.classify(mail('Your order 5522 has shipped', 'UPS tracking number: 1Z999AA10123456784',
                                       sender='Some Store'), reader.NoShip())
        self.assertEqual(tracked.bodies[0]['aliases'], ['order 5522', 'tracking 1Z999AA10123456784'])
        self.assertIn(('tracking', '1Z999AA10123456784'), [(o['attr'], o['value']) for o in tracked.observations])

        class Knows(reader.NoShip):
            def resolve(self, q):
                return [{'id': 'thing/order-5520', 'kind': 'thing', 'name': 'Walnut desk from Some Store'}] if q == 'order 5520' else []
        facts = reader.classify(bare, Knows())
        self.assertEqual(facts.bodies, [])
        self.assertEqual([(o['subject'], o['attr'], o['value']) for o in facts.observations],
                         [('thing/order-5520', 'status', 'shipped'), ('thing/order-5520', 'location', 'in transit')])

    def test_a_folder_with_no_place_starts_at_its_newest_message(self):
        class Imap:
            def __init__(self):
                self.searched = []

            def response(self, code):
                return 'OK', [{'UIDVALIDITY': b'7', 'UIDNEXT': b'101'}[code]]

            def uid(self, cmd, *args):
                if cmd == 'search':
                    self.searched.append(args[-1])
                    return 'OK', [b'99 100']
                return 'OK', [(b'1 (BODY[]', b'raw ' + args[0].encode())]
        state = {}
        self.assertEqual(reader._folder_batch(Imap(), state, 200, None, 'Archive/2016'), ([], 'Archive/2016', 7))
        self.assertEqual(state['Archive/2016'], {'uidvalidity': 7, 'last_uid': 100})
        state['Archive/2016']['last_uid'] = 98
        imap = Imap()
        msgs, _, _ = reader._folder_batch(imap, state, 200, None, 'Archive/2016')
        self.assertEqual(imap.searched, ['UID 99:*'])
        self.assertEqual([u for u, _ in msgs], [99, 100])

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
