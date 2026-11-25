"""The default tag order shared by tools/install-local and the MCP server (MCP-1, MCP-9).

Both resolvers pick the newest `v*` tag with the same Python key, so git's
versionsort config (global, system or repo-local) and tag dates cannot change
the result, and the two tools always agree (#108).
"""

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, 'tools', 'install-local')
SNAPSHOT = os.path.join(REPO, 'server', 'skills_catalog_mcp', 'snapshot.py')
BLOCK_START = '# Release tag order'


def load_install_local():
    loader = importlib.machinery.SourceFileLoader('install_local_tag_order', TOOL)
    spec = importlib.util.spec_from_loader('install_local_tag_order', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_snapshot():
    spec = importlib.util.spec_from_file_location('snapshot_tag_order', SNAPSHOT)
    module = importlib.util.module_from_spec(spec)
    # snapshot.py uses postponed annotations, and dataclasses looks the module
    # up in sys.modules while the class body runs.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


install_local = load_install_local()
snapshot = load_snapshot()
MODULES = (('install-local', install_local), ('snapshot', snapshot))


def shared_block(path, stop):
    with open(path, encoding='utf-8') as handle:
        text = handle.read()
    start = text.index(BLOCK_START)
    return text[start:text.index(stop, start)]


class TagSortKeyCase(unittest.TestCase):

    def test_both_files_hold_the_same_block(self):
        self.assertEqual(shared_block(TOOL, 'def resolve_tag('),
                         shared_block(SNAPSHOT, 'def _resolve_tag('))

    def test_keys_agree_and_reject_names_without_a_version(self):
        names = ['v1', 'v1.9', 'v1.10', 'v01.2', 'v2026.11.11.1', 'v2026.11.11.1-rc1',
                 'v2026.11.11.2-rc1', 'v1.0rc1', 'v1.0+build', 'v1.0-rc10', 'v1.0~',
                 'v1..2', 'v1/x', 'v' + '9' * 5000, 'vendor', 'v', 'v-1', 'V1', 'v²', '']
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(install_local.tag_sort_key(name), snapshot.tag_sort_key(name))
        for name in ('vendor', 'v', 'v-1', 'V1', 'v²', '', 'x1'):
            for label, module in MODULES:
                with self.subTest(module=label, name=name):
                    self.assertIsNone(module.tag_sort_key(name))

    def test_newest_tag(self):
        cases = [
            (['v1.9', 'v1.10'], 'v1.10'),
            (['v2', 'v10', 'v9'], 'v10'),
            (['v2026.11.11.1-rc1', 'v2026.11.11.1', 'v2026.11.11.0'], 'v2026.11.11.1'),
            (['v2026.11.11.1', 'v2026.11.11.2-rc1'], 'v2026.11.11.2-rc1'),
            (['v1.0-rc1', 'v1.0', 'v1.0-rc2'], 'v1.0'),
            (['v1.0', 'v1.0.1-beta'], 'v1.0.1-beta'),
            (['v1.0rc1', 'v0.9'], 'v1.0rc1'),
            (['v1.0rc1', 'v1.0'], 'v1.0'),
            (['v1.0-rc2', 'v1.0-rc10'], 'v1.0-rc10'),
            (['v1.0rc2', 'v1.0rc10'], 'v1.0rc10'),
            (['v1.0-alpha', 'v1.0-beta'], 'v1.0-beta'),
            (['v1.0+build', 'v1.0-rc1'], 'v1.0+build'),
            (['v1.0+build', 'v1.0.1'], 'v1.0.1'),
            (['v' + '9' * 5000, 'v' + '9' * 4999 + '8'], 'v' + '9' * 5000),
            (['vendor', 'v0.1'], 'v0.1'),
            (['vendor'], None),
            ([], None),
        ]
        for names, want in cases:
            for label, module in MODULES:
                with self.subTest(module=label, names=[name[:24] for name in names]):
                    self.assertEqual(module.newest_tag(names), want)
                    self.assertEqual(module.newest_tag(list(reversed(names))), want)


class ResolverCase(unittest.TestCase):
    """Both resolvers against a real repository with hostile repo-local config."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='tag order ')
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update({
            'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 't',
            'GIT_AUTHOR_EMAIL': 't' + chr(64) + 'example.invalid',
            'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't' + chr(64) + 'example.invalid',
        })
        self.git('init', '-q')
        with open(os.path.join(self.repo, 'file.txt'), 'w') as handle:
            handle.write('x\n')
        self.git('add', 'file.txt')
        self.git('commit', '-q', '-m', 'one')
        # Tag dates run against version order: the highest release is the
        # oldest tag, so a resolver that sorts by date picks something else.
        for tag, date in (('v1.0.0', '2020-01-01T00:00:00Z'),
                          ('v1.0.0-rc1', '2021-01-01T00:00:00Z'),
                          ('v1.0.0rc2', '2022-01-01T00:00:00Z'),
                          ('v0.9', '2023-01-01T00:00:00Z')):
            self.git('tag', '-a', '-m', tag, tag, date=date)
        self.git('tag', 'v0.8')

    def git(self, *args, date=None, check=True):
        env = dict(self.env)
        if date is not None:
            env['GIT_COMMITTER_DATE'] = date
        return subprocess.run(['git', '-C', self.repo] + list(args), env=env, check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_repo_local_versionsort_config_has_no_effect(self):
        configs = [
            [],
            [''],
            ['-'],
            ['-rc'],
            ['rc'],
            ['-', '.0'],
            ['', '-'],
            ['-rc', ''],
        ]
        for values in configs:
            with self.subTest(versionsort_suffix=values):
                self.git('config', '--unset-all', 'versionsort.suffix', check=False)
                for value in values:
                    self.git('config', '--add', 'versionsort.suffix', value)
                self.assertEqual(install_local.resolve_tag(self.repo, None)[0], 'v1.0.0')
                self.assertEqual(snapshot._resolve_tag(self.repo, None)[0], 'v1.0.0')

    def test_pre_release_wins_only_over_lower_versions(self):
        self.git('tag', '-d', 'v1.0.0')
        for label, resolve in (('install-local', install_local.resolve_tag),
                               ('snapshot', snapshot._resolve_tag)):
            with self.subTest(module=label):
                self.assertEqual(resolve(self.repo, None)[0], 'v1.0.0rc2')


if __name__ == '__main__':
    unittest.main()
