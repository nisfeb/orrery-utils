"""The console without curses or systemd: the manifests it finds, the units
it writes, secrets set and reported without being shown, and the job command.

    python3 -m unittest test_console
"""
import json
import os
import stat
import tempfile
import unittest

import console


def fake_util(d, **kw):
    return dict({'name': 'demo', 'dir': d, 'about': 'a demo', 'run': ['demo.py', '--loop'],
                 'secrets': {'orrery.token': 'config.json'},
                 'jobs': {'re-ingest': {'cmd': ['demo.py', '--since', '{since}'], 'ask': {'since': 'from day'}}}}, **kw)


class Manifests(unittest.TestCase):
    def test_every_integration_names_scripts_it_has(self):
        utils = console.load_utils()
        self.assertEqual([u['name'] for u in utils], ['generator', 'home-assistant', 'mail', 'telegram'])
        for u in utils:
            scripts = [u['run'][0]] + [j['cmd'][0] for j in u['jobs'].values()]
            for s in scripts:
                self.assertTrue(os.path.isfile(os.path.join(u['dir'], s)), (u['name'], s))
            for file in u['secrets'].values():
                self.assertTrue(os.path.isdir(os.path.dirname(console.secret_path(u, file))), (u['name'], file))


class Units(unittest.TestCase):
    def test_a_loop_is_restarted_and_a_pass_has_a_timer(self):
        loop = console.unit_files(fake_util('/srv/my utils/demo'), python='/usr/bin/python3')
        text = loop['orrery-utils-demo.service']
        self.assertIn("ExecStart=/usr/bin/python3 demo.py --loop", text)
        self.assertIn("WorkingDirectory=/srv/my utils/demo", text)
        self.assertIn('Restart=always', text)
        self.assertIn('WantedBy=default.target', text)
        self.assertIn('SyslogIdentifier=orrery-utils-demo', text)
        timed = console.unit_files(fake_util('/srv/demo', every='5min', run=['demo.py', '--x=50%']), python='/usr/bin/python3')
        self.assertIn('Type=oneshot', timed['orrery-utils-demo.service'])
        self.assertIn("ExecStart=/usr/bin/python3 demo.py --x=50%%", timed['orrery-utils-demo.service'])
        self.assertIn('OnUnitInactiveSec=5min', timed['orrery-utils-demo.timer'])
        self.assertNotIn('Restart=', timed['orrery-utils-demo.service'])


class Secrets(unittest.TestCase):
    def test_a_secret_is_written_alone_and_never_shown(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'config.json')
            with open(path, 'w') as f:
                json.dump({'orrery': {'url': 'https://ship', 'token_env': 'DEMO_TOKEN'}, 'state': 'state.json'}, f)
            u = fake_util(d)
            os.environ.pop('DEMO_TOKEN', None)
            self.assertEqual(console.secret_state(u, 'orrery.token', 'config.json'), 'not set')
            self.assertEqual(console.missing_secrets(u), ['orrery.token'])
            self.assertEqual(console.start_stop(u, {}), 'not started: set orrery.token first (k)')
            os.environ['DEMO_TOKEN'] = 'from-shell'
            self.assertIn('a daemon will not see it', console.secret_state(u, 'orrery.token', 'config.json'))
            console.set_secret(u, 'orrery.token', 'config.json', 'sekrit-value')
            self.assertEqual(console.secret_state(u, 'orrery.token', 'config.json'), 'set (12 chars)')
            self.assertEqual(console.missing_secrets(u), [])
            with open(path) as f:
                cfg = json.load(f)
            self.assertEqual(cfg, {'orrery': {'url': 'https://ship', 'token_env': 'DEMO_TOKEN', 'token': 'sekrit-value'},
                                   'state': 'state.json'})
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            console.set_secret(u, 'orrery.token', 'config.json', '')
            with open(path) as f:
                self.assertNotIn('token', json.load(f)['orrery'])
            self.assertEqual(console.secret_state(u, 'model.api_key', 'missing.json'), 'no missing.json')


class Jobs(unittest.TestCase):
    def test_a_job_stops_the_daemon_and_starts_it_again(self):
        u = fake_util('/srv/demo')
        argv = console.job_argv(u, 're-ingest', {'since': '2026-09-18'}, daemon_active=True, python='/usr/bin/python3')
        self.assertEqual(argv[-4:], ['/usr/bin/python3', 'demo.py', '--since', '2026-09-18'])
        self.assertIn('--unit=orrery-utils-demo-job', argv)
        self.assertIn('Conflicts=orrery-utils-demo.service orrery-utils-demo.timer', argv)
        self.assertIn('SyslogIdentifier=orrery-utils-demo-job', argv)
        self.assertIn('ExecStopPost=-/usr/bin/systemctl --user start orrery-utils-demo.service', argv)
        idle = console.job_argv(u, 're-ingest', {'since': '2026-09-18'}, daemon_active=False)
        self.assertFalse([a for a in idle if a.startswith('ExecStopPost')])
        with self.assertRaises(ValueError):
            console.job_argv(u, 're-ingest', {}, daemon_active=False)


class Summary(unittest.TestCase):
    def test_how_an_integration_is_doing(self):
        with tempfile.TemporaryDirectory() as d:
            u = fake_util(d)
            self.assertEqual(console.summary(u, {}), 'no config.json')
            open(os.path.join(d, 'config.json'), 'w').write('{}')
            self.assertEqual(console.summary(u, {}), 'not installed')
            st = {'orrery-utils-demo.service': {'LoadState': 'loaded', 'ActiveState': 'active', 'NRestarts': '2',
                                                'ExecMainStartTimestamp': 'Fri 2026-09-18 13:08:47 EDT'}}
            self.assertEqual(console.summary(u, st), 'running since 09-18 13:08, 2 restarts')
            t = fake_util(d, every='5min')
            st = {'orrery-utils-demo.service': {'LoadState': 'loaded', 'ActiveState': 'inactive', 'Result': 'success'},
                  'orrery-utils-demo.timer': {'ActiveState': 'active', 'NextElapseUSecRealtime': 'Fri 2026-09-18 13:15:00 EDT'},
                  'orrery-utils-demo-job.service': {'ActiveState': 'active'}}
            self.assertEqual(console.summary(t, st), 'every 5min, next 09-18 13:15, last pass ok, job running')


if __name__ == '__main__':
    unittest.main()
