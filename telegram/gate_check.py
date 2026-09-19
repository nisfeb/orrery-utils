#!/usr/bin/env python3
"""gate_check: what the gate would do over a Telegram Desktop export.

    python3 gate_check.py --config config.json export.json [--chat "Family"] [--limit 200]

Every free-text message is put to the decision model with the three before
it as context, and one line per message says the probability that it
carries a fact and whether the analyst would have been asked. Totals at the
end: how many the gate saves, and what the check cost. Nothing is written
anywhere; the ship is not read.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import analyze  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bot  # noqa: E402


def texts(export, chat=None):
    """(chat name, sender, text) for every text message in the export."""
    out = []
    chats = export.get('chats', {}).get('list', []) if isinstance(export, dict) else []
    for c in chats:
        if chat and c.get('name') != chat:
            continue
        for m in c.get('messages', []):
            t = m.get('text')
            if isinstance(t, list):
                t = ''.join(x if isinstance(x, str) else str(x.get('text', '')) for x in t)
            if m.get('type') == 'message' and t and str(t).strip():
                out.append((c.get('name', ''), m.get('from', ''), str(t).strip()))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default='config.json')
    ap.add_argument('export')
    ap.add_argument('--chat')
    ap.add_argument('--limit', type=int, default=200)
    ap.add_argument('--threshold', type=float)
    ap.add_argument('--at-once', type=int, default=6, help='how many messages are asked about at a time')
    args = ap.parse_args(argv)
    cfg = analyze.load_config(args.config)
    decider = analyze.Decider.from_config(cfg)
    if decider is None:
        raise SystemExit('no "decide" block in the config')
    threshold = args.threshold if args.threshold is not None else float((cfg.get('decide') or {}).get('threshold', bot.GATE_THRESHOLD))
    with open(args.export, encoding='utf-8') as f:
        rows = texts(json.load(f), args.chat)[-args.limit:]
    #  the first alone, so a model that does not answer is said at once
    #  rather than after a run of thirty-second waits; then six at a time,
    #  since one after another is minutes. The answers print in order asked.
    from concurrent.futures import ThreadPoolExecutor
    def ask(i):
        chat, who, text = rows[i]
        earlier = [{'text': t, 'context': True} for _, _, t in rows[max(0, i - 3):i]]
        d = analyze.Decider(decider.api_key, decider.model, decider.url, decider.provider, decider.timeout)
        p = analyze.gate(d, earlier + [{'text': text, 'who': who}], {'bodies': []})
        return p, float((d.last_usage or {}).get('cost') or 0)
    if not rows:
        print('# no messages'); return 0
    try:
        results = [ask(0)]
    except RuntimeError as e:
        raise SystemExit('the decision model did not answer: ' + str(e)[:200])
    with ThreadPoolExecutor(max_workers=args.at_once) as pool:
        results += list(pool.map(ask, range(1, len(rows))))
    read, skipped, cost = 0, 0, 0.0
    probs = []
    for (chat, who, text), (p, c) in zip(rows, results):
        cost += c
        probs.append(p)
        verdict = 'read' if p >= threshold else 'skip'
        read += verdict == 'read'
        skipped += verdict == 'skip'
        print('%.2f %s | %s: %s' % (p, verdict, who, text[:90].replace('\n', ' ')))
    band = ', '.join('%d read at %.2f' % (sum(1 for p in probs if p >= t), t) for t in (0.2, 0.25, 0.3, 0.35, 0.4))
    print('# %d messages: %d read, %d skipped at %.2f; %s; the check cost $%.4f' % (len(rows), read, skipped, threshold, band, cost))
    return 0


if __name__ == '__main__':
    sys.exit(main())
