"""Tests for freshness/finding.py, freshness/state.py and freshness/alerts.py.

Requirements: FRS-13, FRS-17, FRS-22, FRS-24, FRS-25, FRS-27 (docs/SPEC.md).
Pure logic: no network, GitHub, gh or openclaw. `now` is passed in.
"""

import copy
import datetime
import importlib.util
import os
import unittest

from freshness import alerts as A
from freshness import dwell as D
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
        self.assertEqual(len(of(actions, 'comment')), 1)  # opening is not a comment
        actions, state = A.plan_alerts([alert(evidence='v3')], self.open, [], state, 'r3', NOW + 20 * HOUR)
        self.assertEqual(of(actions, 'comment'), [])
        actions, state = A.plan_alerts([alert(evidence='v3')], self.open, [], state, 'r4', NOW + 26 * HOUR)
        self.assertEqual(len(of(actions, 'comment')), 1)
        actions, _ = A.plan_alerts([alert(evidence='v4')], self.open, [], state, 'r5', NOW + 30 * HOUR)
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
        comments = of(actions, 'comment')
        self.assertEqual([c['number'] for c in comments], [20])
        self.assertIn('dwell: t1', comments[0]['body'])
        self.assertEqual(state['issues']['storm_issue'], 20)
        actions, _ = A.plan_alerts(more, open_now, [], state, 'r3', NOW + 2 * HOUR)
        self.assertEqual(of(actions, 'comment'), [])

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
        send = ops(actions)[0]
        # The executor filled in the URL of the issue it opened, then the send failed.
        text = send['text'].replace(A.ISSUE_URL_PLACEHOLDER, A.ISSUE_URL_BASE + '7')
        A.record_send(state, 'ok text', True)
        A.record_send(state, text, False, fingerprint=send['fingerprint'])
        self.assertEqual(state['ops']['sent'], 0)
        self.assertFalse(state['alerts'][fp_of(alert('x'))]['announced'])
        A.record_send(state, 'plain text', False)
        self.assertEqual(state['ops']['failed'], ['plain text'])
        raised = [alert('x')] + [alert('y' + str(i)) for i in range(4)]
        actions, state = A.plan_alerts(raised, opened(state, alert('x'), 7), [], state, 'r2', NOW + HOUR)
        sent = [a['text'] for a in ops(actions)]
        self.assertEqual(sent[0], 'plain text')
        self.assertIn(text, sent[1:])
        self.assertEqual(len(sent), 6)
        self.assertEqual(state['ops']['sent'], 6)
        self.assertEqual(state['ops']['failed'], [])

    def test_failed_announcement_not_sent_after_close(self):
        a = alert('z')
        actions, state = A.plan_alerts([a], [], [], S.empty_state(), 'r1', NOW)
        A.record_send(state, ops(actions)[0]['text'], False, fingerprint=fp_of(a))
        open_now = opened(state, a, 5)
        actions, state = A.plan_alerts([], open_now, [], state, 'r2', NOW + HOUR)
        A.record_send(state, ops(actions)[0]['text'], False, fingerprint=fp_of(a))
        actions, state = A.plan_alerts([], open_now, [], state, 'r3', NOW + 2 * HOUR)
        self.assertEqual([x['op'] for x in actions], ['comment', 'close'])

    def test_failed_digest_is_sent_again_next_run(self):
        state = A.record_send(S.empty_state(), 'f1', False)
        raised = [alert('g' + str(i)) for i in range(7)]
        actions, state = A.plan_alerts(raised, [], [], state, 'r1', NOW)
        digest = ops(actions)[-1]['text']
        self.assertTrue(digest.startswith(A.DIGEST_PREFIX))
        A.record_send(state, digest, False)
        self.assertFalse(state['ops']['digest_sent'])
        self.assertEqual(state['ops']['pending'], [])
        self.assertEqual(state['ops']['failed'], [])
        unannounced = [fp for fp, e in state['alerts'].items() if not e['announced']]
        self.assertEqual(len(unannounced), 1)
        number = {fp: 50 + i for i, fp in enumerate(sorted(state['alerts']))}
        open_now = [{'number': n, 'fingerprint': fp, 'closed_at': None} for fp, n in number.items()]
        snapshot = copy.deepcopy(state)
        url = A.ISSUE_URL_BASE + str(number[unannounced[0]])
        actions, state = A.plan_alerts(raised[:5], open_now, [], state, 'r2', NOW + HOUR)
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['text'].startswith(A.DIGEST_PREFIX))
        self.assertIn(url, sent[0]['text'])
        # On the next UTC day the lost line is sent directly, with its URL.
        tomorrow = datetime.datetime(2026, 11, 19, 6, 0, tzinfo=UTC)
        actions, _ = A.plan_alerts(raised[:5], open_now, [], snapshot, 'r2', tomorrow)
        texts = [x['text'] for x in ops(actions)]
        self.assertTrue(any(t.endswith(url) for t in texts), texts)

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
                                         'pending': [], 'failed': ['f'], 'dwell_groups': {}})


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


class OutOfScopeReminderTests(unittest.TestCase):
    """FRS-17, FRS-24."""

    ACTIONS_SCOPE = {'canary', 'dwell', 'source-failing', 'heartbeat-mac'}

    def setUp(self):
        self.a = alert('zz-canary', kind='canary')
        _, state = A.plan_alerts([self.a], [], [], S.empty_state(), 'a1', NOW,
                                 scope=self.ACTIONS_SCOPE, announce=False)
        state['alerts'][fp_of(self.a)]['announced'] = True
        state['alerts'][fp_of(self.a)]['last_reminder_at'] = S.iso(NOW)
        self.state = state
        self.open = opened(state, self.a, 11)

    def mac(self, state, run_id, when):
        return A.plan_alerts([], self.open, [], state, run_id, when, scope={'heartbeat-actions'})

    def test_mac_run_reminds_open_actions_alert(self):
        actions, state = self.mac(self.state, 'm1', NOW + 2 * DAY)
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertIn('still open', sent[0]['text'])
        self.assertIn('/issues/11', sent[0]['text'])
        self.assertEqual(sent[0]['fingerprint'], fp_of(self.a))
        self.assertEqual(of(actions, 'close'), [])
        entry = state['alerts'][fp_of(self.a)]
        self.assertEqual(entry['clean_runs'], [])
        self.assertEqual(entry['last_reminder_at'], S.iso(NOW + 2 * DAY))

    def test_reminder_respects_cooldown(self):
        _, state = self.mac(self.state, 'm1', NOW + 2 * DAY)
        actions, _ = self.mac(state, 'm2', NOW + 2 * DAY + 2 * HOUR)
        self.assertEqual(ops(actions), [])
        self.assertEqual(of(actions, 'close'), [])

    def test_actions_half_still_sends_nothing(self):
        actions, _ = A.plan_alerts([], self.open, [], self.state, 'a2', NOW + 2 * DAY,
                                   scope=self.ACTIONS_SCOPE, announce=False)
        self.assertEqual(ops(actions), [])

    def test_unannounced_entry_gets_no_reminder(self):
        state = copy.deepcopy(self.state)
        state['alerts'][fp_of(self.a)]['announced'] = False
        state['alerts'][fp_of(self.a)]['last_reminder_at'] = None
        actions, _ = self.mac(state, 'm1', NOW + 2 * DAY)
        self.assertFalse([m for m in ops(actions) if 'still open' in m['text']])


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
        self.assertEqual(F.validate_action(actions[0]), [])
        # Delivery is acknowledged by the runner, not the planner.
        fp = F.finding_fingerprint(finding())
        state['findings'][fp]['evidence_sha'] = actions[0]['finding_evidence_sha']
        self.assertEqual(A.plan_findings([self.routed('now also in README')], open_items, state), [])

    def test_changed_evidence_remains_pending_until_delivery(self):
        state = S.empty_state()
        A.plan_findings([self.routed()], [], state)
        fp = F.finding_fingerprint(finding())
        old = state['findings'][fp]['evidence_sha']
        open_items = [{'number': 11, 'fingerprint': fp}]
        new = F.evidence_sha('now also in README')
        for _ in range(2):
            actions = A.plan_findings([self.routed('now also in README')], open_items, state)
            self.assertEqual([(a['op'], a['number'], a['finding_evidence_sha']) for a in actions],
                             [('comment', 11, new)])
            self.assertEqual(state['findings'][fp]['evidence_sha'], old)

    def test_an_open_item_without_a_record_is_initialized(self):
        state = S.empty_state()
        fp = F.finding_fingerprint(finding())
        self.assertEqual(A.plan_findings([self.routed()], [{'number': 11, 'fingerprint': fp}], state), [])
        self.assertEqual(state['findings'][fp], {'evidence_sha': F.evidence_sha('claude-2.1 is retired')})


class ScrubTests(unittest.TestCase):
    def test_evidence_that_fails_the_scrub_is_withheld(self):
        home = '/' + 'Users' + '/someone/'
        bad = alert('leak', evidence='ok line\nread ' + home + 'x.txt')
        actions, _ = A.plan_alerts([bad], [], [], S.empty_state(), 'r1', NOW)
        body = of(actions, 'open_issue')[0]['body']
        self.assertNotIn(home, body)
        self.assertIn('ok line', body)
        self.assertIn('withheld by the scrub: home-path', body)
        for action in actions:
            for key in ('title', 'body', 'text'):
                self.assertEqual(scrub.scan_text(action.get(key, '')), [], action)
        title = A.alert_title('dwell', home + 'job')
        self.assertEqual(scrub.scan_text(title), [])

    def test_subject_in_state_is_scrubbed_and_bad_fix_becomes_issue(self):
        home = '/' + 'Users' + '/someone/'
        _, state = A.plan_alerts([alert(home + 'job')], [], [], S.empty_state(), 'r1', NOW)
        self.assertEqual(scrub.scan_text(S.dump(state)), [])
        item = {'finding': finding(), 'as': 'pr', 'title': 't', 'body': 'b', 'labels': [],
                'files': {'skills/x/SKILL.md': 'see ' + home + 'notes\n'}}
        action = A.plan_findings([item], [], S.empty_state())[0]
        self.assertEqual(action['op'], 'open_issue')
        self.assertNotIn('files', action)
        private = 'secret' + '-repo'
        item['files'] = {'skills/x/SKILL.md': 'see ' + private + '\n'}
        self.assertEqual(A.plan_findings([item], [], S.empty_state())[0]['op'], 'open_pr')
        action = A.plan_findings([item], [], S.empty_state(), private_names=[private])[0]
        self.assertEqual(action['op'], 'open_issue')

    def test_failed_digest_resets_every_alert_with_the_same_scrubbed_line(self):
        home = '/' + 'Users' + '/someone/'
        state = S.empty_state()
        for i in range(6):
            A.record_send(state, 'f' + str(i), False)
        raised = [alert(home + 'a'), alert(home + 'b')]
        actions, state = A.plan_alerts(raised, [], [], state, 'r1', NOW)
        self.assertEqual(len(state['ops']['pending']), 2)
        A.record_send(state, ops(actions)[-1]['text'], False)
        self.assertEqual([e['announced'] for e in state['alerts'].values()], [False, False])

    def test_stale_marker_in_routed_body_is_replaced(self):
        stale = F.marker(F.fingerprint('dead-link', 'old'))
        item = {'finding': finding(), 'as': 'issue', 'title': 't', 'body': 'text\n' + stale + '\n',
                'labels': [], 'files': {}}
        body = A.plan_findings([item], [], S.empty_state())[0]['body']
        self.assertNotIn(stale, body)
        self.assertEqual(F.parse_marker(body), F.finding_fingerprint(finding()))


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


def stuck(name, *ids, now=NOW):
    """Dwell alerts for items stuck in dwell state `name` (freshness/dwell.check_dwell)."""
    since = S.iso(now - 30 * DAY)
    return D.check_dwell(name, [{'id': i, 'since': since} for i in ids], now)


def open_list(alerts_, first=60):
    return [{'number': first + i, 'fingerprint': fp_of(a), 'closed_at': None} for i, a in enumerate(alerts_)]


def past_cap():
    """plan_alerts arguments for one ordinary and two re-review alerts after the day's 6 messages."""
    state = S.roll_day(S.empty_state(), NOW)
    state['ops']['sent'] = A.OPS_PER_DAY
    raised = [alert('p0')] + stuck('re-review', 1, 2)
    return raised, open_list(raised), [], state, 'r1', NOW


class DwellOpsTests(unittest.TestCase):
    """FRS-17: one ops message per dwell state per UTC day."""

    def test_issue_example_two_items_one_message_with_count(self):
        raised = stuck('re-review', 3, 4)
        actions, state = A.plan_alerts(raised, [], [], None, 'r', NOW)
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(of(actions, 'open_issue')), 2)  # each item keeps its issue
        rep = min(fp_of(a) for a in raised)
        self.assertEqual(sent[0]['fingerprint'], rep)
        self.assertIn('(and 1 more re-review item stuck)', sent[0]['text'])
        self.assertTrue(sent[0]['text'].endswith(': ' + A.ISSUE_URL_PLACEHOLDER))
        self.assertEqual(state['ops']['sent'], 1)
        self.assertTrue(all(state['alerts'][fp_of(a)]['announced'] for a in raised))
        self.assertEqual(set(state['ops']['dwell_groups']['re-review']['members']), {fp_of(a) for a in raised})

    def test_counts_plural_and_duplicates_once(self):
        raised = stuck('re-review', 1, 2, 3)
        actions, _ = A.plan_alerts(raised + raised[:1], [], [], None, 'r', NOW)
        self.assertEqual(len(ops(actions)), 1)
        self.assertIn('(and 2 more re-review items stuck)', ops(actions)[0]['text'])

    def test_every_state_prefix_is_grouped_and_states_are_separate(self):
        for name in D.STATES:
            raised = [alert(name + ':a'), alert(name + ':b', kind='other-kind')]
            actions, _ = A.plan_alerts(raised, [], [], None, 'r', NOW)
            self.assertEqual(len(ops(actions)), 1, name)
        raised = stuck('re-review', 1, 2) + [alert('freshness-pr-unreviewed:5'), alert('freshness-pr-unreviewed:6')]
        actions, _ = A.plan_alerts(raised, [], [], None, 'r', NOW)
        self.assertEqual(len(ops(actions)), 2)

    def test_subject_withheld_by_the_scrub_is_still_grouped(self):
        home = '/' + 'Users' + '/someone/'
        raised = [alert('re-review:' + home + 'a'), alert('re-review:' + home + 'b')]
        actions, state = A.plan_alerts(raised, [], [], None, 'r', NOW)
        self.assertTrue(all('withheld' in state['alerts'][fp_of(a)]['subject'] for a in raised))
        self.assertEqual(len(ops(actions)), 1)
        self.assertIn('(and 1 more re-review item stuck)', ops(actions)[0]['text'])
        # The Mac half still groups them from a state the Actions half wrote.
        _, actions_state = A.plan_alerts(raised, [], [], None, 'r', NOW, announce=False)
        actions, _ = A.plan_alerts([], open_list(raised), [], S.load(S.dump(actions_state)), 'r2', NOW + HOUR)
        self.assertEqual(len(ops(actions)), 1)

    def test_subject_that_looks_like_a_group_count_still_retries(self):
        a = alert('nightly (and 1 more re-review item stuck)')
        actions, state = A.plan_alerts([a], [], [], None, 'r1', NOW)
        A.record_send(state, ops(actions)[0]['text'], False, fingerprint=fp_of(a))
        self.assertFalse(state['alerts'][fp_of(a)]['announced'])
        self.assertEqual(state['ops']['failed'], [])
        actions, state = A.plan_alerts([a], [], [], None, 'r1', NOW)
        A.record_send(state, ops(actions)[0]['text'], False)
        self.assertFalse(state['alerts'][fp_of(a)]['announced'])
        self.assertEqual(state['ops']['failed'], [])

    def test_ambiguous_failed_text_resets_every_match(self):
        plain, literal = alert('nightly'), alert('nightly (and 1 more re-review item stuck)')
        actions, state = A.plan_alerts([plain, literal], [], [], None, 'r1', NOW)
        text = [x['text'] for x in ops(actions) if 'stuck' in x['text']][0]
        A.record_send(state, text, False)  # no fingerprint, as for a digest line
        self.assertFalse(state['alerts'][fp_of(literal)]['announced'])
        self.assertEqual(state['ops']['failed'], [])
        # With the fingerprint only that alert is due again.
        actions, state = A.plan_alerts([plain, literal], [], [], None, 'r1', NOW)
        text = [x for x in ops(actions) if 'stuck' in x['text']][0]
        A.record_send(state, text['text'], False, fingerprint=text['fingerprint'])
        self.assertFalse(state['alerts'][fp_of(literal)]['announced'])
        self.assertTrue(state['alerts'][fp_of(plain)]['announced'])

    def test_group_message_wins_over_a_member_named_like_it(self):
        raised = [alert('re-review:0'), alert('re-review:0 (and 1 more re-review item stuck)')]
        args = list(past_cap())
        args[0], args[1] = raised, open_list(raised)
        actions, state = A.plan_alerts(*args)
        A.record_send(state, ops(actions)[-1]['text'], False)  # the digest failed
        self.assertEqual(state['ops']['dwell_groups'], {})
        self.assertFalse(any(state['alerts'][fp_of(a)]['announced'] for a in raised))

    def test_ordinary_line_named_like_a_group_message_is_retried(self):
        home = '/' + 'Users' + '/someone/'
        dwells = [alert('re-review:' + home + 'a'), alert('re-review:' + home + 'b')]
        withheld = '[line withheld by the scrub: home-path]'
        ordinary = alert(withheld + ' (and 1 more re-review item stuck)')
        actions, state = A.plan_alerts(dwells + [ordinary], [], [], None, 'r1', NOW)
        self.assertEqual(state['alerts'][fp_of(dwells[0])]['subject'], withheld)
        texts = [x['text'] for x in ops(actions)]
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[0].rsplit(': ', 1)[0], texts[1].rsplit(': ', 1)[0])
        A.record_send(state, texts[0], False)  # no fingerprint: every match is due again
        self.assertFalse(state['alerts'][fp_of(ordinary)]['announced'])
        self.assertEqual(state['ops']['dwell_groups'], {})
        self.assertFalse(any(state['alerts'][fp_of(a)]['announced'] for a in dwells))

    def test_unknown_prefix_and_ordinary_alerts_are_not_grouped(self):
        raised = [alert('nightly'), alert('night'), alert('re-reviewx:1'), alert('re-reviewx:2'), alert('re-review')]
        actions, _ = A.plan_alerts(raised, [], [], None, 'r', NOW)
        self.assertEqual(len(ops(actions)), 5)

    def test_one_slot_against_the_cap_and_group_in_digest(self):
        raised = [alert('o' + str(i)) for i in range(5)] + stuck('re-review', 1, 2, 3)
        actions, state = A.plan_alerts(raised, [], [], None, 'r', NOW)
        self.assertEqual(len(ops(actions)), 6)
        self.assertEqual(state['ops']['sent'], 6)
        self.assertFalse(state['ops']['digest_sent'])
        # Past the cap a group adds one digest line.
        actions, state = A.plan_alerts(*past_cap())
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['text'].startswith(A.DIGEST_PREFIX + 'daily cap of 6 ops messages reached, 2 more'))
        self.assertIn('(and 1 more re-review item stuck)', sent[0]['text'])
        self.assertEqual(len(state['ops']['pending']), 2)
        self.assertIn('re-review', state['ops']['dwell_groups'])

    def test_later_runs_same_day_send_nothing_more_for_the_state(self):
        first = stuck('re-review', 1, 2)
        actions, state = A.plan_alerts(first, [], [], None, 'r1', NOW)
        open_now = open_list(first)
        later = first + stuck('re-review', 9, now=NOW + HOUR)
        actions, state = A.plan_alerts(later, open_now, [], state, 'r2', NOW + HOUR)
        self.assertEqual(ops(actions), [])
        self.assertEqual(len(of(actions, 'open_issue')), 1)
        self.assertEqual(state['ops']['sent'], 1)
        new_fp = fp_of(later[-1])
        self.assertTrue(state['alerts'][new_fp]['announced'])
        self.assertIn(new_fp, state['ops']['dwell_groups']['re-review']['members'])
        # Another state still gets its message.
        other = [alert('freshness-pr-unreviewed:7')]
        actions, state = A.plan_alerts(later + other, open_now + open_list(later[-1:], 70), [], state, 'r3',
                                       NOW + 2 * HOUR)
        self.assertEqual(len(ops(actions)), 1)

    def test_next_utc_day_one_reminder_for_the_state(self):
        raised = stuck('re-review', 1, 2, 3)
        _, state = A.plan_alerts(raised, [], [], None, 'r1', NOW)
        open_now = open_list(raised)
        actions, state = A.plan_alerts(raised, open_now, [], state, 'r2', NOW + DAY)
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['text'].startswith('skills-catalog freshness-alert still open: '))
        self.assertIn('(and 2 more re-review items stuck)', sent[0]['text'])
        self.assertEqual(state['ops']['day'], '2026-11-19')
        self.assertTrue(all(state['alerts'][fp_of(a)]['last_reminder_at'] == S.iso(NOW + DAY) for a in raised))
        actions, _ = A.plan_alerts(raised, open_now, [], state, 'r3', NOW + DAY + HOUR)
        self.assertEqual(ops(actions), [])

    def test_failed_announcement_frees_the_slot_and_retries_with_current_urls(self):
        raised = stuck('re-review', 1, 2, 3)
        actions, state = A.plan_alerts(raised, [], [], None, 'r1', NOW)
        send = ops(actions)[0]
        text = send['text'].replace(A.ISSUE_URL_PLACEHOLDER, A.ISSUE_URL_BASE + '61')
        A.record_send(state, text, False, fingerprint=send['fingerprint'])
        self.assertEqual(state['ops']['sent'], 0)
        self.assertEqual(state['ops']['dwell_groups'], {})
        self.assertEqual(state['ops']['failed'], [])
        self.assertFalse(any(state['alerts'][fp_of(a)]['announced'] for a in raised))
        # The representative closed in between; the retry names a current issue.
        rep = send['fingerprint']
        still = [a for a in raised if fp_of(a) != rep]
        closed = [{'number': 99, 'fingerprint': rep, 'closed_at': S.iso(NOW + HOUR)}]
        open_now = open_list(still)
        actions, state = A.plan_alerts(still, open_now, closed, state, 'r2', NOW + 2 * HOUR)
        sent = ops(actions)
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(sent[0]['fingerprint'], rep)
        self.assertIn('(and 1 more re-review item stuck)', sent[0]['text'])
        number = [i['number'] for i in open_now if i['fingerprint'] == sent[0]['fingerprint']][0]
        self.assertTrue(sent[0]['text'].endswith(A.ISSUE_URL_BASE + str(number)))

    def test_failed_reminder_and_send_without_fingerprint_free_the_slot(self):
        raised = stuck('re-review', 1, 2)
        _, state = A.plan_alerts(raised, [], [], None, 'r1', NOW)
        open_now = open_list(raised)
        actions, state = A.plan_alerts(raised, open_now, [], state, 'r2', NOW + DAY)
        A.record_send(state, ops(actions)[0]['text'], False)  # no fingerprint
        self.assertEqual(state['ops']['dwell_groups'], {})
        self.assertEqual(state['ops']['failed'], [])
        self.assertTrue(all(state['alerts'][fp_of(a)]['last_reminder_at'] is None for a in raised))
        actions, _ = A.plan_alerts(raised, open_now, [], state, 'r3', NOW + DAY + HOUR)
        self.assertEqual(len(ops(actions)), 1)

    def test_successful_send_keeps_the_slot(self):
        raised = stuck('re-review', 1, 2)
        actions, state = A.plan_alerts(raised, [], [], None, 'r1', NOW)
        A.record_send(state, ops(actions)[0]['text'], True, fingerprint=ops(actions)[0]['fingerprint'])
        self.assertIn('re-review', state['ops']['dwell_groups'])

    def test_failed_digest_with_group_frees_every_member(self):
        args = past_cap()
        raised = args[0]
        actions, state = A.plan_alerts(*args)
        A.record_send(state, ops(actions)[-1]['text'], False)
        self.assertEqual(state['ops']['dwell_groups'], {})
        self.assertEqual(state['ops']['failed'], [])
        self.assertFalse(any(state['alerts'][fp_of(a)]['announced'] for a in raised[-2:]))

    def test_announce_false_reserves_nothing(self):
        raised = stuck('re-review', 1, 2)
        actions, state = A.plan_alerts(raised, [], [], None, 'r1', NOW, announce=False)
        self.assertEqual(ops(actions), [])
        self.assertEqual(state['ops']['dwell_groups'], {})
        # The Mac half announces both later with one message.
        actions, state = A.plan_alerts([], open_list(raised), [], state, 'r2', NOW + HOUR)
        self.assertEqual(len(ops(actions)), 1)

    def test_state_round_trip_legacy_load_and_no_mutation(self):
        legacy = S.dump({'ops': {'day': '2026-11-18', 'sent': 1, 'digest_sent': False, 'pending': [], 'failed': []}})
        self.assertEqual(S.load(legacy)['ops']['dwell_groups'], {})
        raised = stuck('re-review', 1, 2)
        state = S.load(legacy)
        before = copy.deepcopy(state)
        actions, planned = A.plan_alerts(raised, [], [], state, 'r1', NOW)
        self.assertEqual(state, before)
        reloaded = S.load(S.dump(planned))
        self.assertEqual(reloaded['ops']['dwell_groups'], planned['ops']['dwell_groups'])
        actions, _ = A.plan_alerts(stuck('re-review', 5), [], [], reloaded, 'r2', NOW + HOUR)
        self.assertEqual(ops(actions), [])
        self.assertEqual(S.roll_day(reloaded, NOW + DAY)['ops']['dwell_groups'], {})


if __name__ == '__main__':
    unittest.main()
