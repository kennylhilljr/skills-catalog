#!/usr/bin/env python3
"""Unit tests for the frontmatter linter."""

import importlib.util
import os
import tempfile
import unittest


# Load the linter module
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lint_path = os.path.join(_repo_root, 'tools', 'lint.py')
_spec = importlib.util.spec_from_file_location('catalog_lint', _lint_path)
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)


class TestFrontmatterLinter(unittest.TestCase):
    """Test the frontmatter linter rules."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = _repo_root

    def tearDown(self):
        """Clean up test fixtures."""
        self.temp_dir.cleanup()

    def _make_skill(self, name='good-skill', desc='Do a thing. Use when needed.',
                    extra=None, meta_overrides=None, raw_meta=None, parent='skills', files=None):
        """Create a test skill in a temporary directory.

        Returns the skill directory path.
        """
        if extra is None:
            extra = []
        if meta_overrides is None:
            meta_overrides = {}
        if raw_meta is None:
            raw_meta = []
        if files is None:
            files = []

        # Build metadata with overrides
        default_meta = {
            'catalog-version': '1.0.0',
            'catalog-owner': 'test-owner',
            'catalog-provenance': 'own',
            'catalog-targets': 'claude,codex',
            'catalog-last-verified': '2026-11-11',
            'catalog-max-age-days': '90',
        }
        default_meta.update(meta_overrides)

        # Build frontmatter
        lines = ['---', f'name: {name}', f'description: {desc}'] + extra + ['metadata:']
        for key, value in default_meta.items():
            if value is not None:
                lines.append(f'  {key}: "{value}"')
        lines += raw_meta

        # Add closing fence and body
        lines.extend(['---', '', '# Body', ''])

        # Add changelog
        changelog = '\n## Changelog\n\n| Version | Date | Change |\n| --- | --- | --- |\n| 1.0.0 | 2026-11-11 | Imported from x at `abc1234` |\n'
        content = '\n'.join(lines) + changelog

        # Create skill directory
        skill_dir = os.path.join(self.temp_dir.name, parent, name)
        os.makedirs(skill_dir, exist_ok=True)

        # Write SKILL.md
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as f:
            f.write(content)

        # Create any additional files
        for file_path in files:
            full_path = os.path.join(skill_dir, file_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w') as f:
                f.write('x')

        return skill_dir

    def _lint(self, skill_dir):
        """Lint a skill and return finding codes and messages as a space-separated string."""
        findings = _lint.lint_skill(skill_dir)
        return ' '.join(findings)

    # CAT-4: plain scalars YAML would not read as one string (#131)
    def test_plain_description_with_colon_space_rejected(self):
        skill_dir = self._make_skill(desc='Use when reviewing an ordered checklist: tool schemas, docs.')
        result = self._lint(skill_dir)
        self.assertIn('CAT-4', result)
        self.assertIn('key "description" is a plain scalar', result)

    def test_plain_description_with_space_hash_rejected(self):
        skill_dir = self._make_skill(desc='Use when needed #comment')
        self.assertIn('key "description" is a plain scalar', self._lint(skill_dir))

    def test_plain_description_ending_in_colon_rejected(self):
        skill_dir = self._make_skill(desc='Use when needed:')
        self.assertIn('key "description" is a plain scalar', self._lint(skill_dir))

    def test_plain_metadata_value_with_colon_space_rejected(self):
        skill_dir = self._make_skill(meta_overrides={'catalog-owner': None},
                                     raw_meta=['  catalog-owner: team: infra'])
        self.assertIn('metadata key "catalog-owner" is a plain scalar', self._lint(skill_dir))

    def test_quoted_or_safe_plain_values_accepted(self):
        descs = ['"Use when reviewing an ordered checklist: tool schemas."',
                 "'Use when needed #1'",
                 'Use when a#b or x:y appear.',
                 'Use when needed.']
        for i, desc in enumerate(descs):
            skill_dir = self._make_skill(desc=desc, parent=f'p{i}')
            self.assertNotIn('is a plain scalar', self._lint(skill_dir), desc)

    # Test: clean skill with no findings
    def test_clean_skill(self):
        """Test that a valid skill has no findings."""
        skill_dir = self._make_skill()
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Valid skill should have no findings, got: {result}')

    # CAT-2: name validation
    def test_cat2_name_mismatch_folder(self):
        """Test CAT-2: folder name must match skill name."""
        skill_dir = self._make_skill(name='other-name')
        # Rename folder to mismatch
        new_skill_dir = os.path.join(os.path.dirname(skill_dir), 'different-folder')
        os.rename(skill_dir, new_skill_dir)
        result = self._lint(new_skill_dir)
        self.assertIn('CAT-2', result)

    def test_cat2_name_invalid_format(self):
        """Test CAT-2: name with invalid format."""
        skill_dir = self._make_skill(name='Bad_Name')
        result = self._lint(skill_dir)
        self.assertIn('CAT-2', result)

    def test_cat2_double_hyphen(self):
        """Test CAT-2: name with double hyphen."""
        skill_dir = self._make_skill(name='a--b', parent='skills', extra=[])
        # Rename folder to match for CAT-2 check to pass folder match
        new_skill_dir = os.path.join(os.path.dirname(skill_dir), 'a--b')
        if skill_dir != new_skill_dir:
            os.rename(skill_dir, new_skill_dir)
            skill_dir = new_skill_dir
        result = self._lint(skill_dir)
        self.assertIn('CAT-2', result)

    # CAT-3: description validation
    def test_cat3_no_use_clause(self):
        """Test CAT-3: description without Use clause."""
        skill_dir = self._make_skill(desc='Does a thing with no clause.')
        result = self._lint(skill_dir)
        self.assertIn('CAT-3', result)

    def test_cat3_too_long(self):
        """Test CAT-3: description exceeds 1024 characters."""
        long_desc = 'Use when ' + 'x' * 1030
        skill_dir = self._make_skill(desc=long_desc)
        result = self._lint(skill_dir)
        self.assertIn('CAT-3', result)

    # CAT-4: top-level keys and metadata
    def test_cat4_unknown_key_version(self):
        """Test CAT-4: unknown top-level key 'version'."""
        skill_dir = self._make_skill(extra=['version: 1.0.0'])
        result = self._lint(skill_dir)
        self.assertIn('CAT-4', result)

    def test_cat4_argument_hint_without_claude_target(self):
        """Test CAT-4: argument-hint without catalog-targets 'claude'."""
        skill_dir = self._make_skill(extra=['argument-hint: x'])
        result = self._lint(skill_dir)
        self.assertIn('CAT-4', result)

    # CAT-5: metadata validation
    def test_cat5_missing_required_key(self):
        """Test CAT-5: missing required metadata key."""
        skill_dir = self._make_skill(meta_overrides={'catalog-owner': None})
        result = self._lint(skill_dir)
        self.assertIn('CAT-5', result)

    def test_cat5_invalid_version_format(self):
        """Test CAT-5: invalid catalog-version format."""
        skill_dir = self._make_skill(meta_overrides={'catalog-version': '1.0'})
        result = self._lint(skill_dir)
        self.assertIn('CAT-5', result)

    def test_cat5_invalid_date_format(self):
        """Test CAT-5: invalid catalog-last-verified format."""
        skill_dir = self._make_skill(meta_overrides={'catalog-last-verified': 'yesterday'})
        result = self._lint(skill_dir)
        self.assertIn('CAT-5', result)

    def test_cat5_unquoted_numeric_value(self):
        """Test CAT-5: unquoted numeric value in metadata."""
        skill_dir = self._make_skill(
            meta_overrides={'catalog-max-age-days': None},
            raw_meta=['catalog-max-age-days: 90']
        )
        result = self._lint(skill_dir)
        self.assertIn('CAT-5', result)

    # CAT-6: promoted source validation
    def test_cat6_http_not_https(self):
        """Test CAT-6: promoted source must be https."""
        skill_dir = self._make_skill(
            meta_overrides={
                'catalog-provenance': 'promoted',
                'catalog-source': 'http://example.com/r',
                'catalog-source-sha': 'a' * 40,
                'catalog-source-license': 'MIT',
            },
            files=['LICENSE']
        )
        result = self._lint(skill_dir)
        self.assertIn('CAT-6', result)

    def test_cat6_invalid_sha_length(self):
        """Test CAT-6: promoted source SHA must be 40 hex characters."""
        skill_dir = self._make_skill(
            meta_overrides={
                'catalog-provenance': 'promoted',
                'catalog-source': 'https://example.com/r',
                'catalog-source-sha': 'a' * 39,
                'catalog-source-license': 'MIT',
            },
            files=['LICENSE']
        )
        result = self._lint(skill_dir)
        self.assertIn('CAT-6', result)

    def test_cat6_invalid_license(self):
        """Test CAT-6: promoted source license must be in allowlist."""
        skill_dir = self._make_skill(
            meta_overrides={
                'catalog-provenance': 'promoted',
                'catalog-source': 'https://example.com/r',
                'catalog-source-sha': 'a' * 40,
                'catalog-source-license': 'GPL-3.0',
            },
            files=['LICENSE']
        )
        result = self._lint(skill_dir)
        self.assertIn('CAT-6', result)

    def test_cat6_missing_license_file(self):
        """Test CAT-6: promoted source must have LICENSE file."""
        skill_dir = self._make_skill(
            meta_overrides={
                'catalog-provenance': 'promoted',
                'catalog-source': 'https://example.com/r',
                'catalog-source-sha': 'a' * 40,
                'catalog-source-license': 'MIT',
            }
        )
        result = self._lint(skill_dir)
        self.assertIn('CAT-6', result)

    # CAT-7: body validation
    def test_cat7_file_too_long(self):
        """Test CAT-7: file exceeds 500 lines."""
        skill_dir = self._make_skill()
        # Add many extra lines
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        with open(skill_file, 'a') as f:
            for _ in range(501):
                f.write('\n')
        result = self._lint(skill_dir)
        self.assertIn('CAT-7', result)

    def test_cat7_template_syntax(self):
        """Test CAT-7: file contains template syntax."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace('# Body', 'Run {{FAST_GATE_CMD}}')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertIn('CAT-7', result)

    def test_cat7_missing_reference(self):
        """Test CAT-7: backticked reference to missing file."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace('# Body', 'See `references/missing.md`')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertIn('CAT-7', result)

    def test_cat7_reference_outside_directory(self):
        """Test CAT-7: reference path outside skill directory."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace('# Body', 'See [x](../other/SKILL.md)')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertIn('CAT-7', result)

    # CAT-14: changelog validation
    def test_cat14_missing_changelog(self):
        """Test CAT-14: missing changelog heading."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().split('\n## Changelog')[0] + '\n'
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertIn('CAT-14', result)

    # CAT-22: catalog-consumers validation
    def test_cat22_invalid_consumers_value(self):
        """Test CAT-22: invalid catalog-consumers value."""
        skill_dir = self._make_skill(meta_overrides={'catalog-consumers': 'some'})
        result = self._lint(skill_dir)
        self.assertIn('CAT-22', result)

    def test_cat22_non_held_with_none(self):
        """Test CAT-22: non-held skill cannot have consumers 'none'."""
        skill_dir = self._make_skill(meta_overrides={'catalog-consumers': 'none'})
        result = self._lint(skill_dir)
        self.assertIn('CAT-22', result)

    def test_cat22_held_without_none(self):
        """Test CAT-22: held skill must have consumers 'none'."""
        skill_dir = self._make_skill(parent='.held')
        result = self._lint(skill_dir)
        self.assertIn('CAT-22', result)

    # Valid cases with no findings
    def test_valid_argument_hint_with_claude_target(self):
        """Test valid: argument-hint is allowed with catalog-targets 'claude'."""
        skill_dir = self._make_skill(
            extra=['argument-hint: x'],
            meta_overrides={'catalog-targets': 'claude'}
        )
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Valid argument-hint skill should have no findings, got: {result}')

    def test_valid_held_skill_with_none(self):
        """Test valid: held skill with catalog-consumers 'none'."""
        skill_dir = self._make_skill(
            meta_overrides={'catalog-consumers': 'none'},
            parent='.held'
        )
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Valid held skill should have no findings, got: {result}')

    def test_valid_promoted_skill(self):
        """Test valid: promoted skill with proper metadata and LICENSE."""
        skill_dir = self._make_skill(
            meta_overrides={
                'catalog-provenance': 'promoted',
                'catalog-source': 'https://example.com/r',
                'catalog-source-sha': 'a' * 40,
                'catalog-source-license': 'Apache-2.0',
            },
            files=['LICENSE']
        )
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Valid promoted skill should have no findings, got: {result}')

    def test_valid_with_file_references(self):
        """Test valid: skill with backticked and markdown file references."""
        skill_dir = self._make_skill(
            files=['references/ok.md', 'scripts/y.py']
        )
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace('# Body', 'See `references/ok.md` and [y](scripts/y.py)')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Valid skill with references should have no findings, got: {result}')

    def test_valid_anchor_link_is_not_a_file_reference(self):
        """Test CAT-7: a pure anchor link such as ](#changelog) is not a file reference."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace('# Body', 'See [log](#changelog)')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertEqual(result, '', f'Anchor link should not be a finding, got: {result}')

    def test_cat5_optional_metadata_value_must_be_string(self):
        """Test CAT-5: every metadata value is a string, not only the required ones."""
        skill_dir = self._make_skill(raw_meta=['  catalog-requires: 42'])
        result = self._lint(skill_dir)
        self.assertIn('CAT-5', result)
        self.assertIn('catalog-requires', result)

    def test_cat4_unknown_key_reported_without_metadata(self):
        """Test CAT-4: an unknown top-level key is reported even when metadata is missing."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        text = '---\nname: good-skill\ndescription: Use when x.\nversion: 1\n---\n'
        with open(skill_file, 'w') as f:
            f.write(text)
        result = self._lint(skill_dir)
        self.assertIn('unknown key "version"', result)

    def test_cat14_first_row_must_name_source(self):
        """Test CAT-14: the first changelog row, not just any row, names the source commit."""
        skill_dir = self._make_skill()
        skill_file = os.path.join(skill_dir, 'SKILL.md')
        content = open(skill_file).read().replace(
            '| --- | --- | --- |\n',
            '| --- | --- | --- |\n| 1.0.1 | 2026-11-12 | Edited locally |\n')
        with open(skill_file, 'w') as f:
            f.write(content)
        result = self._lint(skill_dir)
        self.assertIn('CAT-14', result)

    def test_parse_rejects_lines_outside_subset(self):
        """Test CAT-4: lines outside the YAML subset raise ValueError."""
        bad = [
            '---\nname: a\n  stray: x\n---\n',
            '---\nname: a\nmetadata:\n    deep: "x"\n---\n',
            '---\nlicense:MIT\n---\n',
            '---\nname: a\nname: b\n---\n',
            '---\nmetadata:\n  k: "a"\n  k: "b"\n---\n',
        ]
        for text in bad:
            with self.assertRaises(ValueError, msg=repr(text)):
                _lint.parse_frontmatter(text)

    # Parse frontmatter tests
    def test_parse_quoted_value_stays_string(self):
        """Test parse_frontmatter: quoted values stay as strings."""
        # This is tested implicitly by the linter, but we can verify the parser
        text = '---\nname: "test"\ndescription: "Do thing. Use when needed."\nmetadata:\n  key: "value"\n---\n\n# Body'
        fm, body = _lint.parse_frontmatter(text)
        self.assertIsInstance(fm['name'], str)
        self.assertEqual(fm['name'], 'test')
        self.assertIsInstance(fm['metadata']['key'], str)

    def test_parse_unquoted_number_becomes_int(self):
        """Test parse_frontmatter: unquoted numbers become integers."""
        text = '---\nname: test\nmetadata:\n  age: 90\n---\n\n# Body'
        fm, body = _lint.parse_frontmatter(text)
        self.assertIsInstance(fm['metadata']['age'], int)
        self.assertEqual(fm['metadata']['age'], 90)

    def test_parse_block_scalar_folds(self):
        """Test parse_frontmatter: block scalar with > folds into one line."""
        text = '---\nname: test\ndescription: >\n  This is a long\n  description that\n  should fold.\nmetadata:\n  key: "value"\n---\n\n# Body'
        fm, body = _lint.parse_frontmatter(text)
        # The '>' folding should join the lines with spaces
        self.assertIn('This is a long', fm['description'])

    # Test lint_tree and full repo
    def test_lint_tree_repo_clean(self):
        """Test lint_tree: the 22 catalog skills plus zz-canary (23) lint clean."""
        results = _lint.lint_tree(self.repo_root)

        # 22 catalog skills plus the held zz-canary (FRS-18)
        self.assertEqual(len(results), 23, f'Expected 23 skills, got {len(results)}')
        self.assertIn('.held/zz-canary', results)

        # Check all are clean
        for skill_name, findings in results.items():
            self.assertEqual(findings, [], f'{skill_name} has findings: {findings}')


class TestHiddenTextAndPrivilegedKeys(unittest.TestCase):
    """CAT-27 (invisible characters, hidden comments) and CAT-28 (privileged keys)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.schema = _lint.load_schema()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _skill(self, body='# Body\n', extra='', files=None, name='hidden-skill'):
        skill_dir = os.path.join(self.temp_dir.name, 'skills', name)
        os.makedirs(skill_dir)
        text = ('---\nname: ' + name + '\ndescription: Do a thing. Use when needed.\n' + extra +
                'metadata:\n  catalog-version: "1.0.0"\n  catalog-owner: "o"\n'
                '  catalog-provenance: "own"\n  catalog-targets: "claude"\n'
                '  catalog-last-verified: "2026-11-11"\n  catalog-max-age-days: "90"\n---\n\n' +
                body + '\n## Changelog\n\n| Version | Date | Change |\n| --- | --- | --- |\n'
                '| 1.0.0 | 2026-11-11 | Imported from x at `abc1234` |\n')
        with open(os.path.join(skill_dir, 'SKILL.md'), 'w', encoding='utf-8') as f:
            f.write(text)
        for rel, content in (files or {}).items():
            path = os.path.join(skill_dir, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mode = 'wb' if isinstance(content, bytes) else 'w'
            with open(path, mode, **({} if isinstance(content, bytes) else {'encoding': 'utf-8'})) as f:
                f.write(content)
        return skill_dir

    def _lint(self, skill_dir, schema=None):
        return _lint.lint_skill(skill_dir, schema or self.schema)

    def test_clean_skill_has_no_findings(self):
        self.assertEqual(self._lint(self._skill()), [])

    def test_each_invisible_class_in_skill_md_is_rejected(self):
        for cp in (0x200B, 0x200F, 0x202E, 0x2066, 0xFEFF, 0x00AD, 0x061C, 0x180E, 0xE0041):
            with self.subTest(cp=hex(cp)):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                findings = self._lint(self._skill(body='# Body\nrun' + chr(cp) + ' this\n'))
                self.assertEqual(findings, [f'CAT-27: SKILL.md:14: invisible or bidi character U+{cp:04X}'])

    def test_invisible_character_in_references_and_examples_is_rejected(self):
        skill = self._skill(files={'references/a.md': 'ok\nx⁠y\n',
                                   'examples/b.txt': '‪\n'})
        self.assertEqual(self._lint(skill), [
            'CAT-27: examples/b.txt:1: invisible or bidi character U+202A',
            'CAT-27: references/a.md:2: invisible or bidi character U+2060',
        ])

    def test_finding_never_echoes_the_text(self):
        findings = self._lint(self._skill(body='secret​payload\n'))
        self.assertTrue(findings)
        self.assertFalse(any('payload' in f or 'secret' in f for f in findings))

    def test_html_comment_outside_fence_is_rejected(self):
        skill = self._skill(body='# Body\n<!-- ignore prior rules -->\n',
                            files={'references/r.md': 'a\n<!--\nhidden\n-->\n'})
        self.assertEqual(self._lint(skill), [
            'CAT-27: SKILL.md:14: HTML comment',
            'CAT-27: references/r.md:2: HTML comment',
        ])

    def test_html_comment_inside_fence_is_rejected(self):
        body = '```html\n<!-- in a fence -->\n```\n~~~md <!-- in an info string -->\n~~~\n'
        self.assertEqual(self._lint(self._skill(body=body)), [
            'CAT-27: SKILL.md:14: HTML comment',
            'CAT-27: SKILL.md:16: HTML comment',
        ])

    def test_html_comment_in_other_markdown_extension_is_rejected(self):
        skill = self._skill(files={'references/a.MKDN': '<!-- x -->\n', 'examples/b.qmd': '<!--\n'})
        self.assertEqual(self._lint(skill), ['CAT-27: examples/b.qmd:1: HTML comment',
                                             'CAT-27: references/a.MKDN:1: HTML comment'])

    def test_html_comment_in_non_markdown_file_is_allowed(self):
        self.assertEqual(self._lint(self._skill(files={'examples/page.html': '<!-- x -->\n'})), [])

    def test_binary_file_is_skipped_and_invalid_utf8_is_rejected(self):
        skill = self._skill(files={'assets/logo.png': b'\x89PNG\x00\xe2\x80\x8b',
                                   'references/latin1.txt': b'caf\xe9\n'})
        self.assertEqual(self._lint(skill), ['CAT-27: references/latin1.txt is not valid UTF-8'])

    def test_skill_md_read_as_utf8_regardless_of_locale(self):
        findings = self._lint(self._skill(body='# Café → ok\n'))
        self.assertEqual(findings, [])

    def test_symlink_is_not_followed(self):
        outside = os.path.join(self.temp_dir.name, 'outside.md')
        with open(outside, 'w', encoding='utf-8') as f:
            f.write('​')
        skill = self._skill()
        os.symlink(outside, os.path.join(skill, 'link.md'))
        self.assertEqual(self._lint(skill), [])

    def test_privileged_keys_need_an_allowlist_entry(self):
        for line, key in (('allowed-tools: Bash Read\n', 'allowed-tools'),
                          ('shell: bash\n', 'shell'), ('hooks: x\n', 'hooks')):
            with self.subTest(key=key):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                self.assertEqual(self._lint(self._skill(extra=line)),
                                 [f'CAT-28: key "{key}" needs an entry in x-privileged-allowlist'])

    def test_allowlisted_exact_value_passes_and_other_value_fails(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'allowed-tools': 'Read Grep'}}
        self.assertEqual(self._lint(self._skill(extra='allowed-tools: Read Grep\n'), schema), [])
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.assertEqual(self._lint(self._skill(extra='allowed-tools: Read Grep Bash\n'), schema),
                         ['CAT-28: key "allowed-tools" does not match its x-privileged-allowlist entry'])

    def test_block_scalar_privileged_value_is_refused(self):
        # A folded scalar with a blank line parses here as "echo rm -rf x" but
        # YAML reads it as two lines, so it must not match a plain entry.
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'shell': 'echo rm -rf x'}}
        self.assertEqual(self._lint(self._skill(extra='shell: echo rm -rf x\n'), schema), [])
        self.temp_dir.cleanup()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.assertEqual(self._lint(self._skill(extra='shell: >-\n  echo\n\n  rm -rf x\n'), schema),
                         ['CAT-28: key "shell" must be one plain or quoted scalar of printable ASCII on a single line'])

    def test_tag_anchor_and_alias_values_are_refused(self):
        msg = 'CAT-28: key "shell" must be one plain or quoted scalar of printable ASCII on a single line'
        for line in ('shell: !!str |\n', 'shell: &a bash\n', 'shell: *a\n',
                     'shell: [*a]\n', 'shell: {x: bash}\n', 'shell: # c\n',
                     'shell: - x\n', 'shell: "open\n', 'shell: a #c\n',
                     'shell: bash\u2028evil\n', 'shell: "bad\\q"\n', 'shell: foo:\n',
                     "shell: 'it''s'\n"):
            with self.subTest(line=line):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                schema = dict(self.schema)
                schema['x-privileged-allowlist'] = {'hidden-skill': {'shell': line[len('shell: '):-1]}}
                self.assertEqual(self._lint(self._skill(extra=line), schema), [msg])

    def test_star_inside_plain_value_is_allowed(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'allowed-tools': 'Read Bash(git:*)'}}
        self.assertEqual(self._lint(self._skill(extra='allowed-tools: Read Bash(git:*)\n'), schema), [])

    def test_file_path_finding_shows_code_point(self):
        findings = self._lint(self._skill(files={'references/a\u200b.md': 'x\u200b\n'}))
        self.assertFalse(any('\u200b' in f for f in findings), findings)
        self.assertIn('CAT-27: references/a<U+200B>.md:1: invisible or bidi character U+200B', findings)

    def test_cli_output_shows_code_point_for_folder_name(self):
        import subprocess
        import sys
        self._skill(name='bad\u200bname')
        r = subprocess.run([sys.executable, '-I', _lint_path, self.temp_dir.name],
                           capture_output=True, encoding='utf-8')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('\u200b', r.stdout)
        self.assertIn('skills/bad<U+200B>name: ', r.stdout)

    def test_bare_carriage_return_counts_as_line_break(self):
        skill = self._skill(files={'references/r.md': 'a\rb\r\nc\r\u200b<!-- x -->\n'})
        self.assertEqual(self._lint(skill), [
            'CAT-27: references/r.md:4: invisible or bidi character U+200B',
            'CAT-27: references/r.md:4: HTML comment',
        ])

    def test_reference_finding_shows_code_point(self):
        findings = self._lint(self._skill(body='See `references/a\u200bb.md`.\n'))
        self.assertFalse(any('\u200b' in f for f in findings), findings)
        self.assertIn('CAT-7: reference "references/a<U+200B>b.md" does not resolve to a file', findings)

    def test_carriage_return_in_privileged_value_is_rejected(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'shell': 'bash'}}
        findings = self._lint(self._skill(extra='shell: bash\r  evil\n'), schema)
        self.assertEqual(findings, ['CAT-4: Invalid frontmatter line:   evil'])

    def test_value_is_compared_as_written(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'shell': 'bash'}}
        mismatch = 'CAT-28: key "shell" does not match its x-privileged-allowlist entry'
        syntax = 'CAT-28: key "shell" must be one plain or quoted scalar of printable ASCII on a single line'
        for line, want in (('shell: "bash"\n', mismatch), ('shell:  bash\n', syntax),
                           ('shell: bash \n', syntax)):
            with self.subTest(line=line):
                self.temp_dir.cleanup()
                self.temp_dir = tempfile.TemporaryDirectory()
                self.assertEqual(self._lint(self._skill(extra=line), schema), [want])

    def test_parser_error_shows_code_point_not_invisible_character(self):
        findings = self._lint(self._skill(extra='bad\u200bkey: x\n'))
        self.assertFalse(any('\u200b' in f for f in findings), findings)
        self.assertIn('CAT-4: Invalid frontmatter line: bad<U+200B>key: x', findings)
        self.assertIn('CAT-27: SKILL.md:4: invisible or bidi character U+200B', findings)

    def test_quoted_name_uses_parsed_name_for_allowlist(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'hidden-skill': {'shell': 'bash'}}
        skill = self._skill(extra='shell: bash\n')
        path = os.path.join(skill, 'SKILL.md')
        with open(path, encoding='utf-8') as f:
            text = f.read().replace('name: hidden-skill', 'name: "hidden-skill"', 1)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        self.assertEqual(self._lint(skill, schema), [])

    def test_allowlist_entry_does_not_cover_another_skill(self):
        schema = dict(self.schema)
        schema['x-privileged-allowlist'] = {'other-skill': {'shell': 'bash'}}
        self.assertEqual(self._lint(self._skill(extra='shell: bash\n'), schema),
                         ['CAT-28: key "shell" needs an entry in x-privileged-allowlist'])

    def test_main_schema_grants_nothing(self):
        self.assertEqual(self.schema['x-privileged-keys'], ['allowed-tools', 'hooks', 'shell'])
        self.assertEqual(self.schema['x-privileged-allowlist'], {})


if __name__ == '__main__':
    unittest.main()
