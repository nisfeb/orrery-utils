# orrery-utils

Clients that feed [orrery](https://github.com/nisfeb/orrery) from outside Urbit, and act on what it proposes. Orrery keeps the model of your world on your ship and runs no AI. Everything that reads a mailbox, a calendar, a house or a phone lives here, off the ship, talking to orrery's HTTP API with a scoped key.

Each integration is one directory with its own README. The first three are `mail`, an email reader, `home-assistant`, a Home Assistant client, and `telegram`, a bot that captures facts people type to it and delivers approved messages. Anyone can add one; the conventions below are what make them fit together.

## How an integration talks to orrery

An integration is a program that runs somewhere you trust, holds a key for your ship, and does one or both of two jobs: it turns data from a source into facts and sends them in, or it carries out the actions your assistant proposed and you approved.

### A key

The owner mints a key on the ship and gives the integration the token once. The scope names the body kinds it may see and write, the action kinds it may propose and execute, and whether it may write at all.

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "mail reader on the laptop", "by": "mail",
  "scope": {"kinds": ["person", "org", "thing", "situation"], "actions": ["task"], "write": true, "sensitive": "write"}
}'
```

The answer carries the token once; the ship keeps a salted hash. The integration sends it as `Authorization: Bearer <token>` with no cookie. `"sensitive": "write"` (orrery version 13) lets a reader store the medical and money facts it learns from other people under `health` and `income` while every view keeps hiding them from it: it writes what it cannot read. Set `"sensitive_write": true` in the reader's config so the analyst keeps those rows instead of dropping them as unlisted. Ask for the smallest scope that does the job: a reader that only files tasks needs no action kinds but `task`, and a client that only reads needs `"write": false`.

Everything a key writes is signed with the key's identity, whatever the request says, and a key never sees an attribute the owner listed under `sensitive` in the policy. The full rules are in orrery's `docs/keys.md`.

### Facts in

A fact is an observation: one claim about one body, with a pointer to where it came from. Send them in batches to `POST /apps/orrery/api/observe`, bodies first, then observations.

```json
{
  "bodies": [{"id": "thing/order-4471", "name": "the standing desk", "aliases": ["order 4471"]}],
  "observations": [
    {"subject": "thing/order-4471", "attr": "status", "value": "shipped",
     "at": "2026-09-17T14:02:00Z", "until": "2026-09-19T00:00:00Z", "conf": 90,
     "source": {"kind": "mail", "id": "<2b7f9c@shop.example>"}}
  ]
}
```

The pieces that matter:

- `source` is a kind and an opaque id. The kind is your integration's name; the id is the source's own stable id for the item, such as a Message-ID, an entity id with its timestamp, or a calendar uid. The ship keeps the pointer and never the text.
- `at` is when the fact became true, not when you read it. A receipt read at midnight about a delivery at noon carries noon. Times are ISO 8601 UTC.
- `until` says when the fact is expected to stop being true. Use it for anything that expires on its own: a delivery window, a meeting, a washing cycle.
- `conf` is how sure you are, 0 to 100. A rule that matched a shipping notice is 90; a model's guess about a vague message is 60.
- A value of `null` clears an attribute without claiming the new state. A value of `{"ref": "place/home"}` points at another body.
- The answer has one line per item, so you can log the one that was refused and keep the rest.

The same claim from the same source is the same observation, so sending it twice is a no-op and answers `existing: true`. An integration can replay its whole history after a crash and nothing doubles.

Caps: 50 bodies and 200 observations per batch. Send more batches, not bigger ones.

### Names

Before creating a body, ask the ship whether it already has one: `GET /apps/orrery/api/resolve?q=<name>` matches names and aliases, exact first. When nothing matches, create the body in the same batch as the observations about it, with the aliases you saw it under. Never guess a body's `ship`; identity is the owner's to set.

### Reading

`GET /apps/orrery/api/state` is everything the key may see: every body with its current attributes, the open situations, the open actions and the schema. `GET /apps/orrery/api/body/<kind>/<slug>` adds the timeline. Read before you write when you would otherwise repeat a fact or propose a duplicate action.

The state view carries `rev`, which moves on every write. A key cannot read the ship's beacon stream, so an integration that needs to notice changes polls `rev` on a schedule or simply runs on one.

### Actions out

Two roles. A proposer files something to do with `POST /apps/orrery/api/act`: a task, a note, or a kind that some client will carry out.

```json
{"kind": "task", "title": "Pay the electricity bill", "about": ["org/utility-co"], "due": "2026-09-30T00:00:00Z"}
```

The answer says `approved` when the owner's policy auto-approves that kind, or `proposed` when it waits for the owner. A proposal whose kind and title match an open action answers the existing id.

An executor carries out actions of its own kind. The protocol is poll, claim, read back, act, report, and it is five steps because a write answers before the ship has applied it.

1. Poll `GET /apps/orrery/api/actions?status=open` and keep the ones of your kind whose status is `approved` or `claimed`. Not `?status=approved`: an action a dead executor claimed and never finished only ever shows as `claimed`, and skipping those is how one leaves it stranded.
2. Claim it with `POST /apps/orrery/api/actions/<id>` and `{"status": "claimed"}`. Anything but 200 means another executor holds a live lease, so skip that action this pass and leave your cursor alone.
3. Read it back. The claim answered before the writer applied it, and it carries the `by` the ship stores for your step, so fetch `GET /apps/orrery/api/actions?status=claimed` and act only when the last `claimed` step of your action names that `by`. A read that still shows the action `approved` means the writer has not caught up: read again, a few times, a fifth of a second apart. Anything else, including a claim another executor won, means skip it.
4. Do the work.
5. Report with `{"status": "done"}` or `{"status": "failed", "note": "why"}` on the same route. Only the claimant may report either.

The claim is a ten minute lease. A claimed action leaves the `approved` list but stays in `open`, so after ten minutes the next executor to poll claims it again and the work is retried rather than stranded. The ship holds `task` and `note` itself; every other kind (`message`, `calendar`, `home`, ...) exists only because some executor here claims it. Leave executor kinds off the policy's `auto` list so the owner approves them; a light switching on because a model asked is exactly the kind of thing the inbox is for.

## The rules every integration follows

- Pointers, never text. The ship holds a source kind and id; the mail, the transcript and the sensor log stay with you.
- Event time in `at`, honest `conf`, `until` on anything that expires.
- The smallest scope that works. Say in your README which kinds and actions you need and why.
- Sensitive facts are the owner's call. If your source yields health, money or location facts, name the attributes you would write so the owner can list them under `sensitive` or tell you to skip them.
- Idempotent runs. Keep a cursor (the last id or timestamp you handled) and treat replay as harmless, because it is.
- The schema's names. Use the attribute names orrery's starter schema has (`status`, `location`, `phone`, `email`, `owner`, `participants`, ...). When you need a new one, propose it as a schema addition in your README rather than inventing a private name; the schema is advisory vocabulary for the models that read the state, and one name per fact is what makes it useful.
- A dry run. Every integration prints the batch it would send without sending it, so a mapping can be checked without a ship.
- Test data. Ship a small fixture of sample input and the batch it produces, checked in, so the mapping can be tested without credentials.

## Source kinds and ids

| integration | `source.kind` | `source.id` |
|---|---|---|
| mail | `mail` | the RFC 5322 Message-ID |
| home-assistant | `home-assistant` | `<entity_id>@<last_changed>` |
| a calendar | `calendar` | the event uid, with the recurrence id when there is one |
| a chat platform | `chat` | `<platform>/<conversation>/<message id>` |
| a location feed | `location` | `<device>@<timestamp>` |
| the owner by hand | `user` | anything you like |

The `by` on what you write is the key's identity. Name keys after the integration and where it runs.

## The first integrations

### mail: an email reader

Watches a mailbox over IMAP, reads what arrived since its cursor, decides with rules and a small local model what each message says about the world, and submits it. It never sends the message. Scope: kinds `person`, `org`, `thing`, `situation`; actions `task`; write.

| the message | what the reader writes |
|---|---|
| a shipping notice with an arrival day | `thing/<order>.status = "shipped"`, `location = "in transit"`, `until` the arrival day; the body created with the order number as an alias |
| a flight or hotel confirmation | `situation/<date>-<trip>` with `participants`, `location`, `started` and `ended` |
| an invoice with a due date | an action: `task` "Pay <org> <amount>", `about` the org, `due` the date |
| a person writing from a new address | `person/<x>.email = <address>`, `conf` 80 |
| a reply that says where someone is or what they are doing | `person/<x>.location` or `.status`, `conf` from the model, `until` when the message implies one |
| a calendar invitation | nothing; the calendar integration owns those |
| newsletters, receipts already seen, marketing | nothing |

Mail is where sensitive facts arrive: test results, statements, salaries. The reader maps those to `health` and `income` and nothing else, so one line in the policy's `sensitive` list keeps them from every key.

### home-assistant: a Home Assistant client

Both directions. It polls Home Assistant's REST API, turns the state changes of the entities you mapped into observations, and executes `home` actions after the owner approves them, within an allowlist of services. Scope: kinds `person`, `place`, `thing`, `situation`; actions `home`; write.

| Home Assistant | what the client writes |
|---|---|
| a person's device tracker goes `home` or `not_home` | `person/<x>.location = {"ref": "place/home"}`, or `null` when they leave |
| a door lock, a garage door, an alarm panel | `thing/<door>.locked`, `thing/garage.status`, `thing/alarm.status` |
| an appliance starts a cycle with a known length | `thing/washer.status = "running"`, `until` the end of the cycle |
| a car charger or an EV integration | `thing/<car>.charge`, `.location = {"ref": "place/home"}` while plugged in |
| a leak, smoke or CO sensor trips | `situation/<date>-<sensor>` open, `location` the room, `participants` everyone home |
| a thermostat setpoint or mode changes | `place/home.climate = <mode>`; readings themselves stay in Home Assistant |

Sensors chatter. The client writes on meaningful change only, never on every reading, and keeps its cursor on `last_changed`. An observation about a room temperature every minute is noise the analyst has to wade through; a situation named "the basement is wet" is a fact worth an action.

Actions: the analyst proposes `{"kind": "home", "title": "Turn the porch light on", "payload": {"service": "light.turn_on", "entity_id": "light.porch"}}`. The client polls approved `home` actions, calls the service, and reports done or failed with the error text. Keep `home` off `auto`.

### telegram: a bot for capture and delivery

A Bot API client with long polling. People you map tell it facts in a short grammar, in a private chat or a group it sits in: `/at Route 9`, `/status stranded, waiting for a tow`, `/obs thing/subaru status broken down`, `/task Call the shop due 2026-09-18`. The sender's own body is the subject of `/at` and `/status`, so each person reports on themselves. Free text goes to a model hook that does nothing today. Scope: kinds `person`, `place`, `thing`, `situation`; actions `task`, `message`; write.

The same bot delivers approved `message` actions whose payload says `via` `telegram` and names a person it knows, and reports done or failed. Keep `message` off `auto`, so no text leaves without a human reading it. A bot sees only what is sent to it; it never reads your own conversations with other people.

## More sources worth writing

Ranked by how much of the world they explain per hour of work. Each is one directory when it exists.

| source | what it yields | notes |
|---|---|---|
| a calendar (CalDAV, Google) | `situation` per meeting or trip with `participants` and `location`; `person/<x>.status = "in a meeting"` with `until`; a `calendar` executor that creates events the assistant proposes | the highest value after mail; the trip situation ties flights, hotels and people together |
| chat and DMs (Signal, Telegram, Matrix, Slack, SMS) | whereabouts, plans and status from what people say, the way the stranded-car story in orrery's README works; participants become `person` bodies | a local model does the reading; nothing leaves the client but the pointer |
| location (OwnTracks, a phone, a device tracker) | `person/me.location` with `conf` from accuracy; `place` bodies from geofences | consider marking `location` sensitive if keys other than this one exist |
| contacts (CardDAV, a phone) | `person` bodies with `phone`, `email`, `birthday`, `relationship`; aliases from nicknames | the seed that makes every other reader's resolve calls succeed |
| vehicles (OBD dongles, manufacturer APIs) | `thing/<car>` with `odometer`, `fuel` or `charge`, `location`, `last-service`; a `task` when service is due | pairs with home-assistant's charger facts |
| packages (carrier tracking) | `thing/<parcel>.status` with `until` the ETA; a `situation` when one is delayed | mail usually knows the tracking number first |
| finance (bank and card exports, receipts) | `org` relationships, purchases as `thing` bodies, a trip `situation` from hotel charges, a `task` per bill due | `income` and balances are sensitive by default |
| health (wearables, a scale) | `person/me` sleep, steps, resting heart rate | `health` is sensitive by default; write daily summaries, not samples |
| tasks (Todoist, Things, Reminders) | two-way: approved `task` actions appear in the app, completion comes back as `done` | the owner keeps one todo list, not two |
| weather and transit | a `situation` at `place/home` or on an open trip: a storm warning, a cancelled train, with `until` | little value alone, real value attached to a trip |
| news watchlists (RSS) | `note` bodies about an `org` you care about (employer, landlord, school), low `conf` | skip unless the owner names the orgs |
| photos (EXIF) | `person/me.location` at the time a photo was taken, for backfill | low `conf`, and only ever the owner's own photos |
| voice memos and smart speakers | a transcribed ask becomes a `task` or an observation the owner dictated | the transcription stays on the client |

## The analyst and the past

`common/analyze.py` is the one piece the readers share: it hands a window of messages to a local model (LM Studio's OpenAI-compatible server at `http://localhost:1234/v1`, any model it lists) and validates the answer into orrery's shapes before anything is sent: ids well formed, subjects known or created in the same answer, values bounded, times parseable. The mail reader and the Telegram bot use it for the text their rules do not claim; a `model` block in each config turns it on. The message text goes to the model on your machine and nowhere else.

To build state from what already happened, both readers can run over the past. `mail/reader.py --months 6` reads a folder from that day on through the rules and the model, resumably, and `telegram/backfill.py --export result.json --months 6` does the same for a Telegram Desktop export. `at` is each message's own time, so the facts land where they belong and the timeline reads as it happened.

## Associating what belongs together

Readers see one message at a time, so two of them, or one of them on two days, can name the same thing twice: "Dana" and "Dana Quill", or a situation per occurrence of a class that meets twice a week. Three things keep the model in one piece. Orrery's resolve matches a query against names, aliases, email and phone, exact first and then by word containment, so a reader that resolves before it creates usually finds what is already there. The analyst folds twins the model makes anyway into the bodies the ship has, by normalised title for situations and activities and by name words for people. And `common/reconcile.py` cleans up after the fact: occurrences become one `activity` with an observation per occurrence, bodies that name one person become `merge` proposals the owner approves, applied through orrery's merge op, and situations that are over are closed at the time they ended.

Any client that writes to the ship, in this repo or outside it, follows `docs/writing-a-client.md`: resolve before creating, occurrences on activities, never `status: open` for an event, event time in `at`, a key rather than the cookie, no cache pushed back, and never the same message twice.

## Writing a new integration

1. Make a directory named after the source. Its README carries the mapping table (source field to body kind and attribute), the scope the key needs and why, the source kind and id form, what stays on the client, and how to run it.
2. Add a config example with no secrets in it, and a fixture: one sample input and the exact batch it produces.
3. Give it a dry-run flag that prints batches instead of sending them.
4. Run it against a dev ship before a real one. Orrery's `docs/releasing.md` section 7 describes the fake ship and its cookie; `scripts/api-matrix.py` there shows every route being exercised.
5. Keep the raw data on the client, keep a cursor, and make replay harmless.

Any language. The API is JSON over HTTP; a few dozen lines of Python with `requests` is a complete client. A shared library comes when two integrations need the same code, not before.
