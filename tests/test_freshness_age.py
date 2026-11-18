#!/usr/bin/env python3
"""Tests for the skill age lifecycle and the dwell limits (FRS-8, FRS-9, FRS-17, INV-6)."""

import datetime
import importlib.util
import json
import os
import re
import tempfile
import unittest

from freshness import age
from freshness import alerts
from freshness import dwell
from freshness import finding as fmod
from freshness import state as smod

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('age_test_catalog', os.path.join(_ROOT, 'tools', 'catalog.py'))
_catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_catalog)

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


def _iso(dt):
    return smod.iso(dt)


def _skill_text(name, last, max_age='90'):
    return '\n'.join([
        '---',
        'name: ' + name,
        'description: Use when testing the age lifecycle.',
        'metadata:',
        '  catalog-version: "1.0.0"',
        '  catalog-owner: "kennylhilljr"',
        '  catalog-last-verified: "%s"' % last,
        '  catalog-max-age-days: "%s"' % max_age,
        '---',
        '',
        'Body.',
        '',
        '## Changelog',
        '',
        '| Version | Date | Change |',
        '| --- | --- | --- |',
        '| 1.0.0 | %s | Initial; sha256 `%s` |' % (last, 'a' * 12),
        '',
    ])


class Repo(object):
    """A temp catalog with three skills and one held skill."""

    def __init__(self, ages=None, stale=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        ages = ages or {'alpha': '2026-08-01', 'beta': '2026-08-21', 'gamma': '2026-11-11'}
        for name, last in ages.items():
            self.write_skill('skills', name, last)
        self.write_skill('.held', 'sdd-held', '2026-01-01')
        os.makedirs(os.path.join(self.root, 'freshness'))
        self.write(age.STALE_PATH, age.render_stale(stale or []))
        self.write('catalog.json', _catalog.render(self.root))

    def write_skill(self, section, name, last):
        self.write(section + '/' + name + '/SKILL.md', _skill_text(name, last))

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, *rel.split('/')), encoding='utf-8', newline='') as handle:
            return handle.read()

    def apply(self, files):
        for rel, text in files.items():
            self.write(rel, text)

    def close(self):
        self._tmp.cleanup()


def _ok(mac_hours=2, **over):
    result = {'actions_run': 'run-1', 'actions_ok': True, 'mac_run': 'mac-1', 'mac_ok': True,
              'mac_at': _iso(NOW - datetime.timedelta(hours=mac_hours))}
    result.update(over)
    return result


def _changed_lines(old, new):
    a, b = old.split('\n'), new.split('\n')
    return [i for i in range(max(len(a), len(b))) if (a[i:i + 1] or [None]) != (b[i:i + 1] or [None])]


def _only(actions, op):
    return [a for a in actions if a['op'] == op]


class TestAgeFindings(unittest.TestCase):

    def setUp(self):
        self.repo = Repo()
        self.skills = age.read_skills(self.repo.root)

    def tearDown(self):
        self.repo.close()

    def test_past_max_age_gives_one_finding(self):
        """FRS-8: a skill past its max age gives exactly one `past-max-age` finding, subject = name."""
        found = age.age_findings(self.skills, TODAY)
        alpha = [f for f in found if f['skill'] == 'alpha']
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha[0]['kind'], 'past-max-age')
        self.assertEqual(alpha[0]['subject'], 'alpha')
        self.assertEqual(alpha[0]['file'], 'skills/alpha/SKILL.md')
        self.assertEqual(alpha[0]['line'], 7)
        self.assertEqual(fmod.validate(alpha[0]), [])
        self.assertEqual(alerts._scan(alpha[0]['evidence']), [])
        self.assertNotIn('gamma', [f['skill'] for f in found])
        self.assertIn('sdd-held', [f['skill'] for f in found])  # held skills age too (canary, FRS-18)

    def test_one_day_younger_gives_none(self):
        """FRS-8: due date is today (last-verified + max age == today) gives no finding; one day later does."""
        skill = {'name': 'edge', 'file': 'skills/edge/SKILL.md', 'line': 7,
                 'last_verified': (TODAY - datetime.timedelta(days=90)).isoformat(), 'max_age_days': 90}
        self.assertEqual(age.age_findings([skill], TODAY), [])
        self.assertEqual(len(age.age_findings([skill], TODAY + datetime.timedelta(days=1))), 1)

    def test_excluded_skill_is_never_read(self):
        """DESIGN 5.1: an excluded folder is skipped before reading and never reported."""
        names = [s['name'] for s in age.read_skills(self.repo.root, excluded={'alpha'})]
        self.assertNotIn('alpha', names)
        self.assertEqual([f for f in age.age_findings(self.skills, TODAY, excluded={'alpha'})
                          if f['skill'] == 'alpha'], [])

    def test_rereview_issue_routing_and_dedup(self):
        """FRS-8: routed as one `re-review` issue titled `re-review: <skill>`, deduplicated by part 1."""
        found = [f for f in age.age_findings(self.skills, TODAY) if f['skill'] == 'alpha'][0]
        item = age.route_rereview(found)
        self.assertEqual(item['title'], 're-review: alpha')
        self.assertEqual(item['labels'], ['re-review'])
        state = smod.empty_state()
        actions = alerts.plan_findings([item, item], [], state)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['op'], 'open_issue')
        self.assertEqual(fmod.parse_marker(actions[0]['body']), age.rereview_fingerprint('alpha'))
        again = alerts.plan_findings([item], [{'number': 5, 'fingerprint': age.rereview_fingerprint('alpha')}],
                                     state)
        self.assertEqual(again, [])


class TestStale(unittest.TestCase):

    def setUp(self):
        self.repo = Repo()
        self.skills = age.read_skills(self.repo.root)
        self.aged = {f['skill'] for f in age.age_findings(self.skills, TODAY)}

    def tearDown(self):
        self.repo.close()

    def _open(self, days, skill='alpha'):
        return [{'skill': skill, 'number': 7, 'opened_at': _iso(NOW - datetime.timedelta(days=days))}]

    def test_open_14_days_gives_stale_pr(self):
        """FRS-8: a re-review issue open 14 days gives a PR changing only stale.json and catalog.json."""
        actions = age.stale_actions(self._open(14), [], NOW, root=self.repo.root, aged=self.aged)
        prs = _only(actions, 'open_pr')
        self.assertEqual(len(prs), 1)
        self.assertEqual(len(actions), 1)
        pr = prs[0]
        self.assertEqual(fmod.validate_action(pr), [])
        self.assertEqual(sorted(pr['files']), ['catalog.json', 'freshness/stale.json'])
        self.assertEqual(json.loads(pr['files']['freshness/stale.json']), ['alpha'])
        self.assertEqual(fmod.parse_marker(pr['body']), pr['fingerprint'])
        self.assertEqual(alerts._scan(pr['body']), [])
        self.repo.apply(pr['files'])
        self.assertEqual(_catalog.main(['--root', self.repo.root, '--check']), 0)
        doc = json.loads(self.repo.read('catalog.json'))
        flags = {e['name']: e.get('stale') for e in doc['skills'] + doc['held']}
        self.assertEqual(flags, {'alpha': True, 'beta': None, 'gamma': None, 'sdd-held': None})

    def test_open_13_days_gives_none(self):
        """FRS-8: 13 days open gives no stale PR."""
        self.assertEqual(age.stale_actions(self._open(13), [], NOW, root=self.repo.root, aged=self.aged), [])

    def test_same_pr_open_is_kept_and_changed_target_supersedes(self):
        """FRS-8: an open stale PR with the same target is kept; a changed target closes it and opens a new one."""
        first = _only(age.stale_actions(self._open(14), [], NOW, root=self.repo.root, aged=self.aged),
                      'open_pr')[0]
        open_items = [{'number': 30, 'fingerprint': first['fingerprint'], 'is_pr': True, 'state': 'open',
                       'body': first['body']}]
        self.assertEqual(age.stale_actions(self._open(15), [], NOW, root=self.repo.root, aged=self.aged,
                                           open_items=open_items), [])
        both = self._open(14) + [{'skill': 'sdd-held', 'number': 8,
                                  'opened_at': _iso(NOW - datetime.timedelta(days=20))}]
        actions = age.stale_actions(both, [], NOW, root=self.repo.root, aged=self.aged, open_items=open_items)
        self.assertEqual([a['op'] for a in actions], ['comment', 'close', 'open_pr'])
        self.assertEqual(actions[1]['number'], 30)
        self.assertEqual(json.loads(actions[2]['files']['freshness/stale.json']), ['alpha', 'sdd-held'])

    def test_excluded_skill_never_reaches_the_pr(self):
        """DESIGN 5.1: an excluded skill folder is not copied into the regenerated catalog.json."""
        secret = 'priv' + 'ate-local'
        self.repo.write_skill('skills', secret, '2026-11-11')
        actions = age.stale_actions(self._open(14), [], NOW, root=self.repo.root, aged=self.aged,
                                    excluded={secret})
        pr = _only(actions, 'open_pr')[0]
        self.assertNotIn(secret, pr['files']['catalog.json'])
        self.assertNotIn(secret, pr['body'])

    def test_closed_issue_or_verified_again_clears_stale(self):
        """FRS-8: when the issue closes, or the skill is verified again, a PR removes it from stale.json."""
        self.repo.close()
        self.repo = Repo(stale=['alpha'])
        actions = age.stale_actions([], ['alpha'], NOW, root=self.repo.root, aged={'alpha'})
        pr = _only(actions, 'open_pr')[0]
        self.assertEqual(json.loads(pr['files']['freshness/stale.json']), [])
        self.assertNotIn('"stale"', pr['files']['catalog.json'])
        verified = age.stale_actions(self._open(30), ['alpha'], NOW, root=self.repo.root, aged=set())
        self.assertEqual([a['op'] for a in verified], ['comment', 'close', 'open_pr'])
        self.assertEqual(verified[1]['number'], 7)
        self.assertEqual(json.loads(verified[2]['files']['freshness/stale.json']), [])

    def test_rereview_since_tracks_first_seen(self):
        """FRS-8: the 14 days count from the first run that saw the issue open; a closed issue resets it."""
        state = smod.empty_state()
        items = [{'number': 7, 'fingerprint': age.rereview_fingerprint('alpha'), 'is_pr': False,
                  'state': 'open'}]
        first = age.rereview_since(state, items, self.skills, NOW - datetime.timedelta(days=14))
        later = age.rereview_since(state, items, self.skills, NOW)
        self.assertEqual(first, later)
        self.assertEqual(later[0]['skill'], 'alpha')
        self.assertEqual(age.rereview_since(state, [], self.skills, NOW), [])
        self.assertEqual(age.rereview_since(state, items, self.skills, NOW)[0]['opened_at'], _iso(NOW))
        self.assertEqual(age.rereview_since(smod.empty_state(), items, self.skills, NOW, excluded={'alpha'}), [])
        created = dict(items[0], created_at=_iso(NOW - datetime.timedelta(days=20)))
        self.assertEqual(age.rereview_since(state, [created], self.skills, NOW)[0]['opened_at'],
                         _iso(NOW - datetime.timedelta(days=20)))


class TestAdvance(unittest.TestCase):

    def setUp(self):
        self.repo = Repo(stale=['alpha'])
        self.before = self.repo.read('skills/alpha/SKILL.md')

    def tearDown(self):
        self.repo.close()

    def _advance(self, results, **kw):
        return age.advance_actions(results, TODAY, now=NOW, root=self.repo.root, **kw)

    def test_failed_actions_check_not_advanced(self):
        """FRS-9: a skill with a failed Actions check is not advanced."""
        self.assertEqual(self._advance({'alpha': _ok(actions_ok=False)}), [])

    def test_failed_mac_check_not_advanced(self):
        """FRS-9: a skill with a failed (or missing) Mac check is not advanced."""
        self.assertEqual(self._advance({'alpha': _ok(mac_ok=False)}), [])
        self.assertEqual(self._advance({'alpha': _ok(mac_run=None, mac_ok=None)}), [])

    def test_mac_run_27_hours_old_not_advanced(self):
        """FRS-9: a Mac run 27 hours old is too old; 25 hours is fine."""
        self.assertEqual(self._advance({'alpha': _ok(mac_hours=27)}), [])
        self.assertEqual(len(_only(self._advance({'alpha': _ok(mac_hours=25)}), 'open_pr')), 1)

    def test_eligible_at_max_age_gets_one_line_pr(self):
        """FRS-9: under at-max-age, an eligible skill gets a PR changing only its last-verified line, stale.json and catalog.json."""
        actions = self._advance({'alpha': _ok(), 'gamma': _ok()})
        self.assertEqual(len(actions), 1)
        pr = actions[0]
        self.assertEqual(fmod.validate_action(pr), [])
        self.assertEqual(sorted(pr['files']), ['catalog.json', 'freshness/stale.json', 'skills/alpha/SKILL.md'])
        after = pr['files']['skills/alpha/SKILL.md']
        changed = _changed_lines(self.before, after)
        self.assertEqual(changed, [6])
        self.assertEqual(after.split('\n')[6], '  catalog-last-verified: "%s"' % TODAY.isoformat())
        self.assertEqual(json.loads(pr['files']['freshness/stale.json']), [])
        self.assertEqual(alerts._scan(pr['body']), [])
        self.repo.apply(pr['files'])
        self.assertEqual(_catalog.main(['--root', self.repo.root, '--check']), 0)

    def test_policy_window_and_every_clean_run(self):
        """FRS-9: at-max-age takes a skill within 7 days of its max age, not a young one; every-clean-run takes all."""
        # beta: verified 2026-08-21, due 2026-11-19 (1 day left): within 7 days.
        self.assertIn('skills/beta/SKILL.md', self._advance({'beta': _ok()})[0]['files'])
        self.assertEqual(self._advance({'gamma': _ok()}), [])
        every = self._advance({'gamma': _ok()}, policy=age.POLICY_EVERY_CLEAN_RUN)
        self.assertIn('skills/gamma/SKILL.md', every[0]['files'])
        self.assertNotIn('freshness/stale.json', every[0]['files'])
        self.assertEqual(age.DEFAULT_POLICY, 'at-max-age')
        with self.assertRaises(ValueError):
            self._advance({'gamma': _ok()}, policy='sometimes')

    def test_held_skill_and_failed_evals_not_advanced(self):
        """FRS-9, SDD-1: held skills are never advanced; from M6 a failed eval blocks the advance."""
        self.assertEqual(self._advance({'sdd-held': _ok()}), [])
        self.assertEqual(self._advance({'alpha': _ok(evals_ok=False)}), [])
        self.assertEqual(self._advance({'alpha': _ok()}, evals_required=True), [])

    def test_no_other_module_writes_last_verified(self):
        """FRS-9: only freshness/age.py writes `catalog-last-verified`; elsewhere in freshness/ it only appears in prose."""
        key = 'catalog-last-verified'
        folder = os.path.join(_ROOT, 'freshness')
        writers = []
        for dirpath, _dirs, files in os.walk(folder):
            for name in files:
                if not name.endswith('.py'):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, name), _ROOT).replace(os.sep, '/')
                with open(os.path.join(dirpath, name), encoding='utf-8') as handle:
                    text = handle.read()
                if rel == 'freshness/age.py':
                    self.assertIn('def set_last_verified', text)
                    continue
                # Prose mentions are quoted as `catalog-last-verified`; any other use is a writer.
                bare = re.sub(r'`' + key + r'`', '', text)
                if key in bare or 'last_verified' in bare:
                    writers.append(rel)
        self.assertEqual(writers, [])


class TestDwell(unittest.TestCase):

    def test_actions_heartbeat_27_hours_alerts(self):
        """FRS-17: an Actions heartbeat 27 hours old raises the dwell alert; 25 hours does not."""
        old = [{'id': 'nightly', 'since': _iso(NOW - datetime.timedelta(hours=27))}]
        fresh = [{'id': 'nightly', 'since': _iso(NOW - datetime.timedelta(hours=25))}]
        found = dwell.check_dwell('actions-run', old, NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['kind'], 'heartbeat-actions')
        self.assertEqual(found[0]['subject'], 'actions-run:nightly')
        self.assertEqual(dwell.check_dwell('actions-run', fresh, NOW), [])
        actions, _state = alerts.plan_alerts(found, [], [], None, 'run-1', NOW, announce=False)
        self.assertEqual(_only(actions, 'open_issue')[0]['title'],
                         'freshness-alert: heartbeat-actions: actions-run:nightly')

    def test_one_alert_per_state(self):
        """FRS-17: two stuck items of one state get two issues but one ops message per state per day."""
        items = [{'id': n, 'since': _iso(NOW - datetime.timedelta(days=15))} for n in (3, 4)]
        found = dwell.check_dwell('re-review', items, NOW)
        self.assertEqual([a['subject'] for a in found], ['re-review:3', 're-review:4'])
        hb = dwell.check_dwell('mac-job', [{'id': 'mac', 'since': _iso(NOW - datetime.timedelta(hours=30))}], NOW)
        actions, new_state = alerts.plan_alerts(found + hb, [], [], None, 'run-1', NOW)
        self.assertEqual(len(_only(actions, 'open_issue')), 3)
        self.assertEqual(len(_only(actions, 'send_ops')), 3)
        folded = dwell.fold_ops(actions, new_state, NOW)
        ops = _only(folded, 'send_ops')
        self.assertEqual(len(ops), 2)
        self.assertIn('(+1 more `re-review` item)', ops[0]['text'])
        self.assertEqual(len(_only(folded, 'open_issue')), 3)
        later_same_day = NOW + datetime.timedelta(hours=1)
        self.assertEqual(_only(dwell.fold_ops(actions, new_state, later_same_day), 'send_ops'), [])
        next_day = NOW + datetime.timedelta(days=1)
        self.assertEqual(len(_only(dwell.fold_ops(actions, new_state, next_day), 'send_ops')), 2)

    def test_m5_row_skipped_in_m2(self):
        """FRS-17: an M5 row is skipped in M2 and enabled from M5."""
        item = [{'id': 42, 'since': _iso(NOW - datetime.timedelta(days=10))}]
        self.assertEqual(dwell.check_dwell('freshness-issue-unpicked', item, NOW, milestone='M2'), [])
        self.assertEqual(len(dwell.check_dwell('freshness-issue-unpicked', item, NOW, milestone='M5')), 1)
        with self.assertRaises(ValueError):
            dwell.check_dwell('no-such-state', item, NOW)

    def test_run_rows_and_pr_ping_then_alert(self):
        """FRS-17: 3 failing runs alert; a PR without review is pinged first and alerted on a later run."""
        self.assertEqual(len(dwell.check_dwell('source-failing', [{'id': 'src', 'runs': 3}], NOW)), 1)
        self.assertEqual(dwell.check_dwell('source-failing', [{'id': 'src', 'runs': 2}], NOW), [])
        state = smod.empty_state()
        prs = [{'id': 12, 'fingerprint': 'f' * 64, 'since': _iso(NOW - datetime.timedelta(days=8))}]
        actions, items = dwell.ping_actions(prs, state, NOW)
        self.assertEqual([a['op'] for a in actions], ['comment'])
        self.assertEqual(dwell.check_dwell('freshness-pr-unreviewed', items, NOW), [])
        later = NOW + datetime.timedelta(days=1)
        actions, items = dwell.ping_actions(prs, state, later)
        self.assertEqual(actions, [])
        self.assertEqual(len(dwell.check_dwell('freshness-pr-unreviewed', items, later)), 1)

    def test_every_state_has_limit_and_alert(self):
        """INV-6: every state in STATES has a LIMITS row with a positive limit and an alert kind."""
        self.assertEqual(len(dwell.STATES), 7)
        rows = {row['state']: row for row in dwell.LIMITS}
        for name in dwell.STATES:
            row = rows[name]
            limit = row.get('max_hours', row.get('max_runs'))
            self.assertTrue(isinstance(limit, int) and limit > 0, name)
            self.assertIn(row['alert_kind'], fmod.ALERT_KINDS, name)
            self.assertIn(row['enabled_from'], ('M2', 'M5'), name)
        self.assertEqual(rows['actions-run']['max_hours'], 26)
        self.assertEqual(rows['mac-job']['max_hours'], 26)
        self.assertEqual(rows['re-review']['max_hours'], 14 * 24)


if __name__ == '__main__':
    unittest.main()
