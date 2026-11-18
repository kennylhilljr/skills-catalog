#!/usr/bin/env python3
"""Tests for the Mac-only CLI checks and held drift (FRS-3, FRS-4, FRS-15, SDD-1).

No test runs a real command or touches the network: runners and npm
fetchers are fakes.
"""

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone

from freshness import cli_checks, held_drift

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location('catalog_scrub', os.path.join(_repo_root, 'tools', 'scrub.py'))
_scrub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_scrub)

FRONT = (
    '---\n'
    'name: {name}\n'
    'description: "Do a thing. Use when needed."\n'
    '{extra}'
    'metadata:\n'
    '  catalog-version: "1.0.0"\n'
    '{requires}'
    '---\n'
)

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  -p, --print                 Print response and exit
  --output-format <format>    Output format
  --model <model>             Model for the session

Commands:
  mcp                         Configure and manage MCP servers
  plugin                      Manage plugins
"""

CLAUDE_MCP_HELP = """Usage: claude mcp [options] [command]

Commands:
  add <name> <commandOrUrl>   Add an MCP server
  list                        List configured MCP servers
"""

CLAUDE_MCP_ADD_HELP = """Usage: claude mcp add [options] <name> <commandOrUrl>

Options:
  -s, --scope <scope>   Configuration scope
"""

CLAUDE_PLUGIN_HELP = """Usage: claude plugin [options] [command]

Options:
  --json   Output JSON

Commands:
  eval    Evaluate a plugin
"""


class FakeRun:
    """Fake runner: `responses` maps an argv tuple to (rc, stdout)."""

    def __init__(self, responses=None, default=(127, '')):
        self.responses = dict(responses or {})
        self.default = default
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        return self.responses.get(tuple(argv), self.default)


def ok_for(*cmds):
    return {(c, '--version'): (0, c + ' 1.0.0') for c in cmds}


def claude_helps():
    return {
        ('claude', '--help'): (0, CLAUDE_HELP),
        ('claude', 'mcp', '--help'): (0, CLAUDE_MCP_HELP),
        ('claude', 'mcp', 'add', '--help'): (0, CLAUDE_MCP_ADD_HELP),
        ('claude', 'plugin', '--help'): (0, CLAUDE_PLUGIN_HELP),
    }


class Base(unittest.TestCase):
    def setUp(self):
        cli_checks.reset_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        cli_checks.reset_cache()
        self.tmp.cleanup()

    def skill(self, name, body, requires=None, extra='', parent='skills'):
        folder = os.path.join(self.root, parent, name)
        os.makedirs(folder, exist_ok=True)
        req = '  catalog-requires: "%s"\n' % requires if requires is not None else ''
        with open(os.path.join(folder, 'SKILL.md'), 'w', encoding='utf-8') as fh:
            fh.write(FRONT.format(name=name, extra=extra, requires=req) + '\n# ' + name + '\n\n' + body)
        return folder

    def assert_scrub_clean(self, findings):
        for f in findings:
            self.assertEqual(_scrub.scan_text(f['evidence']), [], f['evidence'])
            self.assertNotIn(self.root, f['evidence'])
            if f['file']:
                self.assertFalse(os.path.isabs(f['file']), f['file'])


class TestCommandsFor(Base):
    def test_catalog_requires_collected(self):
        folder = self.skill('needs', 'Text.\n', requires='git, gh,node')
        cmds = cli_checks.commands_for(folder)
        self.assertEqual([c for _, _, c in cmds], ['git', 'gh', 'node'])
        self.assertTrue(all(f == 'skills/needs/SKILL.md' and line == 6 for f, line, _ in cmds))

    def test_fenced_block_first_words(self):
        body = (
            '```sh\nmake build\n```\n\n'
            '```bash\njq . file\n```\n\n'
            '```zsh\nrg pattern\n```\n\n'
            '```shell\nnpm test\n```\n\n'
            '```console\n$ curl -s x\noutput line here\n```\n\n'
            '```python\nprint("not shell")\n```\n\n'
            '```\nuntagged run\n```\n'
        )
        folder = self.skill('fences', body)
        self.assertEqual([c for _, _, c in cli_checks.commands_for(folder)],
                         ['make', 'jq', 'rg', 'npm', 'curl'])

    def test_builtins_dollar_assignments_sudo_and_continuations(self):
        body = (
            '```sh\n'
            '# a comment\n'
            '\n'
            '$ git status\n'
            'FOO=1 BAR=two python3 -m tool\n'
            'sudo -E brew install x\n'
            'sudo -u root launchctl list\n'
            'sudo FOO=1 jq . f\n'
            'cd somewhere\n'
            'export X=1\n'
            'echo hi\n'
            'if true; then\n'
            'for x in a b; do\n'
            'source env.sh\n'
            '. env.sh\n'
            'set -e\n'
            'docker run \\\n'
            '  --rm image\n'
            '  | tail -n 1\n'
            'cat <<EOF\n'
            'heredoc body line\n'
            'EOF\n'
            '```\n'
        )
        folder = self.skill('mixed', body)
        self.assertEqual([c for _, _, c in cli_checks.commands_for(folder)],
                         ['git', 'python3', 'brew', 'launchctl', 'jq', 'docker', 'cat'])


class TestCheckCommands(Base):
    def test_missing_command_gives_finding(self):
        folder = self.skill('a', 'Text.\n', requires='git,nosuchtool')
        run = FakeRun(ok_for('git'))
        findings = cli_checks.check_commands('a', folder, run)
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('missing-command', 'a:nosuchtool')])
        self.assertIn('not found on PATH', findings[0]['evidence'])
        self.assert_scrub_clean(findings)

    def test_nonzero_exit_on_both_gives_finding(self):
        folder = self.skill('b', '```sh\nbroken arg\n```\n')
        run = FakeRun({('broken', '--version'): (2, ''), ('broken', '--help'): (1, '')})
        findings = cli_checks.check_commands('b', folder, run)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['kind'], 'missing-command')
        self.assertEqual(findings[0]['line'], 11)
        self.assertIn('exited 2', findings[0]['evidence'])

    def test_working_command_gives_none_and_help_fallback(self):
        folder = self.skill('c', '```sh\nhelponly x\n```\n', requires='git')
        run = FakeRun({('git', '--version'): (0, 'git version 2'),
                       ('helponly', '--version'): (1, ''), ('helponly', '--help'): (0, 'usage')})
        self.assertEqual(cli_checks.check_commands('c', folder, run), [])

    def test_each_command_runs_once_per_job(self):
        f1 = self.skill('d1', '```sh\ngit log\ngit status\n```\n', requires='git')
        f2 = self.skill('d2', '```sh\ngit diff\n```\n', requires='git')
        run = FakeRun(ok_for('git'))
        cli_checks.check_commands('d1', f1, run)
        cli_checks.check_commands('d2', f2, run)
        self.assertEqual(run.calls, [('git', '--version')])


class TestCliRefs(Base):
    TOOLS = ['Bash', 'Read', 'Edit', 'Write', 'Grep', 'WebFetch', 'TodoWrite']

    def test_unknown_tool_gives_finding(self):
        folder = self.skill('t1', 'Use `WebBrowse` to open pages.\n',
                            extra='allowed-tools: Read Bash(git:*) Frobnicate\n')
        findings = cli_checks.check_cli_refs('t1', folder, self.TOOLS, FakeRun())
        self.assertEqual(sorted(f['subject'] for f in findings), ['t1:Frobnicate', 't1:WebBrowse'])
        self.assertTrue(all(f['kind'] == 'unknown-tool' for f in findings))
        self.assert_scrub_clean(findings)

    def test_known_tool_gives_none(self):
        body = ('Call `WebFetch`, then the `Read` tool. A `ValueError` is a code name,\n'
                'and `Parent` in plain prose is skipped.\n')
        folder = self.skill('t2', body, extra='allowed-tools: Read Grep\n')
        self.assertEqual(cli_checks.check_cli_refs('t2', folder, self.TOOLS, FakeRun()), [])

    def test_unknown_claude_flag_gives_finding(self):
        folder = self.skill('f1', 'Run `claude -p --frobnicate "x"` headless.\n')
        run = FakeRun(claude_helps())
        findings = cli_checks.check_cli_refs('f1', folder, self.TOOLS, run)
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('unknown-flag', 'f1:claude --frobnicate')])
        self.assert_scrub_clean(findings)

    def test_unknown_subcommand_gives_finding(self):
        folder = self.skill('f2', '```sh\nclaude teleport --now\n```\n')
        findings = cli_checks.check_cli_refs('f2', folder, self.TOOLS, FakeRun(claude_helps()))
        self.assertEqual([f['subject'] for f in findings], ['f2:claude teleport'])

    def test_known_flags_and_subcommand_flags_give_none(self):
        body = ('Run `claude -p --output-format stream-json` and `claude plugin eval --json`.\n\n'
                '```sh\nclaude mcp add --scope user name cmd\n```\n')
        folder = self.skill('f3', body)
        run = FakeRun(claude_helps())
        self.assertEqual(cli_checks.check_cli_refs('f3', folder, self.TOOLS, run), [])
        cli_checks.check_cli_refs('f3', folder, self.TOOLS, run)
        self.assertEqual(run.calls.count(('claude', '--help')), 1)

    def test_unknown_nested_subcommand_gives_finding(self):
        folder = self.skill('f5', 'Run `claude mcp teleport` and `claude mcp add --scope user`.\n')
        findings = cli_checks.check_cli_refs('f5', folder, self.TOOLS, FakeRun(claude_helps()))
        self.assertEqual([f['subject'] for f in findings], ['f5:claude mcp teleport'])

    def test_shell_scripts_are_checked(self):
        folder = self.skill('f6', 'Text.\n')
        os.makedirs(os.path.join(folder, 'scripts'))
        with open(os.path.join(folder, 'scripts', 'go.sh'), 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh\nclaude -p --print --bogus "x"\n')
        findings = cli_checks.check_cli_refs('f6', folder, self.TOOLS, FakeRun(claude_helps()))
        self.assertEqual([(f['file'], f['line'], f['subject']) for f in findings],
                         [('skills/f6/scripts/go.sh', 2, 'f6:claude --bogus')])

    def test_acronym_tool_counts_only_on_a_tool_line(self):
        folder = self.skill('f7', 'Use the `LSP` tool here.\nThe `API` is documented.\n')
        findings = cli_checks.check_cli_refs('f7', folder, self.TOOLS, FakeRun())
        self.assertEqual([f['subject'] for f in findings], ['f7:LSP'])

    def test_subcommand_must_be_listed_under_commands(self):
        folder = self.skill('f8', 'Run `claude mcp command` and `claude mcp list`.\n')
        findings = cli_checks.check_cli_refs('f8', folder, self.TOOLS, FakeRun(claude_helps()))
        self.assertEqual([f['subject'] for f in findings], ['f8:claude mcp command'])

    def test_short_flags_sudo_continuations_and_quoted_prompts(self):
        body = ('Run `claude -p "fix -z and teleport"` and `claude "hello there"`.\n\n'
                '```sh\n'
                'sudo -u root claude -z\n'
                'claude mcp add \\\n'
                '  --scope user \\\n'
                '  --bogus name cmd\n'
                '```\n')
        folder = self.skill('f9', body)
        findings = cli_checks.check_cli_refs('f9', folder, self.TOOLS, FakeRun(claude_helps()))
        self.assertEqual(sorted((f['subject'], f['line']) for f in findings),
                         [('f9:claude -z', 13), ('f9:claude mcp --bogus', 14)])

    def test_globals_aliases_end_of_options_and_camel_flags(self):
        codex_help = ('Usage: codex [OPTIONS] [PROMPT]\n\nCommands:\n'
                      '  exec  Run Codex non-interactively [aliases: e]\n'
                      '  mcp   Manage MCP servers\n\n'
                      'Options:\n  -p, --profile <PROFILE>  Config profile\n'
                      '      --oss  Use a local model\n')
        responses = claude_helps()
        responses[('claude', '--help')] = (0, CLAUDE_HELP + '  --allowedTools <tools>  Allowed tools\n')
        responses[('codex', '--help')] = (0, codex_help)
        responses[('codex', 'exec', '--help')] = (0, 'Usage: codex exec [OPTIONS]\n\nOptions:\n  --json  JSONL\n')
        responses[('codex', 'e', '--help')] = responses[('codex', 'exec', '--help')]
        body = ('Run `codex --profile p exec --json`, `codex e --json`,\n'
                '`claude --allowedTools Read --disallowedThing`,\n'
                '`claude mcp add name -- tool --tool-flag` and `codex --profile p teleport`.\n'
                'Also `codex --oss nowhere`, `codex --profile exec nested` and `codex "teleport"`.\n\n'
                '```sh\nsudo --user root git status\nsudo -R /var/empty --prompt "Pass word: " jq . f\n```\n')
        folder = self.skill('g1', body)
        run = FakeRun(responses)
        findings = cli_checks.check_cli_refs('g1', folder, self.TOOLS, run)
        self.assertEqual(sorted(f['subject'] for f in findings),
                         ['g1:claude --disallowedThing', 'g1:codex nested', 'g1:codex nowhere', 'g1:codex teleport'])
        self.assertEqual([c for _, _, c in cli_checks.commands_for(folder)], ['git', 'jq'])

    def test_cli_without_help_is_skipped(self):
        folder = self.skill('f4', 'Run `codex exec --nope`.\n')
        self.assertEqual(cli_checks.check_cli_refs('f4', folder, self.TOOLS, FakeRun()), [])


class TestParserRareSyntaxes(Base):
    """Shell syntaxes the parser used to miss (issue #80)."""

    TOOLS = TestCliRefs.TOOLS

    def test_sudo_inline_quoted_option_value(self):
        folder = self.skill('r1', '```sh\nsudo --prompt="Pass word: " jq . f\n```\n')
        findings = cli_checks.check_commands('r1', folder, FakeRun(ok_for('sudo')))
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('missing-command', 'r1:jq')])

    def test_operator_stuck_to_word(self):
        folder = self.skill('r2', '```sh\ncodex teleport; echo done\n```\n')
        responses = {('codex', '--help'): (0, 'Usage: codex\n\nCommands:\n  exec  Run\n')}
        findings = cli_checks.check_cli_refs('r2', folder, self.TOOLS, FakeRun(responses))
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('unknown-flag', 'r2:codex teleport')])

    def test_subcommand_in_odd_case_or_with_underscore(self):
        folder = self.skill('r3', 'Run `codex Teleport` or `codex teleport_now`.\n')
        responses = {('codex', '--help'): (0, 'Usage: codex\n\nCommands:\n  exec  Run\n')}
        findings = cli_checks.check_cli_refs('r3', folder, self.TOOLS, FakeRun(responses))
        self.assertEqual(sorted((f['kind'], f['subject']) for f in findings),
                         [('unknown-flag', 'r3:codex Teleport'), ('unknown-flag', 'r3:codex teleport_now')])

    def test_grouped_sudo_flags_taking_an_argument(self):
        folder = self.skill('r4', '```sh\nsudo -Eu root git status\nsudo -uroot gh pr list\n```\n')
        self.assertEqual([c for _, _, c in cli_checks.commands_for(folder)], ['git', 'gh'])
        findings = cli_checks.check_commands('r4', folder, FakeRun(ok_for('gh')))
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('missing-command', 'r4:git')])


def npm(latest, times):
    return lambda package: {'latest': latest, 'time': times}


class TestCliLag(Base):
    NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)

    def run_for(self, claude, codex):
        return FakeRun({('claude', '--version'): (0, claude + ' (Claude Code)'),
                        ('codex', '--version'): (0, 'codex-cli ' + codex)})

    def test_one_minor_behind_gives_cli_lag(self):
        times = {'2.1.311': '2026-11-10T00:00:00Z', '2.2.0': '2026-11-15T00:00:00Z'}
        state = {}
        findings = cli_checks.check_cli_lag(self.run_for('2.1.311', '2.2.0'), npm('2.2.0', times), self.NOW, state)
        self.assertEqual([(f['kind'], f['subject']) for f in findings], [('cli-lag', 'cli:claude')])
        self.assertIn('minor', findings[0]['evidence'])
        self.assert_scrub_clean(findings)

    def test_31_days_behind_gives_cli_lag(self):
        times = {'2.1.300': '2026-10-01T00:00:00Z', '2.1.330': '2026-11-01T00:00:00Z'}
        findings = cli_checks.check_cli_lag(self.run_for('2.1.300', '2.1.300'), npm('2.1.330', times), self.NOW)
        self.assertEqual(sorted(f['subject'] for f in findings), ['cli:claude', 'cli:codex'])
        self.assertTrue(all('31 days' in f['evidence'] for f in findings))

    def test_30_days_patch_behind_gives_none(self):
        times = {'2.1.300': '2026-10-02T00:00:00Z', '2.1.330': '2026-11-01T00:00:00Z'}
        self.assertEqual(cli_checks.check_cli_lag(self.run_for('2.1.300', '2.1.330'), npm('2.1.330', times), self.NOW), [])

    def test_just_over_30_days_gives_cli_lag(self):
        times = {'2.1.300': '2026-10-02T00:00:00Z', '2.1.330': '2026-11-01T00:00:01Z'}
        findings = cli_checks.check_cli_lag(self.run_for('2.1.300', '2.1.330'), npm('2.1.330', times), self.NOW)
        self.assertEqual([f['subject'] for f in findings], ['cli:claude'])

    def test_current_gives_none_and_versions_recorded(self):
        state = {'other': 1}
        findings = cli_checks.check_cli_lag(self.run_for('2.1.311', '2.1.311'), npm('2.1.311', {}), self.NOW, state)
        self.assertEqual(findings, [])
        self.assertEqual(state['other'], 1)
        self.assertEqual(state['cli'], {
            'claude': {'installed': '2.1.311', 'latest': '2.1.311', 'checked_at': '2026-11-18T12:00:00Z'},
            'codex': {'installed': '2.1.311', 'latest': '2.1.311', 'checked_at': '2026-11-18T12:00:00Z'},
        })

    def test_fetch_failure_records_none(self):
        def broken(package):
            raise OSError('offline')
        state = {}
        self.assertEqual(cli_checks.check_cli_lag(self.run_for('2.1.1', '0.1.0'), broken, self.NOW, state), [])
        self.assertIsNone(state['cli']['claude']['latest'])


SOURCE_BODY = (
    '# sdd-x\n\nSome verbatim body text.\n\n## Changelog\n\n'
    '| Version | Date | Change |\n| --- | --- | --- |\n'
    '| 1.1.0 | 2026-10-23 | Second change |\n'
    '| 1.0.0 | 2026-10-01 | First release |\n'
)


class TestHeldDrift(Base):
    def setUp(self):
        super().setUp()
        self.src_dir = os.path.join(self.root, 'private-source')
        os.makedirs(self.src_dir)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)

    def held(self, name, body):
        self.write(os.path.join(self.root, '.held', name, 'SKILL.md'),
                   '---\nname: %s\nmetadata:\n  catalog-consumers: "none"\n---\n%s' % (name, body))

    def source(self, name, body):
        path = os.path.join(self.src_dir, name, 'SKILL.md')
        self.write(path, '---\nname: %s\nargument-hint: "[x]"\n---\n%s' % (name, body))
        return path

    def test_identical_bodies_give_nothing(self):
        self.held('sdd-x', SOURCE_BODY)
        sources = {'sdd-x': self.source('sdd-x', SOURCE_BODY)}
        self.assertEqual(held_drift.check_held(self.root, sources, set()), [])

    def test_changed_body_gives_drift_with_hashes_only(self):
        secret_line = 'Only in the private ' + 'source.'
        self.held('sdd-x', SOURCE_BODY)
        sources = {'sdd-x': self.source('sdd-x', SOURCE_BODY.replace('Some verbatim body text.', secret_line))}
        findings = held_drift.check_held(self.root, sources, set())
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual((f['kind'], f['skill'], f['line']), ('held-drift', 'sdd-x', None))
        self.assertNotIn(secret_line, f['evidence'])
        self.assertNotIn('private-source', f['evidence'])
        self.assertNotIn('/', f['evidence'])
        self.assertRegex(f['evidence'], r'^sdd-x: catalog body sha256 [0-9a-f]{64}, source body sha256 [0-9a-f]{64}$')
        self.assert_scrub_clean(findings)

    def test_changed_first_changelog_row_alone_gives_nothing(self):
        self.held('sdd-x', SOURCE_BODY.replace('| 1.1.0 | 2026-10-23 | Second change |',
                                               '| 1.1.0 | 2026-11-11 | Imported as a held mirror |'))
        sources = {'sdd-x': self.source('sdd-x', SOURCE_BODY)}
        self.assertEqual(held_drift.check_held(self.root, sources, set()), [])

    def test_added_import_row_gives_nothing(self):
        self.held('sdd-x', SOURCE_BODY.replace('| --- | --- | --- |\n',
                                               '| --- | --- | --- |\n| 1.1.0 | 2026-11-11 | Imported |\n'))
        sources = {'sdd-x': self.source('sdd-x', SOURCE_BODY)}
        self.assertEqual(held_drift.check_held(self.root, sources, set()), [])

    def test_changed_second_changelog_row_gives_drift(self):
        self.held('sdd-x', SOURCE_BODY.replace('First release', 'Rewritten history'))
        sources = {'sdd-x': self.source('sdd-x', SOURCE_BODY)}
        self.assertEqual(len(held_drift.check_held(self.root, sources, set())), 1)

    def test_missing_source_gives_finding_without_path(self):
        self.held('sdd-x', SOURCE_BODY)
        missing = os.path.join(self.src_dir, 'gone', 'SKILL.md')
        findings = held_drift.check_held(self.root, {'sdd-x': missing}, set())
        self.assertEqual([f['evidence'] for f in findings], ['sdd-x: source missing'])

    def test_excluded_and_unmapped_held_names_are_skipped(self):
        self.held('sdd-x', SOURCE_BODY)
        self.held('sdd-y', SOURCE_BODY)
        self.held('zz-canary', SOURCE_BODY)
        sources = {'sdd-x': self.source('sdd-x', 'different\n'),
                   'zz-canary': self.source('zz-canary', 'different\n')}
        self.assertEqual(held_drift.check_held(self.root, sources, {'sdd-x'}), [])


if __name__ == '__main__':
    unittest.main()
