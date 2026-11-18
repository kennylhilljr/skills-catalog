"""Tests for tools/consumer-smoke, the CLI skill smoke check (SUB-14, SUB-17).

Fake `claude` and `codex` executables on a temporary PATH print canned
stream output, so no real CLI or login is needed.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO, 'tools', 'consumer-smoke')


def _load():
    loader = importlib.machinery.SourceFileLoader('consumer_smoke_under_test', TOOL)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


smoke = _load()


def claude_init(skills):
    events = [
        {'type': 'system', 'subtype': 'hook_started'},
        {'type': 'system', 'subtype': 'init', 'skills': skills, 'tools': []},
        {'type': 'result', 'subtype': 'success', 'result': 'ignored'},
    ]
    return ''.join(json.dumps(e) + '\n' for e in events)


def claude_reply(text):
    events = [
        {'type': 'system', 'subtype': 'init', 'tools': []},
        {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}},
        {'type': 'result', 'subtype': 'success', 'result': text},
    ]
    return ''.join(json.dumps(e) + '\n' for e in events)


def codex_reply(text):
    events = [
        {'type': 'thread.started', 'thread_id': 't'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'error', 'message': 'note'}},
        {'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'agent_message', 'text': text}},
        {'type': 'turn.completed', 'usage': {}},
    ]
    return ''.join(json.dumps(e) + '\n' for e in events)


FAKE = textwrap.dedent('''\
    #!{python}
    import os, sys
    if sys.argv[1:] == ['--version']:
        print({version!r})
        sys.exit(0)
    out = {out!r}
    if any(os.environ.get(k) for k in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'ANTHROPIC_AUTH_TOKEN')):
        out = out.replace('cross-review', 'api-key-leaked')
    sys.stdout.write(out)
    sys.stderr.write('trace that must not be printed\\n')
    sys.exit({rc})
''')


class ConsumerSmokeTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, 'bin')
        os.mkdir(self.bin)
        self.consumer = os.path.join(self.tmp.name, 'consumer')
        shared = os.path.join(self.consumer, '.agents', 'skills', 'shared')
        os.makedirs(os.path.join(shared, 'skills', 'cross-review'))
        os.makedirs(os.path.join(shared, '.held', 'sdd-spec'))
        os.makedirs(os.path.join(shared, '.held', 'sdd-extra'))
        with open(os.path.join(shared, 'catalog.json'), 'w', encoding='utf-8') as handle:
            json.dump({'version': 1, 'skills': [{'name': 'cross-review'}],
                       'held': [{'name': 'sdd-spec'}, {'name': 'sdd-adopt'}]}, handle)
        with open(os.path.join(shared, 'skills', 'cross-review', 'SKILL.md'), 'w') as handle:
            handle.write('---\nname: cross-review\n---\n')
        os.makedirs(os.path.join(self.consumer, '.claude', 'skills'))
        os.symlink('../../.agents/skills/shared/skills/cross-review',
                   os.path.join(self.consumer, '.claude', 'skills', 'cross-review'))

    def fake(self, name, out, rc=0, version=None):
        path = os.path.join(self.bin, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(FAKE.format(python=sys.executable, out=out, rc=rc,
                                     version=version or name + ' 9.8.7'))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run_tool(self, *args, extra_env=None):
        env = {'PATH': self.bin, 'HOME': self.tmp.name}
        env.update(extra_env or {})
        proc = subprocess.run(
            [sys.executable, '-B', TOOL, self.consumer] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, env=env, timeout=60,
        )
        return proc.returncode, proc.stdout.splitlines(), proc.stderr

    def both_ok(self):
        self.fake('claude', claude_init(['tdd', 'cross-review', 'ecc:pr']))
        self.fake('codex', codex_reply('imagegen\ncross-review\nskill-creator'))

    def test_both_list_expected_exit_0(self):
        self.both_ok()
        rc, lines, err = self.run_tool('--expect', 'cross-review', '--cli', 'claude,codex')
        self.assertEqual(rc, 0, (lines, err))
        self.assertEqual(lines, [
            'consumer-smoke: claude 9.8.7: expected ok; held none',
            'consumer-smoke: codex 9.8.7: expected ok; held none',
        ])
        self.assertEqual(err, '')

    def test_default_cli_list_is_claude_and_codex(self):
        self.both_ok()
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 0)
        self.assertEqual([line.split()[1] for line in lines], ['claude', 'codex'])

    def test_one_cli_misses_expected_exit_1_names_cli(self):
        self.fake('claude', claude_init(['cross-review']))
        self.fake('codex', codex_reply('imagegen\nskill-creator'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review', '--cli', 'claude,codex')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], 'consumer-smoke: claude 9.8.7: expected ok; held none')
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected missing cross-review; held none')

    def test_user_level_copy_shadows_expected_exit_1(self):
        self.both_ok()
        write_skill(os.path.join(self.tmp.name, '.claude', 'skills', 'cross-review'), 'cross-review')
        os.makedirs(os.path.join(self.tmp.name, '.agents', 'skills', 'cross-review'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], 'consumer-smoke: claude 9.8.7: expected shadowed cross-review; held none')
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected ok; held none')

    def test_user_agents_skills_shadow_codex(self):
        self.both_ok()
        real = os.path.join(self.tmp.name, 'elsewhere', 'cross-review')
        write_skill(real, 'cross-review')
        os.makedirs(os.path.join(self.tmp.name, '.agents', 'skills'))
        os.symlink(real, os.path.join(self.tmp.name, '.agents', 'skills', 'cross-review'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected shadowed cross-review; held none')

    def test_nested_user_copy_shadows_codex_only(self):
        self.both_ok()
        write_skill(os.path.join(self.tmp.name, '.agents', 'skills', 'vendor', 'pack', 'alias'), 'cross-review')
        write_skill(os.path.join(self.tmp.name, '.claude', 'skills', 'vendor', 'cross-review'), 'cross-review')
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], 'consumer-smoke: claude 9.8.7: expected ok; held none')
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected shadowed cross-review; held none')

    def test_held_name_from_catalog_array_exit_1(self):
        self.fake('claude', claude_init(['cross-review', 'sdd-adopt']))
        self.fake('codex', codex_reply('cross-review'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0], 'consumer-smoke: claude 9.8.7: expected ok; held sdd-adopt')
        self.assertTrue(lines[1].endswith('held none'))

    def test_zz_canary_is_held_exit_1(self):
        self.fake('claude', claude_init(['cross-review']))
        self.fake('codex', codex_reply('cross-review\nzz-canary'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected ok; held zz-canary')

    def test_held_folder_name_exit_1(self):
        self.fake('claude', claude_init(['cross-review']))
        self.fake('codex', codex_reply('cross-review\nsdd-extra\nsdd-spec'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: expected ok; held sdd-extra,sdd-spec')

    def test_fallback_parser_reads_plain_list_reply(self):
        self.fake('claude', claude_reply('- `cross-review`\n- tdd\n'))
        self.fake('codex', codex_reply('Here are the skills:\n\n1. imagegen\n2. **cross-review**\n'))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 0, lines)

    def test_parse_list_reply_unit(self):
        text = 'Skills:\n```\n- cross-review\n* /tdd\n3) ecc:pr\nnot a name here\n\n```\ncross-review\n'
        self.assertEqual(smoke.parse_list_reply(text), ['cross-review', 'tdd', 'ecc:pr'])

    def test_commands_disable_hooks_and_writes(self):
        claude, prompt = smoke.command_for('claude')
        self.assertEqual(prompt, smoke.LIST_PROMPT)
        self.assertIn('{"disableAllHooks": true}', claude)
        self.assertIn('--no-session-persistence', claude)
        self.assertIn('--strict-mcp-config', claude)
        self.assertEqual(claude[claude.index('--tools') + 1], '')
        codex, stdin = smoke.command_for('codex')
        self.assertIsNone(stdin)
        self.assertEqual(codex[codex.index('--disable') + 1], 'hooks')
        self.assertEqual(codex[codex.index('--sandbox') + 1], 'read-only')
        self.assertIn('--ephemeral', codex)
        self.assertIn('--ignore-user-config', codex)
        self.assertEqual(codex[-1], smoke.LIST_PROMPT)

    def test_claude_init_wins_over_reply(self):
        out = claude_init(['a-skill']).replace('"ignored"', '"other-skill"')
        self.assertEqual(smoke.parse_claude(out), ['a-skill'])

    def test_nonzero_exit_fails_with_cli_name(self):
        self.fake('claude', claude_init(['cross-review']))
        self.fake('codex', codex_reply('cross-review'), rc=3)
        rc, lines, err = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[1], 'consumer-smoke: codex 9.8.7: failed (exit 3)')
        self.assertNotIn('trace', '\n'.join(lines) + err)

    def test_missing_cli_fails_with_cli_name(self):
        self.fake('claude', claude_init(['cross-review']))
        rc, lines, _ = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines[1], 'consumer-smoke: codex unknown: not found on PATH')

    def test_empty_list_fails(self):
        self.fake('claude', claude_reply('I cannot tell.'))
        rc, lines, _ = self.run_tool('--cli', 'claude')
        self.assertEqual(rc, 1)
        self.assertEqual(lines, ['consumer-smoke: claude 9.8.7: listed no skills'])

    def test_api_keys_are_not_passed_to_clis(self):
        self.both_ok()
        rc, lines, _ = self.run_tool('--expect', 'cross-review', extra_env={
            'ANTHROPIC_API_KEY': 'x', 'OPENAI_API_KEY': 'x', 'CODEX_API_KEY': 'x'})
        self.assertEqual(rc, 0, lines)

    def test_missing_pinned_catalog_fails(self):
        os.remove(os.path.join(self.consumer, '.agents', 'skills', 'shared', 'catalog.json'))
        self.both_ok()
        rc, lines, err = self.run_tool('--expect', 'cross-review')
        self.assertEqual(rc, 1)
        self.assertEqual(lines, [])
        self.assertIn('no pinned catalog', err)

    def test_unknown_cli_is_usage_error(self):
        rc, _, err = self.run_tool('--cli', 'gemini')
        self.assertEqual(rc, 2)
        self.assertIn('gemini', err)

    def test_consumer_folder_unchanged(self):
        before = tree_digest(self.consumer)
        self.both_ok()
        self.run_tool('--expect', 'cross-review')
        self.fake('codex', codex_reply('zz-canary'), rc=1)
        self.run_tool('--expect', 'cross-review')
        self.assertEqual(tree_digest(self.consumer), before)


def write_skill(folder, name):
    os.makedirs(folder)
    with open(os.path.join(folder, 'SKILL.md'), 'w', encoding='utf-8') as handle:
        handle.write('---\nname: "%s"\ndescription: Use when testing.\n---\nbody\n' % name)


def tree_digest(root):
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            digest.update(os.path.relpath(path, root).encode())
            digest.update(str((info.st_mode, info.st_size, info.st_mtime_ns)).encode())
            if os.path.islink(path):
                digest.update(os.readlink(path).encode())
            elif os.path.isfile(path):
                with open(path, 'rb') as handle:
                    digest.update(handle.read())
    return digest.hexdigest()


if __name__ == '__main__':
    unittest.main()
