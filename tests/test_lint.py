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
        """Test lint_tree: all 22 catalog skills lint clean."""
        results = _lint.lint_tree(self.repo_root)

        # Check we have 22 skills
        self.assertEqual(len(results), 22, f'Expected 22 skills, got {len(results)}')

        # Check all are clean
        for skill_name, findings in results.items():
            self.assertEqual(findings, [], f'{skill_name} has findings: {findings}')


if __name__ == '__main__':
    unittest.main()
