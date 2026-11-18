"""Tests for the static freshness checks (FRS-2, FRS-5, FRS-6, FRS-7)."""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from freshness import checks, links

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location('freshness_test_scrub', os.path.join(ROOT, 'tools', 'scrub.py'))
scrub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrub)

MODELS = [
    {'id': 'claude-2.1', 'vendor': 'anthropic', 'status': 'retired', 'replacement': 'claude-opus-4-8'},
    {'id': 'gpt-5', 'vendor': 'openai', 'status': 'deprecated', 'replacement': 'gpt-6.1-sol'},
    {'id': 'old-model-x', 'vendor': 'openai', 'status': 'retired', 'replacement': None},
    {'id': 'claude-opus-4-8', 'vendor': 'anthropic', 'status': 'current', 'replacement': None},
]

KEYS = {'kind', 'skill', 'subject', 'file', 'line', 'evidence', 'fix'}
FIX_KEYS = {'file', 'line', 'old', 'new'}

GOOD_DESC = 'Check widgets for drift. Use when a widget changes.'


def skill_md(name, description=GOOD_DESC, body='Body.\n'):
    return (
        '---\n'
        'name: ' + name + '\n'
        'description: "' + description + '"\n'
        'metadata:\n'
        '  catalog-version: "1.0.0"\n'
        '  catalog-owner: "ecc-customization"\n'
        '  catalog-provenance: "own"\n'
        '  catalog-targets: "claude,codex"\n'
        '  catalog-last-verified: "2026-11-11"\n'
        '  catalog-max-age-days: "90"\n'
        '---\n\n'
        '# ' + name + '\n\n' + body + '\n'
        '## Changelog\n\n'
        '| Version | Date | Change |\n'
        '| --- | --- | --- |\n'
        '| 1.0.0 | 2026-11-11 | Imported at `abcdef1`. |\n'
    )


def shape_errors(f):
    """Part 1's Finding shape, restated (part 1's validate is not imported)."""
    errors = []
    if set(f) != KEYS:
        errors.append('keys: %r' % sorted(f))
    for key in ('kind', 'skill', 'subject', 'evidence'):
        if not isinstance(f.get(key), str) or not f.get(key):
            errors.append(key)
    if f.get('file') is not None and not isinstance(f['file'], str):
        errors.append('file')
    if f.get('line') is not None and not isinstance(f['line'], int):
        errors.append('line')
    fix = f.get('fix')
    if fix is not None:
        if not isinstance(fix, dict) or set(fix) != FIX_KEYS or not isinstance(fix['line'], int):
            errors.append('fix')
    return errors


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.seen = []

    def make(self, name, section='skills', description=GOOD_DESC, body='Body.\n', files=None):
        folder = os.path.join(self.root, section, name)
        os.makedirs(folder)
        with open(os.path.join(folder, 'SKILL.md'), 'w') as handle:
            handle.write(skill_md(name, description, body))
        for rel, text in (files or {}).items():
            path = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as handle:
                handle.write(text)
        return folder

    def check_all(self, findings):
        self.seen.extend(findings)
        for f in findings:
            self.assertEqual(shape_errors(f), [], f)
            # The whole serialized finding, fix included, passes the scrub.
            text = json.dumps(f, indent=1)
            self.assertEqual(scrub.scan_text(text, 'issue.md'), [], text)
            self.assertNotIn(self.root, f['evidence'])
            self.assertNotIn(self.root, f['subject'])
        return findings


class ModelTests(Base):
    def test_deprecated_id_in_skill_md(self):
        folder = self.make('a-skill', body='Call claude-2.1 here.\n')
        out = self.check_all(checks.check_models(folder, 'a-skill', MODELS))
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertEqual(f['kind'], 'deprecated-model')
        self.assertEqual(f['file'], 'skills/a-skill/SKILL.md')
        self.assertEqual(f['subject'], 'skills/a-skill/SKILL.md:claude-2.1')
        self.assertEqual(f['line'], 15)
        for part in ('skills/a-skill/SKILL.md', ':15', 'claude-2.1', 'claude-opus-4-8'):
            self.assertIn(part, f['evidence'])
        self.assertEqual(f['fix'], {'file': 'skills/a-skill/SKILL.md', 'line': 15,
                                    'old': 'Call claude-2.1 here.', 'new': 'Call claude-opus-4-8 here.'})

    def test_deprecated_id_in_scripts(self):
        folder = self.make('a-skill', files={'scripts/x.py': 'import os\nMODEL = "gpt-5"\n'})
        out = self.check_all(checks.check_models(folder, 'a-skill', MODELS))
        self.assertEqual([(f['file'], f['line']) for f in out], [('skills/a-skill/scripts/x.py', 2)])
        self.assertIn('gpt-5', out[0]['evidence'])
        self.assertIn('gpt-6.1-sol', out[0]['evidence'])
        self.assertEqual(out[0]['fix']['new'], 'MODEL = "gpt-6.1-sol"')

    def test_current_id_gives_none(self):
        folder = self.make('a-skill', body='Use claude-opus-4-8 for this.\n')
        self.assertEqual(checks.check_models(folder, 'a-skill', MODELS), [])

    def test_longer_id_prefix_gives_none(self):
        body = 'Try claude-2.1-beta, gpt-5-mini, gpt-5.1, gpt-5.5 and xgpt-5.\n'
        folder = self.make('a-skill', body=body)
        self.assertEqual(checks.check_models(folder, 'a-skill', MODELS), [])

    def test_id_at_sentence_end_and_no_replacement(self):
        folder = self.make('a-skill', body='We used gpt-5.\nAnd old-model-x too.\nSee gpt-5.json.\n')
        out = self.check_all(checks.check_models(folder, 'a-skill', MODELS))
        self.assertEqual([f['subject'].split(':')[-1] for f in out], ['gpt-5', 'old-model-x', 'gpt-5'])
        self.assertIsNone(out[1]['fix'])

    def test_symlinked_and_special_files_not_read(self):
        outside = os.path.join(self.root, 'outside.py')
        with open(outside, 'w') as handle:
            handle.write('MODEL = "gpt-5"\n')
        folder = self.make('a-skill', files={'scripts/ok.py': 'x = 1\n'})
        os.symlink(outside, os.path.join(folder, 'scripts', 'link.py'))
        os.symlink(os.devnull, os.path.join(folder, 'scripts', 'dev.py'))
        os.symlink(outside, os.path.join(folder, 'references.md'))
        self.assertEqual(checks.check_models(folder, 'a-skill', MODELS), [])
        with open(outside, 'w') as handle:
            handle.write('https://example.com/outside\n')
        self.assertEqual(links.extract_links(folder), [])
        real = self.make('real-skill')
        os.symlink(real, os.path.join(self.root, 'skills', 'linked-skill'))
        self.assertEqual([n for _s, n, _f in checks.iter_skills(self.root)], ['a-skill', 'real-skill'])

    def test_models_table_seeded(self):
        with open(os.path.join(ROOT, 'freshness', 'models.json')) as handle:
            table = json.load(handle)
        for vendor in ('anthropic', 'openai'):
            old = [m for m in table if m['vendor'] == vendor and m['status'] != 'current']
            self.assertGreaterEqual(len(old), 3)
            self.assertTrue(all(m['replacement'] for m in old[:3]))
        for m in table:
            self.assertEqual(set(m), {'id', 'vendor', 'status', 'replacement'})
            self.assertIn(m['status'], ('current', 'deprecated', 'retired'))


class PathTests(Base):
    def test_missing_reference(self):
        folder = self.make('a-skill', body='See [x](references/x.md).\n')
        out = self.check_all(checks.check_paths(folder, 'a-skill'))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['kind'], 'missing-path')
        self.assertEqual(out[0]['subject'], 'skills/a-skill/SKILL.md:references/x.md')
        self.assertEqual(out[0]['line'], 15)
        self.assertIn('references/x.md', out[0]['evidence'])

    def test_existing_reference(self):
        folder = self.make('a-skill', body='See [x](references/x.md) and `scripts/y.js`.\n',
                           files={'references/x.md': 'x\n', 'scripts/y.js': 'y\n'})
        self.assertEqual(checks.check_paths(folder, 'a-skill'), [])


class DescriptionTests(Base):
    def test_missing_use_clause(self):
        out = self.check_all(checks.check_description([('a', 'Checks widgets.', 'skills/a/SKILL.md')]))
        self.assertEqual([f['subject'] for f in out], ['skills/a/SKILL.md:use-clause'])
        self.assertEqual(out[0]['kind'], 'description')

    def test_long_first_sentence(self):
        desc = 'W' * 200 + '. Use when needed.'
        self.assertEqual(len(checks.first_sentence(desc)), 201)
        out = self.check_all(checks.check_description([('a', desc, 'skills/a/SKILL.md')]))
        self.assertEqual([f['subject'] for f in out], ['skills/a/SKILL.md:first-sentence'])
        ok = 'W' * 199 + '. Use when needed.'
        self.assertEqual(checks.check_description([('a', ok, 'skills/a/SKILL.md')]), [])

    def test_collision_once_on_first(self):
        skills = [
            ('zeta', 'Review pull requests for security bugs. Use when reviewing a pull request.', 'skills/zeta/SKILL.md'),
            ('alpha', 'Reviews pull requests for security bug. Use when reviewing pull requests.', 'skills/alpha/SKILL.md'),
            ('other', 'Bake bread from sourdough starter. Use for baking loaves.', 'skills/other/SKILL.md'),
        ]
        out = self.check_all(checks.check_description(skills, threshold=0.35))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['skill'], 'alpha')
        self.assertIn('zeta', out[0]['evidence'])
        self.assertEqual(out[0]['subject'], 'skills/alpha/SKILL.md:collision:zeta')

    def test_stemmer(self):
        self.assertEqual(checks._terms('Reviewing reviewed reviews quickly'),
                         ['review', 'review', 'review', 'quick'])

    def test_real_catalog_has_no_collision(self):
        skills = checks.catalog_descriptions(ROOT)
        self.assertGreater(len(skills), 10)
        out = checks.check_description(skills)
        self.assertEqual([f['subject'] for f in out if ':collision:' in f['subject']], [])
        self.assertLess(max(checks.similarities({n: d for n, d, _ in skills}).values()),
                        checks.DEFAULT_THRESHOLD)


class FrontmatterTests(Base):
    def test_clean_skill_gives_none(self):
        folder = self.make('a-skill')
        self.assertEqual(checks.check_frontmatter(folder, 'a-skill'), [])

    def test_lint_field_table_and_listing_cap(self):
        folder = self.make('a-skill', description='Checks widgets.', body='See [x](references/x.md).\n')
        with open(os.path.join(folder, 'SKILL.md')) as handle:
            text = handle.read()
        with open(os.path.join(folder, 'SKILL.md'), 'w') as handle:
            handle.write(text.replace('metadata:\n', 'license: MIT\nmetadata:\n', 1))
        out = self.check_all(checks.check_frontmatter(
            folder, 'a-skill', field_table=['name', 'description', 'metadata'], listing_cap=10))
        subjects = [f['subject'] for f in out]
        self.assertIn('skills/a-skill/SKILL.md:field:license', subjects)
        self.assertIn('skills/a-skill/SKILL.md:listing-cap', subjects)
        self.assertTrue(all(f['kind'] == 'frontmatter' for f in out))
        # Every lint finding becomes a frontmatter finding.
        self.assertTrue(any('CAT-7' in s for s in subjects))
        self.assertTrue(any('CAT-3' in s for s in subjects))

    def test_lint_finding_becomes_frontmatter(self):
        folder = self.make('a-skill')
        os.rename(folder, os.path.join(self.root, 'skills', 'b-skill'))
        out = self.check_all(checks.check_frontmatter(os.path.join(self.root, 'skills', 'b-skill'), 'b-skill'))
        self.assertEqual(len(out), 1)
        self.assertIn('CAT-2', out[0]['evidence'])


class IterSkillsTests(Base):
    def test_skips_excluded_without_reading(self):
        self.make('a-skill')
        self.make('b-held', section='.held')
        hidden = self.make('hidden-one')
        touched = []
        real_isfile, real_lstat, real_open = os.path.isfile, os.lstat, open

        def isfile(path):
            touched.append(str(path))
            return real_isfile(path)

        def lstat(path, *args, **kwargs):
            touched.append(str(path))
            return real_lstat(path, *args, **kwargs)

        def opener(path, *args, **kwargs):
            touched.append(str(path))
            return real_open(path, *args, **kwargs)

        with mock.patch('os.path.isfile', isfile), mock.patch('os.lstat', lstat), \
                mock.patch('builtins.open', opener):
            got = checks.iter_skills(self.root, {'hidden-one'})
        self.assertEqual([(s, n) for s, n, _ in got], [('skills', 'a-skill'), ('.held', 'b-held')])
        self.assertFalse([p for p in touched if p.startswith(hidden)])
        self.assertEqual([n for n, _d, _f in checks.catalog_descriptions(self.root, {'hidden-one'})],
                         ['a-skill', 'b-held'])


T0 = datetime(2026, 11, 1, 6, 0, 0)
URL = 'https://example.com/docs'
ITEM = ('a-skill', 'skills/a-skill/SKILL.md', 3, URL)


class FakeFetch:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


class LinkTests(Base):
    def run_links(self, answers, history, now, items=(ITEM,)):
        findings, history = links.check_links(list(items), history, FakeFetch(answers), now)
        self.check_all(findings)
        return findings, history

    def test_extract_links(self):
        folder = self.make('a-skill', body='See (https://example.com/a). Also <http://example.org/b>;\n',
                           files={'references/r.md': 'x https://example.net/c, y\n'})
        got = links.extract_links(folder)
        self.assertEqual(got, [
            ('skills/a-skill/SKILL.md', 15, 'https://example.com/a'),
            ('skills/a-skill/SKILL.md', 15, 'http://example.org/b'),
            ('skills/a-skill/references/r.md', 1, 'https://example.net/c'),
        ])

    def test_first_failure_only_recorded(self):
        findings, history = self.run_links({URL: (404, None)}, {}, T0)
        self.assertEqual(findings, [])
        self.assertEqual(history[URL]['first_fail_at'], T0.isoformat())
        self.assertEqual(history[URL]['last_status'], 404)

    def test_fail_19h_apart_nothing(self):
        _f, history = self.run_links({URL: (500, None)}, {}, T0)
        findings, history = self.run_links({URL: (500, None)}, history, T0 + timedelta(hours=19))
        self.assertEqual(findings, [])
        self.assertEqual(history[URL]['first_fail_at'], T0.isoformat())

    def test_fail_21h_apart_dead_link(self):
        _f, history = self.run_links({URL: OSError('boom')}, {}, T0)
        findings, history = self.run_links({URL: (404, None)}, history, T0 + timedelta(hours=21))
        self.assertEqual([f['kind'] for f in findings], ['dead-link'])
        self.assertEqual(findings[0]['subject'], 'skills/a-skill/SKILL.md:' + URL)
        self.assertEqual(findings[0]['line'], 3)
        self.assertIsNone(findings[0]['fix'])

    def test_success_clears_history(self):
        _f, history = self.run_links({URL: (404, None)}, {}, T0)
        findings, history = self.run_links({URL: (200, None)}, history, T0 + timedelta(hours=21))
        self.assertEqual(findings, [])
        self.assertNotIn(URL, history)

    def test_moved_gives_fix_not_dead_link(self):
        new = 'https://example.com/new-docs'
        _f, history = self.run_links({URL: (404, None)}, {}, T0)
        findings, history = self.run_links({URL: (301, new)}, history, T0 + timedelta(hours=21))
        self.assertEqual(len(findings), 1)
        self.assertNotEqual(findings[0]['kind'], 'dead-link')
        self.assertEqual(findings[0]['fix'], {'file': 'skills/a-skill/SKILL.md', 'line': 3,
                                              'old': URL, 'new': new})
        self.assertEqual(history[URL]['moved_to'], new)
        findings, _h = self.run_links({URL: (308, '/other')}, {}, T0)
        self.assertEqual(findings[0]['fix']['new'], 'https://example.com/other')
        self.assertEqual(links.safe_location(URL, 'https://docs.example.co.uk:8443/a?b=1#c'),
                         'https://docs.example.co.uk:8443/a?b=1#c')

    def test_unsafe_redirect_target_not_published(self):
        bad = [
            'https://build' + '.corp' + '.internal/docs',
            'javascript' + ':alert(1)',
            'https://example.com/a b',
            'https://[::1/path',
            'https://example.com:bad/path',
            'https://example.com/a' + chr(1) + 'b',
            'https://user' + ':pw' + '@example.com/x',
            'https://localhost/x',
            'https://' + '.'.join(['10', '0', '0', '1']) + '/x',
            'https://999.999.999.999/x',
            'https://example.org/path' + chr(10),
            'https://example.org/)![x](https://tracker.example/x',
            'https://example.org/a*b|c',
            'https://' + 'a' * 64 + '.example.com/x',
        ]
        for target in bad:
            findings, history = self.run_links({URL: (301, target)}, {}, T0)
            self.assertEqual(findings, [], target)
            self.assertIsNone(history[URL]['moved_to'])
            self.assertNotIn(target, json.dumps(history))
            findings, _h = self.run_links({URL: (308, target)}, history, T0 + timedelta(hours=21))
            self.assertEqual([f['kind'] for f in findings], ['dead-link'], target)
            self.assertNotIn(target, json.dumps(findings))

    def test_url_used_twice_fetched_once(self):
        fetch = FakeFetch({URL: (200, None)})
        items = [ITEM, ('b-skill', 'skills/b-skill/SKILL.md', 9, URL)]
        links.check_links(items, {}, fetch, T0)
        self.assertEqual(fetch.calls, [URL])
        fetch = FakeFetch({URL: (404, None)})
        history = {URL: {'first_fail_at': T0.isoformat(), 'last_status': 404, 'moved_to': None}}
        findings, _h = links.check_links(items, history, fetch, T0 + timedelta(hours=24))
        self.assertEqual(fetch.calls, [URL])
        self.assertEqual(sorted(f['skill'] for f in self.check_all(findings)), ['a-skill', 'b-skill'])

    def test_history_not_mutated(self):
        history = {URL: {'first_fail_at': T0.isoformat(), 'last_status': 404, 'moved_to': None}}
        before = json.dumps(history, sort_keys=True)
        links.check_links([ITEM], history, FakeFetch({URL: (200, None)}), T0)
        self.assertEqual(json.dumps(history, sort_keys=True), before)


class NoSideEffectsTests(unittest.TestCase):
    def test_modules_do_no_network_or_subprocess(self):
        for module in (checks, links):
            with open(module.__file__) as handle:
                source = handle.read()
            for banned in ('import subprocess', 'import socket', 'urllib.request', 'http.client'):
                self.assertNotIn(banned, source)

    def test_all_findings_seen_pass_scrub(self):
        # Runs the real catalog through every check; every finding must have
        # the shape and pass the scrub.
        models = checks.load_models()
        out = []
        for _section, name, folder in checks.iter_skills(ROOT):
            out += checks.check_models(folder, name, models)
            out += checks.check_paths(folder, name)
            out += checks.check_frontmatter(folder, name)
        out += checks.check_description(checks.catalog_descriptions(ROOT))
        for f in out:
            self.assertEqual(shape_errors(f), [], f)
            self.assertEqual(scrub.scan_text(json.dumps(f, indent=1), 'issue.md'), [])


if __name__ == '__main__':
    unittest.main()
