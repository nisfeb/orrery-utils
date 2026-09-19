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
        {'subject': 'person/me', 'attr': 'mood', 'value': 'tired', 'message': 'telegram/1001/10'},
        {'subject': 'person/me', 'attr': 'location', 'value': 'Route 9', 'conf': 'high', 'message': 'not-a-message'},
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

    def test_a_known_body_learns_new_aliases(self):
        context = {'bodies': [{'id': 'place/neighbors', 'name': 'the neighbors', 'aliases': ['next door']},
                              {'id': 'person/theo', 'name': 'Theo', 'aliases': []}], 'attrs': {}, 'me': 'person/me', 'channel': 'chat', 'action_kinds': ['task']}
        answer = {'bodies': [{'id': 'place/neighbors', 'name': 'the neighbors', 'aliases': ['next door', 'the Hendersons']}],
                  'observations': [{'subject': 'person/theo', 'attr': 'location', 'value': {'ref': 'place/neighbors'}, 'message': 'm1'}]}
        facts = analyze.validate(answer, [{'id': 'm1', 'at': '2026-09-18T12:00:00Z', 'who': 'person/me', 'text': "Theo is at the Hendersons'"}], context)
        self.assertEqual(facts['bodies'], [{'id': 'place/neighbors', 'aliases': ['the Hendersons']}])
        self.assertEqual(facts['observations'][0]['value'], {'ref': 'place/neighbors'})

    def test_new_bodies_only(self):
        self.assertEqual([b['id'] for b in self.facts['bodies']],
                         ['place/johns-machine-shop', 'situation/2026-09-16-breakdown'])
        self.assertEqual(self.facts['bodies'][0]['aliases'], ["John's", 'the shop'])
        self.assertIn('dropped body with a bad id: bad id', self.facts['notes'])

    def test_observations(self):
        rows = {(o['subject'], o['attr']): o for o in self.facts['observations']}
        self.assertEqual(len(rows), 5)
        #  person/me.mood is gone: the schema speaks for person and does not list it
        self.assertNotIn(('person/me', 'mood'), rows)
        self.assertNotIn('mood', [o['attr'] for o in self.facts['observations']])
        self.assertFalse(any('mood' in n for n in self.facts['notes']))
        me = rows[('person/me', 'status')]
        self.assertEqual((me['at'], me['until'], me['conf'], me['message']),
                         ('2026-09-16T22:05:00Z', '2026-09-17T02:00:00Z', 90, 'telegram/1001/10'))
        self.assertEqual(rows[('thing/subaru', 'location')]['at'], '2026-09-16T22:00:00Z')
        self.assertEqual(rows[('thing/subaru', 'status')]['conf'], 70)
        self.assertEqual(rows[('thing/subaru', 'status')]['at'], '2026-09-16T23:40:00Z')
        self.assertEqual(rows[('situation/2026-09-16-breakdown', 'participants')]['value'], {'ref': 'person/me'})
        #  a bad conf and an unknown message id still fall back, on a listed attr
        back = rows[('person/me', 'location')]
        self.assertEqual((back['conf'], back['message'], back['at']), (70, 'telegram/1001/11', '2026-09-16T23:40:00Z'))
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
        self.assertEqual(n('Reminder: Pottery @ Thu May 14, 2026 6:00pm'), 'pottery')
        self.assertEqual(n('Pottery'), 'pottery')
        self.assertEqual(n('Robin- Pottery/Wheel'), 'robin pottery/wheel')
        self.assertNotEqual(n('Robin- Pottery/Wheel'), n('Pottery'))
        self.assertEqual(n('Invitation: Book Club 2026-05-16'), 'book club')

    def test_same_person(self):
        self.assertTrue(analyze.same_person('Dana', 'Dana Quill'))
        self.assertTrue(analyze.same_person('dana quill', 'Dana'))
        self.assertFalse(analyze.same_person('Dana', 'Daniel Quill'))
        self.assertFalse(analyze.same_person('', 'Dana'))
        self.assertFalse(analyze.same_person('wife', 'dana wife'))
        self.assertFalse(analyze.same_person('Quill', 'Dana Quill'))
        self.assertTrue(analyze.same_person('Dana Quill', 'Dana O Quill'))

    def test_a_one_word_name_prefers_the_exact_body(self):
        context = {'bodies': [{'id': 'person/me', 'name': 'dana', 'aliases': []},
                              {'id': 'person/x', 'name': 'dana wife', 'aliases': []}]}
        self.assertEqual(analyze.existing_for({'id': 'person/dana-2', 'name': 'dana'}, context, []), 'person/me')
        self.assertIsNone(analyze.existing_for({'id': 'person/j', 'name': 'dana quill'}, context, []))

    def test_twins_fold_into_existing_bodies(self):
        context = {'bodies': [{'id': 'person/dana', 'name': 'Dana', 'aliases': ['wife']},
                              {'id': 'activity/pottery', 'name': 'Pottery', 'aliases': []},
                              {'id': 'person/me', 'name': 'me', 'aliases': []}],
                   'attrs': {}, 'me': 'person/me', 'channel': 'mail', 'action_kinds': ['task']}
        answer = {'bodies': [{'id': 'person/dana-quill', 'name': 'Dana Quill'},
                             {'id': 'situation/pottery-may-14', 'name': 'Reminder: Pottery @ May 14, 6:00pm'},
                             {'id': 'situation/pottery-may-16', 'name': 'Pottery'},
                             {'id': 'activity/book-club', 'name': 'Book Club'},
                             {'id': 'situation/book-club-2', 'name': 'Reminder: Book Club'}],
                  'observations': [{'subject': 'person/dana-quill', 'attr': 'email', 'value': 'a@x.example', 'message': 'm1'},
                                   {'subject': 'situation/pottery-may-14', 'attr': 'last', 'value': '2026-05-14T22:00:00Z', 'message': 'm1'},
                                   {'subject': 'situation/book-club-2', 'attr': 'last', 'value': '2026-05-16T15:00:00Z', 'message': 'm1'},
                                   {'subject': 'person/me', 'attr': 'spouse', 'value': {'ref': 'person/dana-quill'}, 'message': 'm1'}],
                  'actions': [{'kind': 'task', 'title': 'Pay Dana', 'about': ['person/dana-quill', 'person/dana'], 'message': 'm1'}]}
        facts = analyze.validate(answer, [{'id': 'm1', 'at': '2026-09-16T12:00:00Z', 'who': 'x', 'text': ''}], context)
        self.assertEqual([b['id'] for b in facts['bodies']], ['activity/book-club'])
        subjects = [(o['subject'], o['attr']) for o in facts['observations']]
        self.assertEqual(subjects, [('person/dana', 'email'), ('activity/pottery', 'last'),
                                    ('activity/book-club', 'last'), ('person/me', 'spouse')])
        self.assertEqual(facts['observations'][3]['value'], {'ref': 'person/dana'})
        self.assertEqual(facts['actions'][0]['about'], ['person/dana'])
        self.assertIn('person/dana-quill is person/dana', facts['notes'])
        self.assertIn('situation/pottery-may-16 is activity/pottery', facts['notes'])


class Prompt(unittest.TestCase):
    def test_the_prompt_is_the_markdown_file(self):
        with open(analyze.PROMPT_PATH, encoding='utf-8') as f:
            text = f.read().strip()
        self.assertEqual(analyze.SYSTEM, text)
        self.assertIn('never a feeling, a quote or a wish', text)
        self.assertIn('Answer with one JSON object and nothing else', text)


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
        self.assertIn('--- message telegram/1001/11 | %s | from person/me' % analyze.local_time('2026-09-16T23:40:00Z'), m.asked[0])

    def test_context_leaves_out_long_closed_situations(self):
        state = {'me': 'person/me', 'bodies': [
            {'id': 'situation/old', 'name': 'Old', 'aliases': [], 'attrs': {'status': {'value': 'closed', 'at': '2026-01-01T00:00:00Z'}, 'ended': {'value': '2026-01-01T00:00:00Z'}}},
            {'id': 'situation/recent', 'name': 'Recent', 'aliases': [], 'attrs': {'status': {'value': 'closed', 'at': '2099-01-01T00:00:00Z'}, 'ended': {'value': '2099-01-01T00:00:00Z'}}},
            {'id': 'situation/open', 'name': 'Open', 'aliases': [], 'attrs': {'status': {'value': 'open', 'at': '2026-01-01T00:00:00Z'}}},
            {'id': 'person/me', 'name': 'me', 'aliases': [], 'attrs': {}}], 'schema': {'kinds': {}}}
        ids = [b['id'] for b in analyze.context_from_state(state, 'mail')['bodies']]
        self.assertEqual(ids, ['situation/recent', 'situation/open', 'person/me'])

    def test_sensitive_attrs_pass_only_for_a_writing_key(self):
        state = {'me': 'person/me', 'bodies': [{'id': 'person/mom', 'name': 'Mom', 'aliases': []}],
                 'schema': {'kinds': {'person': {'attrs': ['status', 'location']}}}}
        answer = json.dumps({'observations': [{'subject': 'person/mom', 'attr': 'health', 'value': 'biopsy clear', 'conf': 85, 'message': 'm1'}]})
        msgs = [{'id': 'm1', 'at': '2026-09-18T12:00:00Z', 'who': 'person/me', 'text': "mom's biopsy came back clear"}]
        plain = analyze.analyze(analyze.FakeModel(answer), msgs, analyze.context_from_state(state, 'chat'))
        self.assertEqual(plain['observations'], [])
        self.assertTrue(any('health' in n for n in plain['notes']))
        #  a key minted with sensitive: write sees health in its schema view
        state['schema']['kinds']['person']['attrs'].append('health')
        writer = analyze.analyze(analyze.FakeModel(answer), msgs, analyze.context_from_state(state, 'chat'))
        self.assertEqual([(o['attr'], o['value']) for o in writer['observations']], [('health', 'biopsy clear')])

    def test_a_situation_status_is_open_closed_or_cancelled(self):
        context = {'bodies': [{'id': 'situation/meeting', 'name': 'Meeting', 'aliases': []}], 'attrs': {}, 'me': 'person/me', 'channel': 'mail', 'action_kinds': ['task']}
        answer = {'observations': [{'subject': 'situation/meeting', 'attr': 'status', 'value': 'under way', 'message': 'm1'},
                                   {'subject': 'situation/meeting', 'attr': 'ends', 'value': '2026-12-05T20:00:00Z', 'message': 'm1'},
                                   {'subject': 'situation/meeting', 'attr': 'status', 'value': 'cancelled', 'message': 'm1'}]}
        facts = analyze.validate(answer, [{'id': 'm1', 'at': '2026-09-18T12:00:00Z', 'who': 'x', 'text': ''}], context)
        self.assertEqual([(o['attr'], o['value']) for o in facts['observations']], [('ends', '2026-12-05T20:00:00Z'), ('status', 'cancelled')])
        self.assertTrue(any('under way' in n for n in facts['notes']))


        state = {'me': 'person/me', 'bodies': [{'id': 'person/sarah', 'name': 'Sarah', 'aliases': []}],
                 'schema': {'kinds': {'person': {'attrs': ['status', 'location'],
                                                 'notes': {'status': 'what they are doing right now, never a feeling'}}}}}
        context = analyze.context_from_state(state, 'chat')
        self.assertEqual(context['notes'], {'person': {'status': 'what they are doing right now, never a feeling'}})
        m = analyze.FakeModel(json.dumps({'observations': [
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'on jury duty', 'conf': 80, 'message': 'm1'},
            {'subject': 'person/sarah', 'attr': 'mood', 'value': 'frustrated', 'conf': 60, 'message': 'm1'}]}))
        facts = analyze.analyze(m, [{'id': 'm1', 'at': '2026-09-18T12:00:00Z', 'who': 'person/sarah', 'text': 'jury duty makes me want to scream'}], context)
        self.assertIn('person.status: what they are doing right now, never a feeling', m.asked[0])
        self.assertEqual([(o['attr'], o['value']) for o in facts['observations']], [('status', 'on jury duty')])
        self.assertEqual(facts['notes'], [])

    def test_context_messages_yield_no_facts(self):
        window = [{'id': 'c1', 'at': '2026-09-18T11:00:00Z', 'who': 'person/sarah', 'text': 'jury duty tomorrow, ugh', 'context': True},
                  {'id': 'n1', 'at': '2026-09-18T12:00:00Z', 'who': 'person/sarah', 'text': 'still here, want to scream'}]
        m = analyze.FakeModel(json.dumps({'observations': [
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'jury duty tomorrow', 'message': 'c1'},
            {'subject': 'person/sarah', 'attr': 'status', 'value': 'on jury duty', 'message': 'n1'},
            {'subject': 'person/sarah', 'attr': 'location', 'value': 'court', 'message': 'nope'}],
            'actions': [{'kind': 'task', 'title': 'x', 'message': 'c1'}]}))
        facts = analyze.analyze(m, window, CONTEXT)
        self.assertEqual([(o['value'], o['message']) for o in facts['observations']], [('on jury duty', 'n1'), ('court', 'n1')])
        self.assertEqual(facts['actions'], [])
        self.assertIn('Earlier messages, context only', m.asked[0])
        self.assertIn('--- context c1', m.asked[0])
        self.assertIn('--- message n1', m.asked[0])
        self.assertEqual(analyze.analyze(m, [dict(window[0])], CONTEXT)['notes'], [])
        self.assertEqual(len(m.asked), 1)

    def test_context_from_state(self):
        state = {'me': 'person/me', 'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': ['I']}],
                 'schema': {'kinds': {'person': {'attrs': ['status']}}}}
        c = analyze.context_from_state(state, 'mail')
        self.assertEqual(c['bodies'], [{'id': 'person/me', 'name': 'me', 'aliases': ['I']}])
        self.assertEqual(c['attrs'], {'person': ['status']})
        self.assertEqual((c['channel'], c['action_kinds']), ('mail', ['task']))



class Hosted(unittest.TestCase):
    def test_a_hosted_model_sends_its_key_and_its_routing(self):
        import os
        os.environ['ANALYZE_TEST_KEY'] = 'secret'
        m = analyze.Model.from_config({'url': 'https://openrouter.ai/api/v1', 'name': 'm', 'api_key_env': 'ANALYZE_TEST_KEY',
                                       'provider': {'zdr': True}})
        sent = []
        m.request = lambda path, body=None: sent.append(body) or {'choices': [{'message': {'content': '{}'}}]}
        m.chat('system', 'user')
        self.assertEqual((m.api_key, sent[0]['provider']), ('secret', {'zdr': True}))
        local = analyze.Model.from_config({})
        local.name = 'qwen'
        local.request = lambda path, body=None: sent.append(body) or {'choices': [{'message': {'content': '{}'}}]}
        local.chat('system', 'user')
        self.assertNotIn('provider', sent[1])
        self.assertIsNone(local.api_key)


class SharedConfig(unittest.TestCase):
    def test_an_include_fills_in_what_the_reader_leaves_out(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'mail'))
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'model': {'name': 'shared'}, 'state': 'shared.json'}, f)
            with open(os.path.join(d, 'mail', 'config.json'), 'w') as f:
                json.dump({'include': '../config.json', 'state': 'mail.json'}, f)
            with open(os.path.join(d, 'mail', 'alone.json'), 'w') as f:
                json.dump({'state': 'alone.json'}, f)
            self.assertEqual(analyze.load_config(os.path.join(d, 'mail', 'config.json')),
                             {'model': {'name': 'shared'}, 'state': 'mail.json'})
            self.assertEqual(analyze.load_config(os.path.join(d, 'mail', 'alone.json')), {'state': 'alone.json'})


class ModelDown(unittest.TestCase):
    def test_a_model_that_never_read_the_message_is_told_apart(self):
        def notes(e):
            return analyze.analyze(type('M', (), {'chat': lambda self, s, u: (_ for _ in ()).throw(e)})(),
                                   [{'id': 'm1', 'text': 'hi'}], {})['notes']
        for e in (RuntimeError('model unreachable at x: refused'), RuntimeError('model answered 401: no auth'),
                  RuntimeError('model answered 402: no credit'), RuntimeError('model answered 429: slow down'),
                  RuntimeError('model answered 503: busy')):
            self.assertTrue(analyze.model_down(notes(e)), e)
        for e in (RuntimeError('model answered 400: too long'), ValueError('no JSON object in the answer')):
            self.assertFalse(analyze.model_down(notes(e)), e)

    def test_a_key_in_the_config_wins_and_a_missing_one_stops_the_run_unechoed(self):
        import os
        os.environ.pop('ANALYZE_TEST_MISSING', None)
        self.assertEqual(analyze.Model.from_config({'api_key': 'sk-or-in-config', 'api_key_env': 'ANALYZE_TEST_MISSING'}).api_key,
                         'sk-or-in-config')
        with self.assertRaises(SystemExit) as e:
            analyze.Model.from_config({'api_key_env': 'sk-or-pasted-in-the-wrong-field'})
        self.assertNotIn('sk-or-pasted', str(e.exception))

    def test_a_secret_sits_in_its_block_or_in_the_variable_it_names(self):
        import os
        os.environ['ANALYZE_TEST_TOKEN'] = 'from-env'
        self.assertEqual(analyze.secret({'token': 'inline', 'token_env': 'ANALYZE_TEST_TOKEN'}, 'token', 'X'), 'inline')
        self.assertEqual(analyze.secret({'token_env': 'ANALYZE_TEST_TOKEN'}, 'token', 'X'), 'from-env')
        self.assertEqual(analyze.secret({}, 'token', 'ANALYZE_TEST_TOKEN'), 'from-env')
        self.assertEqual(analyze.secret(None, 'token', 'ANALYZE_TEST_UNSET'), '')

    def test_an_empty_answer_is_a_note_and_one_cut_short_stops_the_run(self):
        def notes(finish, reasoning=None):
            m = analyze.Model.from_config({'name': 'm', 'reasoning': reasoning})
            sent = []
            m.request = lambda path, body=None: sent.append(body) or {
                'choices': [{'finish_reason': finish, 'message': {'content': None, 'reasoning': '...'}}]}
            return analyze.analyze(m, [{'id': 'm1', 'text': 'hi'}], {})['notes'], sent[0]
        cut, body = notes('length', {'enabled': False})
        self.assertTrue(analyze.model_down(cut), cut)
        self.assertEqual(body['reasoning'], {'enabled': False})
        filtered, body = notes('content_filter')
        self.assertFalse(analyze.model_down(filtered), filtered)
        self.assertNotIn('reasoning', body)


class Remember(unittest.TestCase):
    def test_new_bodies_join_and_new_names_fold_into_the_body(self):
        context = {'bodies': [{'id': 'place/neighbors', 'name': 'the neighbors', 'aliases': []}]}
        analyze.remember(context, [{'id': 'place/neighbors', 'aliases': ['next door']},
                                   {'id': 'person/pat', 'name': 'Pat'}])
        self.assertEqual(context['bodies'], [{'id': 'place/neighbors', 'name': 'the neighbors', 'aliases': ['next door']},
                                             {'id': 'person/pat', 'name': 'Pat', 'aliases': []}])


class LocalTime(unittest.TestCase):
    def test_a_message_time_is_shown_on_the_owners_clock(self):
        import os
        import time
        was = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            self.assertEqual(analyze.local_time('2026-08-19T14:03:47Z'), '2026-08-19T10:03:47-04:00')
            self.assertEqual(analyze.local_time('2026-01-19T14:03:47Z'), '2026-01-19T09:03:47-05:00')
            #  what the model writes back on that clock lands in UTC
            self.assertEqual(analyze.iso_or_none('2026-08-19T11:30:00-04:00'), '2026-08-19T15:30:00Z')
            self.assertEqual(analyze.local_time(''), '')
        finally:
            if was is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = was
            time.tzset()

class Budget(unittest.TestCase):
    """The model block: an include merges blocks field by field, a reasoning
    model sends no temperature, and the budget is the block's."""

    def test_a_block_in_both_files_merges_field_by_field(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'generator'))
            with open(os.path.join(d, 'config.json'), 'w') as f:
                json.dump({'model': {'url': 'https://router.example/v1', 'name': 'small', 'api_key': 'k'}, 'state': 'root.json'}, f)
            with open(os.path.join(d, 'generator', 'config.json'), 'w') as f:
                json.dump({'include': '../config.json', 'model': {'name': 'big', 'reasoning': {'effort': 'high'}, 'max_tokens': 32000}}, f)
            cfg = analyze.load_config(os.path.join(d, 'generator', 'config.json'))
            self.assertEqual(cfg['model'], {'url': 'https://router.example/v1', 'name': 'big', 'api_key': 'k',
                                            'reasoning': {'effort': 'high'}, 'max_tokens': 32000})
            self.assertEqual(cfg['state'], 'root.json')
            #  a third file including the second reaches the first through it
            with open(os.path.join(d, 'generator', 'check.json'), 'w') as f:
                json.dump({'include': 'config.json', 'model': {'name': 'cheap', 'reasoning': {'enabled': False}}}, f)
            cfg = analyze.load_config(os.path.join(d, 'generator', 'check.json'))
            self.assertEqual(cfg['model'], {'url': 'https://router.example/v1', 'name': 'cheap', 'api_key': 'k',
                                            'reasoning': {'enabled': False}, 'max_tokens': 32000})

    def test_a_reasoning_model_gets_no_temperature(self):
        self.assertIsNone(analyze.Model.from_config({'url': 'http://x', 'reasoning': {'effort': 'high'}}).temperature)
        self.assertEqual(analyze.Model.from_config({'url': 'http://x', 'reasoning': {'enabled': False}}).temperature, 0.0)
        self.assertEqual(analyze.Model.from_config({'url': 'http://x'}).temperature, 0.0)
        self.assertIsNone(analyze.Model.from_config({'url': 'http://x', 'temperature': None}).temperature)
        self.assertEqual(analyze.Model.from_config({'url': 'http://x', 'max_tokens': 32000}).max_tokens, 32000)


class Decisions(unittest.TestCase):
    def test_the_decider_takes_the_router_key_and_rule_from_the_model_block(self):
        d = analyze.Decider.from_config({'model': {'url': 'https://openrouter.ai/api/v1', 'api_key': 'k', 'provider': {'zdr': True}}, 'decide': {'threshold': 0.3}})
        self.assertEqual((d.api_key, d.model, d.url, d.provider), ('k', 'typesafe/jev-1.13', analyze.DECISIONS_URL, {'zdr': True}))
        body = d.body({'message': 'x'}, analyze.GATE_QUESTION)
        self.assertEqual(body['model'], 'typesafe/jev-1.13')
        self.assertEqual(body['provider'], {'zdr': True})
        self.assertEqual(body['questions']['worth_reading']['type'], 'noul')
        self.assertIsNone(analyze.Decider.from_config({'model': {'url': 'http://localhost:1234/v1'}}))
        self.assertIsNone(analyze.Decider.from_config({'decide': {'enabled': False}}))
        with self.assertRaises(SystemExit):
            analyze.Decider.from_config({'model': {'url': 'http://localhost:1234/v1'}, 'decide': {}})

    def test_the_gate_reads_the_newest_message_with_the_earlier_ones_as_context(self):
        fake = analyze.FakeDecider({'worth_reading': {'type': 'noul', 'noul': 0.12}})
        window = [{'text': 'jury duty tomorrow', 'context': True}, {'text': 'ugh', 'who': 'person/sarah'}]
        p = analyze.gate(fake, window, {'bodies': [{'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']}]})
        self.assertEqual(p, 0.12)
        state = fake.asked[0][0]
        self.assertEqual((state['message'], state['from'], state['earlier']), ('ugh', 'person/sarah', ['jury duty tomorrow']))
        self.assertEqual(state['known_bodies'], ['person/sarah | Sarah | wife'])
        self.assertEqual(analyze.gate(analyze.FakeDecider({}), window, {}), 1.0)


class Picks(unittest.TestCase):
    """Bodies ranked for the decision model, the relevance pick, and the
    status check, on a fake decider."""

    CTX = {'me': 'person/me', 'bodies': [
        {'id': 'thing/subaru', 'name': 'the Subaru', 'aliases': ['the car']},
        {'id': 'situation/2026-09-16-breakdown', 'name': 'The breakdown', 'aliases': []},
        {'id': 'person/sarah', 'name': 'Sarah', 'aliases': ['wife']},
        {'id': 'person/me', 'name': 'me', 'aliases': []},
        {'id': 'activity/ballet', 'name': 'Ballet', 'aliases': []}]}
    WINDOW = [{'id': 'telegram/1/1', 'at': '2026-09-18T12:00:00Z', 'text': 'sarah is on jury duty', 'who': 'person/me'}]

    def test_rank_bodies(self):
        ids = [b['id'] for b in analyze.rank_bodies(self.CTX['bodies'], self.WINDOW)]
        self.assertEqual(ids, ['person/sarah', 'person/me', 'activity/ballet', 'situation/2026-09-16-breakdown', 'thing/subaru'])
        self.assertEqual(analyze.gate_state(self.WINDOW, self.CTX)['known_bodies'][0], 'person/sarah | Sarah | wife')

    def test_relevance_pick_and_chosen(self):
        fake = analyze.FakeDecider({'b0': {'type': 'noul', 'noul': 0.93}, 'b1': {'type': 'noul', 'noul': 0.04}, 'b2': {'type': 'noul', 'noul': 0.02}, 'b3': {'type': 'noul', 'noul': 0.01}, 'b4': {'type': 'noul', 'noul': 0.7}})
        picked = analyze.relevance_pick(fake, self.WINDOW, self.CTX)
        self.assertFalse(picked['failed'])
        self.assertEqual(picked['scores']['person/sarah'], 0.93)
        self.assertEqual(len(fake.asked), 1)
        self.assertIn('Is the new message about person/sarah (Sarah, wife)?', fake.asked[0][1]['b0']['instructions'])
        self.assertEqual(len(fake.asked[0][0]['known_bodies']), 5)
        shown = [b['id'] for b in analyze.chosen(self.CTX, self.WINDOW, picked, 0.5, 'person/me')]
        self.assertEqual(shown, ['person/sarah', 'person/me', 'thing/subaru'])

        class Down:
            def ask(self, s, q):
                raise RuntimeError('model unreachable')
        failed = analyze.relevance_pick(Down(), self.WINDOW, self.CTX)
        self.assertTrue(failed['failed'])
        self.assertEqual(len(analyze.chosen(self.CTX, self.WINDOW, failed, 0.5, 'person/me')), 5)

    def test_prompt_lists_only_the_shown(self):
        text = analyze.prompt(self.WINDOW, dict(self.CTX, channel='chat'), shown=self.CTX['bodies'][2:3])
        self.assertIn('person/sarah | Sarah | wife', text)
        self.assertNotIn('thing/subaru', text)

    def test_status_check(self):
        obs = [{'subject': 'person/sarah', 'attr': 'status', 'value': 'on jury duty'},
               {'subject': 'person/sarah', 'attr': 'status', 'value': 'want to scream'},
               {'subject': 'person/me', 'attr': 'status', 'value': 'at the dentist tomorrow'},
               {'subject': 'thing/subaru', 'attr': 'status', 'value': 'in the shop'},
               {'subject': 'person/sarah', 'attr': 'location', 'value': 'court'}]
        fake = analyze.FakeDecider({'status_0': {'type': 'choice', 'choice': 'circumstance', 'probabilities': {'circumstance': 0.99}},
                                    'status_1': {'type': 'choice', 'choice': 'feeling', 'probabilities': {'feeling': 0.97}},
                                    'status_2': {'type': 'choice', 'choice': 'neither', 'probabilities': {'neither': 0.55}}})
        kept, notes = analyze.status_check(fake, self.WINDOW, obs)
        self.assertEqual([o['value'] for o in kept], ['on jury duty', 'at the dentist tomorrow', 'in the shop', 'court'])
        self.assertIn('status "want to scream": 0.97 feeling, dropped', notes)
        self.assertIn('status "at the dentist tomorrow": uncertain, kept', notes)
        st, q = fake.asked[0]
        self.assertEqual([p['n'] for p in st['proposals']], [0, 1, 2])
        self.assertIn('The proposal is n=1: "want to scream"', q['status_1']['instructions'])
        self.assertEqual(analyze.status_check(fake, self.WINDOW, [obs[3]]), ([obs[3]], []))


class CalendarActions(unittest.TestCase):
    """A message that fixes a plan in time may propose a calendar event,
    held to the schema's payload shape; the reader's kinds come from the
    schema, never message or home."""

    STATE = {'me': 'person/me', 'bodies': [{'id': 'person/me', 'name': 'me', 'aliases': []}, {'id': 'person/sarah', 'name': 'Sarah', 'aliases': []}],
             'schema': {'kinds': {'person': {'attrs': ['status']}}, 'actions': ['task', 'note', 'message', 'home', 'calendar'],
                        'payloads': {'calendar': {'title': 'required', 'starts': 'required: ISO 8601 UTC', 'ends': 'optional: ISO 8601 UTC', 'location': 'optional'},
                                     'message': {'via': 'required: telegram', 'to': 'required', 'text': 'required'}}}}

    def test_kinds_and_shapes_come_from_the_schema(self):
        ctx = analyze.context_from_state(self.STATE, 'chat')
        self.assertEqual(ctx['action_kinds'], ['task', 'calendar'])
        self.assertEqual(list(ctx['payloads']), ['calendar'])
        self.assertEqual(analyze.context_from_state({'schema': {'kinds': {}}}, 'chat')['action_kinds'], ['task'])
        text = analyze.prompt([{'id': 'm1', 'at': '2026-09-19T12:00:00Z', 'who': 'person/me', 'text': 'x'}], ctx)
        self.assertIn('Action kinds you may propose: task, calendar', text)
        self.assertIn('calendar payload: {"title": "required"', text)

    def test_a_calendar_action_is_kept_with_its_payload_and_dropped_without_its_time(self):
        ctx = analyze.context_from_state(self.STATE, 'chat')
        msgs = [{'id': 'm1', 'at': '2026-09-19T12:00:00Z', 'who': 'person/me', 'text': 'dinner with sarah friday at 8'}]
        answer = {'bodies': [], 'observations': [], 'actions': [
            {'kind': 'calendar', 'title': 'Dinner with Sarah', 'about': ['person/sarah'], 'payload': {'title': 'Dinner with Sarah', 'starts': '2026-09-25T20:00:00-04:00', 'location': 'the usual place'}, 'message': 'm1'},
            {'kind': 'calendar', 'title': 'Something sometime', 'payload': {'title': 'Something sometime'}, 'message': 'm1'},
            {'kind': 'message', 'title': 'Tell Sarah', 'payload': {'via': 'telegram', 'to': 'person/sarah', 'text': 'x'}, 'message': 'm1'}]}
        got = analyze.validate(answer, msgs, ctx)
        self.assertEqual([a['kind'] for a in got['actions']], ['calendar'])
        self.assertEqual(got['actions'][0]['payload'], {'title': 'Dinner with Sarah', 'starts': '2026-09-26T00:00:00Z', 'location': 'the usual place'})
        self.assertIn('dropped action Something sometime: payload lacks starts', got['notes'])
        self.assertIn('dropped action: Tell Sarah', got['notes'])


if __name__ == '__main__':
    unittest.main()


class Vocabulary(unittest.TestCase):
    """A kind must be real, and a kind the schema speaks for keeps to its words."""

    def run_one(self, answer, context=None):
        return analyze.validate(answer, [{'id': 'm1', 'at': '2026-05-14T12:00:00Z', 'who': 'person/me', 'text': ''}],
                                context or CONTEXT)

    def test_the_prompts_own_placeholder_is_not_a_body(self):
        #  "kind/slug" is the literal example in the prompt and matches BID_RE
        self.assertTrue(analyze.BID_RE.match('kind/slug'))
        facts = self.run_one({'bodies': [{'id': 'kind/slug', 'name': 'whatever'}],
                              'observations': [{'subject': 'kind/slug', 'attr': 'status', 'value': 'open', 'message': 'm1'}],
                              'actions': []})
        self.assertEqual(facts['bodies'], [])
        self.assertEqual(facts['observations'], [])
        self.assertIn('dropped body of an unknown kind: kind/slug', facts['notes'])

    def test_an_invented_attr_on_a_listed_kind_is_dropped(self):
        facts = self.run_one({'bodies': [], 'actions': [], 'observations': [
            {'subject': 'person/me', 'attr': 'soreness', 'value': True, 'message': 'm1'},
            {'subject': 'person/me', 'attr': 'status', 'value': 'resting', 'message': 'm1'}]})
        self.assertEqual([(o['subject'], o['attr']) for o in facts['observations']], [('person/me', 'status')])
        self.assertIn('dropped person/me.soreness: not an attribute of person', facts['notes'])

    def test_a_redacted_sensitive_attr_is_dropped_with_its_own_reason(self):
        #  a key whose ship marks health sensitive is never served it in the schema,
        #  so it falls to the same rule as any unlisted name, with a clearer note
        facts = self.run_one({'bodies': [], 'actions': [], 'observations': [
            {'subject': 'person/me', 'attr': 'health', 'value': 'recovering', 'message': 'm1'}]})
        self.assertEqual(facts['observations'], [])
        self.assertIn("dropped person/me.health: the owner's policy keeps it from keys", facts['notes'])

    def test_a_sensitive_attr_the_schema_does_serve_is_kept(self):
        ctx = dict(CONTEXT, attrs=dict(CONTEXT['attrs'], person=['status', 'location', 'health']))
        facts = self.run_one({'bodies': [], 'actions': [], 'observations': [
            {'subject': 'person/me', 'attr': 'health', 'value': 'recovering', 'message': 'm1'}]}, ctx)
        self.assertEqual([o['attr'] for o in facts['observations']], ['health'])

    def test_a_kind_the_schema_is_silent_on_takes_any_attr(self):
        context = dict(CONTEXT, bodies=CONTEXT['bodies'] + [{'id': 'activity/pottery', 'name': 'Pottery', 'aliases': []}])
        facts = self.run_one({'bodies': [], 'actions': [], 'observations': [
            {'subject': 'activity/pottery', 'attr': 'cadence', 'value': 'weekly', 'message': 'm1'}]}, context)
        self.assertEqual([(o['subject'], o['attr']) for o in facts['observations']], [('activity/pottery', 'cadence')])
