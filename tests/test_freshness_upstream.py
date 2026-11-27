"""Tests for the upstream watch (SPEC FRS-10, FRS-11, FRS-14; DESIGN 7.1, 7.8).

No test touches the network: `fetch` is a fake that serves fixture pages from
tests/fixtures/upstream/.
"""

import datetime
import json
import os
import shutil
import tempfile
import unittest

from freshness import upstream
from tools import scrub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, 'tests', 'fixtures', 'upstream')
SCHEMA = os.path.join(ROOT, 'schema', 'upstream.json')

FRS_11_PREFIXES = (
    'claude-code-skills', 'claude-code-releases', 'claude-code-tools', 'claude-code-plugins',
    'codex-skills', 'codex-releases', 'agent-skills-spec', 'fastmcp-docs', 'fastmcp-pypi',
    'anthropic-models', 'openai-models',
)

FINDING_KEYS = {'kind', 'skill', 'subject', 'file', 'line', 'evidence', 'fix'}
NOW = datetime.datetime(2026, 11, 18, 11, 0, 0)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding='utf-8') as fh:
        return fh.read()


def finding_shape_errors(finding):
    """Part 1's Finding shape, restated (part 1 is not imported)."""
    errors = []
    if set(finding) != FINDING_KEYS:
        errors.append('keys %s' % sorted(set(finding) ^ FINDING_KEYS))
        return errors
    for key in ('kind', 'skill', 'subject', 'evidence'):
        if not isinstance(finding[key], str) or not finding[key]:
            errors.append('%s must be a non-empty string' % key)
    if finding['file'] is not None and not isinstance(finding['file'], str):
        errors.append('file must be a string or None')
    if finding['line'] is not None and not isinstance(finding['line'], int):
        errors.append('line must be an int or None')
    if finding['fix'] is not None and not isinstance(finding['fix'], dict):
        errors.append('fix must be a dict or None')
    return errors


class FakeFetch(object):
    """`fetch(url) -> (status, text)` serving a mutable url table."""

    def __init__(self, pages):
        self.pages = dict(pages)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value


SKILLS_URL = 'https://docs.example.com/skills.md'
TOOLS_URL = 'https://docs.example.com/tools-reference.md'

FRONTMATTER = {
    'id': 'claude-code-skills-frontmatter', 'url': SKILLS_URL,
    'extract': 'markdown-sections', 'params': {'headings': ['Frontmatter reference']},
}
LOCATIONS = {
    'id': 'claude-code-skills-locations', 'url': SKILLS_URL,
    'extract': 'markdown-sections', 'params': {'headings': ['Choose where skills load']},
}
TOOLS = {
    'id': 'claude-code-tools', 'url': TOOLS_URL,
    'extract': 'table-column', 'params': {'heading': '# Tools reference', 'column': 'Tool'},
}


class SchemaTest(unittest.TestCase):

    def test_schema_loads_and_covers_every_frs11_area(self):
        sources = upstream.load_sources(SCHEMA)
        ids = [s['id'] for s in sources]
        self.assertEqual(len(ids), len(set(ids)))
        for prefix in FRS_11_PREFIXES:
            self.assertTrue(any(i.startswith(prefix) for i in ids), prefix)
        for source in sources:
            self.assertIn(source['extract'], upstream.EXTRACT_KINDS)
            self.assertTrue(source['url'].startswith('https://'))
        self.assertIn(upstream.TOOLS_SOURCE, ids)
        self.assertIn(upstream.FIELDS_SOURCE, ids)

    def test_schema_holds_no_last_seen_hashes(self):
        # INV-5: hashes live in state.sources on freshness-state, not on main.
        with open(SCHEMA, encoding='utf-8') as fh:
            text = fh.read()
        self.assertNotIn('sha256', text)
        self.assertEqual(scrub.scan_text(text, 'schema/upstream.json'), [])

    def test_load_sources_rejects_duplicates_and_unknown_kinds(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, 'upstream.json')
        good = {'id': 'a-source', 'url': 'https://example.com/a', 'extract': 'npm-version'}
        cases = [
            {'version': 1, 'sources': [good, dict(good)]},
            {'version': 1, 'sources': [dict(good, extract='rss')]},
            {'version': 1, 'sources': [dict(good, extract='markdown-sections')]},
            {'version': 1, 'sources': [dict(good, extract='table-column', params={'heading': 'X'})]},
            {'version': 1, 'sources': [dict(good, url='http://example.com/a')]},
            {'version': 2, 'sources': [good]},
        ]
        for data in cases:
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(data, fh)
            with self.assertRaises(ValueError, msg=data):
                upstream.load_sources(path)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'version': 1, 'sources': [good]}, fh)
        self.assertEqual(upstream.load_sources(path), [good])


class NormalizeTest(unittest.TestCase):

    def test_normalize_is_deterministic_and_drops_footer_date(self):
        raw = fixture('skills.md')
        page = dict(FRONTMATTER, params={'headings': ['# Extend Claude with skills']})
        first = upstream.normalize(page, raw)
        self.assertEqual(first, upstream.normalize(page, raw))
        self.assertNotIn('Last updated', first)
        self.assertNotIn('November 17, 2026', first)
        self.assertNotIn('Edit this page', first)
        self.assertNotIn('utm_source', first)
        self.assertIn('https://example.com/plugins)', first)
        # A different footer date and extra whitespace give the same extract.
        moved = raw.replace('November 17, 2026', 'December 1, 2026').replace(
            '| `name` | No |', '|   `name`   |  No  |')
        moved = moved.replace('\n', '\r\n')
        self.assertEqual(first, upstream.normalize(page, moved))

    def test_markdown_sections_keeps_subsections_and_skips_fences(self):
        out = upstream.normalize(FRONTMATTER, fixture('skills.md'))
        self.assertTrue(out.startswith('### Frontmatter reference'))
        self.assertIn('#### How a skill gets its command name', out)
        self.assertIn('# not a heading: inside a fence', out)
        self.assertNotIn('Types of skill content', out)
        self.assertNotIn('Choose where skills load', out)

        two = dict(FRONTMATTER, params={'headings': ['Frontmatter reference', 'No such heading']})
        self.assertIn('[missing section: No such heading]', upstream.normalize(two, fixture('skills.md')))
        none = dict(FRONTMATTER, params={'headings': ['No such heading']})
        with self.assertRaises(upstream.ExtractError):
            upstream.normalize(none, fixture('skills.md'))

    def test_html_is_reduced_to_text(self):
        source = {'id': 'codex-skills', 'url': 'https://example.com/codex',
                  'extract': 'markdown-sections', 'params': {'headings': ['# Build skills']}}
        out = upstream.normalize(source, fixture('page.html'))
        self.assertEqual(out, upstream.normalize(source, fixture('page.html')))
        self.assertIn('# Build skills', out)
        self.assertIn('## Where skills load', out)
        self.assertIn('Skills package instructions for an agent.', out)
        self.assertIn('| Repo | .agents/skills |', out)
        for dropped in ('window.track', 'color: red', 'Home', 'Last updated', 'Copyright'):
            self.assertNotIn(dropped, out)

    def test_versions_and_releases(self):
        npm = {'id': 'x-npm', 'url': 'https://example.com/n', 'extract': 'npm-version'}
        self.assertEqual(upstream.normalize(npm, fixture('npm-latest.json')), 'latest: 2.1.327')

        pypi = {'id': 'x-pypi', 'url': 'https://example.com/p', 'extract': 'pypi-version'}
        self.assertEqual(upstream.normalize(pypi, fixture('pypi.json')).split('\n'), [
            'latest: 4.1.2',
            '4.10.0b2 (pre-release)',
            '4.2.0rc1 (pre-release)',
            '4.1.2',
            '4.1.1 (yanked)',
            '4.1.0',
            '3.9.0',
        ])

        rel = {'id': 'x-gh', 'url': 'https://example.com/r', 'extract': 'github-releases'}
        out = upstream.normalize(rel, fixture('github-releases.json')).split('\n')
        self.assertEqual(out[0], 'v2.1.328-alpha.1 2026-11-18 (pre-release)')
        self.assertIn('v2.1.327 2026-11-17', out)
        self.assertIn('  - Added the `--skills-dir` flag', out)
        self.assertIn('  - Plugin installs now verify the manifest', out)
        self.assertIn('  - New slash command /recap', out)
        self.assertIn('  - The Read tool reads notebooks', out)
        self.assertNotIn('  - Fixed a crash on startup', out)
        self.assertFalse(any('v2.1.999' in line for line in out))

        stable = dict(rel, params={'include_prereleases': False, 'limit': 1})
        self.assertEqual(upstream.normalize(stable, fixture('github-releases.json')).split('\n')[0],
                         'v2.1.327 2026-11-17')
        with self.assertRaises(upstream.ExtractError):
            upstream.normalize(npm, '<html>rate limited</html>')

        # `/releases/latest` returns a single release object.
        latest = json.dumps(json.loads(fixture('github-releases.json'))[0])
        self.assertEqual(upstream.normalize(stable, latest).split('\n')[0], 'v2.1.327 2026-11-17')
        with self.assertRaises(upstream.ExtractError):
            upstream.normalize(rel, '{"message": "Not Found"}')


class WatchTest(unittest.TestCase):

    def run_watch(self, sources, state, fetch, run_id):
        return upstream.watch(sources, state, fetch, NOW, run_id)

    def test_first_fetch_is_a_baseline_with_no_finding(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        findings, alerts, state = self.run_watch([FRONTMATTER], {}, fetch, 'r1')
        self.assertEqual((findings, alerts), ([], []))
        entry = state['claude-code-skills-frontmatter']
        self.assertEqual(entry['sha256'], upstream._sha256(entry['extract']))
        self.assertEqual(len(entry['sha256']), 64)
        self.assertEqual(entry['fetched_at'], '2026-11-18T11:00:00Z')
        self.assertEqual(entry['failures'], 0)
        self.assertIsNone(entry['last_error'])

    def test_unchanged_extract_gives_no_finding(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        _, _, state = self.run_watch([FRONTMATTER], {}, fetch, 'r1')
        # Only the footer date moved.
        fetch.pages[SKILLS_URL] = (200, fixture('skills.md').replace('November 17', 'November 18'))
        findings, alerts, state2 = self.run_watch([FRONTMATTER], state, fetch, 'r2')
        self.assertEqual((findings, alerts), ([], []))
        self.assertEqual(state2['claude-code-skills-frontmatter']['sha256'],
                         state['claude-code-skills-frontmatter']['sha256'])

    def test_changed_extract_gives_one_upstream_change_with_diff(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        _, _, state = self.run_watch([FRONTMATTER, LOCATIONS], {}, fetch, 'r1')
        changed = fixture('skills.md').replace(
            "| `when_to_use` | No | Extra trigger phrases. |",
            "| `when_to_use` | No | Extra trigger phrases. |\n| `priority` | No | Listing order. |")
        fetch.pages[SKILLS_URL] = (200, changed)
        findings, alerts, state2 = self.run_watch([FRONTMATTER, LOCATIONS], state, fetch, 'r2')
        self.assertEqual(alerts, [])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        new_sha = state2['claude-code-skills-frontmatter']['sha256']
        self.assertNotEqual(new_sha, state['claude-code-skills-frontmatter']['sha256'])
        self.assertEqual(f['kind'], 'upstream-change')
        self.assertEqual(f['skill'], 'claude-code-skills-frontmatter')
        self.assertEqual(f['subject'], 'claude-code-skills-frontmatter:' + new_sha)
        self.assertIsNone(f['file'])
        self.assertIsNone(f['line'])
        self.assertIsNone(f['fix'])
        self.assertIn('--- claude-code-skills-frontmatter (stored)', f['evidence'])
        self.assertIn('+| `priority` | No | Listing order. |', f['evidence'])
        self.assertEqual(finding_shape_errors(f), [])

    def test_diff_is_cut_to_200_lines(self):
        src = {'id': 'x-docs', 'url': 'https://example.com/d', 'extract': 'markdown-sections',
               'params': {'headings': ['Big']}}
        before = '# Big\n\n' + '\n'.join('line %d' % i for i in range(400))
        after = '# Big\n\n' + '\n'.join('changed %d' % i for i in range(400))
        fetch = FakeFetch({src['url']: (200, before)})
        _, _, state = self.run_watch([src], {}, fetch, 'r1')
        fetch.pages[src['url']] = (200, after)
        findings, _, _ = self.run_watch([src], state, fetch, 'r2')
        lines = findings[0]['evidence'].split('\n')
        self.assertEqual(len(lines), upstream.DIFF_MAX_LINES)
        self.assertTrue(lines[-1].startswith('[diff cut: '))

    def test_frs14_third_failure_alerts_and_success_resets(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        _, _, state = self.run_watch([FRONTMATTER], {}, fetch, 'r1')
        baseline = state['claude-code-skills-frontmatter']['extract']
        baseline_sha = state['claude-code-skills-frontmatter']['sha256']

        failures = [(503, ''), OSError('connection reset'), (404, 'gone')]
        for n, failure in enumerate(failures, start=2):
            fetch.pages[SKILLS_URL] = failure
            findings, alerts, state = self.run_watch([FRONTMATTER], state, fetch, 'r%d' % n)
            entry = state['claude-code-skills-frontmatter']
            self.assertEqual(findings, [])
            self.assertEqual(entry['failures'], n - 1)
            self.assertEqual(entry['extract'], baseline)
            self.assertEqual(entry['sha256'], baseline_sha)
            self.assertEqual(entry['fetched_at'], '2026-11-18T11:00:00Z')
            if n < 4:
                self.assertEqual(alerts, [], 'failure %d must not alert' % (n - 1))
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert['kind'], 'source-failing')
        self.assertEqual(alert['subject'], 'claude-code-skills-frontmatter')
        for run_id, error in (('r2', 'HTTP 503'), ('r3', 'OSError: connection reset'), ('r4', 'HTTP 404')):
            self.assertIn('run %s at 2026-11-18T11:00:00Z: %s' % (run_id, error), alert['evidence'])
        self.assertEqual(entry['last_error'], 'HTTP 404')

        # Still failing: the alert repeats every run (part 1 deduplicates it).
        _, alerts, state = self.run_watch([FRONTMATTER], state, fetch, 'r5')
        self.assertEqual([a['kind'] for a in alerts], ['source-failing'])
        self.assertIn('run r5', alerts[0]['evidence'])
        self.assertNotIn('run r2', alerts[0]['evidence'])

        # Recovery with the same content: count reset, no alert, no finding.
        fetch.pages[SKILLS_URL] = (200, fixture('skills.md'))
        findings, alerts, state = self.run_watch([FRONTMATTER], state, fetch, 'r6')
        entry = state['claude-code-skills-frontmatter']
        self.assertEqual((findings, alerts), ([], []))
        self.assertEqual(entry['failures'], 0)
        self.assertIsNone(entry['last_error'])
        self.assertEqual(entry['errors'], [])
        self.assertEqual(entry['sha256'], baseline_sha)

        # Two failures after a reset do not alert.
        fetch.pages[SKILLS_URL] = (500, '')
        for run_id in ('r7', 'r8'):
            _, alerts, state = self.run_watch([FRONTMATTER], state, fetch, run_id)
            self.assertEqual(alerts, [])

    def test_unreadable_page_counts_as_a_failure(self):
        # A 200 that lacks the watched table is a fault, not "no change".
        fetch = FakeFetch({TOOLS_URL: (200, '# Tools reference\n\nMoved.')})
        for run_id in ('r1', 'r2', 'r3'):
            findings, alerts, state = self.run_watch([TOOLS], {} if run_id == 'r1' else state, fetch, run_id)
            self.assertEqual(findings, [])
        self.assertEqual([a['subject'] for a in alerts], ['claude-code-tools'])
        self.assertIn('ExtractError', state['claude-code-tools']['last_error'])
        self.assertIsNone(state['claude-code-tools']['sha256'])

    def test_each_url_is_fetched_once_and_input_state_is_kept(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        before = {'other-source': {'sha256': 'ab', 'extract': 'x'}}
        snapshot = json.loads(json.dumps(before))
        _, _, state = self.run_watch([FRONTMATTER, LOCATIONS], before, fetch, 'r1')
        self.assertEqual(fetch.calls, [SKILLS_URL])
        self.assertEqual(before, snapshot)
        self.assertIn('other-source', state)
        self.assertIn('| Personal |', state['claude-code-skills-locations']['extract'])

    def test_findings_and_alerts_pass_shape_and_scrub(self):
        # Values that look private are built at runtime (the repo is public).
        ip = '.'.join(['192', '168', '7', '9'])
        mail = 'someone' + '@' + 'example' + '.org'
        home = '/' + 'Users' + '/someone/'
        src = {'id': 'x-docs', 'url': 'https://example.com/d', 'extract': 'markdown-sections',
               'params': {'headings': ['Setup']}}
        fetch = FakeFetch({src['url']: (200, '# Setup\n\nRun it.')})
        _, _, state = self.run_watch([src], {}, fetch, 'r1')
        fetch.pages[src['url']] = (200, '# Setup\n\nRun it on %s.\nMail %s.\nCopy %sfile.\nDone.' % (ip, mail, home))
        findings, _, state = self.run_watch([src], state, fetch, 'r2')
        self.assertEqual(len(findings), 1)
        evidence = findings[0]['evidence']
        self.assertIn('+Done.', evidence)
        self.assertIn('[line withheld by scrub', evidence)
        for value in (ip, mail, home):
            self.assertNotIn(value, evidence)
        # The stored extract is published on freshness-state, so it is scrubbed too.
        self.assertEqual(scrub.scan_text(state['x-docs']['extract']), [])
        self.assertIn('Done.', state['x-docs']['extract'])

        fetch.pages[src['url']] = OSError('cannot reach ' + ip + ' from ' + home)
        alerts = []
        for run_id in ('r3', 'r4', 'r5'):
            _, alerts, state = self.run_watch([src], state, fetch, run_id)
        self.assertEqual(len(alerts), 1)
        self.assertNotIn(ip, alerts[0]['evidence'])
        self.assertIn('OSError', alerts[0]['evidence'])

        for f in findings:
            self.assertEqual(finding_shape_errors(f), [])
            self.assertEqual(scrub.scan_text(f['evidence']), [])
            self.assertEqual(scrub.scan_text(f['subject']), [])
        for a in alerts:
            self.assertEqual(set(a), {'kind', 'subject', 'evidence'})
            self.assertEqual(scrub.scan_text(a['evidence']), [])

    def test_long_model_ids_stay_in_the_extract_but_not_in_evidence(self):
        # A dated model ID that trips the entropy rule, built at runtime.
        model = '-'.join(['gpt', '4o', 'mini', 'search', 'preview', '2025', '03', '11'])
        src = {'id': 'openai-models-deprecations', 'url': 'https://example.com/m',
               'extract': 'table-column', 'params': {'heading': 'Upcoming', 'column': 'Model'}}
        page = '## Upcoming\n\n| Model |\n| - |\n| `gpt-6-luna` |\n'
        fetch = FakeFetch({src['url']: (200, page)})
        _, _, state = self.run_watch([src], {}, fetch, 'r1')
        fetch.pages[src['url']] = (200, page + '| `%s` |\n' % model)
        findings, _, state = self.run_watch([src], state, fetch, 'r2')
        self.assertIn(model, state['openai-models-deprecations']['extract'])
        self.assertEqual(len(findings), 1)
        self.assertEqual(scrub.scan_text(findings[0]['evidence']), [])
        self.assertIn('withheld by scrub: high-entropy', findings[0]['evidence'])

    def test_opaque_tokens_never_reach_the_stored_extract(self):
        # An opaque token, built at runtime (the repo is public).
        token = 'opaque_' + ''.join(c + str(i % 10) for i, c in enumerate('AbCdEfGhIjKlMnOpQr'))
        src = {'id': 'x-docs', 'url': 'https://example.com/d', 'extract': 'markdown-sections',
               'params': {'headings': ['Setup']}}
        fetch = FakeFetch({src['url']: (200, '# Setup\n\nUse %s here.\nDone.' % token)})
        _, _, state = self.run_watch([src], {}, fetch, 'r1')
        extract = state['x-docs']['extract']
        self.assertNotIn(token, extract)
        self.assertIn('[line withheld by scrub: high-entropy]', extract)
        self.assertIn('Done.', extract)

        # A lowercase opaque run is not a slug either.
        lower = ''.join(c + str(i % 10) for i, c in enumerate('abcdefghijklmnopqrst'))
        fetch.pages[src['url']] = (200, '# Setup\n\nUse %s here.\nDone.' % lower)
        _, _, state = self.run_watch([src], {}, fetch, 'r2')
        self.assertNotIn(lower, state['x-docs']['extract'])


class ParsedListsTest(unittest.TestCase):

    def test_tool_names_parse_the_tools_reference(self):
        fetch = FakeFetch({TOOLS_URL: (200, fixture('tools-reference.md'))})
        _, _, state = upstream.watch([TOOLS], {}, fetch, NOW, 'r1')
        self.assertEqual(upstream.tool_names(state), ['Agent', 'Bash', 'Edit', 'Read', 'Skill', 'WebFetch'])
        self.assertEqual(upstream.tool_names({}), [])

    def test_wrapped_table_rows_do_not_end_the_table(self):
        page = fixture('tools-reference.md').replace(
            '| `Bash` | Executes shell commands. See [Bash tool behavior](#bash-tool-behavior) | Yes |',
            '| `Bash` | Executes shell commands.\nSee [Bash tool behavior](#bash-tool-behavior) | Yes |')
        self.assertNotEqual(page, fixture('tools-reference.md'))
        fetch = FakeFetch({TOOLS_URL: (200, page)})
        _, _, state = upstream.watch([TOOLS], {}, fetch, NOW, 'r1')
        self.assertEqual(upstream.tool_names(state), ['Agent', 'Bash', 'Edit', 'Read', 'Skill', 'WebFetch'])

        two = dict(TOOLS, params={'heading': '# Tools reference', 'column': ['Tool', 'Permission required']})
        out = upstream.normalize(two, page).split('\n')
        self.assertIn('Bash | Yes', out)

    def test_longer_fences_hide_headings(self):
        page = '# Doc\n\n## Keep\n\n````md\n```\n## Not a heading\n```\n````\n\nAfter.\n\n## Next\n'
        src = {'id': 'x-docs', 'url': 'https://example.com/d', 'extract': 'markdown-sections',
               'params': {'headings': ['Keep']}}
        out = upstream.normalize(src, page)
        self.assertIn('## Not a heading', out)
        self.assertIn('After.', out)
        self.assertNotIn('## Next', out)

    def test_field_table_parses_the_frontmatter_table(self):
        fetch = FakeFetch({SKILLS_URL: (200, fixture('skills.md'))})
        _, _, state = upstream.watch([FRONTMATTER, LOCATIONS], {}, fetch, NOW, 'r1')
        self.assertEqual(upstream.field_table(state), [
            'name', 'description', 'when_to_use', 'allowed-tools', 'disable-model-invocation',
        ])
        self.assertEqual(upstream.field_table(state, 'claude-code-skills-locations'), [])
        self.assertEqual(upstream.field_table({}), [])


class FixturesTest(unittest.TestCase):

    def test_fixtures_are_scrubbed(self):
        for name in sorted(os.listdir(FIXTURES)):
            rel = 'tests/fixtures/upstream/' + name
            self.assertEqual(scrub.scan_text(fixture(name), rel), [], rel)


if __name__ == '__main__':
    unittest.main()
