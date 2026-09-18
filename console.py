#!/usr/bin/env python3
"""console: run, watch and configure the orrery-utils integrations.

Every directory beside this file with a util.json is an integration. Its
manifest says how it runs (a loop systemd restarts, or a pass on a timer),
which fields of which config file are secrets, and which jobs it offers. The
console writes the systemd user units, starts and stops them, shows their
state and logs, sets secrets without ever showing one, and runs a job as a
transient unit that stops the daemon while it runs and starts it after, so
the two never write the same state.json at once.

    python3 console.py            # the TUI
    python3 console.py status     # one line per integration

util.json:
    {"about": "...",
     "run": ["bot.py", "--config", "config.json", "--loop"],   the daemon, run in its directory
     "every": "5min",                                          optional: a pass on a timer instead of a loop
     "secrets": {"orrery.token": "config.json"},               dotted field -> file, relative to the directory
     "jobs": {"re-ingest": {"cmd": [..., "{since}"], "ask": {"since": "from day"}}}}

Standard library only. Linux with systemd user units.
"""
import curses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UNIT_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'systemd', 'user')
EVERY_RE = re.compile(r'^\d+(s|min|h)$')


# ==  the integrations

def load_utils(root=HERE):
    """Every directory under root with a util.json, as a dict with its name and dir."""
    utils = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, 'util.json')
        if os.path.isfile(path):
            with open(path) as f:
                m = json.load(f)
            if m.get('every') and not EVERY_RE.match(m['every']):
                raise SystemExit('%s: every must look like 30s, 5min or 1h' % path)
            utils.append(dict(m, name=name, dir=os.path.join(root, name)))
    return utils


def unit(u, kind='service'):
    return 'orrery-utils-%s.%s' % (u['name'], kind)


def daemon_unit(u):
    """The unit that keeps it running: the timer for a pass, the service for a loop."""
    return unit(u, 'timer' if u.get('every') else 'service')


def job_unit(u):
    return 'orrery-utils-%s-job.service' % u['name']


def tag(u, job=False):
    """The journal identifier of what the program itself prints, apart from
    systemd's own lines about the unit."""
    return 'orrery-utils-%s%s' % (u['name'], '-job' if job else '')


def exec_line(argv):
    #  systemd reads quotes like a shell does, and % as a specifier
    return ' '.join(shlex.quote(a) for a in argv).replace('%', '%%')


def unit_files(u, python=sys.executable):
    """{file name: text} for the integration's units."""
    service = ['[Unit]', 'Description=orrery-utils %s: %s' % (u['name'], u.get('about', '')), '',
               '[Service]', 'WorkingDirectory=%s' % u['dir'], 'ExecStart=%s' % exec_line([python] + u['run']),
               'Environment=PYTHONUNBUFFERED=1', 'SyslogIdentifier=%s' % tag(u)]
    if u.get('every'):
        service += ['Type=oneshot']
        timer = ['[Unit]', 'Description=orrery-utils %s, every %s' % (u['name'], u['every']), '',
                 '[Timer]', 'OnActiveSec=10s', 'OnUnitInactiveSec=%s' % u['every'], '',
                 '[Install]', 'WantedBy=timers.target']
        return {unit(u): '\n'.join(service) + '\n', unit(u, 'timer'): '\n'.join(timer) + '\n'}
    #  a loop that exits (the model is down, the ship refused) is started again,
    #  and picks up from the message it stopped before
    service += ['Restart=always', 'RestartSec=30', '', '[Install]', 'WantedBy=default.target']
    return {unit(u): '\n'.join(service) + '\n'}


# ==  secrets: set, cleared or reported, never shown

def secret_path(u, file):
    return os.path.normpath(os.path.join(u['dir'], file))


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def get_field(cfg, dotted):
    for k in dotted.split('.'):
        if not isinstance(cfg, dict):
            return None
        cfg = cfg.get(k)
    return cfg


def secret_state(u, dotted, file):
    """What the console may say about a secret: set and how long, missing, or
    only in this shell's environment, which a daemon never sees."""
    cfg = read_json(secret_path(u, file))
    if cfg is None:
        return 'no %s' % file
    value = get_field(cfg, dotted)
    if value:
        return 'set (%d chars)' % len(str(value))
    env = get_field(cfg, dotted + '_env')
    if env and os.environ.get(env):
        return 'only in $%s here: a daemon will not see it' % env
    return 'not set'


def set_secret(u, dotted, file, value):
    """Write one secret into its config file, which stays readable by you alone.
    An empty value removes it."""
    path = secret_path(u, file)
    cfg = read_json(path)
    if cfg is None:
        raise ValueError('no %s to write to' % file)
    node = cfg
    keys = dotted.split('.')
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    if value:
        node[keys[-1]] = value
    else:
        node.pop(keys[-1], None)
    tmp = path + '.tmp'
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(cfg, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def missing_secrets(u):
    """The secrets a daemon would start without: a daemon reads its config
    files, never the shell's environment."""
    return [d for d, f in u.get('secrets', {}).items() if not secret_state(u, d, f).startswith('set ')]


def create_config(u):
    """config.json from config.example.json, when there is none."""
    src, dst = os.path.join(u['dir'], 'config.example.json'), os.path.join(u['dir'], 'config.json')
    if os.path.exists(dst):
        return 'config.json exists'
    if not os.path.exists(src):
        return 'no config.example.json to start from'
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return 'config.json made from config.example.json; set its secrets and fill in the rest'


# ==  systemd

def systemctl(*args):
    r = subprocess.run(['systemctl', '--user'] + list(args), capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


PROPS = 'Id,LoadState,ActiveState,SubState,Result,NRestarts,ExecMainStartTimestamp,NextElapseUSecRealtime'


def show(units):
    """{unit: {property: value}} for these units, in one call."""
    rc, out = systemctl('show', *units, '-p', PROPS)
    found, cur = {}, {}
    for line in out.splitlines() + ['']:
        if not line.strip():
            if cur.get('Id'):
                found[cur['Id']] = cur
            cur = {}
            continue
        k, _, v = line.partition('=')
        cur[k] = v
    return found


def when(stamp):
    """"Thu 2026-09-18 13:08:47 EDT" -> "09-18 13:08"."""
    p = stamp.split()
    return (p[1][5:] + ' ' + p[2][:5]) if len(p) >= 3 else stamp


def summary(u, st):
    """One phrase for the list: how the integration is doing."""
    if not os.path.exists(os.path.join(u['dir'], 'config.json')):
        return 'no config.json'
    svc = st.get(unit(u), {})
    if svc.get('LoadState') != 'loaded':
        return 'not installed'
    job = st.get(job_unit(u), {})
    extra = ', job running' if job.get('ActiveState') == 'active' else (', job failed' if job.get('ActiveState') == 'failed' else '')
    if u.get('every'):
        tim = st.get(unit(u, 'timer'), {})
        if svc.get('ActiveState') == 'activating':
            return 'running a pass' + extra
        last = 'last pass ' + ('ok' if svc.get('Result') == 'success' else svc.get('Result', '?'))
        if tim.get('ActiveState') != 'active':
            return 'stopped, ' + last + extra
        return 'every %s, next %s, %s%s' % (u['every'], when(tim.get('NextElapseUSecRealtime', '')), last, extra)
    if svc.get('ActiveState') == 'active':
        return 'running since %s, %s restarts%s' % (when(svc.get('ExecMainStartTimestamp', '')), svc.get('NRestarts', '0'), extra)
    if svc.get('ActiveState') == 'activating':
        return 'restarting (%s restarts)%s' % (svc.get('NRestarts', '0'), extra)
    return '%s (%s)%s' % (svc.get('ActiveState', '?'), svc.get('Result', '?'), extra)


def install(u):
    os.makedirs(UNIT_DIR, exist_ok=True)
    for name, text in unit_files(u).items():
        with open(os.path.join(UNIT_DIR, name), 'w') as f:
            f.write(text)
    rc, out = systemctl('daemon-reload')
    return 'units written to %s' % UNIT_DIR if rc == 0 else out


def start_stop(u, st):
    d = daemon_unit(u)
    if st.get(d, {}).get('ActiveState') in ('active', 'activating'):
        rc, out = systemctl('disable', '--now', d)
        if u.get('every'):
            systemctl('stop', unit(u))
        return out or 'stopped'
    missing = missing_secrets(u)
    if missing:
        return 'not started: set %s first (k)' % ', '.join(missing)
    rc, out = systemctl('enable', '--now', d)
    return out or 'started'


def restart(u):
    #  a timer's service is a pass: starting it runs one now
    rc, out = systemctl('start' if u.get('every') else 'restart', '--no-block', unit(u))
    return out or ('a pass started' if u.get('every') else 'restarted')


def job_argv(u, job, answers, daemon_active, python=sys.executable):
    """The systemd-run command for a job: it stops the daemon (and a pass in
    flight) while it runs, and starts the daemon again when it ends if it was
    running, because both write the same state.json."""
    spec = u['jobs'][job]
    cmd = []
    for a in spec['cmd']:
        for k, v in answers.items():
            a = a.replace('{%s}' % k, v)
        if re.search(r'\{\w+\}', a):
            raise ValueError('no answer for %s' % a)
        cmd.append(a)
    argv = ['systemd-run', '--user', '--unit=' + job_unit(u)[:-len('.service')], '--working-directory=' + u['dir'],
            '--setenv=PYTHONUNBUFFERED=1', '-p', 'SyslogIdentifier=' + tag(u, job=True),
            '-p', 'Conflicts=%s %s' % (unit(u), unit(u, 'timer'))]
    if daemon_active:
        argv += ['-p', 'ExecStopPost=-/usr/bin/systemctl --user start ' + daemon_unit(u)]
    return argv + [python] + cmd


def run_job(u, job, answers, st):
    active = st.get(daemon_unit(u), {}).get('ActiveState') in ('active', 'activating')
    argv = job_argv(u, job, answers, active)
    systemctl('reset-failed', job_unit(u))
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        return ((r.stdout + r.stderr).strip().splitlines() or ['systemd-run failed'])[-1]
    return 'job started: %s (L for its log)' % job


def last_line(u):
    r = subprocess.run(['journalctl', '--user', '-t', tag(u), '-t', tag(u, job=True), '-n', '1', '-o', 'cat', '--no-pager', '-q'],
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''


def linger():
    r = subprocess.run(['loginctl', 'show-user', os.environ.get('USER', ''), '-p', 'Linger', '--value'],
                       capture_output=True, text=True)
    return r.stdout.strip() == 'yes'


# ==  the TUI

HELP = 'i install  s start/stop  r restart  l log  L job log  k secret  j job  c create config  E linger  q quit'


def ask(scr, label, hide=False):
    """A line of input on the bottom row; Esc cancels. Hidden input shows stars."""
    scr.timeout(-1)
    buf = ''
    try:
        while True:
            h, w = scr.getmaxyx()
            scr.move(h - 1, 0)
            scr.clrtoeol()
            scr.addstr(h - 1, 0, (label + ': ' + ('*' * len(buf) if hide else buf))[:w - 1], curses.A_BOLD)
            ch = scr.get_wch()
            if ch in ('\n', '\r', curses.KEY_ENTER):
                return buf
            if ch == '\x1b':
                return None
            if ch in (curses.KEY_BACKSPACE, '\x7f', '\b'):
                buf = buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                buf += ch
    finally:
        scr.timeout(2000)


def pick(scr, label, items):
    """One of items by its number, or None."""
    if not items:
        return None
    got = ask(scr, '%s %s' % (label, '  '.join('%d %s' % (n, i) for n, i in enumerate(items, 1))))
    try:
        return items[int(got) - 1] if got and 0 < int(got) <= len(items) else None
    except ValueError:
        return None


def pager(cmd):
    """A log in less until q. systemd's default flags for less include F,
    which quits at once when the log fits on one screen."""
    curses.endwin()
    subprocess.call(cmd, env=dict(os.environ, SYSTEMD_LESS='RSXMK'))


def draw(scr, utils, sel, st, lines, note, lingering):
    scr.erase()
    h, w = scr.getmaxyx()

    def put(y, x, text, attr=0):
        if 0 <= y < h - 1:
            scr.addstr(y, x, str(text)[:max(0, w - x - 1)], attr)
    put(0, 0, 'orrery-utils console', curses.A_BOLD)
    put(0, 22, 'linger on' if lingering else 'linger off: daemons stop when you log out (E to enable)')
    for n, u in enumerate(utils):
        mark = '>' if n == sel else ' '
        put(2 + n, 0, '%s %-16s %-44s %s' % (mark, u['name'], summary(u, st), lines.get(u['name'], '')),
            curses.A_REVERSE if n == sel else 0)
    u = utils[sel]
    y = 3 + len(utils)
    put(y, 0, '%s: %s' % (u['name'], u.get('about', '')), curses.A_BOLD)
    put(y + 1, 2, 'runs     %s%s' % (' '.join(u['run']), ', every ' + u['every'] if u.get('every') else ', restarted when it exits'))
    y += 2
    for n, (dotted, file) in enumerate(u.get('secrets', {}).items(), 1):
        put(y, 2, '%-8s %d %-22s %-16s %s' % ('secrets' if n == 1 else '', n, dotted, file, secret_state(u, dotted, file)))
        y += 1
    for n, job in enumerate(u.get('jobs', {}), 1):
        put(y, 2, '%-8s %d %s' % ('jobs' if n == 1 else '', n, job))
        y += 1
    put(h - 3, 0, HELP)
    put(h - 2, 0, note, curses.A_BOLD)
    scr.refresh()


def tui(scr):
    curses.curs_set(0)
    scr.timeout(2000)
    utils = load_utils()
    if not utils:
        raise SystemExit('no util.json under ' + HERE)
    sel, note = 0, ''
    while True:
        st = show([x for u in utils for x in (unit(u), unit(u, 'timer'), job_unit(u))])
        lines = {u['name']: last_line(u) for u in utils}
        draw(scr, utils, sel, st, lines, note, linger())
        try:
            ch = scr.get_wch()
        except curses.error:
            continue
        u = utils[sel]
        note = ''
        if ch in ('q', 'Q'):
            return
        if ch == curses.KEY_DOWN:
            sel = (sel + 1) % len(utils)
        elif ch == curses.KEY_UP:
            sel = (sel - 1) % len(utils)
        elif ch == 'i':
            note = install(u)
        elif ch == 's':
            note = start_stop(u, st)
        elif ch == 'r':
            note = restart(u)
        elif ch == 'l':
            pager(['journalctl', '--user', '-u', unit(u), '-n', '2000', '-e'])
        elif ch == 'L':
            pager(['journalctl', '--user', '-u', job_unit(u), '-n', '2000', '-e'])
        elif ch == 'c':
            note = create_config(u)
        elif ch == 'E':
            r = subprocess.run(['loginctl', 'enable-linger'], capture_output=True, text=True)
            note = 'linger on' if r.returncode == 0 else (r.stderr.strip() or 'loginctl refused')
        elif ch == 'k':
            dotted = pick(scr, 'secret', list(u.get('secrets', {})))
            if dotted:
                value = ask(scr, '%s (empty removes it, Esc cancels)' % dotted, hide=True)
                if value is not None:
                    try:
                        set_secret(u, dotted, u['secrets'][dotted], value.strip())
                        note = '%s %s; restart for a running daemon to read it' % (dotted, 'set' if value.strip() else 'removed')
                    except ValueError as e:
                        note = str(e)
        elif ch == 'j':
            job = pick(scr, 'job', list(u.get('jobs', {})))
            if job:
                answers = {}
                for k, label in u['jobs'][job].get('ask', {}).items():
                    got = ask(scr, label)
                    if not got:
                        break
                    answers[k] = os.path.expanduser(got.strip())
                else:
                    if (ask(scr, 'run "%s" now%s? y/n' % (job, ', stopping the daemon until it ends'
                                                       if st.get(daemon_unit(u), {}).get('ActiveState') == 'active' else '')) or '').lower() == 'y':
                        try:
                            note = run_job(u, job, answers, st)
                        except ValueError as e:
                            note = str(e)


def status():
    utils = load_utils()
    st = show([x for u in utils for x in (unit(u), unit(u, 'timer'), job_unit(u))])
    for u in utils:
        print('%-16s %s' % (u['name'], summary(u, st)))
        for dotted, file in u.get('secrets', {}).items():
            print('  %-22s %-16s %s' % (dotted, file, secret_state(u, dotted, file)))


if __name__ == '__main__':
    if sys.argv[1:] == ['status']:
        status()
    else:
        curses.wrapper(tui)
