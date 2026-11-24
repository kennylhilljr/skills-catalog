"""Regression tests for FRS-9: both halves record the content hash of each skill they checked."""
import os
import types
import unittest
from unittest import mock

from freshness import age
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


if __name__ == '__main__':
    unittest.main()
