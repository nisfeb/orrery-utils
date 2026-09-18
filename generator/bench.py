#!/usr/bin/env python3
"""bench: the same question to every model, nothing filed.

One state is read once and the prompt is built from it with the open
actions and the recent decisions blanked and one fixed clock, so every
model gets byte-identical input and nothing it proposes is dropped as
already open. The models run in parallel; each answer is kept whole, and
one report lays the proposals and notes side by side with what each
call cost.

    python3 bench.py --config config.json --models anthropic/claude-opus-5,deepseek/deepseek-v4.1-flash
    python3 bench.py --config config.json                     # the "bench" list in the config
    python3 bench.py --config config.json --save-state s.json # keep the snapshot
    python3 bench.py --config config.json --state s.json      # rerun on a kept snapshot

Writes bench/<stamp>/<model>.json and bench/<stamp>/report.md next to this file.
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402
from run import analyze  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def blank(state):
    """The state with nothing open: what a model would see on a fresh ship."""
    s = dict(state)
    s['actions'] = []
    return s


def ask(model, system, user, parts, state, limit):
    """One model's turn: the raw answer, what validation keeps, its notes,
    the usage and the seconds it took; an error instead when it fails."""
    t = time.time()
    out = {'raw': '', 'proposals': [], 'notes': [], 'usage': None, 'error': None}
    try:
        out['raw'] = model.chat(system, user, parts)
        answer = analyze.parse_json(out['raw'])
        out['proposals'], out['notes'] = run.validate(answer if isinstance(answer, dict) else {}, state, [], limit)
    except (RuntimeError, ValueError) as e:
        out['error'] = str(e)[:500]
    out['usage'] = getattr(model, 'last_usage', None)
    out['seconds'] = round(time.time() - t, 1)
    return out


def slug(name):
    return re.sub(r'[^a-z0-9.]+', '-', name.lower()).strip('-')


def report(results, stamp, prompt_tokens_note=''):
    """Markdown: a table, then every model's proposals and notes in full."""
    lines = ['# bench %s' % stamp, '', '| model | cost | prompt | completion (reasoning) | seconds | proposals |', '|---|---|---|---|---|---|']
    for name, r in results:
        u = r.get('usage') or {}
        det = u.get('completion_tokens_details') or {}
        cost = '$%.4f' % float(u['cost']) if u.get('cost') is not None else ''
        comp = '%s (%s)' % (u.get('completion_tokens', ''), det.get('reasoning_tokens', '') if isinstance(det, dict) else '')
        lines.append('| %s | %s | %s | %s | %s | %s |' % (name, cost, u.get('prompt_tokens', ''), comp, r.get('seconds', ''), 'error' if r.get('error') else len(r['proposals'])))
    for name, r in results:
        lines += ['', '## ' + name, '']
        if r.get('error'):
            lines.append('error: ' + r['error'])
            continue
        if not r['proposals']:
            lines.append('_no proposals_')
        for p in r['proposals']:
            why = (p.get('payload') or {}).get('why', '')
            about = ', '.join(p.get('about') or [])
            lines.append('- **%s** (%s%s%s)%s' % (p.get('title'), p.get('kind'), '; about ' + about if about else '', '; due ' + p['due'] if p.get('due') else '', ': ' + why if why else ''))
        if r['notes']:
            lines.append('')
            for n in r['notes']:
                lines.append('- note: ' + n)
    return '\n'.join(lines) + '\n'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--models', help='comma-separated model names; else the config\'s "bench" list')
    ap.add_argument('--state', help='a saved state view instead of the ship')
    ap.add_argument('--save-state', help='write the state view read from the ship here')
    ap.add_argument('--out', default=os.path.join(HERE, 'bench'), help='where the runs go')
    args = ap.parse_args(argv)
    cfg = analyze.load_config(args.config)
    names = [m.strip() for m in (args.models.split(',') if args.models else cfg.get('bench') or []) if m.strip()]
    if not names:
        raise SystemExit('no models: pass --models or put a "bench" list in the config')
    if args.state:
        with open(args.state, encoding='utf-8') as f:
            state = json.load(f)
    else:
        token = analyze.secret(cfg.get('orrery'), 'token', 'ORRERY_TOKEN')
        if not token:
            raise SystemExit('no orrery key: put it in orrery.token, or set the variable orrery.token_env names')
        state = run.Ship(cfg['orrery']['url'], token).state()
        if args.save_state:
            with open(args.save_state, 'w', encoding='utf-8') as f:
                json.dump(state, f)
    state = blank(state)
    limit = int(cfg.get('max_actions', 5))
    me = next((b for b in state.get('bodies', []) if b.get('id') == state.get('me', 'person/me')), {})
    tz = run.val(me.get('attrs') or {}, 'timezone') or cfg.get('timezone')
    now = run.iso_now()
    parts = run.build_parts(state, [], now, tz, limit)
    user = '\n'.join(parts)
    with open(run.PROMPT_PATH, encoding='utf-8') as f:
        system = f.read().strip()
    stamp = now.replace(':', '').replace('-', '')[:15]
    out_dir = os.path.join(args.out, stamp)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'prompt.txt'), 'w', encoding='utf-8') as f:
        f.write(system + '\n\n----\n\n' + user)
    models = [(n, run.model_from(dict(cfg, model=dict(cfg.get('model') or {}, name=n)))) for n in names]
    print('# asking %d model(s) the same question, %d bodies, nothing filed' % (len(models), len(state.get('bodies', []))))
    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = [(n, pool.submit(ask, m, system, user, parts, state, limit)) for n, m in models]
        results = [(n, f.result()) for n, f in futures]
    for n, r in results:
        with open(os.path.join(out_dir, slug(n) + '.json'), 'w', encoding='utf-8') as f:
            json.dump(r, f, indent=1)
    text = report(results, stamp)
    with open(os.path.join(out_dir, 'report.md'), 'w', encoding='utf-8') as f:
        f.write(text)
    print(text)
    print('# written to', out_dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
