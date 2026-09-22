# orrery-utils

The companion toolkit to [orrery](https://github.com/nisfeb/orrery), an Urbit app that keeps a model of your world on your ship and runs no AI itself. Everything that reads a mailbox, a house or a chat, and everything that calls a model, lives here, off the ship, talking to orrery's HTTP API with a scoped key. Python 3, standard library only, no packages to install.

## What is here

| directory | what it is | run it with |
|---|---|---|
| `common/` | the shared prompts (`analyst-prompt.md`, `generator-prompt.md`, `refine-prompt.md`, `brief-prompt.md`), `analyze.py`, the analyst every reader calls, and `reconcile.py`, the cleanup passes | `cd common && python3 -m unittest`; `python3 reconcile.py --help` |
| `telegram/` | the Telegram bot: a backfill over a Telegram Desktop export, a dry-run harness for the gate and the model, and a reader and sender for a ship older than orrery 34 (newer ships read and send Telegram themselves) | `python3 bot.py --config config.json`; `python3 backfill.py --export result.json` |
| `mail/` | an IMAP reader: rules first, then the model, for what mail says about the world | `python3 reader.py --config config.json` |
| `home-assistant/` | a Home Assistant client, both directions: presence, locks and appliances in, approved `home` actions out | `python3 client.py --config config.json --loop 60` |
| `generator/` | the action generator's bench and dry run: the same state to several models, side by side, with what each cost | `python3 bench.py --config config.json --models a,b` |
| `console.py` | a terminal console that runs the integrations as systemd user units | `python3 console.py` |
| `docs/writing-a-client.md` | the rules a client follows so that it does not fight the ship | read it before writing one |

Three of the prompts in `common/` are mirrored byte for byte inside orrery itself (`analyst-prompt.md`, `generator-prompt.md`, `refine-prompt.md`), and orrery's `scripts/prompt-drift.py` holds the two copies together; a change to one of those files needs the same change there.

Each integration is one directory with its own README, a `config.example.json` with no secrets in it, a fixture (sample input and the exact batch it produces), tests, and a `util.json` the console reads. Anyone can add one; the conventions below are what make them fit together.

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

The answer carries the token once; the ship keeps a salted hash. The integration sends it as `Authorization: Bearer <token>` with no cookie. `"sensitive": "write"` (orrery version 13) lets a reader store the medical and money facts it learns from other people under `health` and `income` while every view keeps hiding them from it: it writes what it cannot read. Such a key's schema view lists the two names, so the analyst keeps those rows; a key without the flag never sees the names and the analyst drops them as unlisted. Ask for the smallest scope that does the job: a reader that only files tasks needs no action kinds but `task`, and a client that only reads needs `"write": false`.

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

## The integrations

### mail: an email reader

Watches a mailbox over IMAP, reads what arrived since its cursor, decides with rules and a model (local, or hosted through the shared config) what each message says about the world, and submits it. It never sends the message. Scope: kinds `person`, `org`, `thing`, `situation`; actions `task`; write.

| the message | what the reader writes |
|---|---|
| a shipping notice with an arrival day | `thing/<order>.status = "shipped"`, `location = "in transit"`, `until` the arrival day; the body named for the product and seller, the order number as an alias. A notice that names no product, no arrival day and no tracking number is skipped |
| a flight or hotel confirmation | `situation/<date>-trip` with `status` open, `participants` `person/me` and `started` |
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
| a leak, smoke or CO sensor trips | `situation/<date>-<sensor>` open, `location` the room, `participants` `person/me` |
| a thermostat setpoint or mode changes | `place/home.climate = <mode>`; readings themselves stay in Home Assistant |

Sensors chatter. The client writes on meaningful change only, never on every reading, and keeps its cursor on `last_changed`. An observation about a room temperature every minute is noise the analyst has to wade through; a situation named "the basement is wet" is a fact worth an action.

Actions: the analyst proposes `{"kind": "home", "title": "Turn the porch light on", "payload": {"service": "light.turn_on", "entity_id": "light.porch"}}`. The client polls approved `home` actions, calls the service, and reports done or failed with the error text. Keep `home` off `auto`.

### telegram: a bot for capture and delivery

A Bot API client with long polling. People you map tell it facts in a short grammar, in a private chat or a group it sits in: `/at Route 9`, `/status stranded, waiting for a tow`, `/obs thing/subaru status broken down`, `/task Call the shop due 2026-09-18`. The sender's own body is the subject of `/at` and `/status`, so each person reports on themselves. Free text goes to the model with the chat's last four messages as context, and what it answers is held to rules that trace each fact to its message. Scope: kinds `person`, `place`, `thing`, `situation`; actions `task`, `message`; write.

From orrery version 29 the ship reads Telegram itself through a webhook, and from version 34 it sends: an approved `message` action whose payload says `via` `telegram` goes out through the token on the ship's Telegram card the moment the owner approves it. On such a ship the bot is the backfill (`backfill.py`, for what came before any reader was connected) and the dry-run harness, and it must not poll beside the webhook, which Telegram enforces by answering the long poll with 409. On an older ship it is the reader and the sender. Keep `message` off `auto`, so no text leaves without a human reading it. A bot sees only what is sent to it, unless you connect it to your account with Telegram Business (Premium), which lets it read the private chats you pick as they arrive; groups need the bot as a member.

## More sources worth writing

Ranked by how much of the world they explain per hour of work. Each is one directory when it exists.

| source | what it yields | notes |
|---|---|---|
| a calendar (CalDAV, Google) | `situation` per meeting or trip with `participants` and `location`; `person/<x>.status = "in a meeting"` with `until`; a `calendar` executor that creates events the assistant proposes | the highest value after mail; the trip situation ties flights, hotels and people together |
| chat and DMs (Signal, Matrix, Slack, SMS) | whereabouts, plans and status from what people say, the way the stranded-car story in orrery's README works; participants become `person` bodies | the analyst does the reading, as it does for Telegram; the ship gets facts and pointers |
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

`common/analyze.py` is the one piece the readers share: it hands a window of messages to a model (LM Studio's OpenAI-compatible server at `http://localhost:1234/v1` by default, or a hosted one, below) and validates the answer into orrery's shapes before anything is sent: ids well formed, subjects known or created in the same answer, values bounded, times parseable. The mail reader and the Telegram bot use it for the text their rules do not claim; a `model` block in each config, or in the shared one below, turns it on. With a local model the message text stays on your machine; with a hosted one it goes to that host.

To build state from what already happened, both readers can run over the past. `mail/reader.py --months 6` reads each folder from that day on through the rules and the model, up to `--limit` messages per folder per run, resumably (run it again to continue), and `telegram/backfill.py --export result.json --months 6` does the same for a Telegram Desktop export. `at` is each message's own time, so the facts land where they belong and the timeline reads as it happened.

### One model block for every reader

The readers that call the analyst (mail and both Telegram programs) take their `model` block from a shared `config.json` at the root of the repo when their own config says `"include": "../config.json"`. A key the reader's own file sets wins whole, so one reader can still run a different model or none. The root file is git-ignored like the others:

```json
{"model": {"url": "https://openrouter.ai/api/v1", "name": "deepseek/deepseek-v4-flash",
           "api_key": "sk-or-...", "provider": {"zdr": true},
           "reasoning": {"enabled": false}, "timeout": 60}}
```

Any secret may sit in its config file, which is git-ignored: `api_key` in the model block, `token` in the `orrery`, `telegram` and `home_assistant` blocks, `password` in `imap`. A value there wins; without one, the reader reads the environment variable the matching `*_env` field names, as before. `provider` is OpenRouter's per-request routing; `{"zdr": true}` sends these requests, and no others from the account, only to hosts that keep nothing. The message text leaves your machine for that host. Home Assistant never calls the analyst. The fixture configs include nothing, so a run with `--config fixtures/...` never reaches a hosted model; `mail/reader.py --eml` uses `config.json` when there is one, model and all, unless you pass `--no-model`.

## The action generator

The generator is the model that reads the whole state back and proposes what to do about it: it is given the state view, the recent decisions with the owner's reasons, and the schema, and it answers with actions the ship validates against the schema (the action kinds, the payload shape per kind, the bodies named) and against what is open or was already decided. Orrery runs it on the ship, on every change, under a cooldown and a daily cap the owner sets on the page, with `common/generator-prompt.md` as its system prompt.

`generator/` is the bench and the dry run for it. `run.py --no-model` prints the prompt a state produces, `run.py --dry-run` asks the model and prints what would be filed, and `bench.py` asks several models the same question from one blanked state, in parallel, and writes a report with the proposals, the notes and the cost side by side, so a prompt change or a new model can be judged before the ship is pointed at it. `run.py` without flags files what survives, for a ship that has no key of its own. The generator's README has the configuration, the key scope and what a pass costs.

## Associating what belongs together

Readers see one message at a time, so two of them, or one of them on two days, can name the same thing twice: "Dana" and "Dana Quill", or a situation per occurrence of a class that meets twice a week. Three things keep the model in one piece. Orrery's resolve matches a query against names, aliases, email and phone, exact first and then by word containment, so a reader that resolves before it creates usually finds what is already there. The analyst folds twins the model makes anyway into the bodies the ship has, by normalised title for situations and activities and by name words for people. And `common/reconcile.py` cleans up after the fact: occurrences become one `activity` with an observation per occurrence, bodies that name one person become `merge` proposals the owner approves, applied through orrery's merge op, and situations that are over are closed at the time they ended.

Any client that writes to the ship, in this repo or outside it, follows `docs/writing-a-client.md`: resolve before creating, occurrences on activities, never `status: open` for an event, event time in `at`, a key rather than the cookie, no cache pushed back, and never the same message twice.

## Running them: the console

`python3 console.py` is a terminal console over systemd user units. It finds every directory with a `util.json`, and for each one shows whether it runs, since when and how often it restarted (or, for a pass on a timer, when the next one is and how the last one went), the last line of its log, and each of its secrets as set, not set, or only in your shell's environment, which a daemon never sees. It never shows a secret.

| key | does |
|---|---|
| `i` | write or rewrite the integration's units in `~/.config/systemd/user/` |
| `s` | start and enable it, or stop and disable it |
| `r` | restart a loop, or run a pass now |
| `l` / `L` | its log, or its last job's log, in `journalctl`'s pager |
| `k` | set a secret (typed as stars; empty removes it) in the file the manifest names, kept readable by you alone |
| `j` | run one of its jobs, asking for what the job needs |
| `c` | make `config.json` from `config.example.json` |
| `E` | turn on linger, so the daemons run when you are not logged in |

A job runs as its own transient unit, `orrery-utils-<name>-job`, that stops the daemon while it runs and starts it again when it ends: a backfill and the daemon write the same `state.json`, and Telegram lets one poller at a time read the bot's updates. `python3 console.py status` prints the same state as text.

`util.json`:

```json
{"about": "Mail reader: rules, then the model, for new mail in every folder",
 "run": ["reader.py", "--config", "config.json"],
 "every": "5min",
 "secrets": {"imap.password": "config.json", "orrery.token": "config.json", "model.api_key": "../config.json"},
 "jobs": {"re-ingest since": {"cmd": ["reader.py", "--config", "config.json", "--since", "{since}"],
                              "ask": {"since": "from day, YYYY-MM-DD"}}}}
```

`run` is started in the integration's directory. Without `every` it is a loop, restarted 30 seconds after it exits; with `every` (a systemd time span: `30s`, `5min`, `1h`) it is a pass that long after the last one ended, and a first one 10 seconds after the timer starts. `secrets` maps a dotted field to the file it lives in, relative to the directory. A job's `{name}` is asked for when it runs.

## Writing a new integration

1. Make a directory named after the source. Its README carries the mapping table (source field to body kind and attribute), the scope the key needs and why, the source kind and id form, what stays on the client, and how to run it.
2. Add a config example with no secrets in it, and a fixture: one sample input and the exact batch it produces.
3. Give it a dry-run flag that prints batches instead of sending them.
4. Run it against a dev ship before a real one. Orrery's `docs/releasing.md` describes the fake ship and its cookie; `scripts/api-matrix.py` there shows every route being exercised.
5. Keep the raw data on the client, keep a cursor, and make replay harmless.
6. Give it a `util.json` (above) so the console can run it, set its secrets and offer its jobs. It needs a mode that runs for ever (a loop that exits on trouble, for systemd to restart) or a single pass the console puts on a timer.

Any language. The API is JSON over HTTP; a few dozen lines of Python with `requests` is a complete client. A shared library comes when two integrations need the same code, not before.

## Development and tests

Every directory has its own `unittest` suite, and none needs a ship, a model or a token: the analyst runs against `FakeModel`, the readers against their fixtures, the console against a temporary directory.

```bash
python3 -m unittest                              # the console
for d in common generator mail telegram home-assistant; do (cd $d && python3 -m unittest); done
```

The tests in `common/` also check that the prompt files are what the code sends. After changing `analyst-prompt.md`, `generator-prompt.md` or `refine-prompt.md`, make the same change to the cord in orrery's `code/lib/orrery.hoon` and run `scripts/prompt-drift.py <path to this repo>/common` there.

Prose in this repo, in the prompts and in what the prompts ask a model to write follows the same rules: no em dashes, no semicolons or colons joining independent clauses, simple direct sentences of varied length.
