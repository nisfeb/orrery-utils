# common: the analyst

`analyze.py` is the one piece the readers share: it hands a window of messages to a local model and turns the answer into facts orrery accepts. The model runs on your machine; the readers talk to it at an OpenAI-compatible chat endpoint, LM Studio at `http://localhost:1234/v1` by default. Standard library only.

## What it does

1. Builds a prompt from the messages (id, time, sender, text) and the context: the bodies the ship already knows (id, name, aliases), the schema's attribute names per kind, who the owner is, the channel, and the action kinds the caller may propose.
2. Asks the model for one JSON object: `bodies` to create, `observations` (subject, attr, value, at, until, conf, and the message each one comes from) and `actions` (kind, title, about, due, message).
3. Validates the answer before anything is sent: body ids well formed and new, subjects known or created in the same answer, attribute names lowercase, values a string under 2000 bytes, a number, a boolean, null or a `{"ref": "kind/slug"}`, times parseable and normalised to UTC, confidence 0 to 100, action kinds within what the caller allows. What fails is dropped with a note; a broken answer yields nothing but a note.
4. `to_batch` turns the facts into an observe batch and a list of actions with each row's `source` pointer set from its message id, the caller's source kind and nothing else.

A message window is one conversation, oldest first: one mail, or a run of consecutive Telegram messages, so a reply can be read against what it answers.

## Using it from a reader

```python
sys.path.insert(0, os.path.join(HERE, '..', 'common'))
import analyze
model = analyze.Model(url, name)                      # name None: the first model the server lists
context = analyze.context_from_state(ship.state(), 'mail')
facts = analyze.analyze(model, messages, context)      # bodies, observations, actions, notes
bodies, observations, actions = analyze.to_batch(facts, 'mail')
```

Config, in each reader's `config.json`:

```json
"model": {"url": "http://localhost:1234/v1", "name": null, "timeout": 180, "enabled": true}
```

`FakeModel(answer)` answers a canned string, for tests and for judging the validation without a server.

## The prompt

The system prompt in `analyze.py` teaches the model the three shapes and the rules: only what the messages say, existing bodies by id, new bodies only for named things, attribute names from the schema, `at` only when the message says when, honest confidence, one JSON object and nothing else. Change it there when the model's answers drift; the tests in `test_analyze.py` check the validation, not the model.

## What stays here

The message text goes to the model on your machine and nowhere else. The ship receives the validated facts and the message ids.
