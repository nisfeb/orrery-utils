#!/usr/bin/env python3
"""work: tell orrery when the owner is at the computer, never what they do.

Each minute the reporter decides whether the owner is active: a graphical
session of theirs is unlocked (logind's LockedHint; the desktop locks after
its idle timeout), or one of their terminals took a key in the last five
minutes (a tty's access time moves when its reader takes input, so typing
over SSH counts). It keeps each local day's active minutes in its state file
and every five minutes sends the day to orrery as blocks of activity, gaps of
ten minutes or less merged: POST /apps/orrery/api/work {day, blocks}. No
content, no window titles, no keystrokes: whether, never what.

    python3 reporter.py --config config.json --loop    # the daemon
    python3 reporter.py --config config.json --once    # one sample and a send

config.json: {"orrery": {"url": "https://your-ship.example", "token_env": "ORRERY_TOKEN"},
              "state": "state.json"}
The token is a key with write, minted on orrery's Keys page.

Standard library only.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

IDLE = 300          # seconds since a terminal's last key that still count as active
GAP = 10            # minutes between two active minutes that still make one block
KEEP = 3            # days of minutes the state file keeps


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def desktop_unlocked(runner=run, uid=None):
    """Whether a graphical session of this user is unlocked."""
    uid = os.getuid() if uid is None else uid
    for line in runner(['loginctl', 'list-sessions', '--no-legend']).splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != str(uid):
            continue
        props = dict(p.split('=', 1) for p in runner(['loginctl', 'show-session', parts[0], '-p', 'Type', '-p', 'LockedHint', '-p', 'State']).split() if '=' in p)
        if props.get('Type') in ('wayland', 'x11') and props.get('LockedHint') == 'no' and props.get('State') == 'active':
            return True
    return False


def login_ttys(runner=run, user=None):
    """This user's login terminals as utmp lists them (`who`): SSH sessions and
    terminal windows, not tmux's panes. A pane also takes keys from scripts
    that drive a console with tmux send-keys; the owner's own typing in tmux
    reaches it through the terminal tmux is attached to, which is listed."""
    user = user or os.environ.get('USER') or ''
    out = []
    for line in runner(['who']).splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != user or not parts[1].startswith('pts/'):
            continue
        if '(tmux(' in line:
            continue
        out.append(parts[1])
    return out


def tty_recent(now, ttys=None, root='/dev', idle=IDLE):
    """Whether one of those terminals took a key in the last idle seconds."""
    for t in (login_ttys() if ttys is None else ttys):
        try:
            st = os.stat(os.path.join(root, t))
        except OSError:
            continue
        if now - st.st_atime < idle:
            return True
    return False


def active(now):
    return desktop_unlocked() or tty_recent(now)


def local_day(minute):
    return datetime.datetime.fromtimestamp(minute * 60).date().isoformat()


def record(state, now):
    """This minute, kept under its local day; days past KEEP dropped."""
    minute = int(now // 60)
    day = local_day(minute)
    days = state.setdefault('days', {})
    mins = set(days.get(day, []))
    mins.add(minute)
    days[day] = sorted(mins)
    for d in sorted(days)[:-KEEP]:
        del days[d]
    return day


def blocks(minutes, gap=GAP):
    """Active minutes as blocks of [start, end), ISO UTC, a gap of gap minutes or less merged."""
    out = []
    for m in sorted(minutes):
        if out and m <= out[-1][1] + gap:
            out[-1][1] = m + 1
        else:
            out.append([m, m + 1])
    iso = lambda m: datetime.datetime.fromtimestamp(m * 60, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return [{'start': iso(a), 'end': iso(b)} for a, b in out]


def send(cfg, day, minutes, opener=urllib.request.urlopen):
    o = cfg.get('orrery') or {}
    token = o.get('token') or os.environ.get(o.get('token_env') or 'ORRERY_TOKEN', '')
    body = json.dumps({'day': day, 'blocks': blocks(minutes)}).encode()
    req = urllib.request.Request(o['url'].rstrip('/') + '/apps/orrery/api/work', data=body, method='POST',
                                 headers={'content-type': 'application/json', 'authorization': 'Bearer ' + token})
    with opener(req, timeout=60) as r:
        return r.status


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(path, state):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, path)


def tick(cfg, state_path, now, sent_at, force=False):
    """One minute: record it when active; every five minutes, send today, and yesterday's
    last word in the first hour after midnight. Answers when it last sent."""
    state = load(state_path)
    if active(now):
        record(state, now)
    save(state_path, state)
    if not force and now - sent_at < 300:
        return sent_at
    today = local_day(int(now // 60))
    days = [today]
    if datetime.datetime.fromtimestamp(now).hour == 0:
        days.insert(0, (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat())
    for d in days:
        if state.get('days', {}).get(d):
            try:
                send(cfg, d, state['days'][d])
            except Exception as e:
                print('send %s: %s' % (d, e), file=sys.stderr)
                return sent_at
    return now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = json.load(f)
    state_path = os.path.join(os.path.dirname(os.path.abspath(a.config)), cfg.get('state', 'state.json'))
    if not a.loop:
        tick(cfg, state_path, time.time(), 0, force=True)
        return
    sent_at = 0
    while True:
        sent_at = tick(cfg, state_path, time.time(), sent_at)
        time.sleep(60 - time.time() % 60)


if __name__ == '__main__':
    main()
