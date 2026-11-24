"""Regression tests for FRS-9: an Actions pass pairs only with a Mac pass of the same content."""

import os
import re
import tempfile
import unittest

from freshness import age
from tests.test_freshness_age import NOW, TODAY, Repo, _ok, _only


class TestEligibleHashes(unittest.TestCase):
    def test_mismatched_hashes_not_eligible(self):
        """FRS-9: a Mac pass of other content, or a missing hash, makes a skill not eligible."""
        self.assertIs(age.eligible(_ok(mac_hash='b' * 64), NOW), False)
        self.assertIs(age.eligible(_ok(mac_hash=None), NOW), False)
        self.assertIs(age.eligible(_ok(actions_hash=None, mac_hash=None), NOW), False)
        self.assertIs(age.eligible(_ok(), NOW), True)


class TestAdvanceHashes(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(stale=['alpha'])

    def tearDown(self):
        self.repo.close()

    def test_mac_checked_other_content_not_advanced(self):
        """FRS-9: an Actions pass is not paired with a Mac pass of different content."""
        # The Mac passed X at commit A, X changed at commit B, Actions passed X at B.
        results = {'alpha': _ok(mac_hash='b' * 64)}
        self.assertEqual(age.advance_actions(results, TODAY, now=NOW, root=self.repo.root), [])
        control = age.advance_actions({'alpha': _ok()}, TODAY, now=NOW, root=self.repo.root)
        self.assertEqual(len(_only(control, 'open_pr')), 1)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)


class TestContentHash(unittest.TestCase):
    def test_hash_follows_content_not_location(self):
        """FRS-9: content_hash depends on the folder's content, not its location."""
        first = tempfile.TemporaryDirectory()
        second = tempfile.TemporaryDirectory()
        self.addCleanup(first.cleanup)
        self.addCleanup(second.cleanup)
        folders = []
        for tmp in (first.name, second.name):
            folder = os.path.join(tmp, 'skill')
            _write(os.path.join(folder, 'SKILL.md'), b'---\nname: skill\n---\n')
            _write(os.path.join(folder, 'scripts', 'run.sh'), b'echo hi\n')
            folders.append(folder)
        one, two = (age.content_hash(folder) for folder in folders)
        self.assertEqual(one, two)
        self.assertTrue(re.fullmatch('[0-9a-f]{64}', one))
        _write(os.path.join(folders[1], 'scripts', 'run.sh'), b'echo ho\n')
        self.assertNotEqual(age.content_hash(folders[0]), age.content_hash(folders[1]))
        self.assertIsNone(age.content_hash(os.path.join(first.name, 'missing')))


if __name__ == '__main__':
    unittest.main()
