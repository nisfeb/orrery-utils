# common: the analyst

`analyze.py` is the one piece the readers share: it hands a window of messages to a model and turns the answer into facts orrery accepts. The readers talk to it at an OpenAI-compatible chat endpoint: LM Studio on your machine at `http://localhost:1234/v1` by default, or a hosted one such as OpenRouter (below). Standard library only.

## What it does

1. Builds a prompt from the messages (id, time, sender, text; times on this machine's clock with the offset, so "until 11:30" in a message can be written back as a time) and the context: the bodies the ship already knows (id, name, aliases), the schema's attribute names per kind, who the owner is, the channel, and the action kinds the caller may propose.
2. Asks the model for one JSON object: `bodies` to create, `observations` (subject, attr, value, at, until, conf, and the message each one comes from) and `actions` (kind, title, about, due, message).
3. Validates the answer before anything is sent: body ids well formed, of a real kind and new, subjects known or created in the same answer, attribute names lowercase, values a string (cut to 2000 characters), a number, a boolean, null, a `{"ref": "kind/slug"}` or other JSON up to 2000 bytes, times parseable and normalised to UTC, confidence 0 to 100, action kinds within what the caller allows. What fails is dropped with a note; a broken answer yields nothing but a note.

Two of those checks exist because a model reaches for them anyway. `kind/slug` is the literal placeholder in
the prompt's own shape and matches the id pattern, so the kind is checked against a list rather than a regex.
And an observation on a kind the schema lists attributes for keeps to that vocabulary, because the owner's
`sensitive` policy shields attributes by name: a health fact written to an invented `soreness` or `concern`
would be readable by every key, so it is dropped and the prompt says to use `health` or `income`. Those two are kept only when the key's schema lists them; a policy that
marks them sensitive hides them from the key's schema, and then they are dropped like any other unlisted name. A kind the schema is silent on, such as `activity`,
still takes any short lowercase name.
4. `to_batch` turns the facts into an observe batch and a list of actions with each row's `source` pointer set from its message id, the caller's source kind and nothing else.

A message window is one conversation, oldest first: one mail, or a run of consecutive Telegram messages, so a reply can be read against what it answers. Earlier messages of the conversation ride along marked `"context": true`: the prompt shows them under their own heading, the model is told to write facts only from the new ones, and `validate` drops any fact or action attributed to a context message, since those facts exist already.

## Using it from a reader

```python
sys.path.insert(0, os.path.join(HERE, '..', 'common'))
import analyze
model = analyze.Model.from_config(cfg['model'])      # url, name (None: the first model listed), key, provider, reasoning
context = analyze.context_from_state(ship.state(), 'mail')
facts = analyze.analyze(model, messages, context)      # bodies, observations, actions, notes
bodies, observations, actions = analyze.to_batch(facts, 'mail')
```

Config, in each reader's `config.json`:

```json
"model": {"url": "http://localhost:1234/v1", "name": null, "timeout": 180, "enabled": true}
```

A hosted OpenAI-compatible endpoint works the same way with a key, in the block as `api_key` or in the environment variable `api_key_env` names: `{"url": "https://openrouter.ai/api/v1", "name": "<model id>", "api_key": "sk-or-...", "provider": {"zdr": true}}`. `provider` and `reasoning` are OpenRouter's per-request fields and go out with every request as written. `"reasoning": {"enabled": false}` matters for a model that thinks by default (DeepSeek V4 Flash does): its hidden reasoning otherwise spends the whole answer's tokens, returns no JSON and bills for it; a reader stops on such an answer rather than skipping the message. `{"zdr": true}` sends these requests only to hosts that keep nothing, whatever the account's own setting. Name the model there; `null` takes the first one the server lists, which on a hosted router is arbitrary. The message text then leaves your machine for that provider.

`FakeModel(answer)` answers a canned string, for tests and for judging the validation without a server.

## The prompt

The system prompt is `analyst-prompt.md` beside `analyze.py`, plain text, read at import; a client in any language uses the same file. It teaches the model the three shapes and the rules: only what the messages say, existing bodies by id, new bodies only for named things, attribute names from the schema, `at` only when the message says when, honest confidence, one JSON object and nothing else. Change it in the markdown when the model's answers drift; the tests in `test_analyze.py` check the validation and that the file is what the code sends, not the model.

`brief-prompt.md` beside it is the morning brief's system prompt, the one Talon sends the frontier model at seven: what the brief is about (today, and the next two days only for what today must do for them), what it points out, and how short it is. A client that composes a brief sends this text word for word and holds it to the file with a test, the way Talon holds `analyst-prompt.md`. Guide the brief by editing this file, not the client.

## Small models and the meaning of an attribute

Every message goes through a small model, local or a cheap hosted one, because frontier rates per message are not affordable, so the prompt does the work a bigger model would do on its own. Three things help a small model most: definitions, a sink and examples. The schema may carry a `notes` block per kind (`"person": {"attrs": [...], "notes": {"status": "..."}}`), and the prompt prints every note under "What the attributes mean", so a status is defined as what the person is doing or dealing with right now and never a feeling. Feelings get a sink: the prompt offers `mood`, and `validate` throws `mood` (and `feeling`, `emotion`) away without a note, so "jury duty makes me want to scream" lands as `status = on jury duty` and the scream goes nowhere. Four worked examples sit in the prompt for the same reason. The notes are advisory, like the rest of the schema; the owner writes them with `PUT /schema`, and `reconcile.py` does not touch them.

## Association: twins the model makes anyway

`validate` folds a new body into one the ship already has when they are the same thing: a `situation` or `activity` whose normalised title matches an existing one (prefixes like "Reminder:", dates, times and weekdays stripped, so "Reminder: Pottery @ May 14, 6:00pm" is `activity/pottery`), and a `person` whose name words are all contained in an existing person's name or alias, or the other way round ("Dana Quill" is `person/dana`; "Dana" is not "Daniel Quill"). Subjects, references and `about` lists in the same answer follow the fold, and a note names the twin. The prompt says the same in words: an occurrence of a repeating event is an observation on one `activity` (`last` at the occurrence's start, `next` when known), never a body per occurrence, and a person is never an org. Money and health facts go to `income` and `health`, which orrery's starter schema names for a person and its starter policy lists as sensitive.

## reconcile.py: associating what the readers left apart

Three passes over a ship's state, run by the owner with the cookie jar, each with `--dry-run` first:

```bash
python3 reconcile.py --ship https://your-ship.example --jar jar activities --dry-run
python3 reconcile.py --ship https://your-ship.example --jar jar activities
python3 reconcile.py --ship https://your-ship.example --jar jar people              # files merge proposals
python3 reconcile.py --ship https://your-ship.example --jar jar people --apply      # runs the approved merges
python3 reconcile.py --ship https://your-ship.example --jar jar retire                # closes situations that are over (the ship does this itself since orrery 25)
python3 reconcile.py --ship https://your-ship.example --jar jar times                 # future facts become the schedule
```

`activities` groups situations that are occurrences of one repeating event: by calendar id when the body id carries one (`situation/cal-...-<uid>-<n>`), otherwise by identical title; groups whose common title normalises alike are one activity. Each group of at least three (`--min`) becomes `activity/<slug>` with the group's most common title as its name, the other titles and the calendar ids as aliases, `status` `active`, `location` and `participants` from the occurrences, and one `last` observation per dated occurrence at that occurrence's start, and one `next` for the nearest occurrence ahead, expiring when it ends. The occurrence bodies are deleted. Trips (`situation/<date>-trip`) and one-offs are left alone. The schema gains the `activity` kind if it lacks it.

`times` repairs the two mistakes a model makes with time: a `started` or `ended` in the future becomes `starts` or `ends` (the schedule, dated when it was learned), and a situation status that is not `open`, `closed` or `cancelled` is retracted, since upcoming, under way and over are read off the times. Every pass here runs on the ship as of orrery 26 (twice a day, and `POST /reconcile`), with the same rules; the commands stay for a ship that runs an older version, for a dry run against a saved state, and for reading what a pass would do before letting the ship do it. `retire` closes what is over: a situation whose `ended` has passed gets `status = closed` written at that end (one second after a later `open` row when a reminder said "open" after the event), a trip from the mail rule with no end closes a week after it starts, and a situation with a start but no end that began more than `--stale` days ago (30) with nothing observed since closes at its newest observation. A closed situation leaves everyone's `involved` list and the open list; the body and its timeline stay. `--prune DAYS` also deletes closed situations that ended more than DAYS ago, for a ship that wants old situations gone rather than closed. The analyst's context leaves out situations closed more than thirty days ago, so the model is not offered them as referents.

Consolidation runs off the ship on purpose while its rules are still being tuned. Once they hold still, the plan is to port it into orrery itself: the people pass first (a `merge` proposal filed by the writer when a new body matches an existing one, using the ship's own resolve), then the activities fold as a writer op on a nightly tick, so no timer and no owner cookie are needed.

`people` also reads people out of what the ship holds about events, so nobody has to seed their household by hand. The analyst does the same at write time from the whole event (title, description, attendees, organizer); reconcile only has what reached the ship, so it works from titles: "Mira- Ballet/Tap" and "Theo and Juno- Opti Sail" name participants, "Felix Birthday" names Felix, and a title that starts with a first name the ship already knows ("Felix Fencing Lesson") names that person. People the ship lacks are created with the first name as their name, and every activity or situation whose title names them gets them as a participant. A leading word the ship does not know as a person is only logged ("Swan Lake rehearsal" names nobody), so no body is invented from a production or a place. `people` finds bodies that name one person: an org whose name reads like a person's, two persons with the same email or phone, or names where every word of the shorter is in the longer. Each pair becomes an action of kind `merge`, payload `{"from", "into", "why"}`, with the person body (or the older body) as `into`, for the owner to approve on the page. `--apply` claims each approved merge and runs it through the ship's `POST /merge`, which moves the observations, re-points every reference, unions the aliases and deletes the duplicate; that route exists from orrery version 10.

## What stays here

The message text goes to the model and nowhere else: your machine with a local one, the provider with a hosted one (`"provider": {"zdr": true}` keeps those requests on hosts that retain nothing). The ship receives the validated facts and the message ids.
