# work

Tells orrery when you are at the computer, so its Sunday review can show your
work hours and late nights (orrery 74). Each minute it asks whether you are
active: a graphical session of yours is unlocked (logind's `LockedHint`; the
desktop locks after its idle timeout), or one of your login terminals took a key
in the last five minutes (typing over SSH counts; tmux's panes do not, since
scripts type into them too, and your own typing in tmux reaches the terminal
tmux is attached to). Every five minutes it sends the
day's blocks of activity, gaps of ten minutes or less merged, to
`POST /apps/orrery/api/work`. It sends whether, never what: no content, window
titles or keystrokes.

Set up: copy `config.example.json` to `config.json`, give it your ship's URL and
a key with write from orrery's Keys page (`orrery.token`, or `ORRERY_TOKEN`),
then start it from the console (`python3 ../console.py`), which writes the
systemd user unit from `util.json`.

Tests: `python3 -m unittest` here.
