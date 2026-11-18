"""search_skills, skill_status and the FastMCP pin notice (MCP-3, MCP-4, MCP-8, INV-7, CAT-20)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import tempfile
import tomllib
import unittest
import unittest.mock
import urllib.request

from fastmcp import Client, FastMCP

from skills_catalog_mcp import findings, tools, version
from skills_catalog_mcp.app import build_server
from skills_catalog_mcp.snapshot import materialize
from tests.fixtures import catalog_json, make_catalog_repo, skill_md

SERVER = pathlib.Path(__file__).resolve().parents[1]
ISSUES = findings.ISSUE_PREFIX


def _skills(spec):
    """Map name -> description into the fixture's skill dict."""
    return {name: {'SKILL.md': skill_md(name, desc)} for name, desc in spec.items()}


class _FakeFetcher:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.result


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.exclude = os.path.join(self.tmp, 'exclude.txt')
        # No test may open a network connection. A call is recorded and fails
        # the test in cleanup, because the tools catch fetcher exceptions.
        patcher = unittest.mock.patch.object(urllib.request, 'urlopen', side_effect=AssertionError('network used'))
        self.urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: self.urlopen.assert_not_called())

    def snapshot(self, skills, held=None, manifest=None, exclude=()):
        with open(self.exclude, 'w', encoding='utf-8') as handle:
            handle.write('# local\n' + ''.join(n + '\n' for n in exclude))
        repo = make_catalog_repo(self.tmp, skills=skills, held=held, manifest=manifest)
        snap = materialize(repo, None, exclude_file=self.exclude)
        self.addCleanup(snap.cleanup)
        return snap

    def server(self, snap, fetcher=None, excluded=None):
        self.fetcher = fetcher if fetcher is not None else _FakeFetcher()
        mcp = FastMCP('test')
        tools.register(mcp, snap, findings_fetcher=self.fetcher, excluded=excluded)
        return mcp

    async def call(self, mcp, tool, **args):
        async with Client(mcp) as client:
            return await client.call_tool(tool, args, raise_on_error=False)

    async def search(self, mcp, query, **kw):
        result = await self.call(mcp, 'search_skills', query=query, **kw)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content['result']

    async def status(self, mcp, name):
        result = await self.call(mcp, 'skill_status', name=name)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    def error_of(self, result):
        self.assertTrue(result.is_error)
        return result.content[0].text


SEARCH_SPEC = {
    'alpha-debug': 'Use when tracing a failing build.',
    'beta-notes': 'Use when you need to debug a flaky test.',
    'gamma-docs': 'Use when writing release notes.',
}


class SearchSkillsTests(_Base):
    async def test_name_hit_ranks_before_description_hit(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        found = await self.search(mcp, 'debug')
        self.assertEqual([f['name'] for f in found], ['alpha-debug', 'beta-notes'])
        self.assertEqual(found[0], {
            'name': 'alpha-debug',
            'description': 'Use when tracing a failing build.',
            'uri': 'skill://alpha-debug/SKILL.md',
        })

    async def test_ties_sort_by_name(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        found = await self.search(mcp, 'notes')
        # beta-notes has it in its name; gamma-docs only in its description.
        self.assertEqual([f['name'] for f in found], ['beta-notes', 'gamma-docs'])
        found = await self.search(mcp, 'use')
        self.assertEqual([f['name'] for f in found], sorted(SEARCH_SPEC))

    async def test_matching_is_case_insensitive_whole_words(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        self.assertEqual([f['name'] for f in await self.search(mcp, 'DeBuG')], ['alpha-debug', 'beta-notes'])
        self.assertEqual([f['name'] for f in await self.search(mcp, 'FLAKY')], ['beta-notes'])
        self.assertEqual(await self.search(mcp, 'deb'), [])  # a part of a word is not a word
        self.assertEqual(await self.search(mcp, 'flak'), [])

    async def test_results_are_capped_at_twenty(self):
        spec = {'skill-%02d' % i: 'Use when testing the cap.' for i in range(25)}
        mcp = self.server(self.snapshot(_skills(spec)))
        found = await self.search(mcp, 'cap')
        self.assertEqual(len(found), 20)
        self.assertEqual([f['name'] for f in found], sorted(spec)[:20])

    async def test_empty_query_fails(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        for query in ('', '   ', '--'):
            self.assertEqual(self.error_of(await self.call(mcp, 'search_skills', query=query)), tools.EMPTY_QUERY)

    async def test_third_party_scope_fails_with_message(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        result = await self.call(mcp, 'search_skills', query='debug', scope='third-party')
        self.assertEqual(self.error_of(result), 'third-party index is not available')

    async def test_unknown_scope_fails(self):
        mcp = self.server(self.snapshot(_skills(SEARCH_SPEC)))
        for scope in ('everything', 'Catalog', ''):
            result = await self.call(mcp, 'search_skills', query='debug', scope=scope)
            self.assertEqual(self.error_of(result), 'unknown scope')

    def test_description_follows_the_lint_grammar(self):
        cases = {
            '---\nname: x\ndescription: Use when debugging: follow logs\n---\n': 'Use when debugging: follow logs',
            '---\nname: x\ndescription: "Use when \\"quoted\\" text"\n---\n': 'Use when "quoted" text',
            "---\ndescription: 'It''s for logs'\nname: x\n---\n": "It's for logs",
            '---\nname: x\ndescription: >-\n  Use when folding\n  long lines\nmetadata:\n  a: "b"\n---\n': 'Use when folding long lines',
            '---\nname: x\nmetadata:\n  description: "nested"\n---\n': '',
            'no frontmatter\ndescription: x\n': '',
            '---\ndescription: unterminated\n': '',
        }
        for text, expected in cases.items():
            self.assertEqual(tools.parse_description(text), expected, text)

    def test_description_equals_the_lint_parser(self):
        spec = importlib.util.spec_from_file_location('catalog_lint', SERVER.parent / 'tools' / 'lint.py')
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        bodies = [
            'description: Use when debugging: follow logs',
            'description:   Use when   spaced  out  ',
            'description: "Use when \\"quoted\\" and \\\\ slashed"',
            "description: 'It''s for logs'",
            'description: |\n  Use when\n\n  literal',
            'description: |-\n  Use when\n  literal',
            'description: >\n  Use when\n\n  folded',
            'description: >-\n  Use when\n  folded',
        ]
        for body in bodies:
            text = '---\nname: x\n' + body + '\nmetadata:\n  a: "b"\n---\n'
            self.assertEqual(tools.parse_description(text), lint.parse_frontmatter(text)[0]['description'], body)

    async def test_description_with_a_colon_is_searchable(self):
        mcp = self.server(self.snapshot(_skills({'alpha-skill': 'Use when debugging: follow logs'})))
        found = await self.search(mcp, 'logs')
        self.assertEqual(found[0]['description'], 'Use when debugging: follow logs')

    async def test_unicode_words_are_whole_words(self):
        mcp = self.server(self.snapshot(_skills({'alpha-skill': 'Use when ordering at a café.'})))
        self.assertEqual(await self.search(mcp, 'caf'), [])
        self.assertEqual([f['name'] for f in await self.search(mcp, 'CAFÉ')], ['alpha-skill'])

    async def test_held_canary_and_excluded_never_match(self):
        hidden = 'hidden-' + 'secretive'
        skills = _skills({'alpha-debug': 'Use when debugging.', hidden: 'Use when debugging privately.'})
        held = _skills({'zz-canary': 'Use when debugging the canary.', 'sdd-held': 'Use when debugging held work.'})
        mcp = self.server(self.snapshot(skills, held=held, exclude=[hidden.upper()]))
        for query in ('debugging', 'canary', 'held', 'sdd', 'zz', 'privately', hidden):
            names = [f['name'] for f in await self.search(mcp, query)]
            self.assertTrue(set(names) <= {'alpha-debug'}, (query, names))
        self.assertEqual([f['name'] for f in await self.search(mcp, 'debugging')], ['alpha-debug'])


def _entry_manifest(skills, **extra):
    """catalog.json text with extra keys merged into named skill entries."""
    data = json.loads(catalog_json(skills))
    for entry in data['skills']:
        entry.update(extra.get(entry['name'], {}))
    return json.dumps(data, indent=2) + '\n'


class SkillStatusTests(_Base):
    async def test_fields_equal_the_tag_catalog_entry(self):
        skills = _skills({'alpha-skill': 'Use when testing.', 'beta-skill': 'Use when testing more.'})
        manifest = _entry_manifest(skills, **{'alpha-skill': {'version': '2.3.4', 'owner': 'someone', 'last_verified': '2099-02-03'}})
        snap = self.snapshot(skills, manifest=manifest)
        status = await self.status(self.server(snap), 'alpha-skill')
        entry = next(e for e in json.loads(manifest)['skills'] if e['name'] == 'alpha-skill')
        for key in ('name', 'version', 'owner', 'last_verified'):
            self.assertEqual(status[key], entry[key], key)
        self.assertEqual(status['catalog_tag'], 'v2099.1.1.1')
        self.assertEqual(status['catalog_commit'], snap.commit)
        self.assertEqual(set(status), {
            'name', 'version', 'owner', 'last_verified', 'stale', 'findings', 'findings_error',
            'catalog_tag', 'catalog_commit', 'fastmcp_version', 'fastmcp_prerelease', 'notice',
        })

    async def test_stale_passes_through_and_defaults_to_false(self):
        skills = _skills({'alpha-skill': 'Use when testing.', 'beta-skill': 'Use when testing more.'})
        manifest = _entry_manifest(skills, **{'alpha-skill': {'stale': True}})
        self.assertNotIn('stale', next(e for e in json.loads(manifest)['skills'] if e['name'] == 'beta-skill'))
        mcp = self.server(self.snapshot(skills, manifest=manifest))
        self.assertIs((await self.status(mcp, 'alpha-skill'))['stale'], True)
        self.assertIs((await self.status(mcp, 'beta-skill'))['stale'], False)

    async def test_fetcher_findings_are_returned(self):
        issues = [{'url': ISSUES + '7', 'title': 're-review: alpha-skill'}]
        mcp = self.server(self.snapshot(['alpha-skill']), _FakeFetcher(issues))
        status = await self.status(mcp, 'alpha-skill')
        self.assertEqual(status['findings'], issues)
        self.assertIsNone(status['findings_error'])
        self.assertEqual(self.fetcher.calls, ['alpha-skill'])

    async def test_findings_naming_an_excluded_skill_are_dropped(self):
        hidden = 'private-' + 'thing'
        issues = [
            {'url': ISSUES + '7', 'title': 're-review: alpha-skill'},
            {'url': ISSUES + '8', 'title': 'upstream-change: alpha-skill and ' + hidden.upper()},
        ]
        snap = self.snapshot(['alpha-skill', hidden], exclude=[hidden])
        mcp = self.server(snap, _FakeFetcher(issues), excluded=[hidden])
        status = await self.status(mcp, 'alpha-skill')
        self.assertEqual(status['findings'], issues[:1])
        self.assertNotIn(hidden, json.dumps(status).lower())

    async def test_excluded_list_comes_from_the_snapshot(self):
        hidden = 'private-' + 'thing'
        snap = self.snapshot(['alpha-skill', hidden], exclude=[hidden.upper()])
        self.assertNotIn(hidden, repr(snap))
        issues = [
            {'url': ISSUES + '8', 'title': 'freshness-alert: drift: alpha-skill ' + hidden},
            {'url': ISSUES + '9', 'title': 're-review: alpha-skill (see ' + hidden + '-extra)'},
            {'url': ISSUES + '10', 'title': 're-review: alpha-skill'},
        ]
        status = await self.status(self.server(snap, _FakeFetcher(issues)), 'alpha-skill')
        self.assertEqual(status['findings'], issues[2:])
        self.assertNotIn(hidden, json.dumps(status).lower())

    async def test_fetcher_that_raises_gives_empty_findings_and_an_error(self):
        mcp = self.server(self.snapshot(['alpha-skill']), _FakeFetcher(error=OSError('boom')))
        status = await self.status(mcp, 'alpha-skill')
        self.assertEqual(status['findings'], [])
        self.assertEqual(status['findings_error'], 'github unavailable')
        self.assertEqual(status['version'], '1.0.0')

    async def test_excluded_and_nonexistent_names_give_identical_errors(self):
        hidden = 'private-' + 'thing'
        snap = self.snapshot(['alpha-skill', hidden], held=['zz-canary'], exclude=[hidden])
        mcp = self.server(snap)
        answers = []
        for name in (hidden, 'no-such-skill', 'zz-canary', hidden.upper()):
            result = await self.call(mcp, 'skill_status', name=name)
            self.assertTrue(result.is_error)
            answers.append((result.content[0].text.encode('utf-8'), result.structured_content))
            self.assertNotIn(name.lower(), result.content[0].text.lower())
        self.assertEqual(answers[0][0], b'unknown skill')
        self.assertEqual(len(set(a[0] for a in answers)), 1)
        self.assertEqual(len(set(repr(a[1]) for a in answers)), 1)
        self.assertEqual(self.fetcher.calls, [])

    async def test_bad_arguments_never_echo_the_input(self):
        hidden = 'private-' + 'thing'
        mcp = self.server(self.snapshot(['alpha-skill', hidden], exclude=[hidden]))
        calls = [
            ('skill_status', {'name': [hidden]}),
            ('skill_status', {'name': 'alpha-skill', 'extra': hidden}),
            ('skill_status', {}),
            ('search_skills', {'query': {'q': hidden}}),
        ]
        for tool, args in calls:
            result = await self.call(mcp, tool, **args)
            self.assertEqual(self.error_of(result), tools.INVALID_ARGUMENTS, args)
        self.assertEqual(self.fetcher.calls, [])

    async def test_build_server_registers_both_tools(self):
        mcp = build_server(self.snapshot(_skills(SEARCH_SPEC)))
        async with Client(mcp) as client:
            names = sorted(t.name for t in await client.list_tools())
            result = await client.call_tool('search_skills', {'query': 'debug'})
        self.assertEqual(names, ['search_skills', 'skill_status'])
        self.assertEqual([f['name'] for f in result.structured_content['result']], ['alpha-debug', 'beta-notes'])


class PinNoticeTests(_Base):
    def pin(self):
        with open(SERVER / 'pyproject.toml', 'rb') as handle:
            deps = tomllib.load(handle)['project']['dependencies']
        pins = [d.split('==', 1)[1] for d in deps if d.startswith('fastmcp==')]
        self.assertEqual(len(pins), 1, deps)
        return pins[0]

    def test_is_prerelease(self):
        for v in ('4.2.0a5', '4.2.0rc1', '4.2.1.dev3', '4.2.0b2', '4.2.0.alpha1', '4.2.1rc1.dev2', 'not-a-version'):
            self.assertTrue(version.is_prerelease(v), v)
        for v in ('4.2.1', '4.2.0', '4.2.1.post1', '4.2.1+local.1'):
            self.assertFalse(version.is_prerelease(v), v)

    async def test_prerelease_pin_sets_flag_and_notice(self):
        mcp = self.server(self.snapshot(['alpha-skill']))
        with unittest.mock.patch.object(version, 'fastmcp_version', return_value='4.2.0a5'):
            status = await self.status(mcp, 'alpha-skill')
        self.assertEqual(status['fastmcp_version'], '4.2.0a5')
        self.assertIs(status['fastmcp_prerelease'], True)
        self.assertEqual(status['notice'], 'FastMCP 4.2.0a5 is a pre-release; the skills API may change')

    async def test_stable_pin_has_no_notice(self):
        mcp = self.server(self.snapshot(['alpha-skill']))
        with unittest.mock.patch.object(version, 'fastmcp_version', return_value='4.2.1'):
            status = await self.status(mcp, 'alpha-skill')
        self.assertIs(status['fastmcp_prerelease'], False)
        self.assertIsNone(status['notice'])

    async def test_installed_version_is_the_pin(self):
        pin = self.pin()
        self.assertEqual(version.fastmcp_version(), pin)
        status = await self.status(self.server(self.snapshot(['alpha-skill'])), 'alpha-skill')
        self.assertEqual(status['fastmcp_version'], pin)
        self.assertEqual(status['fastmcp_prerelease'], version.is_prerelease(pin))

    def test_readme_section_names_the_pin_and_prerelease_sentence(self):
        pin = self.pin()
        text = (SERVER / 'README.md').read_text(encoding='utf-8')
        start = text.index('\n## FastMCP version\n')
        end = text.find('\n## ', start + 1)
        section = text[start:end if end != -1 else len(text)]
        self.assertIn('`fastmcp==%s`' % pin, section)
        self.assertEqual('This is a pre-release of FastMCP.' in section, version.is_prerelease(pin))


class GitHubFindingsTests(unittest.TestCase):
    def payload(self, items):
        return _Response(json.dumps({'total_count': len(items), 'items': items}).encode('utf-8'))

    def test_parses_filters_and_caches(self):
        items = [
            {'html_url': ISSUES + '2', 'title': 'freshness-alert: stale: alpha-skill'},
            {'html_url': ISSUES + '3', 'title': 'upstream-change: alpha-skill-extra'},
            {'html_url': ISSUES + '4', 'title': 're-review: ALPHA-SKILL', 'pull_request': {}},
            {'html_url': 'https://github.com/other/repo/issues/5', 'title': 'alpha-skill'},
            {'html_url': ISSUES + '1', 'title': 're-review: Alpha-Skill now'},
        ]
        seen = []
        now = [1000.0]

        def urlopen(request, timeout):
            seen.append((request.full_url, timeout, request.get_header('Authorization')))
            return self.payload(items)

        fetch = findings.GitHubFindings(urlopen=urlopen, clock=lambda: now[0])
        expected = [
            {'url': ISSUES + '1', 'title': 're-review: Alpha-Skill now'},
            {'url': ISSUES + '2', 'title': 'freshness-alert: stale: alpha-skill'},
        ]
        self.assertEqual(fetch('alpha-skill'), expected)
        now[0] += 599
        self.assertEqual(fetch('alpha-skill'), expected)
        self.assertEqual(len(seen), 1)
        now[0] += 2
        fetch('alpha-skill')
        self.assertEqual(len(seen), 2)
        url, timeout, auth = seen[0]
        self.assertTrue(url.startswith(findings.SEARCH_URL + '?'))
        self.assertIn('label%3Afreshness%2Cre-review%2Cupstream-change%2Cfreshness-alert', url)
        self.assertIn('alpha-skill', url)
        self.assertEqual(timeout, 5)
        self.assertIsNone(auth)

    def test_failures_become_unavailable(self):
        def broken(request, timeout):
            raise OSError('down')

        def garbage(request, timeout):
            return _Response(b'not json')

        def incomplete(request, timeout):
            body = {'incomplete_results': True, 'items': [{'html_url': ISSUES + '1', 'title': 'alpha-skill'}]}
            return _Response(json.dumps(body).encode('utf-8'))

        for urlopen in (broken, garbage, incomplete):
            fetch = findings.GitHubFindings(urlopen=urlopen)
            self.assertEqual(findings.lookup(fetch, 'alpha-skill'), ([], 'github unavailable'))
        self.assertEqual(findings.lookup(lambda name: [{'nope': 1}], 'alpha-skill'), ([], 'github unavailable'))


if __name__ == '__main__':
    unittest.main()
