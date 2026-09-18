"""The bench without a ship or a model: the blanked state, one fake model's
turn, and the report.

    cd generator && python3 -m unittest test_bench
"""
import json
import unittest

import bench
import run
from test_run import STATE, NOW


class Bench(unittest.TestCase):
    def test_blank_state_keeps_bodies_and_drops_open_actions(self):
        b = bench.blank(STATE)
        self.assertEqual(b['actions'], [])
        self.assertEqual(len(b['bodies']), len(STATE['bodies']))
        self.assertEqual(STATE['actions'][0]['title'], 'Call the shop about the Subaru')

    def test_ask_and_report(self):
        state = bench.blank(STATE)
        model = run.analyze.FakeModel(json.dumps({'actions': [
            {'kind': 'task', 'title': 'Call the shop about the Subaru', 'about': ['thing/subaru'], 'why': 'two days at the shop'},
            {'kind': 'email', 'title': 'nope'}], 'notes': ['the trip has no end']}))
        model.last_usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': 5}, 'cost': 0.001}
        parts = run.build_parts(state, [], NOW, 'UTC', 5)
        r = bench.ask(model, 'sys', '\n'.join(parts), parts, state, 5)
        #  the open action of the same title is not there to drop it
        self.assertEqual([p['title'] for p in r['proposals']], ['Call the shop about the Subaru'])
        self.assertIsNone(r['error'])
        text = bench.report([('fake/one', r), ('fake/two', {'proposals': [], 'notes': [], 'usage': None, 'error': 'model unreachable', 'seconds': 1})], 'stamp')
        self.assertIn('| fake/one | $0.0010 | 100 | 20 (5) |', text)
        self.assertIn('- **Call the shop about the Subaru** (task; about thing/subaru): two days at the shop', text)
        self.assertIn('- note: dropped: kind email or no title (nope)', text)
        self.assertIn('- note: model note: the trip has no end', text)
        self.assertIn('error: model unreachable', text)
        self.assertEqual(bench.slug('deepseek/deepseek-v4.1-flash'), 'deepseek-deepseek-v4.1-flash')


if __name__ == '__main__':
    unittest.main()
