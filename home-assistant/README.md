# home-assistant: a Home Assistant client for orrery

Both directions. It reads the entities you map, turns their state changes into observations and sends them to orrery with a scoped key. It also carries out actions of kind `home` after the owner approves them, by calling Home Assistant services within an allowlist, and reports done or failed on each.

Standard library only. One file, `client.py`. It polls Home Assistant's REST API; a run reads every state once, writes only what changed since the last run, and executes what is waiting.

## What it writes

You name the entities in `config.json`. Everything else in Home Assistant is ignored. Three kinds of entry:

| kind | entity | what the client writes |
|---|---|---|
| `presence` | a `device_tracker` or `person` | `<body>.location = {"ref": "place/home"}` when the state is `home`, `null` when it is `not_home`, or the zone name as a string. The body is the person's, usually `person/sarah` or `person/me`; it must already exist |
| `attr` | anything | `<body>.<attr> = <state>`. `values` translates states (`{"locked": true, "unlocked": false}`); `numeric` writes a number; `ignore` lists states to skip; `until_minutes_attr` names a Home Assistant attribute holding minutes left, and the observation expires then. The body is created with `name` or the entity's friendly name |
| `alarm` | a `binary_sensor` | `on` opens `situation/<date>-<name>` with `status` `open`, `started`, `participants` `person/me` and `location` from the entry; `off` writes `status` `closed` and `ended` on the situation it opened |

`at` is the entity's `last_changed`, which is when the fact became true, not when the client saw it. `source` is `{"kind": "home-assistant", "id": "<entity_id>@<last_changed>"}`, so a state the client reads twice is one observation on the ship. States `unavailable` and `unknown` write nothing.

Sensors chatter. Map the entities whose changes mean something in a life (someone came home, the door is unlocked, the basement is wet, the car is charged), not the ones that report every minute. A room temperature every minute is noise the analyst wades through; a situation named "basement leak" is a fact worth an action.

## What it executes

The assistant proposes an action of kind `home`:

```json
{"kind": "home", "title": "Turn the porch light on",
 "payload": {"service": "light.turn_on", "entity_id": "light.porch", "data": {"brightness": 120}}}
```

The owner approves it (keep `home` off the policy's `auto` list). The client polls approved actions, keeps the ones of kind `home`, claims each one for ten minutes before it acts, checks the payload against `allow`, calls the service with `entity_id` and `data`, and reports `done`, or `failed` with the reason. A claim the ship refuses means another client holds that action, so this client skips it for the pass and never calls the service.

`allow` is a list of patterns matched against `"<service> <entity_id>"`: `"light.* light.*"` lets any light service run on any light; `"switch.turn_off switch.*"` lets switches be turned off but not on; `"climate.set_temperature climate.living_room"` names one thing exactly. An empty list runs nothing. A payload that does not match fails with a note saying what to add. Nothing in this client unlocks a door unless you write `lock.unlock` into the list yourself.

An action is executed once. Its id goes into `state.json` after the report, and a run that finds it approved again skips it.

## The key

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "home assistant on the hub", "by": "home",
  "scope": {"kinds": ["person", "place", "thing", "situation"], "actions": ["home"], "write": true}
}'
```

`write` is needed for the observations and for moving actions to done or failed. The key sees only those four kinds and the `home` actions.

## Configuration

Copy `config.example.json` to `config.json` (git ignores it). Tokens live in the environment: a Home Assistant long-lived access token in `HASS_TOKEN`, the orrery key in `ORRERY_TOKEN`, or the names the config gives.

```json
{
  "home_assistant": {"url": "http://homeassistant.local:8123", "token_env": "HASS_TOKEN"},
  "orrery": {"url": "https://your-ship.example", "token_env": "ORRERY_TOKEN"},
  "state": "state.json",
  "home": "place/home",
  "map": [
    {"entity": "device_tracker.sarah_phone", "kind": "presence", "body": "person/sarah"},
    {"entity": "lock.front_door", "kind": "attr", "body": "thing/front-door", "attr": "locked", "name": "the front door", "values": {"locked": true, "unlocked": false}},
    {"entity": "sensor.washer", "kind": "attr", "body": "thing/washer", "attr": "status", "name": "the washing machine", "until_minutes_attr": "remaining_minutes", "ignore": ["off"]},
    {"entity": "binary_sensor.basement_leak", "kind": "alarm", "name": "basement leak", "location": "the basement"}
  ],
  "allow": ["light.* light.*", "switch.turn_off switch.*"]
}
```

`home` is the body a `presence` entry points at when someone is home; create `place/home` once on the ship.

## Running it

```bash
cd home-assistant
python3 -m unittest                                                        # mapping and executor, no network
python3 client.py --dry-run --config fixtures/mapping.json --states fixtures/states.json   # the fixture, printed
python3 client.py --config config.json --dry-run                           # your entities, printed, nothing sent
HASS_TOKEN=... ORRERY_TOKEN=... python3 client.py --config config.json     # one pass
HASS_TOKEN=... ORRERY_TOKEN=... python3 client.py --config config.json --loop 60   # every minute
```

A dry run still reads Home Assistant (a read only call) unless `--states` names a file; it never calls a service and never contacts the ship. The cursor is `seen`, each mapped entity's last `last_changed`, plus the situations alarms opened and the actions executed, in `state.json`. A batch the ship refuses whole leaves the cursor where it was, so the next run sends it again; the same claim twice is one observation.

## What stays here

Every entity you did not map, every attribute of the ones you did, the readings between changes, and the tokens. The ship gets the mapped facts and, for each, the entity id and the timestamp that identify the change.

## Fixtures

`fixtures/states.json` is a snapshot of `GET /api/states` with nine entities, `fixtures/mapping.json` maps eight of them (one is missing on purpose, one is unavailable), and `fixtures/expected.json` is the exact batch a first pass produces. `test_client.py` checks that, the cursor, presence, expiry, an alarm closing, numbers, the allowlist and the executor against stubs. When you change the mapping code, change the fixture that shows it and regenerate `expected.json` from a dry run.

## Limits

- Polling, not a subscription. A change that reverts between two polls is missed; run every minute for doors and presence.
- `presence` needs the person's body to exist. A tracker for a person the ship does not have is refused per item, with the subject named, and the cursor moves on.
- Zone names are written as strings. Map a zone to a place body by making the zone a `place` and changing the string by hand, or wait for a `zones` entry.
- One alarm situation per entity per day; a sensor that trips twice in a day reopens the same situation.

Checked end to end on a dev ship on 2026-09-17 with the fixture states and a stub Home Assistant: the observations landed under a key scoped as above, an approved `light.turn_on` action reached the stub with its `entity_id` and `data` and was marked `done` by `home`, and an approved `lock.unlock` outside the allowlist was marked `failed` with the note naming the pattern to add.
