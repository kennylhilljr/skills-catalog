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
        os.makedirs(canary)
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

    @unittest.skipUnless(hasattr(os, 'symlink'), 'symlinks not supported')
    def test_symlink_skipped(self):
        """CAT-11: symlinked files and folders are not hashed."""
        _copy_repo(self.tmp)
        outside = os.path.join(self.tmp, 'outside.md')
        with open(outside, 'w', encoding='utf-8') as handle:
            handle.write('outside\n')
        outside_dir = os.path.join(self.tmp, 'outside-dir')
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, 'inner.md'), 'w', encoding='utf-8') as handle:
            handle.write('inner\n')
        folder = os.path.join(self.tmp, 'skills', 'new-mcp')
        try:
            os.symlink(outside, os.path.join(folder, 'link.md'))
            os.symlink(outside_dir, os.path.join(folder, 'linkdir'))
        except (OSError, NotImplementedError) as err:
            self.skipTest('cannot create symlink: %s' % err)
        files = self._entry(_catalog.build(self.tmp), 'new-mcp')['files']
        self.assertNotIn('link.md', files)
        self.assertNotIn('linkdir/inner.md', files)
        self.assertIn('SKILL.md', files)

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


if __name__ == '__main__':
    unittest.main()
