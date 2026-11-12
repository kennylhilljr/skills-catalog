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

    def stage_gitlink(self, rel, sha):
        """Stage a gitlink (mode 160000) at rel that points at commit sha."""
        staged = self.git('update-index', '--add', '--cacheinfo', '160000,%s,%s' % (sha, rel))
        self.assertEqual(staged.returncode, 0, staged.stderr)

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

        # Every pushed commit is checked, so the leak must leave the history.
        self.assertEqual(self.git('reset', '-q', '--hard', 'HEAD~1').returncode, 0)
        self.write('notes.md', 'clean again' + NL)
        result, out = self.commit('fix')
        self.assertEqual(result.returncode, 0, out)
        push = self.git('push', '-q', remote, 'HEAD:refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)


class PrePushRefsTest(HookTestCase):
    """pre-push checks the commits being pushed, not the index (CAT-10, CAT-19)."""

    def setUp(self):
        super().setUp()
        self.remote = os.path.join(self.tmp, 'remote.git')
        subprocess.run(['git', 'init', '-q', '--bare', self.remote], env=self.env, check=True)
        self.write('notes.md', 'clean' + NL)
        result, out = self.commit('base')
        self.assertEqual(result.returncode, 0, out)
        self.assertEqual(self.install().returncode, 0)
        self.git('remote', 'add', 'origin', self.remote)
        push = self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.git('fetch', '-q', 'origin')

    def leak_commit(self, rel, content, message='leak'):
        self.write(rel, content)
        self.git('add', '-A')
        self.assertEqual(self.git('commit', '-q', '--no-verify', '-m', message).returncode, 0)
        return self.git('rev-parse', 'HEAD').stdout.strip()

    def remote_has(self, ref, sha):
        got = subprocess.run(
            ['git', '--git-dir', self.remote, 'rev-parse', '--verify', '-q', ref],
            capture_output=True, text=True, env=self.env,
        )
        return got.stdout.strip() == sha

    def test_pushed_commit_checked_even_when_index_is_clean(self):
        self.write_exclude(HIDDEN + NL)
        sha = self.leak_commit('notes.md', 'x' + NL + HIDDEN + NL)
        # The index and working tree no longer hold the name; the commit does.
        self.write('notes.md', 'clean' + NL)
        self.git('add', '-A')
        push = self.git('push', '-q', 'origin', sha + ':refs/heads/main')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(sha[:7] + ':notes.md:2: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())
        self.assertFalse(self.remote_has('refs/heads/main', sha))

    def test_intermediate_commit_checked(self):
        first = self.leak_commit('b.md', 'token ' + GH_TOKEN + NL)
        tip = self.leak_commit('b.md', 'removed again' + NL, message='undo')
        push = self.git('push', '-q', 'origin', tip + ':refs/heads/main')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(first[:7] + ':b.md:1: github-token', out)
        self.assertNotIn(GH_TOKEN, out)

    def test_staged_leak_does_not_block_a_clean_push(self):
        self.write_exclude(HIDDEN + NL)
        sha = self.leak_commit('notes.md', 'clean and new' + NL, message='fine')
        self.write('scratch.md', HIDDEN + NL + GH_TOKEN + NL)
        self.git('add', '-A')
        push = self.git('push', '-q', 'origin', sha + ':refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertTrue(self.remote_has('refs/heads/main', sha))

    def test_clean_new_branch_push_passes(self):
        # A new branch whose whole history is clean pushes without findings.
        sha = self.leak_commit('more.md', 'clean too' + NL, message='more')
        push = self.git('push', '-q', 'origin', sha + ':refs/heads/topic')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)

    def test_new_branch_with_leak_blocked(self):
        sha = self.leak_commit('c.md', GH_TOKEN + NL)
        push = self.git('push', '-q', 'origin', sha + ':refs/heads/topic')
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(sha[:7] + ':c.md:1: github-token', push.stdout + push.stderr)

    def test_commit_message_checked_for_excluded_names(self):
        self.write_exclude(HIDDEN + NL)
        sha = self.leak_commit('d.md', 'clean' + NL, message='subject' + NL + NL + 'about ' + HIDDEN)
        push = self.git('push', '-q', 'origin', sha + ':refs/heads/main')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(sha[:7] + ':(commit message):3: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())

    def test_annotated_tag_message_checked(self):
        """A1: an excluded name in a pushed tag message blocks the push."""
        self.write_exclude(HIDDEN + NL)
        self.assertEqual(self.git('tag', '-a', 'v0', '-m', 'about ' + HIDDEN, 'HEAD').returncode, 0)
        tag = self.git('rev-parse', 'refs/tags/v0').stdout.strip()
        push = self.git('push', '-q', 'origin', 'refs/tags/v0')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(tag[:7] + ':(tag message):1: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())
        self.assertFalse(self.remote_has('refs/tags/v0', tag))

    def test_clean_annotated_tag_pushes(self):
        """A2: an annotated tag with a clean message pushes."""
        self.write_exclude(HIDDEN + NL)
        self.assertEqual(self.git('tag', '-a', 'v1', '-m', 'release notes', 'HEAD').returncode, 0)
        tag = self.git('rev-parse', 'refs/tags/v1').stdout.strip()
        push = self.git('push', '-q', 'origin', 'refs/tags/v1')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertTrue(self.remote_has('refs/tags/v1', tag))

    def test_nested_tag_inner_message_checked(self):
        """A3: the message of a tag that a pushed tag points to is checked."""
        self.write_exclude(HIDDEN + NL)
        self.assertEqual(self.git('tag', '-a', 'inner', '-m', 'about ' + HIDDEN, 'HEAD').returncode, 0)
        # A tag of a tag object; git may print a nested tag hint.
        self.assertEqual(
            self.git('tag', '-a', 'outer', '-m', 'clean outer', 'refs/tags/inner').returncode, 0
        )
        inner_id = self.git('rev-parse', 'refs/tags/inner').stdout.strip()
        outer_id = self.git('rev-parse', 'refs/tags/outer').stdout.strip()
        push = self.git('push', '-q', 'origin', 'refs/tags/outer')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0)
        self.assertIn(inner_id[:7] + ':(tag message):1: excluded skill name', out)
        self.assertNotIn(outer_id[:7] + ':(tag message)', out)
        self.assertNotIn('hidden', out.lower())
        self.assertFalse(self.remote_has('refs/tags/outer', outer_id))

    def test_deleting_a_ref_checks_nothing(self):
        sha = self.leak_commit('e.md', GH_TOKEN + NL)
        self.assertEqual(
            self.git('push', '-q', '--no-verify', 'origin', sha + ':refs/heads/old').returncode, 0
        )
        push = self.git('push', '-q', 'origin', ':refs/heads/old')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)

    # Stale remote-tracking refs (issue: tracking refs hid pushed commits).

    def retarget_origin(self):
        """Point origin at a new empty bare repository; origin/main stays stale."""
        second = os.path.join(self.tmp, 'second.git')
        subprocess.run(['git', 'init', '-q', '--bare', second], env=self.env, check=True)
        self.assertEqual(self.git('remote', 'set-url', 'origin', second).returncode, 0)
        return second

    def publish_leak(self, rel, content):
        """Commit and push a leak with --no-verify, so origin/main points at it."""
        sha = self.leak_commit(rel, content)
        push = self.git('push', '-q', '--no-verify', 'origin', 'HEAD:refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertEqual(self.git('fetch', '-q', 'origin').returncode, 0)
        self.assertEqual(self.git('rev-parse', 'origin/main').stdout.strip(), sha)
        return sha

    def missing_commit(self):
        """A commit id made in a separate repository, so this clone lacks it."""
        other = os.path.join(self.tmp, 'other')
        subprocess.run(['git', 'init', '-q', other], env=self.env, check=True)
        subprocess.run(
            ['git', '-C', other, 'commit', '-q', '--allow-empty', '-m', 'elsewhere'],
            env=self.env, check=True,
        )
        sha = subprocess.run(
            ['git', '-C', other, 'rev-parse', 'HEAD'],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertNotEqual(self.git('cat-file', '-e', sha + '^{commit}').returncode, 0)
        return sha

    def pre_push(self, stdin, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.repo, 'tools', 'local_checks.py'), '--pre-push']
            + list(args),
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.repo,
        )

    def second_has_main(self, second):
        got = subprocess.run(
            ['git', '--git-dir', second, 'rev-parse', '--verify', '-q', 'refs/heads/main'],
            capture_output=True, env=self.env,
        )
        return got.returncode == 0

    def test_stale_tracking_ref_does_not_hide_commits(self):
        self.write_exclude(HIDDEN + NL)
        sha = self.publish_leak('notes.md', 'x' + NL + HIDDEN + NL)
        second = self.retarget_origin()
        push = self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        out = push.stdout + push.stderr
        self.assertEqual(push.returncode, 1, out)
        self.assertIn(sha[:7] + ':notes.md:2: excluded skill name', out)
        self.assertNotIn('hidden', out.lower())
        self.assertFalse(self.second_has_main(second), 'the blocked push reached the remote')

    def test_stale_tracking_ref_does_not_hide_scrub_findings(self):
        sha = self.publish_leak('t.md', 'token ' + GH_TOKEN + NL)
        second = self.retarget_origin()
        push = self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        out = push.stdout + push.stderr
        self.assertEqual(push.returncode, 1, out)
        self.assertIn(sha[:7] + ':t.md:1: github-token', out)
        self.assertNotIn(GH_TOKEN, out)
        self.assertFalse(self.second_has_main(second), 'the blocked push reached the remote')

    def test_missing_remote_sha_ignores_tracking_refs(self):
        self.write_exclude(HIDDEN + NL)
        sha = self.publish_leak('notes.md', 'x' + NL + HIDDEN + NL)
        absent = self.missing_commit()
        stdin = 'refs/heads/main %s refs/heads/main %s%s' % (sha, absent, NL)
        for args in (('origin', self.remote), ()):
            with self.subTest(args=args):
                result = self.pre_push(stdin, *args)
                out = result.stdout + result.stderr
                self.assertEqual(result.returncode, 1, out)
                self.assertIn(sha[:7] + ':notes.md:2: excluded skill name', out)
                self.assertNotIn('hidden', out.lower())

    def test_gitlink_path_counted(self):
        # A gitlink has no blob: its path is counted and no content is read.
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.write_exclude(HIDDEN + NL)
        self.stage_gitlink('vendor/clean-sub', base)
        self.assertEqual(self.git('commit', '-q', '--no-verify', '-m', 'clean sub').returncode, 0)
        clean = self.git('rev-parse', 'HEAD').stdout.strip()
        push = self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.assertEqual(push.returncode, 0, push.stdout + push.stderr)
        self.assertTrue(self.remote_has('refs/heads/main', clean))

        # Committed directly, without the helper's add -A.
        self.stage_gitlink('vendor/' + HIDDEN.upper(), base)
        self.assertEqual(self.git('commit', '-q', '--no-verify', '-m', 'sub').returncode, 0)
        sha = self.git('rev-parse', 'HEAD').stdout.strip()
        push = self.git('push', '-q', 'origin', 'HEAD:refs/heads/main')
        out = push.stdout + push.stderr
        self.assertNotEqual(push.returncode, 0, out)
        self.assertIn('exclusion check: 1 pushed path(s) contain an excluded skill name', out)
        self.assertIn('exclusion check: 1 findings', out)
        self.assertIn('scrub: 0 findings', out)
        self.assertNotIn('hidden', out.lower())
        self.assertNotIn('local checks: error', out)
        self.assertFalse(self.remote_has('refs/heads/main', sha))
        self.assertTrue(self.remote_has('refs/heads/main', clean), 'the remote tip moved')

    def test_known_remote_sha_excludes_its_ancestry(self):
        self.write_exclude(HIDDEN + NL)
        leak = self.publish_leak('notes.md', 'x' + NL + HIDDEN + NL)
        tip = self.leak_commit('more.md', 'clean' + NL, message='more')
        stdin = 'refs/heads/main %s refs/heads/main %s%s' % (tip, leak, NL)
        result = self.pre_push(stdin, 'origin', self.remote)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('exclusion check: 0 findings', result.stdout)


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

    def test_gitlink_path_counted(self):
        # A gitlink has no blob: its path is counted and no content is read.
        self.write_exclude(HIDDEN + NL)
        self.write('notes.md', 'clean' + NL)
        result, out = self.commit('base')
        self.assertEqual(result.returncode, 0, out)
        base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.stage_gitlink('vendor/clean-sub', base)
        result = self.checks()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn('exclusion check: 0 findings', out)
        self.assertNotIn('local checks: error', out)

        self.stage_gitlink('vendor/' + HIDDEN, base)
        result = self.checks()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn('exclusion check: 1 staged path(s) contain an excluded skill name', out)
        self.assertIn('exclusion check: 1 findings', out)
        self.assertIn('scrub: 0 findings', out)
        self.assertNotIn('hidden', out.lower())
        self.assertNotIn('local checks: error', out)

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

    def test_read_error_path_redacted(self):
        self.write_exclude(HIDDEN + NL)
        rel = os.path.join('docs', HIDDEN + '.md')
        self.write(rel, 'clean' + NL)
        self.git('add', '-A')
        # Remove the staged blob's loose object, so reading it fails.
        oid = self.git('rev-parse', ':docs/' + HIDDEN + '.md').stdout.strip()
        os.remove(os.path.join(self.repo, '.git', 'objects', oid[:2], oid[2:]))
        result = self.checks()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2)
        self.assertIn('local checks: error: cannot read docs/[excluded].md', out)
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
    def test_scrub_reads_index_not_working_tree(self):
        self.write('notes.md', 'token ' + GH_TOKEN + NL)
        self.git('add', '-A')
        self.write('notes.md', 'clean in the working tree' + NL)
        result = self.checks()
        self.assertEqual(result.returncode, 1)
        self.assertIn('notes.md:1: github-token', result.stdout)

        self.git('add', '-A')
        self.write('notes.md', 'token ' + GH_TOKEN + NL)
        result = self.checks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
