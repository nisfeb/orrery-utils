import json, os, tempfile, time, unittest
import reporter


class Blocks(unittest.TestCase):
    def test_a_gap_of_ten_merges_and_eleven_splits(self):
        b = reporter.blocks([0, 1, 2, 12, 24])
        self.assertEqual(len(b), 2)
        self.assertEqual(b[0], {'start': '1970-01-01T00:00:00Z', 'end': '1970-01-01T00:13:00Z'})
        self.assertEqual(b[1], {'start': '1970-01-01T00:24:00Z', 'end': '1970-01-01T00:25:00Z'})

    def test_none(self):
        self.assertEqual(reporter.blocks([]), [])


class Record(unittest.TestCase):
    def test_a_minute_once_under_its_day_and_old_days_dropped(self):
        st = {'days': {'2000-01-01': [1], '2000-01-02': [2], '2000-01-03': [3]}}
        now = time.time()
        day = reporter.record(st, now)
        reporter.record(st, now + 1)
        self.assertEqual(st['days'][day], [int(now // 60)])
        self.assertEqual(len(st['days']), reporter.KEEP)
        self.assertNotIn('2000-01-01', st['days'])


class Signals(unittest.TestCase):
    def test_a_terminal_typed_in_recently_counts_and_an_old_one_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, 'pts'))
            p = os.path.join(d, 'pts', '7')
            open(p, 'w').close()
            now = time.time()
            os.utime(p, (now - 60, now - 60))
            self.assertTrue(reporter.tty_recent(now, ttys=['pts/7'], root=d))
            os.utime(p, (now - 900, now - 900))
            self.assertFalse(reporter.tty_recent(now, ttys=['pts/7'], root=d))
            self.assertFalse(reporter.tty_recent(now, ttys=['pts/8'], root=d))

    def test_login_terminals_are_ssh_and_windows_not_tmux_panes(self):
        who = ('me       tty1         2026-09-29 08:49\n'
               'me       pts/0        2026-09-29 08:49 (:1)\n'
               'me       pts/2        2026-09-29 08:54 (tmux(11640).%0)\n'
               'me       pts/20       2026-10-06 00:23 (203.0.113.7)\n'
               'other    pts/21       2026-10-06 00:23 (10.0.0.2)\n')
        self.assertEqual(reporter.login_ttys(lambda cmd: who, user='me'), ['pts/0', 'pts/20'])

    def test_only_an_unlocked_graphical_session_of_this_user(self):
        out = {('loginctl', 'list-sessions', '--no-legend'): '3 1000 me seat0 1 user tty1 no -\n9 1000 me - 2 user pts/1 no -\n4 1001 other seat0 3 user tty2 no -\n',
               '3': 'Type=wayland\nLockedHint=no\nState=active\n', '9': 'Type=tty\nLockedHint=no\nState=active\n', '4': 'Type=wayland\nLockedHint=no\nState=active\n'}
        def runner(cmd):
            return out[tuple(cmd)] if tuple(cmd) in out else out[cmd[2]]
        self.assertTrue(reporter.desktop_unlocked(runner, uid=1000))
        out['3'] = 'Type=wayland\nLockedHint=yes\nState=active\n'
        self.assertFalse(reporter.desktop_unlocked(runner, uid=1000))


class Send(unittest.TestCase):
    def test_the_day_goes_as_blocks_with_the_key(self):
        seen = {}
        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
        def opener(req, timeout):
            seen['url'], seen['auth'], seen['body'] = req.full_url, req.headers.get('Authorization'), json.loads(req.data)
            return R()
        cfg = {'orrery': {'url': 'https://ship.example/', 'token': 'tk'}}
        self.assertEqual(reporter.send(cfg, '1970-01-01', [0, 1], opener), 200)
        self.assertEqual(seen['url'], 'https://ship.example/apps/orrery/api/work')
        self.assertEqual(seen['auth'], 'Bearer tk')
        self.assertEqual(seen['body'], {'day': '1970-01-01', 'blocks': [{'start': '1970-01-01T00:00:00Z', 'end': '1970-01-01T00:02:00Z'}]})


if __name__ == '__main__':
    unittest.main()
