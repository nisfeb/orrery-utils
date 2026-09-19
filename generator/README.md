# generator: the frontier model proposes actions

As of orrery 18 the ship runs this itself: the Generator card under Settings holds the model and the key, and a pass runs on every change through `/sys/iris/`. This util remains the bench (`bench.py`), the dry-run harness, and the way to run a pass from a computer when the ship has no key.

The analyst orrery's spec calls the larger model. It reads the state view and the recent decisions with a scoped key, hands them to a frontier model with `../common/generator-prompt.md`, validates every proposal, and files what survives with `POST /act`. The owner approves or dismisses on the page; the executors deliver. It writes no facts.

Standard library only. One file, `run.py`.

## What the model is given

One prompt, built from the ship each run:

- the time now, the owner's body and timezone, and the limit on proposals;
- the action kinds the schema lists and the payload shape of each (the schema's `payloads` block: which keys are required and what they mean);
- every body that matters: situations with their phase read off their times (closed, cancelled and over ones left out), activities with `last` and `next`, people with status and location, things, places, orgs;
- the open actions, so it does not duplicate one;
- the recent decisions (done, dismissed, failed) with the owner's reason when one was given, so it does not propose them again and learns what kind of thing is unwanted. A dismissal is the owner saying no; a dismissal with a reason is the owner saying why, and the model generalises from it.

`--no-model` prints that prompt and stops; `--show-prompt` prints it before asking. The state is read as the key sees it, so a key without `sensitive: write` never puts `health` or `income` in front of a hosted model.

## What is kept of the answer

The model answers one JSON object with `actions` and `notes`. Each action is checked: the kind is one the schema lists; the title is not already open or decided (by its words, not its spelling); every body in `about` exists; the payload carries every key the schema marks required; `due` parses. At most `max_actions` are filed, the rest and the notes are printed. The model's one-sentence `why` rides in the payload for the owner to read on the page.

## Configuration

```json
{
  "include": "../config.json",
  "model": {"name": "anthropic/claude-opus-5", "reasoning": {"effort": "high"}, "max_tokens": 32000, "timeout": 600},
  "orrery": {"url": "https://your-ship.example", "token": ""},
  "max_actions": 5,
  "timezone": "America/New_York"
}
```

The same shape as the readers' configs: `include` pulls the shared `config.json` at the repo root, whose `model` block (a hosted OpenAI-compatible endpoint with its `api_key`, `provider` and `reasoning` fields, or LM Studio) then serves the generator too. The `model` block here merges over it field by field, so the generator takes the shared url and key and names its own model, reasoning and budget: this is the one place to spend the most capable model with its reasoning on, since it runs a few times a day over the whole state rather than once per message. The example is the strongest Anthropic model that OpenRouter can route under the shared block's `{"zdr": true}` rule (as of September 2026 Fable 5.1 has no zero-data-retention endpoint there, so the rule and the strongest model cannot both hold; loosening the rule is the owner's call, since the prompt carries the whole state), with high reasoning effort and a 32000 token budget (the reasoning comes out of it), and a ten minute timeout for the long thoughts; a model that reasons is sent no temperature. For Anthropic's Messages API put `{"api": "anthropic", "name": "claude-sonnet-5", "api_key": "..."}` (or `api_key_env`) in the `model` block. Secrets sit in the git-ignored config or in the environment variable the matching `_env` field names, as for the readers. `timezone` is a fallback; `person/me.timezone` on the ship wins. The answer's token budget is `max_tokens` in the `model` block, 8000 here unless set (the readers use 2000): a frontier model that reasons spends the budget on its reasoning first, and `model ran out of tokens` means it needs more, or `"reasoning": {"enabled": false}`.

The key: read-only, every kind, the action kinds it may propose.

```bash
curl -s -b jar -H 'content-type: application/json' -X POST $SHIP/apps/orrery/api/clients -d '{
  "name": "action generator", "by": "generator",
  "scope": {"kinds": ["person", "place", "thing", "org", "situation", "activity", "note"], "actions": ["task", "note", "message", "home", "calendar"], "write": false}
}'
```

## Running it

```bash
cd generator && python3 -m unittest                  # the prompt and the validation, no network
python3 run.py --config config.json --no-model       # the prompt it would send
python3 run.py --config config.json --dry-run        # ask, print, file nothing
python3 run.py --config config.json                  # one pass, if anything changed
python3 run.py --config config.json --force          # one pass regardless
python3 run.py --config config.json --loop 3600      # look hourly, ask only when something changed; or let the console run util.json's job
```

A pass asks the model only when something it would see has changed. The prompt is built with the clock on its last line, and a hash of everything above the clock is remembered after each real pass (in `state.json` next to the config, or where `state` in the config says); the next pass compares its own hash and stops there, free, when they match. A write on the ship that changes no line of the prompt (a sensitive attribute, a body outside the key's kinds) does not count, and neither does the clock; a situation crossing from upcoming to under way does, since the prompt says so. There is no ceiling: a quiet week is a week of no calls. `--force` asks anyway; a dry run never remembers.

The same layout feeds the cache. The prompt's pieces run from the least changing to the most: things, places and orgs; people and activities; situations; open actions and decisions; the clock. Through OpenRouter or the Messages API the system prompt and the first three pieces carry cache marks, so a call within five minutes of another reads every piece up to the first changed one at a tenth of the input price (a filing changes only the fourth piece and leaves the first three cached), and the usage line says how much came from the cache. Passes an hour apart do not benefit; a burst of them does.

## The trial

For the trial the ship's policy has `auto` set to `[]`, so every proposal waits in the inbox and the owner sees the model's judgment before trusting it; the executors (`telegram/bot.py --loop`, `home-assistant/client.py --loop 60`) must be running for approved messages and home actions to happen. Watch the inbox, the `why` on each card, and the printed notes; when the proposals are good, put `task` and `note` back on `auto`.

## What a pass costs

Measured on 2026-09-18 against a state of 143 bodies, Opus 5 through OpenRouter with high reasoning effort: 8,556 tokens in, 2,125 out of which 1,455 were reasoning, $0.096. The rest of the table follows from that split at $5 per million in and $25 per million out; the reasoning share is what changes.

| reasoning | per pass | hourly, never skipping | hourly, per month |
|---|---|---|---|
| high (measured) | $0.096 | $2.30 a day | about $69 |
| medium (estimate) | $0.077 | $1.85 a day | about $56 |
| low (estimate) | $0.067 | $1.60 a day | about $48 |
| off (estimate) | $0.060 | $1.44 a day | about $43 |

With the skip, the count of passes is what the bill follows: one per change of the state the key can see, not one per hour. Every call prints `# model usage:` with its tokens and cost on stderr, so the console log carries the real spend.

## What stays here

The prompt and the model's answer. The ship gets the actions that passed validation, nothing else.
