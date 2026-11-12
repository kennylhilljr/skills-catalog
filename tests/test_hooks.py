"""Tests for tools/install-hooks and tools/local_checks.py (CAT-10, CAT-19).

Each test works in a temporary git repository with HOME pointing at a
temporary folder, GIT_CONFIG_NOSYSTEM=1 and no inherited git configuration.
Every value that looks like a secret or a private name is built at runtime
by concatenation, so this file itself scans clean (the repository is public).
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, 'tools')
INSTALLER = os.path.join(TOOLS, 'install-hooks')
MARKER = '# installed by skills-catalog tools/install-hooks'

AT = chr(64)
NL = chr(10)
HIDDEN = 'zz-' + 'hidden-skill'
OTHER_HIDDEN = 'zz-' + 'secret-thing'
GH_TOKEN = 'gh' + 'p_' + 'A1b2C3d4' * 5
PRIVATE_REPO = 'zz-' + 'private-repo'
FOREIGN = '#!/bin/sh' + NL + 'exit 0' + NL
SKILLS_ENV = 'SKILLS_CATALOG_EXCLUDE_FILE'


class HookTestCase(unittest.TestCase):
    """A fresh repository with a copy of tools/ and an isolated environment."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='hooks test ')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, 'repo with space')
        os.makedirs(self.repo)
        self.exclude = os.path.join(self.home, 'exclude.txt')
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith('GIT_') and key != SKILLS_ENV
        }
        self.env.update(
            {
                'HOME': self.home,
                'XDG_CONFIG_HOME': os.path.join(self.home, '.config'),
                'GIT_CONFIG_NOSYSTEM': '1',
                'GIT_CONFIG_GLOBAL': os.devnull,
                'GIT_AUTHOR_NAME': 't',
                'GIT_AUTHOR_EMAIL': 't' + AT + 'example.invalid',
                'GIT_COMMITTER_NAME': 't',
                'GIT_COMMITTER_EMAIL': 't' + AT + 'example.invalid',
                SKILLS_ENV: self.exclude,
            }
        )
        self.assertEqual(self.git('init', '-q').returncode, 0)
        shutil.copytree(TOOLS, os.path.join(self.repo, 'tools'))

    # helpers

    def git(self, *args, cwd=None):
        return subprocess.run(
            ['git', '-C', cwd or self.repo] + list(args),
            capture_output=True,
            text=True,
            env=self.env,
        )

    def install(self, repo=None):
        return subprocess.run(
            [sys.executable, INSTALLER, '--repo', repo or self.repo],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def checks(self):
        return subprocess.run(
            [sys.executable, os.path.join(self.repo, 'tools', 'local_checks.py')],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.repo,
        )

    def write(self, rel, content):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as handle:
            handle.write(content)

    def write_exclude(self, content, path=None):
        with open(path or self.exclude, 'w') as handle:
            handle.write(content)

    def commit(self, message):
        self.git('add', '-A')
        result = self.git('commit', '-q', '-m', message)
        return result, result.stdout + result.stderr

    def hook_path(self, name, repo=None):
        return os.path.join(repo or self.repo, '.git', 'hooks', name)



class InstallerTest(HookTestCase):
    def test_installs_marked_executable_hooks(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('pre-commit', 'pre-push'):
            path = self.hook_path(name)
            self.assertTrue(os.access(path, os.X_OK), name)
            with open(path) as handle:
                lines = handle.read().splitlines()
            self.assertIn(MARKER, lines)
            self.assertTrue(lines[0].startswith('#!'))

    def test_rerun_is_idempotent_and_updates_own_hooks(self):
        self.assertEqual(self.install().returncode, 0)
        path = self.hook_path('pre-commit')
        with open(path) as handle:
            fresh = handle.read()
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh' + NL + MARKER + NL + 'exit 0' + NL)
        os.chmod(path, 0o644)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(path) as handle:
            self.assertEqual(handle.read(), fresh)
        self.assertTrue(os.access(path, os.X_OK))

    def test_foreign_hook_left_unchanged(self):
        path = self.hook_path('pre-commit')
        with open(path, 'w') as handle:
            handle.write(FOREIGN)
        os.chmod(path, 0o755)
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('pre-commit', result.stderr)
        with open(path) as handle:
            self.assertEqual(handle.read(), FOREIGN)
        # The other hook is still installed.
        with open(self.hook_path('pre-push')) as handle:
            self.assertIn(MARKER, handle.read().splitlines())

    def test_marker_must_be_a_whole_line(self):
        path = self.hook_path('pre-push')
        content = '#!/bin/sh' + NL + 'echo "' + MARKER + '"' + NL
        with open(path, 'w') as handle:
            handle.write(content)
        self.assertEqual(self.install().returncode, 1)
        with open(path) as handle:
            self.assertEqual(handle.read(), content)

    def test_honours_core_hooks_path_relative_to_repo(self):
        self.assertEqual(self.git('config', 'core.hooksPath', 'my hooks').returncode, 0)
        sub = os.path.join(self.repo, 'sub')
        os.makedirs(sub)
        result = self.install(repo=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('pre-commit', 'pre-push'):
            self.assertTrue(os.access(os.path.join(self.repo, 'my hooks', name), os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(sub, 'my hooks')))

    def test_not_a_repository(self):
        plain = os.path.join(self.tmp, 'plain')
        os.makedirs(plain)
        self.env['GIT_CEILING_DIRECTORIES'] = self.tmp
        result = self.install(repo=plain)
        self.assertEqual(result.returncode, 2)


class PreCommitTest(HookTestCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install().returncode, 0)

    def test_excluded_name_any_case_blocked_without_name(self):
        self.write_exclude('# owner list' + NL + HIDDEN + NL)
        self.write('notes.md', 'fine' + NL + 'mentions ' + HIDDEN.upper() + ' here' + NL)
        result, out = self.commit('one')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('notes.md:2: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())
        self.assertNotIn('mentions', out)

    def test_token_blocked_by_scrub(self):
        self.write('notes.md', 'token ' + GH_TOKEN + NL)
        result, out = self.commit('two')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('notes.md:1: github-token', out)
        self.assertNotIn(GH_TOKEN, out)

    def test_clean_commit_passes(self):
        self.write_exclude(HIDDEN + NL)
        self.write('notes.md', 'clean' + NL)
        result, out = self.commit('three')
        self.assertEqual(result.returncode, 0, out)

    def test_both_checks_report_together(self):
        self.write_exclude(HIDDEN + NL)
        self.write('a.md', HIDDEN + NL)
        self.write('b.md', GH_TOKEN + NL)
        result, out = self.commit('both')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('a.md:1: excluded skill name', out)
        self.assertIn('b.md:1: github-token', out)


class PrePushTest(HookTestCase):
    def test_push_blocked_then_allowed(self):
        remote = os.path.join(self.tmp, 'remote.git')
        self.assertEqual(
            subprocess.run(
                ['git', 'init', '-q', '--bare', remote], env=self.env, capture_output=True
            ).returncode,
            0,
        )
        self.write('notes.md', 'clean' + NL)
        result, out = self.commit('base')
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(self.install().returncode, 0)
        self.write_exclude(HIDDEN + NL)
        # Committed with --no-verify, so only the pre-push hook stands guard.
        self.write('notes.md', 'x' + NL + HIDDEN + NL)
        self.git('add', '-A')
        self.assertEqual(self.git('commit', '-q', '--no-verify', '-m', 'leak').returncode, 0)
        push = self.git('push', '-q', remote, 'HEAD:refs/heads/main')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn('notes.md:2: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())
        listed = subprocess.run(
            ['git', '--git-dir', remote, 'rev-parse', '--verify', '-q', 'refs/heads/main'],
            capture_output=True,
            env=self.env,
        )
        self.assertNotEqual(listed.returncode, 0, 'the blocked push reached the remote')

        self.write('notes.md', 'clean again' + NL)
        result, out = self.commit('fix')
        self.assertEqual(result.returncode, 0, out)
        push = self.git('push', '-q', remote, 'HEAD:refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)


class ExclusionCheckTest(HookTestCase):
    def test_missing_list_skips(self):
        self.write('notes.md', HIDDEN + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('exclusion check: skipped', result.stdout)

    def test_default_list_under_home(self):
        del self.env[SKILLS_ENV]
        default = os.path.join(self.home, '.config', 'skills-catalog', 'exclude.txt')
        os.makedirs(os.path.dirname(default))
        self.write_exclude(HIDDEN + NL, path=default)
        self.write('notes.md', HIDDEN + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('notes.md:1: excluded skill name', result.stdout)

    def test_override_wins_over_default(self):
        default = os.path.join(self.home, '.config', 'skills-catalog', 'exclude.txt')
        os.makedirs(os.path.dirname(default))
        self.write_exclude(HIDDEN + NL, path=default)
        self.write_exclude(OTHER_HIDDEN + NL)
        self.write('notes.md', HIDDEN + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_comments_and_blanks_ignored(self):
        self.write_exclude(
            '# a comment with ' + 'notes' + NL + NL + '   ' + NL + '  ' + HIDDEN + '  # trailing' + NL
        )
        self.write('notes.md', 'just notes' + NL + 'a comment' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write('notes.md', 'x' + NL + 'y' + NL + 'see ' + HIDDEN + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('notes.md:3: excluded skill name', result.stdout)

    def test_only_comments_is_clean(self):
        self.write_exclude('# nothing here' + NL)
        self.write('notes.md', 'anything' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_fixed_string_matching(self):
        self.write_exclude('a.c' + NL + 'x*' + NL)
        self.write('notes.md', 'abc' + NL + 'xxxx' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.write('notes.md', 'abc' + NL + 'A.C' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('notes.md:2: excluded skill name', result.stdout)
        self.assertNotIn('notes.md:1:', result.stdout)

    def test_reads_index_not_working_tree(self):
        self.write_exclude(HIDDEN + NL)
        self.write('notes.md', HIDDEN + NL)
        self.git('add', '-A')
        self.write('notes.md', 'clean in the working tree' + NL)
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('notes.md:1: excluded skill name', result.stdout)

        self.git('add', '-A')
        self.write('notes.md', HIDDEN + NL)
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_untracked_file_not_checked(self):
        self.write_exclude(HIDDEN + NL)
        self.write('scratch.md', HIDDEN + NL)
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_name_in_path_counted_not_printed(self):
        self.write_exclude(HIDDEN + NL)
        self.write(os.path.join('skills', HIDDEN, 'notes.md'), 'clean' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('1 staged path(s) contain an excluded skill name', result.stdout)
        self.assertNotIn('hidden', (result.stdout + result.stderr).lower())

    def test_name_in_path_and_content_redacted(self):
        self.write_exclude(HIDDEN + NL)
        rel = os.path.join('skills', HIDDEN.upper(), 'SKILL.md')
        self.write(rel, 'name: ' + HIDDEN + NL)
        self.write(os.path.join('skills', HIDDEN, 'leak.md'), GH_TOKEN + NL)
        self.git('add', '-A')
        result = self.checks()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1)
        self.assertIn('skills/[excluded]/SKILL.md:1: excluded skill name', out)
        self.assertIn('skills/[excluded]/leak.md:1: github-token', out)
        self.assertNotIn('hidden', out.lower())

    def test_unreadable_list_is_an_error_without_contents(self):
        with open(self.exclude, 'wb') as handle:
            handle.write(b'\xff\xfe' + HIDDEN.encode('ascii'))
        self.write('notes.md', 'x' + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('hidden', (result.stdout + result.stderr).lower())


class ScrubCheckTest(HookTestCase):
    def test_default_private_repo_list_is_loaded(self):
        listed = os.path.join(self.home, '.config', 'skills-catalog', 'private-repos.txt')
        os.makedirs(os.path.dirname(listed))
        with open(listed, 'w') as handle:
            handle.write('# private' + NL + PRIVATE_REPO + NL)
        # Private names count under skills/ and .held/ only, as in CI.
        self.write('skills/demo/notes.md', 'see ' + PRIVATE_REPO + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('skills/demo/notes.md:1: private-repo', result.stdout)
        self.assertNotIn(PRIVATE_REPO, result.stdout + result.stderr)

    def test_scrub_runs_when_exclusions_found(self):
        self.write_exclude(HIDDEN + NL)
        self.write('a.md', HIDDEN + NL)
        self.write('b.md', GH_TOKEN + NL)
        self.git('add', '-A')
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('a.md:1: excluded skill name', result.stdout)
        self.assertIn('b.md:1: github-token', result.stdout)

    def test_runs_from_a_subdirectory(self):
        self.write('docs/x.md', GH_TOKEN + NL)
        self.git('add', '-A')
        result = subprocess.run(
            [sys.executable, os.path.join(self.repo, 'tools', 'local_checks.py')],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=os.path.join(self.repo, 'docs'),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('docs/x.md:1: github-token', result.stdout)


class RepositoryFilesTest(unittest.TestCase):
    def test_installer_is_executable_in_git(self):
        self.assertTrue(os.stat(INSTALLER).st_mode & stat.S_IXUSR)

    def test_agents_md_points_at_the_installer(self):
        with open(os.path.join(REPO, 'AGENTS.md'), encoding='utf-8') as handle:
            text = handle.read()
        checks = text.split('## Checks', 1)[1].split('\n## ', 1)[0]
        self.assertIn('python3 tools/install-hooks', checks)
        self.assertIn('once per clone', checks)
        self.assertNotIn('git grep -niF -f -', checks)
        self.assertNotIn('EXCLUSION CHECK FAILED', checks)
        # The other checks stay.
        self.assertIn('python3 tools/inventory.py --help', checks)
        self.assertIn('git diff --check', checks)


if __name__ == '__main__':
    unittest.main()
