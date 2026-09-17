# telegram: a Telegram bot for orrery

Both directions. People you name tell the bot facts in a short command grammar, in a private chat with the bot or in a group it sits in, and the bot sends them to orrery with a scoped key. It also delivers approved actions of kind `message` addressed via Telegram, to the people you mapped, and reports done or failed.

Standard library only. One file, `bot.py`. It uses the Bot API with long polling, so it runs anywhere with outbound HTTPS and needs no public address.

A bot sees what is sent to it: private chats with it, and groups it is a member of. It does not see your own conversations with other people. Reading those needs a user session (Telethon or the like), which is a different program with a different risk; this one is the capture channel and the delivery channel.

## The grammar

| you type | what the bot writes |
|---|---|
| `/at <place>` | `<you>.location = <place>`. A body id such as `place/home` becomes a reference; anything else is text |
| `/status <text>` | `<you>.status = <text>`. `/status -` clears it |
| `/obs <subject> <attr> <value>` | any fact. The subject is a body id, `me`, or a name the ship resolves to exactly one body. `null` clears, a number is a number, a body id is a reference, `true` and `false` are booleans, the rest is text |
| `/task <title> [due YYYY-MM-DD]` | an action of kind `task`, approved on the spot when the policy auto-approves tasks |
| anything else | the `classify_with_model` hook, which does nothing today. This is where a local model turns "car died on route 9, stranded waiting for a tow" into facts |

`<you>` is the body the config maps your Telegram user id to, so Sarah typing `/at place/home` in the family group puts Sarah at home, and you typing it puts you there. `at` is the message's time. `source` is `{"kind": "chat", "id": "telegram/<chat id>/<message id>"}`. The text of a message is never sent to the ship; the pointer is.

A command the bot cannot apply (an unknown name, a missing argument, a refusal from the ship) gets a one-line reply in the same chat saying why. Set `reply_errors` to `false` to keep the bot silent.

## Delivery

The assistant proposes a message:

```json
{"kind": "message", "title": "Tell Sarah the car is at John's",
 "payload": {"via": "telegram", "to": "person/sarah", "text": "The car is at John's Machine Shop, they'll look at it in the morning."}}
```

The owner approves it; keep `message` off the policy's `auto` list, so nothing is ever sent without a human reading it first. The bot polls approved actions, takes the ones of kind `message` whose payload says `via` `telegram`, finds the chat for `to` (a body id from `people`, or a chat id from `chats`), sends the text, and reports `done` or `failed` with the reason. A `message` for another platform is left alone. Each action is delivered once; its id goes into `state.json`.

The bot can only message people who have started a chat with it or groups it is in. That is Telegram's rule, and it is a good one.

## The key

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "telegram bot", "by": "telegram",
  "scope": {"kinds": ["person", "place", "thing", "situation"], "actions": ["task", "message"], "write": true}
}'
```

## Setting up the bot

1. Make a bot with BotFather and keep its token in `TELEGRAM_TOKEN`.
2. For a group, add the bot and turn privacy mode off with BotFather (`/setprivacy`), or it sees only commands addressed to it.
3. Find the Telegram user ids of the people who may talk to it and the ids of the chats it should listen in: run `python3 bot.py --config config.json --dry-run` and read the ignored senders and chats it prints.
4. Map them in `config.json`: `people` is Telegram user id to body id; `chats` is the chats it listens in. A message from anyone else, or in any other chat, is ignored and logged.

```json
{
  "telegram": {"token_env": "TELEGRAM_TOKEN"},
  "orrery": {"url": "https://your-ship.example", "token_env": "ORRERY_TOKEN"},
  "state": "state.json",
  "chats": [123456789, -1001234567890],
  "people": {"123456789": "person/me", "987654321": "person/sarah"},
  "reply_errors": true
}
```

The person bodies must exist on the ship; the bot never creates a person from a Telegram account.

## Running it

```bash
cd telegram
python3 -m unittest                                                              # grammar and delivery, no network
python3 bot.py --dry-run --config fixtures/config.json --updates fixtures/updates.json   # the fixture, printed
TELEGRAM_TOKEN=... python3 bot.py --config config.json --dry-run                 # pending updates, printed, nothing confirmed
TELEGRAM_TOKEN=... ORRERY_TOKEN=... python3 bot.py --config config.json          # one pass, then exit
TELEGRAM_TOKEN=... ORRERY_TOKEN=... python3 bot.py --config config.json --loop   # long poll for ever
```

The cursor is Telegram's update offset in `state.json`, advanced one update at a time after its facts land, so a refused batch stops the pass before the message that failed and the next pass retries it. A dry run confirms nothing, so the same updates come back on the next real pass. Run `--loop` under a supervisor that restarts it.

## What stays here

Every message, every sender's name, the bot token. The ship gets the facts the grammar produced and the chat and message ids that point back at them.

## Fixtures

`fixtures/updates.json` is a `getUpdates` answer with twelve updates: the stranded-car story typed as commands by two people, an unknown sender, a chat the bot does not listen in, free text, an edit, and a wrong command. `fixtures/config.json` maps two people and two chats, and `fixtures/expected.json` records the facts or the note for every update. `test_bot.py` checks that, the name lookup against a stub ship, the value grammar and the delivery of message actions.

## Limits

- Commands only, until a model sits in `classify_with_model`. The grammar is deliberate so that what lands on the ship is what someone meant to say.
- `/obs` with a name needs the ship to resolve it; a dry run cannot, and says so.
- One bot, one config, one ship. A family with two ships runs two bots, or shares bodies between ships the orrery way.
- `at` is the message's time, so a message stamped ahead of the ship's clock is a future fact on the ship until that time passes.

Checked end to end on a dev ship on 2026-09-17 through a stub Bot API serving the fixture: the facts landed under a key scoped as above, the name in `/obs sarah ...` resolved on the ship, the wrong command got its one-line reply in the chat, the task was auto-approved with `by` `telegram`, and an approved `message` addressed to `person/sarah` was sent to her chat id and marked `done` by `telegram`.
