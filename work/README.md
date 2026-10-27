# work

Tells orrery when you are at the computer, so its Sunday review can show your work hours and late nights (orrery 74), and its nudges know when you have sat too long. Each minute it asks whether you are active: a graphical session of yours is unlocked (logind's `LockedHint`) and its compositor saw input in the last five minutes, or one of your login terminals took a key in the last five minutes (typing over SSH counts; tmux's panes do not, since scripts type into them too, and your own typing in tmux reaches the terminal tmux is attached to). Unlocked alone is not enough: a desktop that locks after ninety idle minutes counted an hour away from it as an hour at the desk.

The compositor says only that no input came for five minutes, and when it came again, over the Wayland idle protocol (`ext_idle_notifier_v1`, version 2's input notification, so a video left playing does not count). The reporter speaks that protocol itself, with the standard library. With no compositor to ask, the desktop counts as away and the journal says why. Every five minutes it sends the day's blocks of activity, gaps of ten minutes or less merged, to `POST /apps/orrery/api/work`. It sends whether, never what: no content, window titles or keystrokes.

Set up: copy `config.example.json` to `config.json`, give it your ship's URL and
a key with write from orrery's Keys page (`orrery.token`, or `ORRERY_TOKEN`),
then start it from the console (`python3 ../console.py`), which writes the
systemd user unit from `util.json`.

Tests: `python3 -m unittest` here.
