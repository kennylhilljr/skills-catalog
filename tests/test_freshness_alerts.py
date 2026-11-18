"""Tests for freshness/finding.py, freshness/state.py and freshness/alerts.py.

Requirements: FRS-13, FRS-22, FRS-24, FRS-25, FRS-27 (docs/SPEC.md).
Pure logic: no network, GitHub, gh or openclaw. `now` is passed in.
"""

import copy
import datetime
import importlib.util
import os
import unittest

from freshness import alerts as A
from freshness import finding as F
from freshness import state as S

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)


def _load_scrub():
    spec = importlib.util.spec_from_file_location('freshness_scrub_under_test', os.path.join(REPO, 'tools', 'scrub.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scrub = _load_scrub()


def alert(subject='nightly', kind='dwell', evidence='run 1: over the limit'):
    return {'kind': kind, 'subject': subject, 'evidence': evidence,
            'what_happened': 'state ' + subject + ' exceeded its dwell limit',
            'files': ['freshness/state.py']}


def fp_of(a):
    return F.fingerprint(a['kind'], a['subject'])


def ops(actions):
    return [a for a in actions if a['op'] == 'send_ops']


def of(actions, op):
    return [a for a in actions if a['op'] == op]


def opened(state, a, number):
    """The open issue list GitHub would return after `a` was opened as `number`."""
    return [{'number': number, 'fingerprint': fp_of(a), 'closed_at': None}]


def finding(evidence='claude-2.1 is retired', subject='skills/x/SKILL.md:claude-2.1'):
    return {'kind': 'deprecated-model', 'skill': 'x', 'subject': subject,
            'file': 'skills/x/SKILL.md', 'line': 12, 'evidence': evidence,
            'fix': {'file': 'skills/x/SKILL.md', 'line': 12, 'old': 'claude-2.1', 'new': 'claude-sonnet-5-5'}}


class FindingTests(unittest.TestCase):
    def test_fingerprint_stable_and_differs_by_kind(self):
        a = F.fingerprint('dead-link', 'https://example.com/a')
        self.assertEqual(a, F.fingerprint('dead-link', 'https://example.com/a'))
        self.assertNotEqual(a, F.fingerprint('missing-path', 'https://example.com/a'))
        self.assertRegex(a, r'^[0-9a-f]{64}$')
        import hashlib
        self.assertEqual(a, hashlib.sha256(b'dead-link:https://example.com/a').hexdigest())

    def test_validate_accepts_good_and_rejects_missing_key(self):
        self.assertEqual(F.validate(finding()), [])
        bad = finding()
        del bad['subject']
        self.assertIn('missing key: subject', F.validate(bad))
        bad = finding()
        bad['kind'] = 'nope'
        self.assertTrue(F.validate(bad))
        bad = finding()
        bad['fix'] = {'file': 'x'}
        self.assertTrue(F.validate(bad))

    def test_marker_round_trip_and_branch(self):
        fp = F.fingerprint('dwell', 'x')
        self.assertEqual(F.parse_marker('text\n' + F.marker(fp) + '\n'), fp)
        self.assertIsNone(F.parse_marker('no marker'))
        self.assertEqual(F.branch_name(fp), 'freshness/' + fp[:12])


class RenderTests(unittest.TestCase):
    def test_render_alert_title_sections_marker(self):
        title, body = A.render_alert('dwell', 'x' * 300, 'it happened', 'run 7', None, ['freshness/alerts.py'], None)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.startswith('freshness-alert: dwell: '))
        positions = [body.index('## ' + s) for s in A.SECTIONS]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(F.marker(F.fingerprint('dwell', 'x' * 300)), body)
        self.assertGreater(body.index('<!-- freshness-fingerprint:'), positions[-1])
        self.assertEqual(scrub.scan_text(body), [])
        self.assertEqual(scrub.scan_text(title), [])

    def test_planned_issue_labels_never_skip(self):
        actions, _ = A.plan_alerts([alert()], [], [], S.empty_state(), 'r1', NOW)
        issue = of(actions, 'open_issue')[0]
        self.assertEqual(issue['labels'], ['freshness-alert', 'dwell'])
        self.assertNotIn('bot:skip', issue['labels'])
        self.assertNotIn('bot:stop', issue['labels'])
        self.assertNotIn('assignees', issue)
        self.assertEqual(F.parse_marker(issue['body']), fp_of(alert()))
        for action in actions:
            self.assertEqual(F.validate_action(action), [], action)


class DedupTests(unittest.TestCase):
    """FRS-24."""

    def setUp(self):
        self.a = alert()
        _, self.state = A.plan_alerts([self.a], [], [], S.empty_state(), 'r1', NOW)
        self.open = opened(self.state, self.a, 7)

    def test_no_second_issue_for_open_fingerprint(self):
        actions, _ = A.plan_alerts([self.a], self.open, [], self.state, 'r2', NOW + 2 * DAY)
        self.assertEqual(of(actions, 'open_issue'), [])
        self.assertEqual(of(actions, 'comment'), [])

    def test_comment_only_on_changed_evidence(self):
        changed = alert(evidence='run 2: worse')
        actions, state = A.plan_alerts([changed], self.open, [], self.state, 'r2', NOW + 25 * HOUR)
        comments = of(actions, 'comment')
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]['number'], 7)
        actions, _ = A.plan_alerts([changed], self.open, [], state, 'r3', NOW + 60 * HOUR)
        self.assertEqual(of(actions, 'comment'), [])

    def test_at_most_one_comment_per_24_hours(self):
        actions, state = A.plan_alerts([alert(evidence='v2')], self.open, [], self.state, 'r2', NOW + 2 * HOUR)
        self.assertEqual(of(actions, 'comment'), [])
        actions, state = A.plan_alerts([alert(evidence='v2')], self.open, [], state, 'r3', NOW + 25 * HOUR)
        self.assertEqual(len(of(actions, 'comment')), 1)
        actions, _ = A.plan_alerts([alert(evidence='v3')], self.open, [], state, 'r4', NOW + 30 * HOUR)
        self.assertEqual(of(actions, 'comment'), [])

    def test_reopen_within_7_days(self):
        closed = [{'number': 7, 'fingerprint': fp_of(self.a), 'closed_at': S.iso(NOW + DAY)}]
        actions, state = A.plan_alerts([self.a], [], closed, self.state, 'r9', NOW + 3 * DAY)
        self.assertEqual([x['op'] for x in actions[:2]], ['reopen', 'comment'])
        self.assertEqual(actions[0]['number'], 7)
        self.assertEqual(of(actions, 'open_issue'), [])
        self.assertIn('reopened', ops(actions)[0]['text'])
        self.assertIn('/issues/7', ops(actions)[0]['text'])
        self.assertIsNone(state['alerts'][fp_of(self.a)]['closed_at'])

    def test_new_issue_after_7_days(self):
        closed = [{'number': 7, 'fingerprint': fp_of(self.a), 'closed_at': S.iso(NOW)}]
        actions, _ = A.plan_alerts([self.a], [], closed, self.state, 'r9', NOW + 8 * DAY)
        self.assertEqual(of(actions, 'reopen'), [])
        self.assertEqual(len(of(actions, 'open_issue')), 1)

    def test_one_reminder_per_24_hours(self):
        first = ops(A.plan_alerts([self.a], [], [], S.empty_state(), 'r1', NOW)[0])
        self.assertEqual(len(first), 1)
        self.assertIn(A.ISSUE_URL_PLACEHOLDER, first[0]['text'])
        actions, state = A.plan_alerts([self.a], self.open, [], self.state, 'r2', NOW + 2 * HOUR)
        self.assertEqual(ops(actions), [])
        actions, state = A.plan_alerts([self.a], self.open, [], state, 'r3', NOW + 24 * HOUR)
        self.assertEqual(len(ops(actions)), 1)
        self.assertIn('still open', ops(actions)[0]['text'])
        actions, _ = A.plan_alerts([self.a], self.open, [], state, 'r4', NOW + 30 * HOUR)
        self.assertEqual(ops(actions), [])

    def test_actions_half_opens_without_ops_and_mac_announces(self):
        actions, state = A.plan_alerts([alert('b')], [], [], S.empty_state(), 'a1', NOW, announce=False)
        self.assertEqual(ops(actions), [])
        self.assertEqual(state['ops']['sent'], 0)
        actions, state = A.plan_alerts([], opened(state, alert('b'), 9), [], state, 'm1', NOW + HOUR,
                                       scope={'heartbeat-actions'})
        self.assertEqual(len(ops(actions)), 1)
        self.assertIn('/issues/9', ops(actions)[0]['text'])
        self.assertEqual(of(actions, 'close'), [])


class CapTests(unittest.TestCase):
    """FRS-25."""

    def test_sixth_issue_of_day_goes_to_one_storm_issue(self):
        raised = [alert('s' + str(i)) for i in range(8)]
        actions, state = A.plan_alerts(raised, [], [], S.empty_state(), 'r1', NOW)
        issues = of(actions, 'open_issue')
        self.assertEqual(len(issues), 6)
        storm = [i for i in issues if i['title'].startswith('freshness-alert: storm: ')]
        self.assertEqual(len(storm), 1)
        self.assertEqual(storm[0]['title'], 'freshness-alert: storm: 2026-11-18')
        for i in (5, 6, 7):
            self.assertIn('dwell: s' + str(i), storm[0]['body'])
        self.assertEqual(state['issues']['opened'], 5)
        storm_fp = F.fingerprint('storm', '2026-11-18')
        open_now = [{'number': 20, 'fingerprint': storm_fp, 'closed_at': None}]
        more = [alert('t' + str(i)) for i in range(2)]
        actions, state = A.plan_alerts(more, open_now, [], state, 'r2', NOW + HOUR)
        self.assertEqual(of(actions, 'open_issue'), [])
        self.assertEqual([c['number'] for c in of(actions, 'comment')], [20])
        self.assertEqual(state['issues']['storm_issue'], 20)

    def test_seventh_ops_message_goes_into_one_digest(self):
        state = S.empty_state()
        actions, state = A.plan_alerts([alert('a' + str(i)) for i in range(5)], [], [], state, 'r1', NOW)
        self.assertEqual(len(ops(actions)), 5)
        open_now = [{'number': 30 + i, 'fingerprint': fp_of(alert('a' + str(i))), 'closed_at': None}
                    for i in range(5)]
        # Next day: the five reminders, then three reopened alerts (8 messages).
        later = NOW + DAY
        closed = [{'number': 40 + i, 'fingerprint': fp_of(alert('c' + str(i))), 'closed_at': S.iso(NOW)}
                  for i in range(3)]
        raised = [alert('a' + str(i)) for i in range(5)] + [alert('c' + str(i)) for i in range(3)]
        actions, state = A.plan_alerts(raised, open_now, closed, state, 'r2', later)
        sent = ops(actions)
        self.assertEqual(len(sent), 7)
        self.assertIn('2 more', sent[-1]['text'])
        self.assertTrue(state['ops']['digest_sent'])
        self.assertEqual(len(state['ops']['pending']), 2)
        # Nothing more that day.
        actions, _ = A.plan_alerts([alert('new')], open_now, [], state, 'r3', later + HOUR)
        self.assertEqual(ops(actions), [])

    def test_failed_send_retried_next_run_and_counts(self):
        state = S.empty_state()
        actions, state = A.plan_alerts([alert('x')], [], [], state, 'r1', NOW)
        text = ops(actions)[0]['text']
        S_after = A.record_send(state, text, False)
        self.assertEqual(S_after['ops']['failed'], [text])
        A.record_send(state, 'ok text', True)
        self.assertEqual(state['ops']['failed'], [text])
        raised = [alert('y' + str(i)) for i in range(4)]
        actions, state = A.plan_alerts(raised, [], [], state, 'r2', NOW + HOUR)
        sent = ops(actions)
        self.assertEqual(sent[0]['text'], text)
        self.assertEqual(len(sent), 5)
        self.assertEqual(state['ops']['sent'], 6)
        self.assertEqual(state['ops']['failed'], [])

    def test_counters_reset_at_day_rollover_and_failed_kept(self):
        state = S.empty_state()
        A.record_send(state, 'f1', False)
        A.record_send(state, 'f2', False)
        actions, state = A.plan_alerts([alert('d' + str(i)) for i in range(7)], [], [], state, 'r1', NOW)
        self.assertEqual([a['text'] for a in ops(actions)[:2]], ['f1', 'f2'])
        self.assertEqual(state['issues']['opened'], 5)
        self.assertTrue(state['ops']['digest_sent'])
        A.record_send(state, 'lost message', False)
        tomorrow = datetime.datetime(2026, 11, 19, 0, 30, tzinfo=UTC)
        actions, state = A.plan_alerts([], [], [], state, 'r2', tomorrow)
        self.assertEqual(state['ops']['day'], '2026-11-19')
        self.assertEqual(state['issues']['opened'], 0)
        self.assertIsNone(state['issues']['storm_issue'])
        self.assertFalse(state['ops']['digest_sent'])
        self.assertEqual(state['ops']['pending'], [])
        self.assertEqual([a['text'] for a in ops(actions)], ['lost message'])
        rolled = S.roll_day({'ops': {'day': '2026-11-18', 'sent': 6, 'digest_sent': True,
                                     'pending': ['p'], 'failed': ['f']}}, tomorrow)
        self.assertEqual(rolled['ops'], {'day': '2026-11-19', 'sent': 0, 'digest_sent': False,
                                         'pending': [], 'failed': ['f']})


class CloseTests(unittest.TestCase):
    """FRS-27."""

    def setUp(self):
        self.a = alert()
        _, self.state = A.plan_alerts([self.a], [], [], S.empty_state(), 'r1', NOW)
        self.open = opened(self.state, self.a, 7)

    def test_close_after_two_clean_runs_without_ops(self):
        actions, state = A.plan_alerts([], self.open, [], self.state, 'r2', NOW + DAY)
        self.assertEqual(of(actions, 'close'), [])
        actions, state = A.plan_alerts([], self.open, [], state, 'r3', NOW + 2 * DAY)
        close = of(actions, 'close')
        self.assertEqual([c['number'] for c in close], [7])
        comment = of(actions, 'comment')[0]['body']
        self.assertIn('r2', comment)
        self.assertIn('r3', comment)
        self.assertEqual(ops(actions), [])
        self.assertIsNotNone(state['alerts'][fp_of(self.a)]['closed_at'])

    def test_reraise_resets_clean_runs(self):
        _, state = A.plan_alerts([], self.open, [], self.state, 'r2', NOW + DAY)
        self.assertEqual(state['alerts'][fp_of(self.a)]['clean_runs'], ['r2'])
        _, state = A.plan_alerts([self.a], self.open, [], state, 'r3', NOW + 2 * DAY)
        self.assertEqual(state['alerts'][fp_of(self.a)]['clean_runs'], [])
        actions, state = A.plan_alerts([], self.open, [], state, 'r4', NOW + 3 * DAY)
        self.assertEqual(of(actions, 'close'), [])

    def test_out_of_scope_alert_is_not_counted_clean(self):
        state = self.state
        for run in ('m1', 'm2', 'm3'):
            actions, state = A.plan_alerts([], self.open, [], state, run, NOW + DAY, scope={'cli-lag'})
            self.assertEqual(of(actions, 'close'), [])
        self.assertEqual(state['alerts'][fp_of(self.a)]['clean_runs'], [])


class FindingPlanTests(unittest.TestCase):
    """FRS-13."""

    def routed(self, evidence='claude-2.1 is retired', as_='issue'):
        return {'finding': finding(evidence), 'as': as_, 'title': 'freshness: x uses a retired model',
                'body': 'Replace the model ID.', 'labels': ['freshness'], 'files': {}}

    def test_new_finding_opens_once_with_marker(self):
        state = S.empty_state()
        actions = A.plan_findings([self.routed(), self.routed()], [], state)
        self.assertEqual(len(actions), 1)
        fp = F.finding_fingerprint(finding())
        self.assertEqual(F.parse_marker(actions[0]['body']), fp)
        pr = A.plan_findings([self.routed(as_='pr')], [], S.empty_state())[0]
        self.assertEqual(pr['op'], 'open_pr')
        self.assertEqual(pr['branch'], 'freshness/' + fp[:12])
        self.assertEqual(F.validate_action(pr), [])

    def test_unchanged_emits_nothing_changed_emits_one_comment(self):
        state = S.empty_state()
        A.plan_findings([self.routed()], [], state)
        open_items = [{'number': 11, 'fingerprint': F.finding_fingerprint(finding())}]
        self.assertEqual(A.plan_findings([self.routed()], open_items, state), [])
        actions = A.plan_findings([self.routed('now also in README')], open_items, state)
        self.assertEqual([(a['op'], a['number']) for a in actions], [('comment', 11)])
        self.assertEqual(A.plan_findings([self.routed('now also in README')], open_items, state), [])


class StateTests(unittest.TestCase):
    def test_round_trip_and_unknown_keys_survive(self):
        state = S.empty_state()
        state['links'] = {'https://example.com': {'fails': 2}}
        state['heartbeats'] = {'actions': {'result': 'ok'}}
        _, planned = A.plan_alerts([alert()], [], [], state, 'r1', NOW)
        for s in (state, planned):
            text = S.dump(s)
            self.assertTrue(text.endswith('}\n'))
            self.assertEqual(S.load(text), s)
            self.assertEqual(S.dump(S.load(text)), text)
        self.assertEqual(S.load(S.dump(planned))['links'], state['links'])
        self.assertEqual(S.load(''), S.empty_state())

    def test_plan_does_not_mutate_input_and_requires_aware_now(self):
        state = S.empty_state()
        before = copy.deepcopy(state)
        A.plan_alerts([alert()], [], [], state, 'r1', NOW)
        self.assertEqual(state, before)
        with self.assertRaises(ValueError):
            A.plan_alerts([alert()], [], [], state, 'r1', datetime.datetime(2026, 11, 18))


if __name__ == '__main__':
    unittest.main()
