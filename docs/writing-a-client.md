# Writing a client that does not fight the ship

The readers in this repo and any other client (Talon included) share one ship, and the ship is the source of truth. A client that re-sends what it already sent, or that names the same thing twice, undoes the consolidation and the retirement the owner's reconcile pass did. These are the rules that keep a client from doing that. They are written for a client that turns calendar reminders and messages into facts, because that is where every problem so far came from.

## 1. Never triage a message twice

Keep the ids of the messages you handled (a cursor, or a set, in a state file the way `mail/state.json` and `telegram/state.json` do). The ship answers `existing: true` for an observation it already holds, so a replayed observation is harmless, but a replayed body upsert recreates a body that reconcile deleted, hollow, and it comes back every time you replay. Replay is the root of most duplicates.

## 2. Resolve before you create, and take a hit as the thing itself

Before making a body for an event or a person, ask the ship: `GET /apps/orrery/api/resolve?q=<title or name>`. Since orrery version 10 resolve matches names and aliases exactly, then by word containment ("Andrea" against "Andrea Egan"), then by prefix, and it matches `email` and `phone` values. Try the calendar UID as well: reconcile stores the UID of a series as an alias of the activity it built from it.

- A hit of kind `activity`: the event is an occurrence of it. Write `last = <start of the occurrence>` on the activity, with `at` = that start, and `next = <start of the following one>` when you know it. Do not create a body.
- A hit of kind `situation`: the same event seen again. Add facts to it. Do not create a twin.
- A hit of kind `person`: the same person, whatever the name on the message. Use that id.
- No hit, and the event repeats (the calendar says so, or the title has been seen before): create one `activity` with `schedule`, `cadence`, `location`, `participants` and `organizer`, and the occurrences as `last` rows. Give it the calendar UID and the title variants as aliases.
- No hit, and it happens once: a `situation` with `started`, `ended`, `location` and `participants`.

`common/analyze.py` has the two functions that decide sameness the way reconcile does: `normalize_title` (prefixes like "Reminder:", dates, times and weekdays stripped) and `same_person` (every word of the shorter name in the longer, role words like "wife" ignored, a one-word name must be the first name). Use them, or `validate`, which applies both to a whole answer and folds twins into the bodies the ship has.

## 3. Never write `status: "open"` for an event

Write `started` and `ended`. A situation counts as open until something says `closed`; the owner's `retire` pass closes it at its end. Write `status` only when the message says something changed: `closed` when it is over or cancelled, `open` never for a calendar event. A `status: open` row dated after a `closed` row reopens the situation, which is what a late reminder did to a trip that had been closed for months.

## 4. Event time in `at`

For `started`, `ended`, `last` and `next`, `at` is the event's own time, not the reminder's. The fold takes the row with the latest `at`, so a reminder stamped after the event would otherwise outrank a close, and the timeline would say the event happened when the reminder arrived.

## 5. Write through a key, never through the owner cookie

Mint a key for the client (`POST /clients`) and send `Authorization: Bearer <token>`. Every row then carries the client's identity in `by` and in `/tr/log`, and its writes are bounded by the key's scope. Writes through the owner cookie arrive with no name, and a mystery writer cannot be told from the owner.

## 6. Do not push a local cache of bodies to the ship

A client learns what exists by reading `GET /state`, once at the start and again every so often during a long run (the readers re-read every 50 messages). It never writes its own list of bodies back. A client that re-upserts what it remembers recreates everything the owner removed, and does it in bulk.

## 7. Sensitive facts have two names

Medical facts go under `health`, money facts under `income`, and nowhere else. Both are in the starter schema for a person and both are in the starter policy's `sensitive` list, so keys never see them. A client that invents another name for a medical fact leaks it to every key.

## 8. A status is a circumstance, not a feeling

`status` on a person is what they are doing or dealing with right now, in plain words: "on jury duty", "stranded, waiting for a tow", "travelling", "sick". Never a feeling, a quote or a wish. The schema's `notes` block says so for every attribute that needs saying; read it from the state view and put it in your prompt. Give feelings a sink the client throws away (`mood`), so a small model has somewhere to put "want to scream" that is not `status`. Small models follow a worked example better than a rule; carry two.

## 9. Replay-safe by construction

Everything above makes a client safe to restart from zero: observations are content-addressed, bodies are resolved before they are made, occurrences land on activities, status is only ever written as `closed`, and the message log says what was handled. A client with those properties can be run again over the same month of mail and the ship ends up the same.

## What reconcile does when a client gets it wrong

`common/reconcile.py` is the owner's cleanup: `activities` folds occurrence situations into activities, `people` proposes merges for bodies that name one person, and `retire` closes situations that are over. It is a net, not a licence: a client that keeps re-creating bodies is undone by reconcile and undoes it back, every few minutes, and nobody wins.
