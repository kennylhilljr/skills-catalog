"""Tests for tools/install-local (MCP-9, INV-8, CAT-20).

Each test builds a temporary git repository with its own catalog.json and
tags, and passes temporary destination, state and exclusion paths, so the
real home folder is never touched. The excluded fixture name is built at
runtime, so this file itself scans clean (the repository is public).
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, 'tools', 'install-local')
AT = chr(64)
HIDDEN = 'zz-' + 'hidden-' + 'fixture'


def sha(data):
    return hashlib.sha256(data).hexdigest()


class InstallLocalCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='install local ')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.claude = os.path.join(self.tmp, 'claude')
        self.agents = os.path.join(self.tmp, 'agents')
        self.state = os.path.join(self.tmp, 'state', 'installed.json')
        self.exclude = os.path.join(self.tmp, 'exclude.txt')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update({
            'HOME': self.home,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_AUTHOR_NAME': 't',
            'GIT_AUTHOR_EMAIL': 't' + AT + 'example.invalid',
            'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't' + AT + 'example.invalid',
        })
        self.git('init', '-q')
        self.skills = {
            'alpha': {'SKILL.md': b'alpha v1\n', 'references/notes.md': b'notes\n'},
            'beta': {'SKILL.md': b'beta v1\n', 'scripts/run.sh': b'#!/bin/sh\necho hi\n'},
        }
        self.held = {'sdd-held': {'SKILL.md': b'held\n'}, 'zz-canary': {'SKILL.md': b'canary\n'}}
        self.executable = {('beta', 'scripts/run.sh')}

    # ------------------------------------------------------------ helpers

    def git(self, *args):
        return subprocess.run(['git', '-C', self.repo] + list(args), env=self.env,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit_tag(self, tag, manifest_override=None):
        """Write skills, .held and catalog.json, commit and tag."""
        for section in ('skills', '.held'):
            shutil.rmtree(os.path.join(self.repo, section), ignore_errors=True)
        entries = []
        for section, group, key in (('skills', self.skills, 'skills'), ('.held', self.held, 'held')):
            for name in sorted(group):
                files = group[name]
                for rel, data in files.items():
                    path = os.path.join(self.repo, section, name, *rel.split('/'))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'wb') as handle:
                        handle.write(data)
                    if (name, rel) in self.executable:
                        os.chmod(path, 0o755)
                entries.append((key, {'name': name, 'path': section + '/' + name,
                                      'files': {r: sha(d) for r, d in sorted(files.items())}}))
        catalog = {'version': 1,
                   'skills': [e for k, e in entries if k == 'skills'],
                   'held': [e for k, e in entries if k == 'held' and e['name'] != 'zz-canary']}
        if manifest_override:
            manifest_override(catalog)
        with open(os.path.join(self.repo, 'catalog.json'), 'w') as handle:
            json.dump(catalog, handle, indent=2)
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', tag)
        self.git('tag', tag)

    def run_tool(self, *extra, ref='v1', defaults=False):
        args = [sys.executable, '-B', TOOL, '--repo', self.repo]
        if ref is not None:
            args += ['--ref', ref]
        if not defaults:
            args += ['--claude-dir', self.claude, '--agents-dir', self.agents,
                     '--state', self.state, '--exclude-file', self.exclude]
        return subprocess.run(args + list(extra), env=self.env, capture_output=True, text=True)

    def snapshot(self):
        """Every path under the temp root except the repository, with bytes."""
        found = {}
        for base, dirs, files in os.walk(self.tmp):
            if base.startswith(self.repo):
                continue
            for name in dirs + files:
                full = os.path.join(base, name)
                if full.startswith(self.repo):
                    continue
                data = None
                if os.path.isfile(full) and not os.path.islink(full):
                    with open(full, 'rb') as handle:
                        data = handle.read()
                found[full] = data
        return found

    def assert_installed(self, dest, skills):
        self.assertEqual(sorted(os.listdir(dest)), sorted(skills))
        for name, files in skills.items():
            got = {}
            for base, dirs, names in os.walk(os.path.join(dest, name)):
                for entry in dirs + names:
                    self.assertFalse(os.path.islink(os.path.join(base, entry)))
                for entry in names:
                    full = os.path.join(base, entry)
                    rel = os.path.relpath(full, os.path.join(dest, name)).replace(os.sep, '/')
                    with open(full, 'rb') as handle:
                        got[rel] = handle.read()
                    want = 0o755 if (name, rel) in self.executable else 0o644
                    self.assertEqual(stat.S_IMODE(os.lstat(full).st_mode), want, full)
            self.assertEqual(got, files)

    # ------------------------------------------------------------ tests

    def test_fresh_install_copies_exactly_the_skills(self):
        self.commit_tag('v1')
        run = self.run_tool()
        self.assertEqual(run.returncode, 0, run.stderr)
        for dest in (self.claude, self.agents):
            self.assert_installed(dest, self.skills)
            self.assertNotIn('sdd-held', os.listdir(dest))
            self.assertNotIn('zz-canary', os.listdir(dest))
        with open(self.state) as handle:
            state = json.load(handle)
        entry = state['destinations'][self.claude]['alpha']
        self.assertEqual(entry['tag'], 'v1')
        self.assertEqual(entry['files']['SKILL.md'], sha(b'alpha v1\n'))

    def test_foreign_folder_is_refused_and_nothing_written(self):
        self.commit_tag('v1')
        os.makedirs(os.path.join(self.claude, 'beta'))
        with open(os.path.join(self.claude, 'beta', 'SKILL.md'), 'w') as handle:
            handle.write('mine')
        before = self.snapshot()
        run = self.run_tool()
        self.assertEqual(run.returncode, 1)
        self.assertIn(os.path.join(self.claude, 'beta')
                      + ': refusing to overwrite (not installed by install-local)', run.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.agents))
        self.assertFalse(os.path.exists(self.state))

    def test_edited_installed_file_is_refused(self):
        self.commit_tag('v1')
        self.assertEqual(self.run_tool().returncode, 0)
        edited = os.path.join(self.agents, 'alpha', 'SKILL.md')
        with open(edited, 'w') as handle:
            handle.write('local edit')
        self.skills['alpha']['SKILL.md'] = b'alpha v2\n'
        self.commit_tag('v2')
        before = self.snapshot()
        run = self.run_tool(ref='v2')
        self.assertEqual(run.returncode, 1)
        self.assertIn(os.path.join(self.agents, 'alpha')
                      + ': refusing to overwrite (changed since install)', run.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_upgrade_updates_and_removes_unchanged(self):
        self.commit_tag('v1')
        self.assertEqual(self.run_tool().returncode, 0)
        self.skills['alpha']['SKILL.md'] = b'alpha v2\n'
        del self.skills['beta']
        self.skills['gamma'] = {'SKILL.md': b'gamma\n'}
        self.commit_tag('v2')
        run = self.run_tool(ref='v2')
        self.assertEqual(run.returncode, 0, run.stderr)
        for dest in (self.claude, self.agents):
            self.assert_installed(dest, self.skills)
        self.assertEqual([n for n in os.listdir(self.claude) if n.startswith('.')], [])
        with open(self.state) as handle:
            state = json.load(handle)
        self.assertEqual(sorted(state['destinations'][self.claude]), ['alpha', 'gamma'])
        self.assertEqual(state['destinations'][self.claude]['alpha']['tag'], 'v2')

    def test_removed_skill_that_changed_is_left_in_place(self):
        self.commit_tag('v1')
        self.assertEqual(self.run_tool().returncode, 0)
        with open(os.path.join(self.claude, 'beta', 'SKILL.md'), 'w') as handle:
            handle.write('local edit')
        del self.skills['beta']
        self.commit_tag('v2')
        run = self.run_tool(ref='v2')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(os.path.join(self.claude, 'beta') + ': left in place', run.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.claude, 'beta')))
        self.assertFalse(os.path.exists(os.path.join(self.agents, 'beta')))

    def test_reinstall_same_tag_is_a_noop(self):
        self.commit_tag('v1')
        self.assertEqual(self.run_tool().returncode, 0)
        before = self.snapshot()
        mtimes = {p: os.lstat(p).st_mtime_ns for p in before}
        run = self.run_tool()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('already installed', run.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({p: os.lstat(p).st_mtime_ns for p in before}, mtimes)

    def test_branch_and_sha_refs_exit_2(self):
        self.commit_tag('v1')
        head = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        branch = self.git('symbolic-ref', '--short', 'HEAD').stdout.decode().strip()
        for ref in (branch, head):
            run = self.run_tool(ref=ref)
            self.assertEqual(run.returncode, 2, ref)
            self.assertIn('ref is not a tag', run.stderr)
        self.assertFalse(os.path.exists(self.claude))

    def test_manifest_mismatch_installs_nothing(self):
        def corrupt(catalog):
            catalog['skills'][1]['files']['SKILL.md'] = sha(b'something else')
        self.commit_tag('v1', manifest_override=corrupt)
        run = self.run_tool()
        self.assertEqual(run.returncode, 1)
        self.assertIn('sha256 does not match', run.stderr)
        self.assertFalse(os.path.exists(self.claude))
        self.assertFalse(os.path.exists(self.agents))
        self.assertFalse(os.path.exists(self.state))

    def test_symlink_in_the_tag_is_refused(self):
        self.commit_tag('v1')
        os.symlink('SKILL.md', os.path.join(self.repo, 'skills', 'alpha', 'link.md'))
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'link')
        self.git('tag', 'v2')
        run = self.run_tool(ref='v2')
        self.assertEqual(run.returncode, 1)
        self.assertIn('symlink', run.stderr)
        self.assertFalse(os.path.exists(self.claude))
        self.assertFalse(os.path.exists(self.state))

    def test_excluded_skill_is_skipped_silently(self):
        self.skills[HIDDEN] = {'SKILL.md': b'private\n'}
        self.commit_tag('v1')
        with open(self.exclude, 'w') as handle:
            handle.write('# list\n' + HIDDEN.upper() + '  # case does not matter\n')
        run = self.run_tool()
        self.assertEqual(run.returncode, 0, run.stderr)
        for dest in (self.claude, self.agents):
            self.assertNotIn(HIDDEN, os.listdir(dest))
            self.assertIn('alpha', os.listdir(dest))
        self.assertNotIn(HIDDEN, run.stdout.lower() + run.stderr.lower())
        with open(self.state) as handle:
            self.assertNotIn(HIDDEN, handle.read())
        dry = self.run_tool('--dry-run')
        self.assertNotIn(HIDDEN, dry.stdout.lower() + dry.stderr.lower())

    def test_dry_run_writes_nothing(self):
        self.commit_tag('v1')
        os.makedirs(os.path.join(self.agents, 'alpha'))
        before = self.snapshot()
        run = self.run_tool('--dry-run')
        self.assertEqual(run.returncode, 1)
        self.assertIn('install ' + os.path.join(self.claude, 'alpha'), run.stdout)
        self.assertIn(os.path.join(self.agents, 'alpha') + ': refusing', run.stderr)
        shutil.rmtree(os.path.join(self.agents, 'alpha'))
        before = self.snapshot()
        run = self.run_tool('--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('install ' + os.path.join(self.agents, 'beta'), run.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.claude))
        self.assertFalse(os.path.exists(self.state))

    def test_defaults_resolve_under_home(self):
        self.commit_tag('v1')
        self.skills['alpha']['SKILL.md'] = b'alpha v2\n'
        self.commit_tag('v2')
        os.makedirs(os.path.join(self.home, '.config', 'skills-catalog'))
        with open(os.path.join(self.home, '.config', 'skills-catalog', 'exclude.txt'), 'w') as handle:
            handle.write('beta\n')
        run = self.run_tool(ref=None, defaults=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        skills = {'alpha': self.skills['alpha']}
        self.assert_installed(os.path.join(self.home, '.claude', 'skills'), skills)
        self.assert_installed(os.path.join(self.home, '.agents', 'skills'), skills)
        with open(os.path.join(self.home, '.config', 'skills-catalog', 'installed.json')) as handle:
            state = json.load(handle)
        claude_dest = os.path.join(self.home, '.claude', 'skills')
        self.assertEqual(state['destinations'][claude_dest]['alpha']['tag'], 'v2')


if __name__ == '__main__':
    unittest.main()
