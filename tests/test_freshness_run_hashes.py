"""Regression tests for FRS-9: both halves record the content hash of each skill they checked."""
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from freshness import age
from freshness import checks
from freshness import cli_checks
from freshness import run as runmod
from tests.test_freshness_run import ROOT, NOW, FakeGitHub, FakeCli, _execute

CANARY_FOLDER = os.path.join(ROOT, '.held', 'zz-canary')


def _bare_job(half):
    job = runmod.Run.__new__(runmod.Run)
    job.state = {'results': {}}
    job.checked_skills = ['x']
    job.checked_hashes = {'x': 'h'}
    job.findings = []
    job.failed = False
    job.half = half
    job.deps = types.SimpleNamespace(run_id='1')
    job.now = NOW
    return job


class RecordHashTests(unittest.TestCase):
    def test_record_results_writes_the_actions_hash(self):
        """FRS-9: each half writes the hash it checked into its own field."""
        job = _bare_job('actions')
        job.record_results()
        self.assertEqual(job.state['results']['x']['actions_hash'], 'h')
        job = _bare_job('mac')
        job.record_results()
        self.assertEqual(job.state['results']['x']['mac_hash'], 'h')

    def test_actions_half_records_the_content_hash(self):
        """FRS-9: the Actions half hands advance_actions the hash of the canary folder."""
        with mock.patch.object(age, 'advance_actions', wraps=age.advance_actions) as spy:
            _execute('actions', FakeGitHub())
        recorded = spy.call_args[0][0]['zz-canary']['actions_hash']
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded, age.content_hash(CANARY_FOLDER))

    def test_mac_half_records_the_same_hash(self):
        """FRS-9: the Mac half writes the same hash for the same folder into the state."""
        fake = FakeGitHub()
        _execute('mac', fake, cli_run=FakeCli())
        recorded = fake.written_state()['results']['zz-canary']['mac_hash']
        self.assertEqual(recorded, age.content_hash(CANARY_FOLDER))


class MutationDuringCheckTests(unittest.TestCase):
    """FRS-9: a folder that changes while the checks read it records no hash and fails the half."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.folder = os.path.join(tmp, 'zz-canary')
        shutil.copytree(CANARY_FOLDER, self.folder)
        self.before = age.content_hash(self.folder)
        skills = mock.patch.object(runmod.Run, '_skills',
                                   lambda job: [('.held', 'zz-canary', self.folder)])
        skills.start()
        self.addCleanup(skills.stop)

    def _mutating(self, real):
        def check(*args, **kwargs):
            out = real(*args, **kwargs)
            with open(os.path.join(self.folder, 'SKILL.md'), 'a') as fh:
                fh.write('\nchanged while the checks ran\n')
            return out
        return check

    def test_mac_half_does_not_record_a_hash_it_did_not_check(self):
        """FRS-9: a skill edited while the Mac checks run gets no mac_hash and fails."""
        fake = FakeGitHub()
        with mock.patch.object(cli_checks, 'check_commands', self._mutating(cli_checks.check_commands)):
            _execute('mac', fake, cli_run=FakeCli())
        entry = fake.written_state()['results']['zz-canary']
        self.assertNotEqual(age.content_hash(self.folder), self.before)
        self.assertIsNone(entry['mac_hash'])
        self.assertFalse(entry['mac_ok'])

    def test_a_detected_change_fails_the_mac_half(self):
        """FRS-9: the half whose checks saw a skill change returns failed and says so in its heartbeat."""
        fake = FakeGitHub()
        with mock.patch.object(cli_checks, 'check_commands', self._mutating(cli_checks.check_commands)):
            ok, job, _ = _execute('mac', fake, cli_run=FakeCli())
        self.assertFalse(ok)
        self.assertTrue(job.failed)
        self.assertEqual(fake.written_state()['heartbeats']['mac']['result'], 'failed')

    def test_a_change_during_the_age_check_cannot_pair_with_a_later_mac_pass(self):
        """FRS-9: content A changed to B while `age.read_skills` reopened it, then back to A for the Mac."""
        skill_md = os.path.join(self.folder, 'SKILL.md')
        with open(skill_md) as fh:
            original = fh.read()
        real_read = age.read_skills

        def read_after_edit(*args, **kwargs):
            with open(skill_md, 'a') as fh:
                fh.write('\nchanged before the age check\n')
            return real_read(*args, **kwargs)

        fake = FakeGitHub()
        with mock.patch.object(age, 'read_skills', read_after_edit):
            ok, job, _ = _execute('actions', fake)
        self.assertFalse(ok)
        self.assertEqual(fake.written_state()['heartbeats']['actions']['result'], 'failed')
        entry = fake.written_state()['results']['zz-canary']
        self.assertIsNone(entry['actions_hash'])
        self.assertFalse(entry['actions_ok'])
        with open(skill_md, 'w') as fh:
            fh.write(original)
        self.assertEqual(age.content_hash(self.folder), self.before)
        _execute('mac', fake, cli_run=FakeCli())
        entry = fake.written_state()['results']['zz-canary']
        self.assertEqual(entry['mac_hash'], self.before)
        self.assertFalse(age.eligible(entry, NOW))

    def test_mac_half_records_the_hash_when_nothing_changed(self):
        """FRS-9: with no edit during the checks, the Mac half records the hash it took first."""
        fake = FakeGitHub()
        _execute('mac', fake, cli_run=FakeCli())
        self.assertEqual(fake.written_state()['results']['zz-canary']['mac_hash'], self.before)

    def test_actions_half_does_not_record_a_hash_it_did_not_check(self):
        """FRS-9: a skill edited while the Actions checks run gets no actions_hash and fails."""
        fake = FakeGitHub()
        with mock.patch.object(checks, 'check_frontmatter', self._mutating(checks.check_frontmatter)):
            _execute('actions', fake)
        entry = fake.written_state()['results']['zz-canary']
        self.assertIsNone(entry['actions_hash'])
        self.assertFalse(entry['actions_ok'])


class DuplicateNameTests(unittest.TestCase):
    """FRS-9: one name under both `skills/` and `.held/` must not let one folder's hash stand for the other."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.live = os.path.join(tmp, 'skills', 'zz-canary')
        self.held = os.path.join(tmp, '.held', 'zz-canary')
        shutil.copytree(CANARY_FOLDER, self.live)
        shutil.copytree(CANARY_FOLDER, self.held)
        with open(os.path.join(self.held, 'SKILL.md'), 'a') as fh:
            fh.write('\nheld copy\n')
        self.held_hash = age.content_hash(self.held)
        self.assertNotEqual(age.content_hash(self.live), self.held_hash)
        skills = mock.patch.object(runmod.Run, '_skills', lambda job: [
            ('skills', 'zz-canary', self.live), ('.held', 'zz-canary', self.held)])
        skills.start()
        self.addCleanup(skills.stop)

    def _assert_refused(self, half, **kwargs):
        fake = FakeGitHub()
        ok, job, _ = _execute(half, fake, **kwargs)
        self.assertFalse(ok)
        self.assertTrue(any('more than one section: zz-canary' in e for e in job.errors), job.errors)
        entry = fake.written_state()['results'].get('zz-canary', {})
        self.assertNotEqual(entry.get(half + '_hash'), self.held_hash)
        self.assertFalse(entry.get(half + '_ok'))

    def test_mac_half_refuses_a_name_in_both_sections(self):
        """FRS-9: the Mac half fails a name found under skills/ and .held/ and records no held hash."""
        self._assert_refused('mac', cli_run=FakeCli())

    def test_actions_half_refuses_a_name_in_both_sections(self):
        """FRS-9: the Actions half fails a name found under skills/ and .held/ and records no held hash."""
        self._assert_refused('actions')


if __name__ == '__main__':
    unittest.main()
