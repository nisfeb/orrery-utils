# mail: an email reader for orrery

Reads what arrived in a mailbox since the last run, decides with rules what each message says about the world, and sends those facts to orrery with a scoped key. The message never leaves the machine it runs on. The ship gets a source pointer, kind `mail` and the message's Message-ID, and the facts.

Standard library only: `imaplib`, `email`, `urllib`. One file, `reader.py`.

## What it writes

The rules run in this order. The first rule that claims a message stops the rest.

| the message | rule | what the reader writes |
|---|---|---|
| a calendar invitation (a `text/calendar` part, or a subject starting `Invitation:`) | `skip_calendar` | nothing; the calendar integration owns those |
| a shipping notice ("has shipped", "is on its way", "out for delivery", "has been delivered") | `shipping` | `thing/order-<number>` created with the order number as an alias; `status` `shipped`, `out for delivery` or `delivered`; `location` `in transit`; `until` the end of the arrival day when the mail names one; `conf` 90 |
| an invoice or bill with an amount ("invoice", "bill", "amount due", ...) | `invoice` | `org/<sender>` created; an action: `task` "Pay <org> <amount>", `about` the org, `due` the date after "due" when there is one |
| a flight, hotel or booking confirmation with a date | `trip` | `situation/<date>-trip` created; `status` `open`, `participants` `person/me`, `started` the date; `conf` 80 |
| bulk mail (a `List-Unsubscribe` header or `Precedence: bulk`) | `skip_bulk` | nothing |
| a person the ship already knows, writing from an address it does not have | `known_person` | `person/<x>.email = <address>`, `conf` 80. Needs the ship: skipped in a dry run |
| anything else | `classify_with_model` | nothing today. This is where a local model plugs in |

Dates: `at` is the message's `Date` header, because the fact became known when the mail arrived; a trip's own start date is the value of `started`. A date with no year takes the message's year, or the next one when it would fall more than a month before the message. `until` on a shipment is midnight after the arrival day.

Sensitive facts: the rules write neither `health` nor `income`. A model that reads statements or results must map money and health facts to those two attributes and nowhere else, so one line in the policy's `sensitive` list keeps them from every key.

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
  "imap": {"host": "imap.example.com", "port": 993, "user": "me@example.com", "password_env": "MAIL_PASSWORD", "folder": "INBOX"},
  "orrery": {"url": "https://your-ship.example", "token_env": "ORRERY_TOKEN"},
  "state": "state.json"
}
```

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
- One folder per run. Point the config at another folder for another run.
- An order without a number gets an id from the Message-ID, so a second mail about the same order without the number makes a second body. The number is in almost every shipping mail.
- The `trip` rule takes the first date in the mail as the start. A confirmation that names a booking date before the travel date needs the model rule.
