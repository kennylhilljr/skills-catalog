#!/usr/bin/env python3
"""Tests for the zz-canary skill and canary verification (FRS-18, FRS-19).

Every test works on the real `.held/zz-canary` folder or on plain dicts. No
test touches the network, GitHub or a CLI: the link fetcher and the command
runner are fakes.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import unittest

from freshness import age
from freshness import canary
from freshness import checks
from freshness import cli_checks
from freshness import links

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANARY_DIR = os.path.join(_ROOT, '.held', 'zz-canary')


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, *rel.split('/')))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_lint = _load('canary_test_lint', 'tools/lint.py')
_scrub = _load('canary_test_scrub', 'tools/scrub.py')
_catalog = _load('canary_test_catalog', 'tools/catalog.py')

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 18, 3, 0, tzinfo=UTC)


def _field_table():
    schema = _lint.load_schema()
    return list(schema['properties']) + list(schema['x-claude-only'])


def _fake_fetch(calls):
    def fetch(url):
        calls.append(url)
        return 404, None
    return fetch


def canary_findings(history, now=NOW):
    """Part 2's static checks plus part 6's age findings over the real canary.

    Returns `(findings for zz-canary, updated link history, fetched urls)`.
    """
    name = canary.CANARY
    out = []
    out += checks.check_models(CANARY_DIR, name, checks.load_models())
    out += checks.check_paths(CANARY_DIR, name)
    out += checks.check_frontmatter(CANARY_DIR, name, field_table=_field_table())
    out += [f for f in checks.check_description(checks.catalog_descriptions(_ROOT)) if f['skill'] == name]
    items = [(name, file, line, url) for file, line, url in links.extract_links(CANARY_DIR)]
    calls = []
    found, history = links.check_links(items, history, _fake_fetch(calls), now)
    out += found
    out += [f for f in age.age_findings(age.read_skills(_ROOT), now.date()) if f['skill'] == name]
    return out, history, calls


def _f(kind, skill=canary.CANARY):
    return {'kind': kind, 'skill': skill, 'subject': kind, 'file': canary.CANARY_FILE, 'line': 1,
            'evidence': kind, 'fix': None}


THREE = [_f('dead-link'), _f('deprecated-model'), _f('past-max-age')]


class TestCanarySkill(unittest.TestCase):

    def test_lints_clean(self):
        """The real .held/zz-canary passes tools/lint.py."""
        self.assertEqual(_lint.lint_skill(CANARY_DIR), [])

    def test_scrubs_clean(self):
        """The real .held/zz-canary passes tools/scrub.py."""
        with open(os.path.join(CANARY_DIR, 'SKILL.md'), encoding='utf-8') as handle:
            text = handle.read()
        self.assertEqual(_scrub.scan_text(text, canary.CANARY_FILE), [])

    def test_frontmatter_values(self):
        """Held, owned, both targets, past its age and no catalog-requires."""
        with open(os.path.join(CANARY_DIR, 'SKILL.md'), encoding='utf-8') as handle:
            fm, body = _lint.parse_frontmatter(handle.read())
        meta = fm['metadata']
        self.assertEqual(meta['catalog-consumers'], 'none')
        self.assertEqual(meta['catalog-owner'], 'kennylhilljr')
        self.assertEqual(meta['catalog-targets'], 'claude,codex')
        self.assertEqual(meta['catalog-last-verified'], '2025-01-01')
        self.assertEqual(meta['catalog-max-age-days'], '30')
        self.assertNotIn('catalog-requires', meta)
        self.assertNotIn('```', body)
        self.assertIn('Use when', fm['description'])

    def test_only_link_is_canary_url(self):
        """The one link in the canary is the URL verify() bootstraps on."""
        urls = [url for _file, _line, url in links.extract_links(CANARY_DIR)]
        self.assertEqual(urls, [canary.CANARY_URL])

    def test_checks_give_exactly_three_findings(self):
        """FRS-18: part 2 checks plus age_findings, 404 fetch, history 21 h old: exactly three."""
        first = (NOW - datetime.timedelta(hours=21)).isoformat()
        history = {canary.CANARY_URL: {'first_fail_at': first, 'last_status': 404, 'moved_to': None}}
        found, _history, calls = canary_findings(history)
        self.assertEqual(calls, [canary.CANARY_URL])
        self.assertEqual(sorted(f['kind'] for f in found), sorted(canary.EXPECTED))
        model = [f for f in found if f['kind'] == 'deprecated-model'][0]
        self.assertIn('retired', model['evidence'])
        self.assertEqual(model['file'], canary.CANARY_FILE)
        self.assertEqual(canary.verify(found, _history, now=NOW), (True, 'canary ok'))

    def test_first_night_after_reset_bootstraps(self):
        """FRS-18: an empty link history gives no dead-link finding, yet verify() passes."""
        found, history, _calls = canary_findings({})
        self.assertEqual(sorted(f['kind'] for f in found), ['deprecated-model', 'past-max-age'])
        ok, reason = canary.verify(found, history, now=NOW)
        self.assertTrue(ok, reason)
        self.assertIn('first failure', reason)

    def test_mac_checks_find_nothing(self):
        """No catalog-requires and no fenced shell blocks: the Mac checks run nothing."""
        calls = []

        def run(argv):
            calls.append(argv)
            raise AssertionError('no command may run for the canary')

        self.assertEqual(cli_checks.commands_for(CANARY_DIR), [])
        cli_checks.reset_cache()
        self.assertEqual(cli_checks.check_commands(canary.CANARY, CANARY_DIR, run), [])
        self.assertEqual(cli_checks.check_cli_refs(canary.CANARY, CANARY_DIR, [], run), [])
        cli_checks.reset_cache()
        self.assertEqual(calls, [])

    def test_catalog_drops_canary(self):
        """FRS-19: build('.') has no zz-canary in skills or held, and catalog.json is unchanged."""
        with open(os.path.join(_ROOT, 'catalog.json'), 'rb') as handle:
            before = handle.read()
        doc = _catalog.build(_ROOT)
        names = [e['name'] for e in doc['skills']] + [e['name'] for e in doc['held']]
        self.assertNotIn(canary.CANARY, names)
        self.assertNotIn(canary.CANARY, json.dumps(doc))
        self.assertEqual(_catalog.render(_ROOT).encode('utf-8'), before)
        with open(os.path.join(_ROOT, 'catalog.json'), 'rb') as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), hashlib.sha256(before).hexdigest())


class TestVerify(unittest.TestCase):

    def test_three_findings_ok(self):
        self.assertEqual(canary.verify(THREE, {}), (True, 'canary ok'))

    def test_other_skills_ignored(self):
        self.assertTrue(canary.verify(THREE + [_f('dead-link', 'new-mcp')], {})[0])

    def test_missing_dead_link(self):
        ok, reason = canary.verify(THREE[1:], {})
        self.assertFalse(ok)
        self.assertIn('missing: dead-link', reason)

    def test_missing_model_finding(self):
        ok, reason = canary.verify([THREE[0], THREE[2]], {})
        self.assertFalse(ok)
        self.assertIn('missing: deprecated-model', reason)
        self.assertNotIn('extra', reason)

    def test_extra_finding(self):
        ok, reason = canary.verify(THREE + [_f('description')], {})
        self.assertFalse(ok)
        self.assertIn('extra: description x1', reason)
        self.assertNotIn('missing', reason)

    def test_duplicate_finding_is_extra(self):
        ok, reason = canary.verify(THREE + [_f('past-max-age')], {})
        self.assertFalse(ok)
        self.assertIn('extra: past-max-age x1', reason)

    def test_none_found(self):
        ok, reason = canary.verify([], {})
        self.assertFalse(ok)
        for kind in canary.EXPECTED:
            self.assertIn(kind, reason)

    def test_bootstrap_with_recorded_first_failure(self):
        history = {canary.CANARY_URL: {'first_fail_at': NOW.isoformat(), 'last_status': 404,
                                       'moved_to': None}}
        ok, reason = canary.verify(THREE[1:], history, now=NOW)
        self.assertTrue(ok, reason)
        self.assertIn('first failure', reason)
        self.assertFalse(canary.verify(THREE[1:], history)[0])  # no run time, no bootstrap

    def test_bootstrap_needs_a_failure_from_this_run(self):
        """An entry from an earlier run (link check skipped this run) never passes."""
        history = {canary.CANARY_URL: {'first_fail_at': NOW.isoformat(), 'last_status': 404,
                                       'moved_to': None}}
        for minutes in (1, 30, 120, 19 * 60):
            ok, reason = canary.verify(THREE[1:], history, now=NOW + datetime.timedelta(minutes=minutes))
            self.assertFalse(ok, minutes)
            self.assertIn('missing: dead-link', reason)
        self.assertFalse(canary.verify(THREE[1:], history, now=NOW - datetime.timedelta(minutes=1))[0])

    def test_bootstrap_without_now_never_passes(self):
        """The two-argument call cannot prove the entry is from this run."""
        for stamp in ('2025-01-01T00:00:00+00:00', datetime.datetime.now(UTC).isoformat()):
            history = {canary.CANARY_URL: {'first_fail_at': stamp, 'last_status': 404, 'moved_to': None}}
            ok, reason = canary.verify(THREE[1:], history)
            self.assertFalse(ok)
            self.assertIn('missing: dead-link', reason)

    def test_bootstrap_naive_timestamp_is_not_trusted(self):
        history = {canary.CANARY_URL: {'first_fail_at': '2026-11-18T03:00:00', 'last_status': 404,
                                       'moved_to': None}}
        self.assertFalse(canary.verify(THREE[1:], history, now=NOW)[0])

    def test_bootstrap_does_not_hide_an_overdue_dead_link(self):
        """A first failure older than the gap should have produced a finding."""
        first = (NOW - datetime.timedelta(hours=30)).isoformat()
        history = {canary.CANARY_URL: {'first_fail_at': first, 'last_status': 404, 'moved_to': None}}
        ok, reason = canary.verify(THREE[1:], history, now=NOW)
        self.assertFalse(ok)
        self.assertIn('missing: dead-link', reason)

    def test_bootstrap_needs_the_canary_url(self):
        history = {'https://example.com/x': {'first_fail_at': NOW.isoformat(), 'last_status': 404,
                                             'moved_to': None}}
        self.assertFalse(canary.verify(THREE[1:], history, now=NOW)[0])
        moved = {canary.CANARY_URL: {'first_fail_at': None, 'last_status': 301,
                                     'moved_to': 'https://example.com/y'}}
        self.assertFalse(canary.verify(THREE[1:], moved, now=NOW)[0])

    def test_bootstrap_does_not_double_count(self):
        """A dead-link finding plus a history entry is still exactly one dead link."""
        history = {canary.CANARY_URL: {'first_fail_at': NOW.isoformat(), 'last_status': 404,
                                       'moved_to': None}}
        self.assertEqual(canary.verify(THREE, history, now=NOW), (True, 'canary ok'))


if __name__ == '__main__':
    unittest.main()
