"""Tests for freshness/notify.py, the Mac ops notifier.

Requirement: FRS-23, with the caps and retries of FRS-25 and the one message
per dwell state of FRS-17 (docs/SPEC.md).
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

    def test_planner_digest_alerts_get_attention_too(self):
        # plan_alerts -> apply -> announce: 3 alerts the Actions half opened
        # plus 5 new ones give 6 messages and one digest of 2.
        old = [alert('o' + str(i)) for i in range(3)]
        state = actions_half(old, [], S.empty_state(), NOW - DAY)
        open_issues = [issue(90 + i, subject='o' + str(i)) for i in range(3)]
        alerts = old + [alert('p' + str(i)) for i in range(5)]
        actions, state = A.plan_alerts(alerts, open_issues, [], state, 'run-m', NOW)
        results, opened = [], {i['fingerprint']: i['url'] for i in open_issues}
        for i, action in enumerate(a for a in actions if a['op'] == 'open_issue'):
            opened[action['fingerprint']] = url(100 + i)
            results.append({'op': 'open_issue', 'fingerprint': action['fingerprint'], 'ok': True,
                            'number': 100 + i, 'url': url(100 + i)})
        for action in actions:
            if action['op'] == 'send_ops':
                text = action['text'].replace(A.ISSUE_URL_PLACEHOLDER, opened.get(action['fingerprint'], ''))
                results.append({'op': 'send_ops', 'fingerprint': action['fingerprint'], 'ok': True, 'text': text})
        run, att = FakeRun(), FakeAttention()
        mac(open_issues, state, run, att, ops_results=results)
        texts = run.texts()
        self.assertEqual(len(opened), 8)
        self.assertEqual(len(texts), 7)
        self.assertTrue(texts[6].startswith(A.DIGEST_PREFIX))
        self.assertEqual(sorted(c[1] for c in att.calls), sorted(opened.values()))

    def _planner_run(self, fail_open=()):
        old = [alert('o' + str(i)) for i in range(3)]
        state = actions_half(old, [], S.empty_state(), NOW - DAY)
        open_issues = [issue(90 + i, subject='o' + str(i)) for i in range(3)]
        alerts = old + [alert('p' + str(i)) for i in range(5)]
        actions, state = A.plan_alerts(alerts, open_issues, [], state, 'run-m', NOW)
        results, opened = [], {}
        for i, action in enumerate(a for a in actions if a['op'] == 'open_issue'):
            ok = action['title'].split(': ')[-1] not in fail_open
            results.append({'op': 'open_issue', 'fingerprint': action['fingerprint'], 'ok': ok,
                            'number': 100 + i if ok else None, 'url': url(100 + i) if ok else None})
            if ok:
                opened[action['fingerprint']] = url(100 + i)
        for action in actions:
            if action['op'] == 'send_ops':
                text = action['text']
                result = {'op': 'send_ops', 'fingerprint': action['fingerprint'], 'ok': True, 'text': text}
                if A.ISSUE_URL_PLACEHOLDER in text:
                    if action['fingerprint'] in opened:
                        result['text'] = text.replace(A.ISSUE_URL_PLACEHOLDER, opened[action['fingerprint']])
                    else:
                        result['ok'] = False
                results.append(result)
        return open_issues, state, results, opened

    def test_planner_digest_line_for_an_issue_not_opened_is_dropped_and_due(self):
        open_issues, state, results, opened = self._planner_run(fail_open={'p4'})
        run, att = FakeRun(), FakeAttention()
        new = mac(open_issues, state, run, att, ops_results=results)
        digest = [t for t in run.texts() if t.startswith(A.DIGEST_PREFIX)]
        for text in run.texts():
            self.assertNotIn('new issue', text)
        self.assertTrue(all(c[1].rstrip('/').rsplit('/', 1)[-1].isdigit() for c in att.calls))
        self.assertFalse(new['alerts'][F.fingerprint('dwell', 'p4')]['announced'])
        self.assertEqual(len(digest), 1)
        self.assertNotIn('p4', digest[0])

    def test_failed_reopen_sends_nothing_and_stays_due(self):
        state = actions_half([alert()], [], S.empty_state(), NOW - 3 * DAY)
        state = mac([issue(7)], state, FakeRun(), now=NOW - 3 * DAY)
        state = actions_half([], [issue(7)], state, NOW - 2 * DAY)
        _, state = A.plan_alerts([], [issue(7)], [], state, 'run-b', NOW - 2 * DAY + HOUR, announce=False)
        closed = [{'number': 7, 'fingerprint': issue(7)['fingerprint'], 'closed_at': S.iso(NOW - 2 * DAY + HOUR)}]
        actions, state = A.plan_alerts([alert()], [], closed, state, 'run-m', NOW)
        fp = issue(7)['fingerprint']
        results = []
        for action in actions:
            ok = action['op'] != 'reopen'
            result = {'op': action['op'], 'fingerprint': fp, 'ok': ok}
            if action['op'] == 'send_ops':
                result['text'] = action['text']
            results.append(result)
        self.assertIn('reopen', [a['op'] for a in actions])
        run, att = FakeRun(), FakeAttention()
        new = mac([], state, run, att, ops_results=results)
        self.assertEqual(run.calls, [])
        self.assertEqual(att.calls, [])
        self.assertFalse(new['alerts'][fp]['announced'])

    def test_digest_lines_that_do_not_fit_stay_due(self):
        issues = [issue(10 + i, subject='subject-' + 'y' * 40 + str(i)) for i in range(12)]
        run, att = FakeRun(), FakeAttention()
        new = mac(issues, S.empty_state(), run, att)
        digest = run.texts()[-1]
        self.assertTrue(digest.startswith(A.DIGEST_PREFIX))
        self.assertLess(len(digest), 300)
        in_digest = [i for i in issues[6:] if i['url'] + '\n' in digest + '\n']
        left = [i for i in issues[6:] if i not in in_digest]
        self.assertTrue(in_digest and left)
        for i in in_digest:
            self.assertTrue(new['alerts'][i['fingerprint']]['announced'])
        for i in left:
            self.assertFalse(new['alerts'][i['fingerprint']]['announced'])
        self.assertEqual(len(att.calls), 6 + len(in_digest))
        run2 = FakeRun()
        mac(issues, new, run2, now=NOW + DAY)
        self.assertEqual(len(run2.calls), len(left))

    def test_planner_digest_lines_with_the_same_scrubbed_text_keep_their_fingerprints(self):
        # Two subjects that scrub to the same text (built at runtime: home paths).
        home = '/' + 'Users' + '/someone/'
        old = [alert('o' + str(i)) for i in range(5)]
        state = actions_half(old, [], S.empty_state(), NOW - 2 * DAY)
        twins = [alert(home + 'a'), alert(home + 'b')]
        state = actions_half(twins, [], state, NOW - DAY)
        open_issues = [issue(90 + i, subject='o' + str(i)) for i in range(5)]
        open_issues += [dict(issue(95 + i), fingerprint=F.fingerprint('dwell', t['subject']))
                        for i, t in enumerate(twins)]
        actions, state = A.plan_alerts(old + twins, open_issues, [], state, 'run-m', NOW)
        results = [{'op': 'send_ops', 'fingerprint': a['fingerprint'], 'ok': True, 'text': a['text']}
                   for a in actions if a['op'] == 'send_ops']
        self.assertTrue(results[-1]['text'].startswith(A.DIGEST_PREFIX))
        run, att = FakeRun(), FakeAttention()
        mac(open_issues, state, run, att, ops_results=results)
        urls = [c[1] for c in att.calls]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(sorted(urls), sorted(i['url'] for i in open_issues))
        for text in run.texts():
            self.assertEqual(scrub.scan_text(text), [])

    def test_new_twin_alerts_across_the_cap_keep_their_own_issue(self):
        # Five alerts already open plus two new ones whose subjects scrub to
        # the same text: one twin is sent directly, the other is in the digest.
        home = '/' + 'Users' + '/someone/'
        old = [alert('o' + str(i)) for i in range(5)]
        state = actions_half(old, [], S.empty_state(), NOW - DAY)
        open_issues = [issue(90 + i, subject='o' + str(i)) for i in range(5)]
        twins = [alert(home + 'c'), alert(home + 'i')]  # fingerprints that straddle the cap
        actions, state = A.plan_alerts(old + twins, open_issues, [], state, 'run-m', NOW)
        self.assertIn('withheld', [a['text'] for a in actions if a['op'] == 'send_ops'][-1])
        results, opened = [], {}
        for i, action in enumerate(a for a in actions if a['op'] == 'open_issue'):
            opened[action['fingerprint']] = url(100 + i)
            results.append({'op': 'open_issue', 'fingerprint': action['fingerprint'], 'ok': True,
                            'number': 100 + i, 'url': url(100 + i)})
        for action in actions:
            if action['op'] == 'send_ops':
                text = action['text'].replace(A.ISSUE_URL_PLACEHOLDER, opened.get(action['fingerprint'], ''))
                results.append({'op': 'send_ops', 'fingerprint': action['fingerprint'], 'ok': True, 'text': text})
        self.assertTrue(results[-1]['text'].startswith(A.DIGEST_PREFIX))
        run, att = FakeRun(), FakeAttention()
        mac(open_issues, state, run, att, ops_results=results)
        urls = [c[1] for c in att.calls]
        self.assertEqual(sorted(urls), sorted([i['url'] for i in open_issues] + list(opened.values())))
        digest = run.texts()[-1]
        direct_twin = [t for t in run.texts()[:-1] if 'withheld' in t]
        self.assertEqual(len(direct_twin), 1)
        self.assertNotIn(direct_twin[0].rsplit(': ', 1)[1], digest)
        # A failed digest makes only the folded twin due again.
        failed = mac(open_issues, state, FakeRun(fail={7}), ops_results=results)
        twin_fps = [F.fingerprint('dwell', t['subject']) for t in twins]
        due = [fp for fp in twin_fps if not failed['alerts'][fp]['announced']]
        self.assertEqual(len(due), 1)
        self.assertNotIn(opened[due[0]], direct_twin[0])
        self.assertFalse(failed['ops']['digest_sent'])
        self.assertEqual(failed['ops']['pending'], [])

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


class DwellGroupTest(unittest.TestCase):
    """FRS-17 through the Mac notifier."""

    def _planned(self, state, subjects, open_issues):
        actions, state = A.plan_alerts([alert(x) for x in subjects], open_issues, [], state, 'run-m', NOW)
        results = [{'op': 'send_ops', 'fingerprint': a['fingerprint'], 'ok': True, 'text': a['text']}
                   for a in actions if a['op'] == 'send_ops']
        return state, results

    def test_members_are_not_announced_one_by_one(self):
        subjects = ['re-review:a', 're-review:b', 're-review:c']
        open_issues = [issue(80 + i, subject=x) for i, x in enumerate(subjects)]
        state, results = self._planned(actions_half([alert(x) for x in subjects], [], S.empty_state()),
                                       subjects, open_issues)
        self.assertEqual(len(results), 1)
        run = FakeRun()
        new = mac(open_issues, state, run, ops_results=results)
        self.assertEqual(len(run.calls), 1)
        self.assertIn('(and 2 more re-review items stuck)', run.texts()[0])
        self.assertEqual(new['ops']['sent'], 1)
        self.assertIn('re-review', new['ops']['dwell_groups'])

    def test_failed_direct_send_frees_the_state_for_the_next_run(self):
        subjects = ['re-review:a', 're-review:b']
        open_issues = [issue(80 + i, subject=x) for i, x in enumerate(subjects)]
        state, results = self._planned(actions_half([alert(x) for x in subjects], [], S.empty_state()),
                                       subjects, open_issues)
        new = mac(open_issues, state, FakeRun(fail={1}), ops_results=results)
        self.assertEqual(new['ops']['dwell_groups'], {})
        self.assertEqual(new['ops']['failed'], [])
        fps = [F.fingerprint('dwell', x) for x in subjects]
        self.assertFalse(any(new['alerts'][fp]['announced'] for fp in fps))
        _, again = self._planned(new, subjects, open_issues)
        self.assertEqual(len(again), 1)

    def test_failed_digest_with_a_group_frees_every_member(self):
        subjects = ['re-review:a', 're-review:b']
        open_issues = [issue(80 + i, subject=x) for i, x in enumerate(subjects)]
        state = actions_half([alert(x) for x in subjects], [], S.empty_state())
        state = S.roll_day(state, NOW)
        state['ops']['sent'] = A.OPS_PER_DAY
        state, results = self._planned(state, subjects, open_issues)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['text'].startswith(A.DIGEST_PREFIX))
        run, att = FakeRun(), FakeAttention()
        ok = mac(open_issues, state, run, att, ops_results=results)
        self.assertEqual(len(run.calls), 1)
        self.assertIn('(and 1 more re-review item stuck)', run.texts()[0])
        self.assertEqual(len(att.calls), 1)
        self.assertIn('re-review', ok['ops']['dwell_groups'])
        failed = mac(open_issues, state, FakeRun(fail={1}), ops_results=results)
        self.assertEqual(failed['ops']['dwell_groups'], {})
        self.assertEqual(failed['ops']['failed'], [])
        fps = [F.fingerprint('dwell', x) for x in subjects]
        self.assertFalse(any(failed['alerts'][fp]['announced'] for fp in fps))


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
