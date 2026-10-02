#!/usr/bin/env python3
"""owners: the generator's prompt against lives that are not its author's.

Each file in owners/ is a state view of an invented owner with a fixed
clock ("at") and, under "expect", what a good assistant would do about
each open situation: the action kinds that would be right, or none when
the right move is to wait. One model is asked once per owner with the
prompt as it stands in ../common/generator-prompt.md, nothing is filed,
and the table says where the proposals met the expectation. Run it
before and after a change to the prompt: a change must not get worse on
any of them.

    python3 owners.py --config config.json
    python3 owners.py --config config.json --model anthropic/claude-opus-5 --out /tmp/run1
"""
import argparse
import glob
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run  # noqa: E402
from run import analyze  # noqa: E402
import bench  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def score(state, proposals):
    """per situation: (expected kinds, proposed kinds, met)"""
    rows = []
    for sid, want in (state.get('expect') or {}).items():
        got = sorted({p['kind'] for p in proposals if sid in (p.get('about') or []) or (p.get('payload') or {}).get('situation') == sid})
        met = (not got) if not want else any(k in want for k in got)
        rows.append((sid, want, got, met))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('--model', help='a model name; else the config\'s')
    ap.add_argument('--out', help='keep each raw answer here')
    args = ap.parse_args(argv)
    cfg = analyze.load_config(args.config)
    if args.model:
        cfg = dict(cfg, model=dict(cfg.get('model') or {}, name=args.model))
    limit = int(cfg.get('max_actions', 5))
    with open(run.PROMPT_PATH, encoding='utf-8') as f:
        system = f.read().strip()
    files = sorted(glob.glob(os.path.join(HERE, 'owners', '*.json')))

    def one(path):
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
        me = next((b for b in state['bodies'] if b['id'] == 'person/me'), {})
        parts = run.build_parts(state, [], state['at'], run.val(me.get('attrs') or {}, 'timezone'), limit)
        r = bench.ask(run.model_from(cfg), system, '\n'.join(parts), parts, state, limit)
        return os.path.basename(path)[:-5], state, r

    with ThreadPoolExecutor(max_workers=len(files)) as pool:
        results = list(pool.map(one, files))
    met = total = 0
    for name, state, r in results:
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, name + '.json'), 'w', encoding='utf-8') as f:
                json.dump(r, f, indent=1, ensure_ascii=False)
        print('== %s  (%ss%s)' % (name, r.get('seconds'), ', ' + r['error'] if r.get('error') else ''))
        for sid, want, got, ok in score(state, r['proposals']):
            total += 1
            met += ok
            print('  %s %-44s want %-18s got %s' % ('ok  ' if ok else 'MISS', sid, '/'.join(want) or 'nothing', '/'.join(got) or 'nothing'))
        for p in r['proposals']:
            print('     - %s | %s | about %s' % (p['kind'], p['title'], ', '.join(p.get('about') or [])))
        for n in r['notes']:
            print('     note: ' + n[:150])
    print('met %d of %d' % (met, total))
    return 0 if met == total else 1


if __name__ == '__main__':
    sys.exit(main())
