#!/usr/bin/env python3
"""Unit tests for the catalog.json manifest builder."""

import builtins
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_catalog_path = os.path.join(_repo_root, 'tools', 'catalog.py')
_spec = importlib.util.spec_from_file_location('catalog_manifest_under_test', _catalog_path)
_catalog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_catalog)

KEYS = ['name', 'path', 'version', 'owner', 'targets', 'consumers', 'last_verified', 'files']
HELD = ['sdd-' + s for s in ['adopt', 'clarify', 'hld', 'implement', 'intake', 'lld',
                             'spec', 'tasks', 'triage', 'verify']]
STALE = 'catalog.json is out of date; run python3 tools/catalog.py\n'


def _copy_repo(tmp):
    for section in ('skills', '.held'):
        shutil.copytree(os.path.join(_repo_root, section), os.path.join(tmp, section))


def _cli(root, *args):
    return subprocess.run([sys.executable, _catalog_path, '--root', root] + list(args),
                          capture_output=True, text=True)


def _append(path, text='changed\n'):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)


def _write_skill(root, meta_lines, name='x'):
    folder = os.path.join(root, 'skills', name)
    os.makedirs(folder)
    lines = ['---', 'name: ' + name, 'description: Use when testing.', 'metadata:']
    lines += ['  ' + line for line in meta_lines]
    lines += ['---', '', 'Body.', '']
    with open(os.path.join(folder, 'SKILL.md'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines))
    os.makedirs(os.path.join(root, '.held'), exist_ok=True)


class TestCatalog(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, doc, name, section='skills'):
        return [e for e in doc[section] if e['name'] == name][0]

    def test_keys_and_hashes(self):
        """CAT-11: every entry has exactly the 8 keys and ears-notation digests match disk."""
        doc = _catalog.build(_repo_root)
        for entry in doc['skills'] + doc['held']:
            self.assertEqual(list(entry), KEYS, entry['name'])
        ears = self._entry(doc, 'ears-notation')
        self.assertEqual(sorted(ears['files']),
                         ['SKILL.md', 'examples/good-requirements.md', 'references/patterns.md'])
        for rel, digest in ears['files'].items():
            with open(os.path.join(_repo_root, ears['path'], rel), 'rb') as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), digest, rel)

    def test_canary_excluded(self):
        """FRS-19: .held/zz-canary never appears in the manifest."""
        _copy_repo(self.tmp)
        canary = os.path.join(self.tmp, '.held', 'zz-canary')
        os.makedirs(canary, exist_ok=True)  # the real canary is copied too (#71)
        shutil.copy(os.path.join(_repo_root, '.held', 'sdd-adopt', 'SKILL.md'), canary)
        held = [e['name'] for e in _catalog.build(self.tmp)['held']]
        self.assertNotIn('zz-canary', held)
        self.assertEqual(held, [e['name'] for e in _catalog.build(_repo_root)['held']])

    def test_check_cycle(self):
        """CAT-11: --check passes after generating, fails after an edit, passes after regenerating."""
        _copy_repo(self.tmp)
        self.assertEqual(_cli(self.tmp).returncode, 0)
        self.assertEqual(_cli(self.tmp, '--check').returncode, 0)
        _append(os.path.join(self.tmp, 'skills', 'new-mcp', 'SKILL.md'))
        r = _cli(self.tmp, '--check')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, STALE)
        self.assertEqual(_cli(self.tmp).returncode, 0)
        self.assertEqual(_cli(self.tmp, '--check').returncode, 0)

    def test_check_missing_file(self):
        """CAT-11: a missing catalog.json counts as out of date."""
        _copy_repo(self.tmp)
        r = _cli(self.tmp, '--check')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, STALE)

    def test_committed_file_current(self):
        """CAT-11: the committed catalog.json matches a fresh build."""
        with open(os.path.join(_repo_root, 'catalog.json'), encoding='utf-8', newline='') as handle:
            committed = handle.read()
        self.assertEqual(committed, json.dumps(_catalog.build(_repo_root), indent=2) + '\n')

    def test_held_array_and_consumers(self):
        """CAT-23: held lists the 10 sdd-* skills with consumers none; skills/ entries are all."""
        doc = _catalog.build(_repo_root)
        self.assertEqual([e['name'] for e in doc['held']], HELD)
        self.assertEqual({e['consumers'] for e in doc['held']}, {'none'})
        self.assertEqual({e['consumers'] for e in doc['skills']}, {'all'})
        self.assertEqual([e['path'] for e in doc['held']], ['.held/' + n for n in HELD])

    def test_check_covers_held(self):
        """CAT-23: --check fails when a held skill file changes."""
        _copy_repo(self.tmp)
        self.assertEqual(_cli(self.tmp).returncode, 0)
        _append(os.path.join(self.tmp, '.held', 'sdd-adopt', 'SKILL.md'))
        r = _cli(self.tmp, '--check')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, STALE)

    def test_targets(self):
        """CAT-11: catalog-targets splits on commas, strips, drops empties and keeps order."""
        cases = [
            (['catalog-targets: "claude,codex"'], ['claude', 'codex']),
            (['catalog-targets: " claude , ,codex "'], ['claude', 'codex']),
            (['catalog-targets: "codex,claude"'], ['codex', 'claude']),
            (['catalog-targets: ""'], []),
            (['catalog-version: "1.0.0"'], []),
        ]
        for meta, expected in cases:
            with tempfile.TemporaryDirectory() as root:
                _write_skill(root, meta)
                self.assertEqual(_catalog.build(root)['skills'][0]['targets'], expected, meta)

    def test_absent_defaults(self):
        """CAT-23: an absent catalog-consumers gives all; absent version, owner and last_verified give empty strings."""
        _write_skill(self.tmp, ['catalog-targets: "claude"'])
        entry = _catalog.build(self.tmp)['skills'][0]
        self.assertEqual(entry['consumers'], 'all')
        self.assertEqual(entry['version'], '')
        self.assertEqual(entry['owner'], '')
        self.assertEqual(entry['last_verified'], '')
        self.assertEqual(entry['path'], 'skills/x')

    def test_traversal_error_propagates(self):
        """CAT-11: an OSError while listing a skill subfolder makes build() raise."""
        _copy_repo(self.tmp)
        real_scandir = os.scandir
        blocked = os.path.join('ears-notation', 'examples')

        def fake_scandir(path='.'):
            if os.fspath(path).rstrip(os.sep).endswith(blocked):
                raise PermissionError(13, 'denied', os.fspath(path))
            return real_scandir(path)

        with mock.patch('os.scandir', side_effect=fake_scandir):
            with self.assertRaises(OSError):
                _catalog.build(self.tmp)

    def test_stat_error_propagates(self):
        """CAT-11: an OSError while checking a file or a SKILL.md makes build() raise."""
        _copy_repo(self.tmp)
        real_lstat = os.lstat
        target = os.path.join('ears-notation', 'references', 'patterns.md')

        def fake_lstat(path, *args, **kwargs):
            if os.fspath(path).endswith(target):
                raise PermissionError(13, 'denied', os.fspath(path))
            return real_lstat(path, *args, **kwargs)

        with mock.patch('os.lstat', side_effect=fake_lstat):
            with self.assertRaises(OSError):
                _catalog.build(self.tmp)

        for blocked in (os.path.join('ears-notation', 'examples'),
                        os.path.join('skills', 'new-mcp'),
                        os.path.join('new-mcp', 'SKILL.md')):
            def fake_lstat(path, *args, **kwargs):
                if os.fspath(path).rstrip(os.sep).endswith(blocked):
                    raise PermissionError(13, 'denied', os.fspath(path))
                return real_lstat(path, *args, **kwargs)

            with mock.patch('os.lstat', side_effect=fake_lstat):
                with self.assertRaises(OSError, msg=blocked):
                    _catalog.build(self.tmp)

    def _symlink(self, target, path):
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError, AttributeError) as err:
            self.skipTest('cannot create symlink: %s' % err)

    def _assert_refused(self, path):
        with self.assertRaises(ValueError) as ctx:
            _catalog.build(self.tmp)
        self.assertIn(path + ': ', str(ctx.exception))
        result = _cli(self.tmp, '--check')
        self.assertEqual(result.returncode, 1)
        self.assertIn(path, result.stderr)

    def test_symlinked_skill_folder_refused(self):
        """CAT-26: a symlinked skill folder fails the manifest check, it is not skipped."""
        _copy_repo(self.tmp)
        outside = os.path.join(self.tmp, 'outside')
        shutil.copytree(os.path.join(_repo_root, 'skills', 'new-mcp'), outside)
        self._symlink(outside, os.path.join(self.tmp, 'skills', 'linked-skill'))
        self._assert_refused('skills/linked-skill')

    def test_symlinked_skill_md_refused(self):
        """CAT-26: a symlinked SKILL.md fails the manifest check and is not read."""
        _copy_repo(self.tmp)
        outside = os.path.join(self.tmp, 'outside.md')
        shutil.copyfile(os.path.join(_repo_root, 'skills', 'new-mcp', 'SKILL.md'), outside)
        os.makedirs(os.path.join(self.tmp, 'skills', 'linked-md'))
        self._symlink(outside, os.path.join(self.tmp, 'skills', 'linked-md', 'SKILL.md'))
        self._assert_refused('skills/linked-md/SKILL.md')

    def test_symlinked_file_in_skill_refused(self):
        """CAT-26: a symlinked file inside a skill folder fails the manifest check."""
        _copy_repo(self.tmp)
        outside = os.path.join(self.tmp, 'outside.md')
        with open(outside, 'w', encoding='utf-8') as handle:
            handle.write('outside\n')
        os.makedirs(os.path.join(self.tmp, 'skills', 'new-mcp', 'refs'))
        self._symlink(outside, os.path.join(self.tmp, 'skills', 'new-mcp', 'refs', 'link.md'))
        self._assert_refused('skills/new-mcp/refs/link.md')

    def test_symlinked_folder_in_skill_refused(self):
        """CAT-26: a symlinked folder inside a skill folder fails the manifest check."""
        _copy_repo(self.tmp)
        outside_dir = os.path.join(self.tmp, 'outside-dir')
        os.makedirs(outside_dir)
        self._symlink(outside_dir, os.path.join(self.tmp, 'skills', 'new-mcp', 'linkdir'))
        self._assert_refused('skills/new-mcp/linkdir')

    def test_symlink_in_held_refused(self):
        """CAT-26: .held/ gets the same symlink check as skills/."""
        _copy_repo(self.tmp)
        self._symlink('SKILL.md', os.path.join(self.tmp, '.held', HELD[0], 'alias.md'))
        self._assert_refused('.held/%s/alias.md' % HELD[0])

    def test_symlink_in_folder_without_skill_md_refused(self):
        """CAT-26: a folder under skills/ with no SKILL.md is still checked, as the server archives it."""
        _copy_repo(self.tmp)
        os.makedirs(os.path.join(self.tmp, 'skills', 'junk'))
        self._symlink('../new-mcp/SKILL.md', os.path.join(self.tmp, 'skills', 'junk', 'link'))
        self._assert_refused('skills/junk/link')

    def test_symlink_in_canary_refused(self):
        """CAT-26: the canary is left out of the manifest but gets the same symlink check."""
        _copy_repo(self.tmp)
        self._symlink('SKILL.md', os.path.join(self.tmp, '.held', 'zz-canary', 'alias.md'))
        self._assert_refused('.held/zz-canary/alias.md')

    def test_symlink_named_ds_store_refused(self):
        """CAT-26: only a regular .DS_Store is ignored; a symlink with that name is refused."""
        _copy_repo(self.tmp)
        self._symlink('SKILL.md', os.path.join(self.tmp, 'skills', 'new-mcp', '.DS_Store'))
        self._assert_refused('skills/new-mcp/.DS_Store')

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'mkfifo not supported')
    def test_special_file_refused(self):
        """CAT-26: a special file (here a FIFO) in a skill folder fails the manifest check."""
        _copy_repo(self.tmp)
        os.mkfifo(os.path.join(self.tmp, 'skills', 'new-mcp', 'pipe'))
        self._assert_refused('skills/new-mcp/pipe')

    def test_regular_ds_store_ignored(self):
        """CAT-26: a regular .DS_Store is left out of the manifest, as the server ignores it."""
        _copy_repo(self.tmp)
        before = _catalog.render(self.tmp)
        for path in (os.path.join(self.tmp, 'skills', '.DS_Store'),
                     os.path.join(self.tmp, 'skills', 'new-mcp', '.DS_Store')):
            with open(path, 'wb') as handle:
                handle.write(b'\x00\x01')
        self.assertEqual(_catalog.render(self.tmp), before)

    def test_null_metadata_raises(self):
        """CAT-11: a present catalog-* value that is not a string raises ValueError."""
        _copy_repo(self.tmp)
        path = os.path.join(self.tmp, 'skills', 'new-mcp', 'SKILL.md')
        with open(path, encoding='utf-8') as handle:
            lines = handle.read().split('\n')
        lines = ['  catalog-owner: null' if l.startswith('  catalog-owner:') else l for l in lines]
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('\n'.join(lines))
        with self.assertRaises(ValueError) as ctx:
            _catalog.build(self.tmp)
        self.assertIn('skills/new-mcp', str(ctx.exception))
        self.assertIn('catalog-owner', str(ctx.exception))

    def test_check_propagates_other_io_errors(self):
        """CAT-11: --check treats only FileNotFoundError as stale; other OSErrors propagate."""
        _copy_repo(self.tmp)
        self.assertEqual(_catalog.main(['--root', self.tmp]), 0)
        real_open = builtins.open

        def fake_open(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and os.fspath(file).endswith('catalog.json'):
                raise PermissionError(13, 'denied', os.fspath(file))
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=fake_open):
            with self.assertRaises(PermissionError):
                _catalog.main(['--root', self.tmp, '--check'])

    def _write_stale(self, text):
        os.makedirs(os.path.join(self.tmp, 'freshness'), exist_ok=True)
        with open(os.path.join(self.tmp, 'freshness', 'stale.json'), 'w', encoding='utf-8') as handle:
            handle.write(text)

    def test_empty_stale_list_is_byte_identical(self):
        """FRS-8: an empty freshness/stale.json leaves catalog.json byte-identical, and no entry has `stale`."""
        _copy_repo(self.tmp)
        without = _catalog.render(self.tmp)
        self._write_stale('[]\n')
        self.assertEqual(_catalog.render(self.tmp), without)
        with open(os.path.join(_repo_root, 'catalog.json'), encoding='utf-8', newline='') as handle:
            self.assertEqual(_catalog.render(_repo_root), handle.read())
        doc = _catalog.build(self.tmp)
        self.assertFalse(any('stale' in entry for entry in doc['skills'] + doc['held']))

    def test_listed_skill_is_stale(self):
        """FRS-8: a listed skill (or held skill) gets `"stale": true` as its last key; others get no key."""
        _copy_repo(self.tmp)
        self._write_stale(json.dumps(['ears-notation', HELD[0]]) + '\n')
        doc = _catalog.build(self.tmp)
        ears = self._entry(doc, 'ears-notation')
        self.assertIs(ears['stale'], True)
        self.assertEqual(list(ears), KEYS + ['stale'])
        self.assertIs(self._entry(doc, HELD[0], 'held')['stale'], True)
        others = [e for e in doc['skills'] + doc['held'] if e['name'] not in ('ears-notation', HELD[0])]
        self.assertTrue(others)
        self.assertFalse(any('stale' in entry for entry in others))

    def test_unknown_stale_name_raises(self):
        """FRS-8: a name in freshness/stale.json that is not a catalog skill is an error."""
        _copy_repo(self.tmp)
        self._write_stale('["no-such-skill"]\n')
        with self.assertRaises(ValueError) as ctx:
            _catalog.build(self.tmp)
        self.assertIn('no-such-skill', str(ctx.exception))
        for bad in ('{}', '["b-skill", "a-skill"]', '[1]'):
            self._write_stale(bad)
            with self.assertRaises(ValueError):
                _catalog.build(self.tmp)

    def test_canary_cannot_be_stale(self):
        """FRS-19: zz-canary is not in the manifest, so listing it is an error."""
        _copy_repo(self.tmp)
        self._write_stale('["zz-canary"]\n')
        with self.assertRaises(ValueError):
            _catalog.build(self.tmp)


if __name__ == '__main__':
    unittest.main()
