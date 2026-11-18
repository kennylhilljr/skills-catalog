"""Tests for tools/link-consumer and consumers.json (SUB-3, INV-8).

Each test builds a temporary consumer folder with a fake shared tree at
`.agents/skills/shared/`: `skills/a`, `skills/b`, `.held/sdd-x`,
`.held/zz-canary`, and a `catalog.json` with `skills` and `held` arrays.
The tool runs as a subprocess, as a consumer runs it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, 'tools', 'link-consumer')
SHARED = os.path.join('.agents', 'skills', 'shared')
PREFIX = '../../.agents/skills/shared/'


def entry(name, path):
    return {'name': name, 'path': path, 'version': '1.0.0',
            'consumers': 'all', 'files': {}}


class LinkConsumerTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='link consumer ')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.shared = os.path.join(self.root, SHARED)
        for rel in ('skills/a', 'skills/b', '.held/sdd-x', '.held/zz-canary'):
            folder = os.path.join(self.shared, rel)
            os.makedirs(folder)
            with open(os.path.join(folder, 'SKILL.md'), 'w') as handle:
                handle.write('---\nname: ' + os.path.basename(rel) + '\n---\n')
        self.skills = [entry('a', 'skills/a'), entry('b', 'skills/b')]
        self.held = [entry('sdd-x', '.held/sdd-x'),
                     entry('zz-canary', '.held/zz-canary')]
        self.write_catalog()
        self.links = os.path.join(self.root, '.claude', 'skills')

    def write_catalog(self):
        data = {'version': 1, 'skills': self.skills, 'held': self.held}
        with open(os.path.join(self.shared, 'catalog.json'), 'w') as handle:
            json.dump(data, handle)

    def run_tool(self, *args, cwd=None, tool=TOOL):
        if cwd is None:
            args = ('--root', self.root) + args
        return subprocess.run(
            [sys.executable, '-B', tool] + list(args),
            cwd=cwd or self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )

    def link_names(self):
        if not os.path.isdir(self.links):
            return []
        return sorted(os.listdir(self.links))

    def snapshot(self):
        """Every path under .claude with its link target or kind."""
        state = {}
        base = os.path.join(self.root, '.claude')
        for dirpath, dirnames, filenames in os.walk(base):
            for item in dirnames + filenames:
                path = os.path.join(dirpath, item)
                rel = os.path.relpath(path, self.root)
                if os.path.islink(path):
                    state[rel] = 'link:' + os.readlink(path)
                else:
                    state[rel] = 'dir' if os.path.isdir(path) else 'file'
        return state

    def lines(self, result):
        return result.stdout.splitlines()


class WriteModeTests(LinkConsumerTestCase):

    def test_creates_exactly_relative_links(self):
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])
        for name in ('a', 'b'):
            path = os.path.join(self.links, name)
            self.assertTrue(os.path.islink(path))
            self.assertEqual(os.readlink(path), PREFIX + 'skills/' + name)
            self.assertTrue(os.path.isfile(os.path.join(path, 'SKILL.md')))

    def test_runs_from_consumer_root_without_root_flag(self):
        tools = os.path.join(self.shared, 'tools')
        os.makedirs(tools)
        shutil.copy(TOOL, os.path.join(tools, 'link-consumer'))
        result = self.run_tool(
            cwd=self.root, tool=os.path.join(SHARED, 'tools', 'link-consumer'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])

    def test_idempotent(self):
        self.assertEqual(self.run_tool().returncode, 0)
        before = self.snapshot()
        second = self.run_tool()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.lines(second)[-1], 'link-consumer: 2 links, 0 changes')

    def test_removes_link_for_dropped_skill(self):
        self.assertEqual(self.run_tool().returncode, 0)
        self.skills = [entry('a', 'skills/a')]
        self.write_catalog()
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a'])
        self.assertIn('link-consumer: removed: .claude/skills/b', self.lines(result))
        # The skill folder in the submodule is untouched.
        self.assertTrue(os.path.isdir(os.path.join(self.shared, 'skills', 'b')))

    def test_never_links_held_skills(self):
        # INV-8: held array, zz-canary, and a skills entry under .held/.
        self.skills = self.skills + [
            entry('sdd-x', '.held/sdd-x'),
            entry('zz-canary', 'skills/zz-canary'),
            entry('zz-canary', '.held/zz-canary'),
        ]
        self.write_catalog()
        os.makedirs(os.path.join(self.shared, 'skills', 'zz-canary'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])
        check = self.run_tool('--check')
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_removes_link_into_held(self):
        self.assertEqual(self.run_tool().returncode, 0)
        os.symlink(PREFIX + '.held/sdd-x', os.path.join(self.links, 'sdd-x'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])

    def test_skips_name_failing_cat2_pattern(self):
        self.skills = self.skills + [entry('../x', 'skills/../x'),
                                     entry('Bad_Name', 'skills/Bad_Name')]
        self.write_catalog()
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])
        self.assertFalse(os.path.lexists(os.path.join(self.root, '.claude', 'x')))

    def test_skips_name_with_newline_or_over_64_chars(self):
        long_name = 'a' * 65
        os.makedirs(os.path.join(self.shared, 'skills', long_name))
        self.skills = self.skills + [entry('a\n', 'skills/a\n'),
                                     entry(long_name, 'skills/' + long_name)]
        self.write_catalog()
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])

    def test_absolute_link_into_shared_counts_as_submodule_link(self):
        os.makedirs(self.links)
        held = os.path.join(self.shared, '.held', 'sdd-x')
        os.symlink(held, os.path.join(self.links, 'sdd-x'))
        os.symlink(os.path.realpath(held), os.path.join(self.links, 'other'))
        os.symlink(os.path.join(self.shared, 'skills', 'a'),
                   os.path.join(self.links, 'a'))
        check = self.run_tool('--check')
        self.assertEqual(check.returncode, 1)
        self.assertIn('link-consumer: extra: .claude/skills/sdd-x', self.lines(check))
        self.assertIn('link-consumer: extra: .claude/skills/other', self.lines(check))
        self.assertIn('link-consumer: wrong-target: .claude/skills/a',
                      self.lines(check))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])
        self.assertEqual(os.readlink(os.path.join(self.links, 'a')),
                         PREFIX + 'skills/a')

    def test_link_through_alias_into_shared_counts(self):
        os.symlink(SHARED, os.path.join(self.root, 'alias'))
        os.makedirs(self.links)
        os.symlink('../../alias/.held/sdd-x', os.path.join(self.links, 'sdd-x'))
        os.symlink('../../alias/skills/a', os.path.join(self.links, 'a'))
        check = self.run_tool('--check')
        self.assertIn('link-consumer: extra: .claude/skills/sdd-x', self.lines(check))
        self.assertIn('link-consumer: wrong-target: .claude/skills/a',
                      self.lines(check))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b'])
        self.assertEqual(self.run_tool('--check').returncode, 0)

    def test_link_escaping_shared_through_inner_link_is_foreign(self):
        outside = os.path.join(self.root, 'outside')
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.shared, 'skills', 'a', 'escape'))
        os.makedirs(self.links)
        os.symlink(PREFIX + 'skills/a/escape', os.path.join(self.links, 'mine'))
        os.symlink(PREFIX + 'skills/a/escape', os.path.join(self.links, 'b'))
        check = self.run_tool('--check')
        self.assertIn('link-consumer: shadow: .claude/skills/b', self.lines(check))
        self.assertNotIn('link-consumer: extra: .claude/skills/mine',
                         self.lines(check))
        self.assertEqual(self.run_tool().returncode, 1)
        self.assertEqual(os.readlink(os.path.join(self.links, 'mine')),
                         PREFIX + 'skills/a/escape')
        self.assertEqual(os.readlink(os.path.join(self.links, 'b')),
                         PREFIX + 'skills/a/escape')

    def test_dangling_after_write_exits_one(self):
        shutil.rmtree(os.path.join(self.shared, 'skills', 'b'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn('link-consumer: dangling: .claude/skills/b', self.lines(result))
        self.assertEqual(self.link_names(), ['a', 'b'])

    def test_relink_leaves_foreign_dot_entries_alone(self):
        os.makedirs(self.links)
        os.symlink(PREFIX + 'skills/b', os.path.join(self.links, 'a'))
        foreign = os.path.join(self.links, '.a.link-consumer-tmp')
        os.symlink('../../elsewhere/x', foreign)
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(foreign), '../../elsewhere/x')
        self.assertEqual(self.link_names(), ['.a.link-consumer-tmp', 'a', 'b'])

    def test_real_folder_shadow_blocks_all_writes(self):
        os.makedirs(os.path.join(self.links, 'b'))
        with open(os.path.join(self.links, 'b', 'SKILL.md'), 'w') as handle:
            handle.write('local\n')
        os.symlink(PREFIX + '.held/sdd-x', os.path.join(self.links, 'sdd-x'))
        before = self.snapshot()
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn('link-consumer: shadow: .claude/skills/b', self.lines(result))
        self.assertEqual(self.snapshot(), before)

    def test_foreign_link_is_shadow_and_kept(self):
        os.makedirs(self.links)
        os.symlink('../../elsewhere/a', os.path.join(self.links, 'a'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn('link-consumer: shadow: .claude/skills/a', self.lines(result))
        self.assertEqual(os.readlink(os.path.join(self.links, 'a')),
                         '../../elsewhere/a')
        self.assertFalse(os.path.lexists(os.path.join(self.links, 'b')))

    def test_leaves_unrelated_entries_alone(self):
        os.makedirs(os.path.join(self.links, 'local-skill'))
        os.symlink('../../elsewhere/x', os.path.join(self.links, 'foreign'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.link_names(), ['a', 'b', 'foreign', 'local-skill'])

    def test_fixes_wrong_target(self):
        os.makedirs(self.links)
        os.symlink(PREFIX + 'skills/b', os.path.join(self.links, 'a'))
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.readlink(os.path.join(self.links, 'a')),
                         PREFIX + 'skills/a')

    def test_refuses_symlinked_links_folder(self):
        outside = os.path.join(self.root, 'outside')
        os.makedirs(outside)
        os.makedirs(os.path.join(self.root, '.claude'))
        os.symlink('../outside', self.links)
        result = self.run_tool()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(os.listdir(outside), [])


class CheckModeTests(LinkConsumerTestCase):

    def test_clean_tree_exits_zero(self):
        self.assertEqual(self.run_tool().returncode, 0)
        result = self.run_tool('--check')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.lines(result), ['link-consumer: 2 links, 0 findings'])

    def test_reports_every_kind(self):
        self.skills = self.skills + [entry('c', 'skills/c'),
                                     entry('d', 'skills/d'),
                                     entry('e', 'skills/e')]
        self.write_catalog()
        os.makedirs(os.path.join(self.shared, 'skills', 'c'))
        os.makedirs(os.path.join(self.shared, 'skills', 'd'))
        os.makedirs(self.links)
        # a: missing. b: dangling (skill folder gone from the submodule).
        os.symlink(PREFIX + 'skills/b', os.path.join(self.links, 'b'))
        shutil.rmtree(os.path.join(self.shared, 'skills', 'b'))
        # c: wrong-target. d: shadow (real folder). e: shadow (foreign link).
        os.symlink(PREFIX + 'skills/a', os.path.join(self.links, 'c'))
        os.makedirs(os.path.join(self.links, 'd'))
        os.symlink('../../elsewhere/e', os.path.join(self.links, 'e'))
        # Extra: a held skill and a name not in the skills array.
        os.symlink(PREFIX + '.held/sdd-x', os.path.join(self.links, 'sdd-x'))
        os.symlink(PREFIX + 'skills/gone', os.path.join(self.links, 'gone'))
        before = self.snapshot()
        result = self.run_tool('--check')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.lines(result), [
            'link-consumer: missing: .claude/skills/a',
            'link-consumer: dangling: .claude/skills/b',
            'link-consumer: wrong-target: .claude/skills/c',
            'link-consumer: shadow: .claude/skills/d',
            'link-consumer: shadow: .claude/skills/e',
            'link-consumer: extra: .claude/skills/gone',
            'link-consumer: extra: .claude/skills/sdd-x',
            'link-consumer: 5 links, 7 findings',
        ])
        self.assertEqual(self.snapshot(), before)

    def test_each_kind_alone_exits_one(self):
        cases = {
            'missing': lambda: os.unlink(os.path.join(self.links, 'a')),
            'dangling': lambda: shutil.rmtree(
                os.path.join(self.shared, 'skills', 'a')),
            'extra': lambda: os.symlink(
                PREFIX + '.held/zz-canary',
                os.path.join(self.links, 'zz-canary')),
            'wrong-target': lambda: (
                os.unlink(os.path.join(self.links, 'a')),
                os.symlink(PREFIX + 'skills/b', os.path.join(self.links, 'a'))),
            'shadow': lambda: (
                os.unlink(os.path.join(self.links, 'a')),
                os.makedirs(os.path.join(self.links, 'a'))),
        }
        for kind, breaker in cases.items():
            with self.subTest(kind=kind):
                shutil.rmtree(self.root)
                self.setUp()
                self.assertEqual(self.run_tool().returncode, 0)
                breaker()
                result = self.run_tool('--check')
                self.assertEqual(result.returncode, 1, kind)
                findings = [l for l in self.lines(result)
                            if l.startswith('link-consumer: ' + kind + ': ')]
                self.assertEqual(len(findings), 1, result.stdout)

    def test_check_without_links_folder_reports_missing(self):
        result = self.run_tool('--check')
        self.assertEqual(result.returncode, 1)
        self.assertIn('link-consumer: missing: .claude/skills/a', self.lines(result))
        self.assertFalse(os.path.lexists(os.path.join(self.root, '.claude')))


class ErrorTests(LinkConsumerTestCase):

    def test_missing_catalog_exits_two(self):
        os.unlink(os.path.join(self.shared, 'catalog.json'))
        for args in ((), ('--check',)):
            result = self.run_tool(*args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('link-consumer: shared skills missing', result.stderr)
        self.assertFalse(os.path.lexists(os.path.join(self.root, '.claude')))

    def test_invalid_catalog_exits_two(self):
        with open(os.path.join(self.shared, 'catalog.json'), 'w') as handle:
            handle.write('{not json')
        result = self.run_tool('--check')
        self.assertEqual(result.returncode, 2)


class ConsumersJsonTests(unittest.TestCase):

    def test_consumers_json(self):
        with open(os.path.join(REPO, 'consumers.json'), encoding='utf-8') as handle:
            data = json.load(handle)
        self.assertEqual(data['version'], 1)
        self.assertEqual(len(data['consumers']), 1)
        consumer = data['consumers'][0]
        self.assertEqual(consumer['path'], '.agents/skills/shared')
        self.assertEqual(consumer['repo'], 'kennylhilljr/pr-review-bots')
        self.assertEqual(consumer['stage'], 'canary')


if __name__ == '__main__':
    unittest.main()
