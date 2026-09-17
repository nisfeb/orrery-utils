# mail: an email reader for orrery

Reads what arrived in a mailbox since the last run, decides with rules what each message says about the world, and sends those facts to orrery with a scoped key. The message never leaves the machine it runs on. The ship gets a source pointer, kind `mail` and the message's Message-ID, and the facts.

Standard library only: `imaplib`, `email`, `urllib`. One file, `reader.py`.

## What it writes

The rules run in this order. The first rule that claims a message stops the rest.

| the message | rule | what the reader writes |
|---|---|---|
| a calendar invitation (a `text/calendar` part, or a subject starting `Invitation:`) | `skip_calendar` | nothing; the calendar integration owns those |
| a shipping notice ("has shipped", "is on its way", "out for delivery", "has been delivered") | `shipping` | `thing/order-<number>` created with the order number as an alias (a number has a digit in it; prose after the word order does not count, and a mail without one gets an id from its Message-ID); `status` `shipped`, `out for delivery` or `delivered`; `location` `in transit` until delivered, when it is cleared; `until` the end of the arrival day when the mail names one; `conf` 90 |
| an invoice or bill with an amount ("invoice", "bill", "amount due", ...) | `invoice` | an action: `task` "Pay <payee> <amount>", `due` the date after "due" when there is one. A company payee gets `org/<name>` created and the task is about it; a payee whose name reads like a person's is resolved against the ship's people and the task is about that person, and no org is ever made from a person's name. A receipt ("payment received", "thank you for your payment", ...) with no due date is claimed and writes nothing |
| a flight, hotel or booking confirmation with a date | `trip` | `situation/<date>-trip` created; `status` `open`, `participants` `person/me`, `started` the date; `conf` 80 |
| bulk mail (a `List-Unsubscribe` header or `Precedence: bulk`) | `skip_bulk` | nothing |
| a person the ship already knows, writing from an address it does not have | `known_person` | `person/<x>.email = <address>`, `conf` 80. Needs the ship: skipped in a dry run |
| anything else | `classify_with_model` | nothing today. This is where a local model plugs in |

Dates: `at` is the message's `Date` header, because the fact became known when the mail arrived; a trip's own start date is the value of `started`. A date with no year takes the message's year, or the next one when it would fall more than a month before the message. `until` on a shipment is midnight after the arrival day.

Sensitive facts: the rules write neither `health` nor `income`. A model that reads statements or results must map money and health facts to those two attributes and nowhere else, which the starter policy's `sensitive` list keeps from every key.

## The model

With a `model` block in the config, every message the rules do not claim goes to a local model through `../common/analyze.py`: an OpenAI-compatible chat endpoint, LM Studio at `http://localhost:1234/v1` by default, with `name` left null to use whatever model the server lists first. The model sees the subject and the text, the bodies the ship already knows (id, name, aliases, read once per run from the state view) and the schema's attribute names, and answers bodies, observations and actions in orrery's shapes. Everything it answers is validated before it is sent: ids well formed, subjects known or created in the same answer, values bounded, times parseable; what fails is dropped with a note on the run's log. `--no-model` runs the rules alone; a server that is down stops the run with a message rather than silently skipping the model.

The rules run first and a claimed message never reaches the model, so a shipping notice is always the same three rows however the model feels that day. Money and health facts belong to the attributes `income` and `health` and nowhere else; the prompt says so, both names are in orrery's starter schema for a person, and the starter policy lists them as `sensitive`, so they are kept from every key. A ship seeded before orrery version 11 adds the two names to its schema and the `sensitive` line to its policy by hand.

## Filtering

Spam usually never reaches the reader: it sits in the Junk folder, and the reader opens only the folders the config lists. What does reach it is newsletters, notifications and marketing, and the `filters` block in the config keeps them away from the model:

```json
"filters": {
  "from": ["noreply@", "no-reply@", "newsletter", "marketing@", "notifications@"],
  "subject": ["unsubscribe", "% off", "webinar"],
  "only_from": []
}
```

`from` and `subject` are substrings, matched without regard to case, against the sender's name and address and against the subject; one match skips the message. `only_from`, when it is not empty, is an allowlist: only senders matching one of its entries reach the model, which is the strictest and simplest setting for a personal world model, where the mail that matters comes from a few dozen people. Bulk mail with a `List-Unsubscribe` header or `Precedence: bulk` is skipped before any of this.

The transactional rules run before the filters, so a shipping notice or an invoice from a `noreply@` address still lands. A skipped message is logged with the entry that matched, so a dry run over a month of mail is the way to tune the lists: run it with `--no-model` first, which takes seconds instead of hours, and read the log.

## Backfill

To build state from what already happened, run the reader over the past:

```bash
MAIL_PASSWORD=... ORRERY_TOKEN=... python3 reader.py --config config.json --months 6
MAIL_PASSWORD=... ORRERY_TOKEN=... python3 reader.py --config config.json --since 2026-01-01 --limit 5000
```

A backfill reads every message in the folder from that day on, oldest first, through the rules and the model, and keeps its own place in `state.json` (under `backfill`) so an interrupted run resumes where it stopped instead of asking the model again. It only ever moves the live cursor forward, so a later live run does not re-read what the backfill covered. `at` is each message's own date, so the facts land in the past where they belong and the timeline reads as it happened. With `--dry-run` it prints what it would send without confirming anything, which is the way to judge the model on your own mail before the first real run.

A local model takes seconds per message, so six months of a busy inbox is hours; the progress line shows `n/total`, the message date and what happened.

## The key

Mint it on the ship with the owner cookie. The scope is the four kinds the rules write and the one action kind.

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "mail reader on the laptop", "by": "mail",
  "scope": {"kinds": ["person", "org", "thing", "situation"], "actions": ["task"], "write": true}
}'
```

Keep the `token` from the answer in the environment variable the config names. The ship keeps a salted hash and shows it once.

## Configuration

Copy `config.example.json` to `config.json` (git ignores it) and fill it in. Secrets stay in the environment: the mailbox password in `MAIL_PASSWORD`, the key in `ORRERY_TOKEN`, or whatever names the config gives.

```json
{
  "imap": {"host": "imap.example.com", "port": 993, "user": "me@example.com", "password_env": "MAIL_PASSWORD",
           "folder": "INBOX", "folders": ["INBOX", "Finance", "Finance/Receipts", "Real Estate"]},
  "orrery": {"url": "https://your-ship.example", "token_env": "ORRERY_TOKEN"},
  "state": "state.json"
}
```

`folders` is read in order and each folder keeps its own cursor in `state.json` under its own name, so they
advance apart and a folder added later starts from its own beginning without disturbing the others. `folder`
is the fallback when `folders` is absent. A folder that cannot be opened is named on stderr and skipped, so
one wrong name does not stop the run.

IMAP over TLS with a password or an app password. Providers that only allow OAuth need a bridge or an app password; that is outside this reader.

## Running it

```bash
cd mail
python3 -m unittest                                              # the mapping, no ship needed
python3 reader.py --dry-run --eml fixtures/shipped.eml           # the rules on one file, printed
python3 reader.py --config config.json --dry-run                 # the mailbox, printed, cursor untouched
MAIL_PASSWORD=... ORRERY_TOKEN=... python3 reader.py --config config.json    # read, send, advance
```

Run the last line on a timer, every five or ten minutes. A run reads at most `--limit` messages (200 by default), oldest first, and advances the cursor one message at a time, so a crash or a refused batch stops before the message that failed and the next run retries it.

The cursor is the mailbox's `UIDVALIDITY` and the last UID handled, in `state.json`. Messages are read with `BODY.PEEK`, so nothing is marked seen. If the mailbox's UIDVALIDITY changes, the reader starts over from the beginning of the folder; that is safe, because the same claim from the same source is the same observation on the ship and answers `existing: true`.

## What stays here

The message: headers, text, attachments, everything. Only the Message-ID crosses, as `source.id`, and only the facts in the table. Attachments are never opened. HTML bodies are flattened to text for the rules and discarded.

## Fixtures

`fixtures/*.eml` are sample messages and `fixtures/expected.json` is the exact batch each one produces. `test_reader.py` checks them and the `known_person` rule against a stub ship. When you change a rule, change the fixture that shows it, and regenerate `expected.json` by hand from a dry run, so the diff shows what the rule now writes.

## Limits

- English patterns. Other languages need their own rule text.
- `--limit` is per folder, not per run: a run over twenty folders reads up to twenty times that many messages.
- Filters are substrings, so a skip entry matches anywhere in the sender or subject. `newsletter` also skips
  `tlmjaxnewsletter@gmail.com`. Prefer the longest substring that still catches what you mean.
- An order without a number gets an id from the Message-ID, so a second mail about the same order without the number makes a second body. The number is in almost every shipping mail.
- The `trip` rule takes the first date in the mail as the start. A confirmation that names a booking date before the travel date needs the model rule.
- `at` comes from the `Date` header, so a message stamped ahead of the ship's clock is a future fact on the ship until that time passes. It lands, and it folds in when its time comes.

Checked end to end on a dev ship on 2026-09-17: the three fact fixtures landed under a key scoped as above, the task was auto-approved with `by` `mail`, and the known-person rule wrote the new address for a person the ship knew.
