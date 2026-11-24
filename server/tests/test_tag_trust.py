"""Reviewed-tag check: an annotated tag on origin/main, on by default for HTTP (DESIGN 4.4)."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock

from skills_catalog_mcp import __main__ as cli
from skills_catalog_mcp.snapshot import NO_MAIN_REF, NOT_REVIEWED, SnapshotError, materialize
from tests.fixtures import commit_all, git, make_catalog_repo, next_stamp, write_files

TAG = 'v2099.1.1.1'


class TagTrustTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.exclude = os.path.join(self.tmp, 'exclude.txt')

    def snap(self, repo, ref=None, **kwargs):
        snap = materialize(repo, ref, exclude_file=self.exclude, **kwargs)
        self.addCleanup(snap.cleanup)
        return snap

    def lightweight_repo(self):
        repo = make_catalog_repo(self.tmp, tag=None)
        git(repo, 'tag', TAG)
        return repo

    def unmerged_tag_repo(self):
        """An annotated tag on a local commit that origin/main does not contain."""
        repo = make_catalog_repo(self.tmp, tag=None)
        git(repo, 'checkout', '-q', '-b', 'local')
        write_files(repo, {'notes.txt': 'not on main\n'})
        commit_all(repo, 'local only', tag=TAG, stamp=next_stamp(1))
        return repo

    def test_annotated_tag_on_main_is_served(self):
        repo = make_catalog_repo(self.tmp)
        self.assertEqual(self.snap(repo, require_reviewed=True).tag, TAG)

    def test_tag_on_an_older_main_commit_is_served(self):
        repo = make_catalog_repo(self.tmp)
        commit = commit_all(repo, 'later', stamp=next_stamp(1))
        git(repo, 'update-ref', 'refs/remotes/origin/main', commit)
        self.assertEqual(self.snap(repo, TAG, require_reviewed=True).tag, TAG)

    def test_lightweight_tag_is_refused(self):
        repo = self.lightweight_repo()
        with self.assertRaisesRegex(SnapshotError, '^%s$' % NOT_REVIEWED):
            materialize(repo, exclude_file=self.exclude, require_reviewed=True)

    def test_tag_not_on_main_is_refused(self):
        repo = self.unmerged_tag_repo()
        with self.assertRaisesRegex(SnapshotError, '^%s$' % NOT_REVIEWED):
            materialize(repo, exclude_file=self.exclude, require_reviewed=True)

    def test_missing_main_ref_is_refused(self):
        repo = make_catalog_repo(self.tmp, origin_main=False)
        with self.assertRaisesRegex(SnapshotError, '^%s$' % NO_MAIN_REF):
            materialize(repo, exclude_file=self.exclude, require_reviewed=True)
        for ref in ('-h', '', '--all'):
            with self.subTest(ref=ref), self.assertRaisesRegex(SnapshotError, '^%s$' % NO_MAIN_REF):
                materialize(repo, exclude_file=self.exclude, require_reviewed=True, main_ref=ref)

    def test_main_ref_can_be_overridden(self):
        repo = self.unmerged_tag_repo()
        self.assertEqual(self.snap(repo, require_reviewed=True, main_ref='local').tag, TAG)

    def test_off_by_default_in_the_library(self):
        self.assertEqual(self.snap(self.lightweight_repo()).tag, TAG)

    def run_cli(self, repo, *extra):
        """Run the CLI with serve() stubbed; return (exit code, stderr, served)."""
        served = []
        err = io.StringIO()
        with unittest.mock.patch.object(cli.transport, 'serve', lambda mcp, args: served.append(args)), \
                contextlib.redirect_stderr(err):
            code = cli.main(['--repo', repo, '--exclude-file', self.exclude, *extra])
        return code, err.getvalue(), served

    def test_http_requires_a_reviewed_tag_by_default(self):
        for name, repo_of in (('lightweight', self.lightweight_repo), ('unmerged', self.unmerged_tag_repo)):
            with self.subTest(name):
                tmp = tempfile.mkdtemp(dir=self.tmp)
                with unittest.mock.patch.object(self, 'tmp', tmp):
                    repo = repo_of()
                code, err, served = self.run_cli(repo, '--transport', 'http')
                self.assertEqual(code, 2)
                self.assertEqual(err.strip().splitlines()[-1], 'error: ' + NOT_REVIEWED)
                self.assertNotIn(TAG, err)
                self.assertEqual(served, [])

    def test_http_serves_a_reviewed_tag(self):
        code, _err, served = self.run_cli(make_catalog_repo(self.tmp), '--transport', 'http')
        self.assertEqual((code, len(served)), (0, 1))

    def test_stdio_default_and_explicit_flags(self):
        repo = self.lightweight_repo()
        self.assertEqual(self.run_cli(repo)[0], 0)
        self.assertEqual(self.run_cli(repo, '--transport', 'http', '--tag-trust', 'any')[0], 0)
        code, err, served = self.run_cli(repo, '--tag-trust', 'reviewed')
        self.assertEqual((code, served), (2, []))
        self.assertIn('error: ' + NOT_REVIEWED, err)


if __name__ == '__main__':
    unittest.main()
