# generator: the frontier model proposes actions

The analyst orrery's spec calls the larger model. It reads the state view and the recent decisions with a scoped key, hands them to a frontier model with `../common/generator-prompt.md`, validates every proposal, and files what survives with `POST /act`. The owner approves or dismisses on the page; the executors deliver. It writes no facts.

Standard library only. One file, `run.py`.

## What the model is given

One prompt, built from the ship each run:

- the time now, the owner's body and timezone, and the limit on proposals;
- the action kinds the schema lists and the payload shape of each (the schema's `payloads` block: which keys are required and what they mean);
- every body that matters: situations with their phase read off their times (closed, cancelled and over ones left out), activities with `last` and `next`, people with status and location, things, places, orgs;
- the open actions, so it does not duplicate one;
- the recent decisions (done, dismissed, failed), so it does not propose them again. A dismissal is the owner saying no.

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
python3 run.py --config config.json                  # one pass
python3 run.py --config config.json --loop 3600      # hourly; or let the console run util.json's job
```

## The trial

For the trial the ship's policy has `auto` set to `[]`, so every proposal waits in the inbox and the owner sees the model's judgment before trusting it; the executors (`telegram/bot.py --loop`, `home-assistant/client.py --loop 60`) must be running for approved messages and home actions to happen. Watch the inbox, the `why` on each card, and the printed notes; when the proposals are good, put `task` and `note` back on `auto`.

## What stays here

The prompt and the model's answer. The ship gets the actions that passed validation, nothing else.
