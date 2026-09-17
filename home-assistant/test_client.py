"""The mapping and the executor, checked without Home Assistant or a ship:
fixtures/states.json through fixtures/mapping.json produces exactly
fixtures/expected.json, the cursor suppresses what was seen, alarms open
and close, and the allowlist decides what runs.

    cd home-assistant && python3 -m unittest
"""
import copy
import json
import os
import unittest

import client

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, 'fixtures')


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return json.load(f)


class Mapping(unittest.TestCase):
    def setUp(self):
        self.cfg = load('mapping.json')
        self.states = load('states.json')

    def test_first_pass_matches_expected(self):
        facts, cursor = client.map_states(self.cfg, self.states, {})
        self.assertEqual(facts.as_json(), load('expected.json'))
        self.assertEqual(cursor['alarms'], {'binary_sensor.basement_leak': 'situation/2026-09-17-basement-leak'})
        self.assertEqual(cursor['seen']['lock.front_door'], '2026-09-17T08:41:10+00:00')

    def test_notes_name_the_gaps(self):
        facts, _ = client.map_states(self.cfg, self.states, {})
        self.assertIn('switch.nowhere: not in home assistant', facts.notes)
        self.assertIn('sensor.garage_temperature: unavailable, nothing written', facts.notes)

    def test_seen_suppresses_everything(self):
        _, cursor = client.map_states(self.cfg, self.states, {})
        facts, again = client.map_states(self.cfg, self.states, cursor)
        self.assertTrue(facts.empty())
        self.assertEqual(again['seen'], cursor['seen'])

    def test_a_change_writes_one_row(self):
        _, cursor = client.map_states(self.cfg, self.states, {})
        states = copy.deepcopy(self.states)
        door = next(s for s in states if s['entity_id'] == 'lock.front_door')
        door.update({'state': 'unlocked', 'last_changed': '2026-09-17T17:00:00+00:00'})
        facts, _ = client.map_states(self.cfg, states, cursor)
        self.assertEqual(len(facts.observations), 1)
        o = facts.observations[0]
        self.assertEqual((o['subject'], o['attr'], o['value'], o['at']),
                         ('thing/front-door', 'locked', False, '2026-09-17T17:00:00Z'))
        self.assertEqual(o['source'], {'kind': 'home-assistant', 'id': 'lock.front_door@2026-09-17T17:00:00+00:00'})

    def test_presence(self):
        rows = {o['subject']: o['value'] for o in client.map_states(self.cfg, self.states, {})[0].observations
                if o['attr'] == 'location'}
        self.assertEqual(rows['person/sarah'], {'ref': 'place/home'})
        self.assertEqual(rows['person/me'], 'work')
        states = copy.deepcopy(self.states)
        phone = next(s for s in states if s['entity_id'] == 'device_tracker.sarah_phone')
        phone.update({'state': 'not_home', 'last_changed': '2026-09-17T10:00:00+00:00'})
        facts, _ = client.map_states(self.cfg, states, {'seen': {'device_tracker.sarah_phone': 'x'}})
        sarah = [o for o in facts.observations if o['subject'] == 'person/sarah']
        self.assertEqual(sarah[0]['value'], None)

    def test_until_from_remaining_minutes(self):
        facts, _ = client.map_states(self.cfg, self.states, {})
        washer = next(o for o in facts.observations if o['subject'] == 'thing/washer')
        self.assertEqual((washer['value'], washer['until']), ('running', '2026-09-17T09:45:00Z'))

    def test_alarm_closes(self):
        _, cursor = client.map_states(self.cfg, self.states, {})
        states = copy.deepcopy(self.states)
        leak = next(s for s in states if s['entity_id'] == 'binary_sensor.basement_leak')
        leak.update({'state': 'off', 'last_changed': '2026-09-17T11:00:00+00:00'})
        facts, after = client.map_states(self.cfg, states, cursor)
        attrs = {o['attr']: o['value'] for o in facts.observations}
        self.assertEqual(attrs['status'], 'closed')
        self.assertEqual(attrs['ended'], '2026-09-17T11:00:00Z')
        self.assertEqual(after['alarms'], {})

    def test_numeric(self):
        facts, _ = client.map_states(self.cfg, self.states, {})
        car = next(o for o in facts.observations if o['subject'] == 'thing/car')
        self.assertEqual(car['value'], 82)


class FakeShip(client.NoShip):
    def __init__(self, approved, claim=200):
        self.approved = approved
        self.claim = claim
        self.moves = []

    def actions(self, status):
        return self.approved if status == 'approved' else []

    def move(self, aid, status, note=''):
        self.moves.append((aid, status, note))
        return (self.claim if status == 'claimed' else 200), None


class FakeHass(client.NoHass):
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    def call(self, domain, service, data):
        self.calls.append((domain + '.' + service, data))
        return self.ok, '' if self.ok else 'home assistant answered 500: boom'


class Executor(unittest.TestCase):
    def setUp(self):
        self.cfg = load('mapping.json')

    def test_allowlist(self):
        ok = {'service': 'light.turn_on', 'entity_id': 'light.porch', 'data': {'brightness': 120}}
        self.assertIsNone(client.check_payload(ok, self.cfg['allow']))
        self.assertIn('not allowed', client.check_payload({'service': 'lock.unlock', 'entity_id': 'lock.front_door'}, self.cfg['allow']))
        self.assertIn('not allowed', client.check_payload({'service': 'switch.turn_on', 'entity_id': 'switch.heater'}, self.cfg['allow']))
        self.assertIn('service', client.check_payload({'service': 'turn_on', 'entity_id': 'light.porch'}, self.cfg['allow']))
        self.assertIn('entity_id', client.check_payload({'service': 'light.turn_on', 'entity_id': 'porch light'}, self.cfg['allow']))
        self.assertIn('object', client.check_payload('light.turn_on', self.cfg['allow']))

    def test_runs_allowed_and_fails_the_rest(self):
        ship = FakeShip([
            {'id': 'a1', 'kind': 'home', 'payload': {'service': 'light.turn_on', 'entity_id': 'light.porch'}},
            {'id': 'a2', 'kind': 'home', 'payload': {'service': 'lock.unlock', 'entity_id': 'lock.front_door'}},
            {'id': 'a3', 'kind': 'task', 'payload': {}},
        ])
        hass = FakeHass()
        state = {}
        client.execute(self.cfg, ship, hass, state)
        self.assertEqual(hass.calls, [('light.turn_on', {'entity_id': 'light.porch'})])
        self.assertEqual([m[:2] for m in ship.moves],
                         [('a1', 'claimed'), ('a1', 'done'), ('a2', 'claimed'), ('a2', 'failed')])
        self.assertIn('not allowed', ship.moves[3][2])
        self.assertEqual(state['executed'], ['a1', 'a2'])

    def test_service_failure_is_reported_once(self):
        ship = FakeShip([{'id': 'a1', 'kind': 'home', 'payload': {'service': 'light.turn_on', 'entity_id': 'light.porch'}}])
        hass = FakeHass(ok=False)
        state = {}
        client.execute(self.cfg, ship, hass, state)
        self.assertEqual(ship.moves, [('a1', 'claimed', ''), ('a1', 'failed', 'home assistant answered 500: boom')])
        client.execute(self.cfg, ship, hass, state)
        self.assertEqual(len(hass.calls), 1)

    def test_a_refused_claim_leaves_the_action_alone(self):
        ship = FakeShip([{'id': 'a1', 'kind': 'home', 'payload': {'service': 'light.turn_on', 'entity_id': 'light.porch'}}], claim=409)
        hass = FakeHass()
        state = {}
        client.execute(self.cfg, ship, hass, state)
        self.assertEqual(hass.calls, [])
        self.assertEqual([m[1] for m in ship.moves], ['claimed'])
        self.assertEqual(state['executed'], [])


if __name__ == '__main__':
    unittest.main()
