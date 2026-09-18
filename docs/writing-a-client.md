# Writing a client that does not fight the ship

The readers in this repo and any other client (Talon included) share one ship, and the ship is the source of truth. A client that re-sends what it already sent, or that names the same thing twice, undoes the consolidation and the retirement the owner's reconcile pass did. These are the rules that keep a client from doing that. They are written for a client that turns calendar reminders and messages into facts, because that is where every problem so far came from.

## 1. Never triage a message twice

Keep the ids of the messages you handled (a cursor, or a set, in a state file the way `mail/state.json` and `telegram/state.json` do). The ship answers `existing: true` for an observation it already holds, so a replayed observation is harmless, but a replayed body upsert recreates a body that reconcile deleted, hollow, and it comes back every time you replay. Replay is the root of most duplicates.

## 2. Resolve before you create, and take a hit as the thing itself

Before making a body for an event or a person, ask the ship: `GET /apps/orrery/api/resolve?q=<title or name>`. Since orrery version 10 resolve matches names and aliases exactly, then by word containment ("Dana" against "Dana Quill"), then by prefix, and it matches `email` and `phone` values. Try the calendar UID as well: reconcile stores the UID of a series as an alias of the activity it built from it.

- A hit of kind `activity`: the event is an occurrence of it. Write `last = <start of the occurrence>` on the activity, with `at` = that start, and `next = <start of the following one>` when you know it. Do not create a body.
- A hit of kind `situation`: the same event seen again. Add facts to it. Do not create a twin.
- A hit of kind `person`: the same person, whatever the name on the message. Use that id.
- No hit, and the event repeats (the calendar says so, or the title has been seen before): create one `activity` with `schedule`, `cadence`, `location`, `participants` and `organizer`, and the occurrences as `last` rows. Give it the calendar UID and the title variants as aliases.
- No hit, and it happens once: a `situation` with `started`, `ended`, `location` and `participants`.
- Any part of an event can name a person: the title ("Mira- Ballet/Tap", "Theo and Juno- Opti Sail", "Felix Birthday"), the description ("bring Juno's helmet"), the attendee list, the organizer ("Coach Pat"), a note. Every person the event names is a participant, and its organizer is its `organizer`. Resolve each name; when nobody by that name exists, create the person, the first name (or the full name when the event gives it) as the body's name. Do not turn a production, a team or a place into a person: "Swan Lake rehearsal" and "Hornets practice" name nobody. The owner never seeds their household by hand; the data says who is in it.

The system prompt the readers use is `common/analyst-prompt.md`, plain text: a client in any language can send the same prompt, with the same context block (the bodies, the attribute names and notes, the messages) that `analyze.prompt` builds. `common/analyze.py` has the two functions that decide sameness the way reconcile does: `normalize_title` (prefixes like "Reminder:", dates, times and weekdays stripped) and `same_person` (every word of the shorter name in the longer, role words like "wife" ignored, a one-word name must be the first name). Use them, or `validate`, which applies both to a whole answer and folds twins into the bodies the ship has.

## 3. The schedule is not the fact, and status is not the clock

A situation has two kinds of time. `starts` and `ends` are the schedule: a meeting on December 5 has `starts` and `ends` on December 5 the day the invitation arrives, and their `at` is when you learned the schedule, never the event's own time (a row dated in the future is hidden until then). `started` and `ended` are facts about what happened, written once it has, with `at` at the moment. Never write the past tense for something ahead. `status` is only ever `open`, `closed` or `cancelled`: never `upcoming`, `under way` or `over`, which the page and the owner's passes read off the times. Write `status` only when the message says something changed; `closed` when it is over or cancelled. A `status: open` row dated after a `closed` row reopens the situation, which is what a late reminder did to a trip that had been closed for months, so for a calendar event write no status at all.

## 4. Event time in `at`

For `started`, `ended`, `last` and `next`, `at` is the event's own time, not the reminder's. The fold takes the row with the latest `at`, so a reminder stamped after the event would otherwise outrank a close, and the timeline would say the event happened when the reminder arrived.

## 5. Write through a key, never through the owner cookie

Mint a key for the client (`POST /clients`) and send `Authorization: Bearer <token>`. Every row then carries the client's identity in `by` and in `/tr/log`, and its writes are bounded by the key's scope. Writes through the owner cookie arrive with no name, and a mystery writer cannot be told from the owner.

## 6. Do not push a local cache of bodies to the ship

A client learns what exists by reading `GET /state`, once at the start and again every so often during a long run (the mail reader re-reads every 50 messages, the Telegram backfill every ten windows, the live bot every ten minutes). It never writes its own list of bodies back. A client that re-upserts what it remembers recreates everything the owner removed, and does it in bulk.

## 7. Sensitive facts have two names

Medical facts go under `health`, money facts under `income`, and nowhere else. Both are in the starter schema for a person and both are in the starter policy's `sensitive` list, so keys never see them. A client that invents another name for a medical fact leaks it to every key. A client that learns such facts from other people (a message says "mom's biopsy came back clear") is minted with `"sensitive": "write"` in its scope (orrery version 13): it may store them, and every view keeps hiding them from it. Its schema view lists the two names, which is how a client knows it may write them. The ship gives such a key nothing back about a sensitive row: no `existing` flag on the write, and the beacon moves the same way for a new row and a repeat.

## 8. A status is a circumstance, not a feeling

`status` on a person is what they are doing or dealing with right now, in plain words: "on jury duty", "stranded, waiting for a tow", "travelling", "sick". Never a feeling, a quote or a wish. The schema's `notes` block says so for every attribute that needs saying; read it from the state view and put it in your prompt. Give feelings a sink the client throws away (`mood`), so a small model has somewhere to put "want to scream" that is not `status`. Small models follow a worked example better than a rule; carry two.

## 9. Read a message with the ones before it

A message alone often says nothing: "yes, at 8", "still here", "ugh". Hand a small model the last three to five messages of the same conversation as context with each new one, and it reads the new one right. Two rules make that safe. The earlier messages are marked as context, and facts are written only from the new ones; anything the model writes from a context message is thrown away, because those facts exist already and a second reading of them makes superseded twins. And each fact still names the message it comes from. Mail stays one message per call: a mail is its own conversation and carries its quoted history. Five is about the right window for a model this size; twelve dilutes it. `common/analyze.py` takes the window as a list of messages with `"context": true` on the earlier ones and enforces both rules; the Telegram bot keeps the last four free-text messages per chat for this, and the backfill reads runs of six with the previous two as context.

## 10. Replay-safe by construction

Everything above makes a client safe to restart from zero: observations are content-addressed, bodies are resolved before they are made, occurrences land on activities, status is only ever written as `closed`, and the message log says what was handled. A client with those properties can be run again over the same month of mail and the ship ends up the same.

## 11. A task is a todo in the calendar, and a todo is not an event

Orrery's `task` actions belong in the calendar's Tasks view, so the owner sees one list on the ship, in Thunderbird and on the phone. The generator files the task and stops; the client that already reads the calendar (Talon on ricsul) mirrors it. The mirror is a sync with three rules.

**One todo per action, linked by the action id.** An approved or claimed `task` (`GET /actions?status=open`, filtered by kind and status; a proposed task waits in the owner's inbox and is not a todo) becomes a todo whose link to the action is stored in the todo itself, never in the client's memory. Over the poke API that is `add-event` with `cat: "todo"`, `meta.name` the action's title, `meta.note` the payload's `why` and `notes`, `meta.tags: ["orrery"]`, `meta.orrery` the action id (a meta field the calendar does not know rides along verbatim and comes back in every `events.json` row) and `due_ms` from `due`. Over CalDAV it is a VTODO with UID `orrery-<action id>`, `CATEGORIES:orrery` and `DUE`. Before creating, look for a todo carrying that id; if there is one, there is nothing to create. That is what makes a restart from zero safe.

**The two sides stay in step, each pass.** Read `GET /actions?status=all` and the calendar's todos (`events.json` rows whose `cat` is `todo`, or a CalDAV `calendar-query` for VTODO). An action that went `done` in orrery ticks its todo (`done-event {id, done: true}`, or `STATUS:COMPLETED` with `COMPLETED`). An action that went `dismissed` or `failed` deletes its todo (`del-event {id}`, or DELETE): the owner said no, and a cancelled todo is noise. A todo ticked in the calendar (`done: true`) while orrery still says approved or claimed moves the action, `POST /actions/<id> {"status": "done", "note": "ticked in the calendar"}`, with the client's key, which needs `task` among its `actions`. No claim is involved: a claim is for the executor that does the work, and a tick is the owner reporting that it is done. If the ship refuses because an executor holds the claim, leave it for the next pass; the claimant reports. Orrery's decisions are final: never reopen a done or dismissed action because its todo was unticked, and once the two sides agree, leave the todo alone.

**A mirrored todo never comes back as a situation.** The same client reads the calendar into orrery, and a todo it wrote must not be read as an event the next time round. Skip every todo that carries `meta.orrery` (or a UID starting `orrery-`, or the `orrery` tag): it is orrery's own, and orrery already has it as an action. A todo the owner typed into the calendar by hand is not a situation either: it has no time span. The reader that writes situations from events looks at `cat` first and takes only `timed`, `allday` and `date` rows; `todo` rows never become bodies.

## What reconcile does when a client gets it wrong

`common/reconcile.py` is the owner's cleanup: `activities` folds occurrence situations into activities, `people` proposes merges for bodies that name one person, and `retire` closes situations that are over. It is a net, not a licence: a client that keeps re-creating bodies is undone by reconcile and undoes it back, every few minutes, and nobody wins.
