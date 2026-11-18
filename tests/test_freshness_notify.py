"""Tests for freshness/notify.py, the Mac ops notifier.

Requirement: FRS-23, with the caps and retries of FRS-25 (docs/SPEC.md).
No test runs `openclaw` or touches the network: `run` is a fake, and prbot
is a fake module written to a temp folder.
"""

import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest

from freshness import alerts as A
from freshness import finding as F
from freshness import notify as N
from freshness import state as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)
# Built at runtime so the public repo holds no channel-id-looking literal.
CHANNEL = 'C' + '0' + 'TESTCHAN' + '42'
CONFIG = {'slack_channel_id': CHANNEL, 'prbot_path': 'unused', 'openclaw_bin': 'openclaw'}


def _load_scrub():
    spec = importlib.util.spec_from_file_location('notify_scrub_under_test', os.path.join(REPO, 'tools', 'scrub.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scrub = _load_scrub()


class FakeRun:
    """Records argv and keyword arguments; fails the calls listed in `fail`."""

    def __init__(self, fail=(), raise_with=None):
        self.calls = []
        self.fail = set(fail)
        self.raise_with = raise_with

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raise_with is not None:
            raise self.raise_with(' '.join(argv))
        code = 1 if len(self.calls) in self.fail else 0
        return type('Done', (), {'returncode': code, 'stdout': '', 'stderr': ''})()

    def texts(self):
        return [argv[-1] for argv, _ in self.calls]


class FakeAttention:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, kind, url, title, problem):
        self.calls.append((kind, url, title, problem))
        if self.error:
            raise self.error


def url(number):
    return A.ISSUE_URL_BASE + str(number)


def issue(number, kind='dwell', subject='nightly'):
    return {'number': number, 'fingerprint': F.fingerprint(kind, subject),
            'title': 'freshness-alert: ' + kind + ': ' + subject, 'url': url(number)}


def alert(subject='nightly', kind='dwell'):
    return {'kind': kind, 'subject': subject, 'evidence': 'run 1: over the limit'}


def actions_half(alerts, open_issues, state, now=NOW, closed=()):
    """The Actions half: plans issues only (announce=False)."""
    _, new = A.plan_alerts(alerts, open_issues, closed, state, 'run-a', now, announce=False)
    return new


def mac(open_issues, state, run, attention_fn=None, now=NOW, ops_results=()):
    return N.announce(open_issues, state, CONFIG, run, attention_fn or FakeAttention(), now, ops_results)


class SendTest(unittest.TestCase):
    def test_send_runs_exact_argv_without_shell_and_with_timeout(self):
        run = FakeRun()
        self.assertTrue(N.send('hello', CONFIG, run))
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ['openclaw', 'message', 'send', '--channel', 'slack',
                                '--target', 'channel:' + CHANNEL, '-m', 'hello'])
        self.assertEqual(kwargs['timeout'], 30)
        self.assertNotIn('shell', kwargs)

    def test_send_failure_and_exception_return_false(self):
        self.assertFalse(N.send('x', CONFIG, FakeRun(fail={1})))
        self.assertFalse(N.send('x', CONFIG, FakeRun(raise_with=RuntimeError)))


class AnnounceTest(unittest.TestCase):
    def test_new_alert_issue_gives_one_attention_and_one_send(self):
        state = actions_half([alert()], [], S.empty_state())
        run, att = FakeRun(), FakeAttention()
        new = mac([issue(7)], state, run, att)
        self.assertEqual(len(att.calls), 1)
        kind, link, _, _ = att.calls[0]
        self.assertEqual((kind, link), ('dwell', url(7)))
        self.assertEqual(run.calls[0][0], ['openclaw', 'message', 'send', '--channel', 'slack',
                                           '--target', 'channel:' + CHANNEL, '-m',
                                           'skills-catalog freshness-alert: dwell nightly: ' + url(7)])
        self.assertEqual(len(run.calls), 1)
        fp = F.fingerprint('dwell', 'nightly')
        self.assertTrue(new['alerts'][fp]['announced'])
        self.assertEqual(new['ops']['sent'], 1)

    def test_issue_opened_by_actions_half_while_mac_was_down_is_announced(self):
        # Two Actions runs opened alerts; the Mac ran neither time.
        state = actions_half([alert('a')], [], S.empty_state(), NOW - 2 * DAY)
        state = actions_half([alert('a'), alert('b')], [issue(1, subject='a')], state, NOW - DAY)
        run = FakeRun()
        new = mac([issue(1, subject='a'), issue(2, subject='b')], state, run)
        self.assertEqual(run.texts(), ['skills-catalog freshness-alert: dwell a: ' + url(1),
                                       'skills-catalog freshness-alert: dwell b: ' + url(2)])
        self.assertTrue(all(e['announced'] for e in new['alerts'].values()))

    def test_issue_unknown_to_state_is_announced_from_its_title(self):
        run = FakeRun()
        new = mac([issue(9, kind='heartbeat', subject='mac')], S.empty_state(), run)
        self.assertEqual(run.texts(), ['skills-catalog freshness-alert: heartbeat mac: ' + url(9)])
        entry = new['alerts'][F.fingerprint('heartbeat', 'mac')]
        self.assertTrue(entry['announced'])
        self.assertEqual(entry['issue'], 9)

    def test_announced_issue_gives_nothing_on_next_run(self):
        state = actions_half([alert()], [], S.empty_state())
        state = mac([issue(7)], state, FakeRun())
        run, att = FakeRun(), FakeAttention()
        again = mac([issue(7)], state, run, att, NOW + HOUR)
        self.assertEqual(run.calls, [])
        self.assertEqual(att.calls, [])
        self.assertEqual(again['alerts'], state['alerts'])

    def test_reopened_issue_is_announced_again(self):
        state = actions_half([alert()], [], S.empty_state())
        state = mac([issue(7)], state, FakeRun())
        # Clean twice on the Actions half: closed.
        state = actions_half([], [issue(7)], state, NOW + HOUR)
        _, state = A.plan_alerts([], [issue(7)], [], state, 'run-b', NOW + 2 * HOUR, announce=False)
        closed = [{'number': 7, 'fingerprint': issue(7)['fingerprint'], 'closed_at': S.iso(NOW + 2 * HOUR)}]
        # Raised again within 7 days: the Actions half reopens it.
        state = actions_half([alert()], [], state, NOW + DAY, closed)
        fp = issue(7)['fingerprint']
        self.assertFalse(state['alerts'][fp]['announced'])
        run, att = FakeRun(), FakeAttention()
        new = mac([issue(7)], state, run, att, NOW + DAY + HOUR)
        self.assertEqual(run.texts(), ['skills-catalog freshness-alert: dwell nightly: ' + url(7)])
        self.assertEqual(len(att.calls), 1)
        self.assertTrue(new['alerts'][fp]['announced'])

    def test_failed_send_is_recorded_and_sent_next_run(self):
        state = actions_half([alert()], [], S.empty_state())
        fp = issue(7)['fingerprint']
        failed = mac([issue(7)], state, FakeRun(fail={1}))
        self.assertFalse(failed['alerts'][fp]['announced'])
        self.assertEqual(failed['ops']['sent'], 0)  # the slot is given back (record_send)
        run = FakeRun()
        retried = mac([issue(7)], failed, run, now=NOW + HOUR)
        self.assertEqual(run.texts(), ['skills-catalog freshness-alert: dwell nightly: ' + url(7)])
        self.assertTrue(retried['alerts'][fp]['announced'])

    def test_seventh_message_of_a_day_goes_into_the_digest(self):
        subjects = ['s' + str(i) for i in range(7)]
        issues = [issue(10 + i, subject=s) for i, s in enumerate(subjects)]
        run, att = FakeRun(), FakeAttention()
        new = mac(issues, S.empty_state(), run, att)
        texts = run.texts()
        self.assertEqual(len(texts), 7)
        self.assertTrue(all(t.startswith('skills-catalog freshness-alert: ') for t in texts[:6]))
        self.assertTrue(texts[6].startswith(A.DIGEST_PREFIX))
        self.assertIn('dwell s6: ' + url(16), texts[6])
        self.assertTrue(new['ops']['digest_sent'])
        self.assertTrue(all(e['announced'] for e in new['alerts'].values()))
        self.assertEqual(len(att.calls), 7)
        # An 8th alert the same day waits for tomorrow, without attention yet.
        run2, att2 = FakeRun(), FakeAttention()
        later = mac(issues + [issue(30, subject='late')], new, run2, att2, NOW + HOUR)
        self.assertEqual(run2.calls, [])
        self.assertEqual(att2.calls, [])
        self.assertFalse(later['alerts'][issue(30, subject='late')['fingerprint']]['announced'])
        run3 = FakeRun()
        mac(issues + [issue(30, subject='late')], later, run3, now=NOW + DAY)
        self.assertEqual(run3.texts(), ['skills-catalog freshness-alert: dwell late: ' + url(30)])

    def test_failed_digest_leaves_its_alerts_due(self):
        issues = [issue(10 + i, subject='s' + str(i)) for i in range(7)]
        new = mac(issues, S.empty_state(), FakeRun(fail={7}))
        self.assertFalse(new['alerts'][issues[6]['fingerprint']]['announced'])
        self.assertFalse(new['ops']['digest_sent'])
        run = FakeRun()
        mac(issues, new, run, now=NOW + HOUR)
        self.assertEqual(len(run.calls), 1)
        self.assertTrue(run.texts()[0].startswith(A.DIGEST_PREFIX))

    def test_attention_error_does_not_stop_the_message(self):
        run = FakeRun()
        new = mac([issue(7)], S.empty_state(), run, FakeAttention(error=RuntimeError('boom')))
        self.assertEqual(len(run.calls), 1)
        self.assertTrue(new['alerts'][issue(7)['fingerprint']]['announced'])

    def test_planned_ops_results_are_delivered_and_recorded(self):
        # The Mac planner opened #7 this run; part 5 filled in the URL.
        actions, state = A.plan_alerts([alert()], [], [], S.empty_state(), 'run-m', NOW)
        sends = [a for a in actions if a['op'] == 'send_ops']
        self.assertEqual(len(sends), 1)
        results = [{'op': 'send_ops', 'fingerprint': sends[0]['fingerprint'], 'ok': True,
                    'text': sends[0]['text'].replace(A.ISSUE_URL_PLACEHOLDER, url(7))}]
        run, att = FakeRun(), FakeAttention()
        new = mac([issue(7)], state, run, att, ops_results=results)
        self.assertEqual(run.texts(), ['skills-catalog freshness-alert: dwell nightly: ' + url(7)])
        self.assertEqual([c[:2] for c in att.calls], [('dwell', url(7))])
        self.assertTrue(new['alerts'][sends[0]['fingerprint']]['announced'])

    def test_planned_result_that_failed_in_apply_is_not_sent_and_is_due_again(self):
        actions, state = A.plan_alerts([alert()], [], [], S.empty_state(), 'run-m', NOW)
        send_op = [a for a in actions if a['op'] == 'send_ops'][0]
        results = [{'op': 'send_ops', 'fingerprint': send_op['fingerprint'], 'ok': False,
                    'text': send_op['text']}]
        run = FakeRun()
        new = mac([], state, run, ops_results=results)
        self.assertEqual(run.calls, [])
        self.assertFalse(new['alerts'][send_op['fingerprint']]['announced'])
        run2 = FakeRun()
        mac([issue(7)], new, run2, now=NOW + HOUR)
        self.assertEqual(run2.texts(), ['skills-catalog freshness-alert: dwell nightly: ' + url(7)])

    def test_messages_are_plain_short_and_scrub_clean(self):
        long_subject = 'x' * 400
        run = FakeRun()
        mac([issue(7, subject=long_subject)] + [issue(20 + i, subject='s' * 60 + str(i)) for i in range(8)],
            S.empty_state(), run)
        self.assertTrue(run.calls)
        for text in run.texts():
            self.assertLess(len(text), 300)
            self.assertEqual(scrub.scan_text(text), [])
        self.assertTrue(run.texts()[0].endswith(': ' + url(7)))

    def test_channel_id_never_in_state_or_exception_text(self):
        run = FakeRun(raise_with=RuntimeError)  # the error text holds the argv
        new = mac([issue(7)], S.empty_state(), run)
        self.assertEqual(len(run.calls), 1)
        self.assertNotIn(CHANNEL, S.dump(new))
        self.assertFalse(new['alerts'][issue(7)['fingerprint']]['announced'])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'freshness.json')
            with open(path, 'w') as handle:
                json.dump({'slack_channel_id': CHANNEL}, handle)
            with self.assertRaises(N.ConfigError) as caught:
                N.load_config(path)
            self.assertNotIn(CHANNEL, str(caught.exception))


class ConfigTest(unittest.TestCase):
    def _write(self, tmp, text):
        path = os.path.join(tmp, 'freshness.json')
        with open(path, 'w') as handle:
            handle.write(text)
        return path

    def test_missing_key_names_the_key_and_not_the_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({'slack_channel_id': CHANNEL, 'other': 'keep-me-private'}))
            with self.assertRaises(N.ConfigError) as caught:
                N.load_config(path)
            message = str(caught.exception)
            self.assertIn('prbot_path', message)
            self.assertNotIn('keep-me-private', message)
            self.assertNotIn(CHANNEL, message)
            self.assertNotIn(tmp, message)

    def test_bad_json_error_holds_no_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, '{"slack_channel_id": "' + CHANNEL + '",')
            with self.assertRaises(N.ConfigError) as caught:
                N.load_config(path)
            self.assertNotIn(CHANNEL, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_valid_config_defaults_openclaw_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({'slack_channel_id': CHANNEL, 'prbot_path': tmp}))
            config = N.load_config(path)
            self.assertEqual(config['openclaw_bin'], 'openclaw')
            self.assertEqual(config['slack_channel_id'], CHANNEL)


FAKE_PRBOT = '''
import json, os
def openclaw_attention(reason, detail):
    with open(os.path.join(os.path.dirname(__file__), 'calls.json'), 'a') as handle:
        handle.write(json.dumps([reason, detail]) + "\\n")
    return True
'''


class AttentionTest(unittest.TestCase):
    def _calls(self, tmp):
        with open(os.path.join(tmp, 'calls.json')) as handle:
            return [json.loads(line) for line in handle]

    def test_fake_prbot_file_gets_reason_and_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'prbot')  # iwb loads the extensionless prbot file
            with open(path, 'w') as handle:
                handle.write(FAKE_PRBOT)
            before = list(sys.path)
            result = N.attention('dwell', url(7), 'freshness-alert: dwell: nightly', 'over the limit', path)
            self.assertTrue(result)
            self.assertEqual(sys.path, before)
            self.assertEqual(self._calls(tmp), [['freshness-alert: dwell', {
                'url': url(7), 'title': 'freshness-alert: dwell: nightly', 'problem': 'over the limit'}]])

    def test_fake_prbot_folder_used_by_announce_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'prbot.py'), 'w') as handle:
                handle.write(FAKE_PRBOT)
            config = dict(CONFIG, prbot_path=tmp)
            run = FakeRun()
            N.announce([issue(7)], S.empty_state(), config, run, None, NOW)
            self.assertEqual(len(run.calls), 1)
            calls = self._calls(tmp)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], 'freshness-alert: dwell')
            self.assertEqual(calls[0][1]['url'], url(7))

    def test_prbot_import_error_is_returned_and_message_still_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'prbot.py'), 'w') as handle:
                handle.write('raise ImportError("broken prbot")\n')
            result = N.attention('dwell', url(7), 't', 'p', tmp)
            self.assertIsInstance(result, str)
            self.assertIn('ImportError', result)
            missing = N.attention('dwell', url(7), 't', 'p', os.path.join(tmp, 'nope'))
            self.assertIsInstance(missing, str)
            run = FakeRun()
            new = N.announce([issue(7)], S.empty_state(), dict(CONFIG, prbot_path=tmp), run, None, NOW)
            self.assertEqual(len(run.calls), 1)
            self.assertTrue(new['alerts'][issue(7)['fingerprint']]['announced'])


if __name__ == '__main__':
    unittest.main()
