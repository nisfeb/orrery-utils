# telegram: a Telegram bot for orrery

Both directions. People you name tell the bot facts in a short command grammar, in a private chat with the bot or in a group it sits in, and the bot sends them to orrery with a scoped key. It also delivers approved actions of kind `message` addressed via Telegram, to the people you mapped, and reports done or failed.

Standard library only: `bot.py`, and `backfill.py` for the past. It uses the Bot API with long polling, so it runs anywhere with outbound HTTPS and needs no public address.

A bot sees what is sent to it: private chats with it, and groups it is a member of. Your own private conversations with other people reach it only through a Telegram Business connection, which is below under Your chats, live; groups you are in but the bot is not stay out of reach, and reading those needs a user session (Telethon or the like), which is a different program with a different risk.

## The grammar

| you type | what the bot writes |
|---|---|
| `/at <place>` | `<you>.location = <place>`. A body id such as `place/home` becomes a reference; anything else is text |
| `/status <text>` | `<you>.status = <text>`. `/status -` clears it |
| `/obs <subject> <attr> <value>` | any fact. The subject is a body id, `me`, or a name the ship resolves to exactly one body. `null` clears, a number is a number, a body id is a reference, `true` and `false` are booleans, the rest is text |
| `/task <title> [due YYYY-MM-DD]` | an action of kind `task`, approved on the spot when the policy auto-approves tasks |
| anything else | the model, with the chat's last four free-text messages as context, so "yes, at 8" is read against what it answers; facts come only from the new message |

`<you>` is the body the config maps your Telegram user id to, so Sarah typing `/at place/home` in the family group puts Sarah at home, and you typing it puts you there. `at` is the message's time. `source` is `{"kind": "chat", "id": "telegram/<chat id>/<message id>"}`. The text of a message is never sent to the ship; the pointer is.

A command the bot cannot apply (an unknown name, a missing argument, a refusal from the ship) gets a one-line reply in the same chat saying why. Set `reply_errors` to `false` to keep the bot silent.

## The gate

A `decide` block puts a decision model in front of the analyst. TypeSafe's Jev, reached through OpenRouter's decisions route with the same key as the `model` block (and its `provider` rule, so `{"zdr": true}` holds), answers one typed question per free-text message: the probability that it carries a fact worth recording, with the chat's last messages and the ship's known bodies as state. Below `threshold` (0.3 unless set) the analyst is not asked and the note says so; a gate that cannot answer lets everything through. It costs about two cents per thousand messages and answers in under half a second, so the small model runs only on messages that say something. `gate_check.py --config config.json export.json` shows what it would do over a Telegram Desktop export, one line per message, six asked at a time after the first (`--at-once`), with what each threshold from 0.2 to 0.4 would have let through, without writing anything.

With `escalate` in the `decide` block (0.6 in the example), the decider is asked one more question once the analyst's facts are known: does this need help within the hour? A breakdown, an injury, being stranded, a child to fetch now. At or above the threshold the bot sends its facts as usual and then asks the ship for an urgent pass, `POST /generate {"about": [the situations it just wrote]}`, which runs the generator at once, past its cooldown, under the ship's own small daily cap (client guide rule 16). One request per batch of messages, whatever they say. Without `escalate` the lane is closed. The backfill never escalates: it reads history.\n\nThe same decider does two more things, both from Talon's measurements on 2026-09-19. Every body the ship knows goes to it, ranked (the ones named in the message first, then people, then activities, places and orgs, then situations, then things), since a cut at eighty could drop the one person a message is about. With `"relevance": true` in the `decide` block it also picks the bodies the analyst is shown: one yes-or-no question per body, in groups of forty, and only those scored at or above `keep` (0.5) plus the sender and the owner reach the small model's prompt; the right bodies scored 0.8 to 0.96 and the rest 0.06 or less on a state of 173 bodies. Off until the owner has watched it, since a wrong cut hides a body from the analyst; validation always goes by every body. And every status the analyst proposes for a person is put to it as circumstance, feeling or neither, each question naming its own proposal (unnamed, the model answers several alike): a feeling or a non-status at 0.6 or more is dropped and noted, an uncertain one kept and noted, and a decider that cannot answer keeps every row.

## Delivery

The assistant proposes a message:

```json
{"kind": "message", "title": "Tell Sarah the car is at John's",
 "payload": {"via": "telegram", "to": "person/sarah", "text": "The car is at John's Machine Shop, they'll look at it in the morning."}}
```

The owner approves it; keep `message` off the policy's `auto` list, so nothing is ever sent without a human reading it first. The bot polls the open actions, takes the ones of kind `message` that are approved or claimed and whose payload says `via` `telegram`, claims each one for ten minutes before it sends, finds the chat for `to` (a body id from `people`, or a chat id from `chats`), sends the text, and reports `done` or `failed` with the reason. A claim the ship refuses means another bot holds that action, so this one skips it for the pass and never sends the message. The full protocol, poll open, claim, read back, act, report, is in the root README under Actions out. A `message` for another platform is left alone. Each action is delivered once; its id goes into `state.json`.

The bot can only message people who have started a chat with it or groups it is in. That is Telegram's rule, and it is a good one.

## The model

With a `model` block, in the config or in the repo's shared `config.json` through `"include": "../config.json"` (see the root README), free text from a known person goes to the model through `../common/analyze.py` with the sender's body, the message time, the chat's last four messages and the bodies the ship knows, and comes back as facts the bot sends under the message's pointer. Commands never reach the model; the grammar answers them first. Everything the model answers is validated before it is sent, and what fails is dropped with a note on the log. What the model writes from chat must also trace back to its message, the same rules as Talon's extractor, because a small model writes what it remembers as readily as what it read: a question never reaches the model, a fact is about a body the message names or its author, and about its author only when the message speaks for them ("grandpa's flight got cancelled" from Sarah is not about Sarah: it points at someone else and says nothing in the first person), a value other than a `status` or `health` is in the message's words (a reference in a name the message uses), and a `status` or `health`, which are paraphrases by design, is dropped when it echoes the earlier messages and not this one. A `status` that names a diagnosis is a medical fact, which goes under `health` and nowhere else: it is moved there when the key's schema lists `health`, and dropped when it does not, because a status is readable by every key. New names the model gives a body the ship already has ("next door" for `place/neighbors`) are sent as aliases only when the message uses them. A new body no kept fact is about is dropped too, and a fact the model dates to midnight of the message's day takes the message's time. Remove the block, or set `enabled` to false, for the grammar alone.

## Your chats, live

The bot also carries out the message actions the owner approves (kind `message`, `via` telegram): it asks the ship for open actions every `execute_every` seconds (300 unless set), claims each one addressed to a chat it knows, sends it and reports done or failed. It used to ask on every long poll, which returns every thirty seconds whether or not anything came, and every request into a grubbery app costs the ship about a second, so that was a fifth of the ship's busy time.

With Telegram Premium you can connect the bot to your own account through Telegram Business, and it then gets every message in the private chats you pick, yours and the other person's, as they arrive, on the same long poll as everything else. No export, no second program.

1. BotFather: `/mybots`, your bot, Bot Settings, Secretary Mode (older BotFathers call it Business Mode), on.
2. Telegram: Settings, Telegram Business, Chatbots, add the bot by its username, and choose the chats it may see. It needs no permission to reply or manage messages and never uses one.
3. Put the other person's user id in `chats` (in a private chat the chat id is their user id) and map both of you in `people`, as for any other chat.

A message there goes through the same grammar and the same model as one sent to the bot, with the sender as `<you>`, and its source is `telegram/<their user id>/<message id>`, the same pointer an export of that chat gives it, so the backfill and the live bot agree. It is never replied to, not even with the one-line error, because a reply by chat id would land in that person's own chat with the bot. The bot reads only connections that belong to an account in `people`, asked of Telegram with `getBusinessConnection`, so someone else who connects the bot to their account feeds it nothing. Groups are not part of Telegram Business.

Run the bot with `--loop` and the backfill once, for what came before the connection.

## Backfill

A bot cannot read what came before it was connected. The past comes from Telegram Desktop: Settings, Advanced, Export Telegram data, JSON, which writes `result.json`. `backfill.py` reads it:

```bash
python3 backfill.py --config config.json --export ~/Downloads/Telegram\ Desktop/DataExport/result.json --months 6 --dry-run
python3 backfill.py --config config.json --export result.json --months 6
python3 backfill.py --config config.json --export result.json --since 2026-01-01 --chat Sarah --chat family
```

`--bot-chats` reads only the chats in `chats`, the ones the live bot reads, and `--last 100` only the last hundred messages of each; together, with `--dry-run`, they are a cheap way to see what a model makes of your recent conversations. Without `--bot-chats` the backfill reads every chat in which someone in `people` speaks, you included. An export holds groups only when Telegram Desktop's export had them ticked.

It reads runs of six messages (`window` in the config) with the previous two as context, keeps the messages from the people in `people` (the export's `user<id>` senders carry the same ids as the Bot API), inside the window you ask for, and hands them to the model in runs of consecutive messages per chat, at most six messages (`window`) or about three thousand characters, so a reply is read against what it answers. Each fact carries `telegram/<export chat id>/<message id>` as its source and the message's own time as `at`. The run keeps its place per chat in `state.json` under `backfill`, so an interrupted run resumes without asking the model twice. Channels and service messages are skipped; so is anyone not in `people`.

## The key

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "telegram bot", "by": "telegram",
  "scope": {"kinds": ["person", "place", "thing", "situation"], "actions": ["task", "message"], "write": true, "sensitive": "write"}
}'
```

`"sensitive": "write"` lets the bot store health and money facts people mention without reading them back; such a key's schema view lists the two names, which is how the analyst knows to keep them.

## Setting up the bot

1. Make a bot with BotFather and put its token in `telegram.token` in `config.json` (or in the environment variable `telegram.token_env` names, `TELEGRAM_TOKEN` by default; a daemon run by the console reads only the file).
2. For a group, add the bot and turn privacy mode off with BotFather (`/setprivacy`), or it sees only commands addressed to it.
3. Find the ids, in two passes, because the chat test returns before the sender test: with `chats` empty, every message is ignored as `chat <id> is not in chats` and you learn only chat ids; fill `chats` in, run again, and the same messages now report `sender <id> is not in people` and give you the user ids. Both passes are `python3 bot.py --config config.json --dry-run`, which confirms nothing, so the same updates come back each time. Put `"model": {"enabled": false}` in this config to do this without calling the model (it overrides a shared block); `bot.py` has no `--no-model` flag.
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
python3 bot.py --config config.json --dry-run                 # pending updates, printed, nothing confirmed
python3 bot.py --config config.json                           # one pass, then exit
python3 bot.py --config config.json --loop                    # long poll for ever
```

Under `--loop` the model's view of the ship (its bodies and schema) is read again every ten minutes, so a bot that runs for days sees the bodies the other readers made since. The cursor is Telegram's update offset in `state.json`, advanced one update at a time after its facts land, so a refused batch, or a model that did not answer (unreachable, no key, no credit, rate limited), stops the pass before that message and the next pass retries it. A dry run confirms nothing, so the same updates come back on the next real pass. A dropped connection to Telegram (a reset, a timeout, a 5xx, a rate limit) loses nothing, since unconfirmed updates wait on Telegram's side, and is asked again after five seconds without exiting. `--loop` exits on a stop like the ones above, or on a token Telegram refuses; the console (`../console.py`) runs it as a systemd unit that starts it again 30 seconds later.

## What stays here

Every message, every sender's name, the bot token. The ship gets the facts and the chat and message ids that point back at them. The model's host reads the text of the free-text messages and the four before each: your machine with a local model, the provider with a hosted one.

## Fixtures

`fixtures/updates.json` is a `getUpdates` answer with twelve updates: the stranded-car story typed as commands by two people, an unknown sender, a chat the bot does not listen in, free text, an edit, and a wrong command. `fixtures/config.json` maps two people and two chats, and `fixtures/expected.json` records the facts or the note for every update. `test_bot.py` checks that, the name lookup against a stub ship, the value grammar, the context the model is shown, the rules its answers are held to, Telegram Business messages and the delivery of message actions; `test_backfill.py` checks the export reader.

## Limits

- The grammar is deliberate, so what lands on the ship from a command is what someone meant to say. Free text is only as good as the model, less the rules above, and reaches the ship only when a `model` block is enabled.
- `/obs` with a name needs the ship to resolve it; a dry run without an orrery key cannot, and says so.
- One bot, one config, one ship. A family with two ships runs two bots, or shares bodies between ships the orrery way.
- `at` is the message's time, so a message stamped ahead of the ship's clock is a future fact on the ship until that time passes.

Checked end to end on a dev ship on 2026-09-17 through a stub Bot API serving the fixture: the facts landed under a key scoped as above, the name in `/obs sarah ...` resolved on the ship, the wrong command got its one-line reply in the chat, the task was auto-approved with `by` `telegram`, and an approved `message` addressed to `person/sarah` was sent to her chat id and marked `done` by `telegram`.
