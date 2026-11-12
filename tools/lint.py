#!/usr/bin/env python3
"""Frontmatter linter for SKILL.md files in the skills catalog."""

import json
import os
import re
import sys
import glob

_KEY = r'([A-Za-z0-9][A-Za-z0-9_-]*)'
_TOP_LINE = re.compile(r'^' + _KEY + r':(?: (.*))?$')
_META_LINE = re.compile(r'^  ' + _KEY + r': (.*)$')


def parse_frontmatter(text):
    """Parse SKILL.md frontmatter and body.

    Returns:
        (dict, body): frontmatter dict and body text

    Raises:
        ValueError: if fences or parsing fails
    """
    lines = text.split('\n')

    if not lines or lines[0] != '---':
        raise ValueError('First line must be exactly "---"')

    # Find closing fence
    fence_end = None
    for i in range(1, len(lines)):
        if lines[i] == '---':
            fence_end = i
            break

    if fence_end is None:
        raise ValueError('Closing --- fence not found')

    # Parse frontmatter
    frontmatter_lines = lines[1:fence_end]
    body = '\n'.join(lines[fence_end + 1:])

    frontmatter = {}
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]

        # Skip blank lines
        if not line.strip():
            i += 1
            continue

        # Check for metadata map start
        if line == 'metadata:' or line.startswith('metadata: '):
            # Check if value is empty (starts a map)
            rest = line[len('metadata:'):].strip()
            if rest:
                raise ValueError('metadata must have empty value to start map')

            if 'metadata' in frontmatter:
                raise ValueError('Duplicate frontmatter key: metadata')
            # Parse metadata pairs
            metadata = {}
            i += 1
            while i < len(frontmatter_lines):
                meta_line = frontmatter_lines[i]

                if not meta_line.strip():
                    i += 1
                    continue

                if not meta_line.startswith('  '):
                    break

                # Exactly a two-space indent and `key: value`; anything else
                # is outside the supported subset.
                m = _META_LINE.match(meta_line)
                if not m:
                    raise ValueError(f'Invalid metadata line: {meta_line}')

                if m.group(1) in metadata:
                    raise ValueError(f'Duplicate metadata key: {m.group(1)}')
                metadata[m.group(1)] = parse_scalar(m.group(2))
                i += 1

            frontmatter['metadata'] = metadata
            continue

        # Parse top-level key: value (no indent, `key: value` or bare `key:`)
        m = _TOP_LINE.match(line)
        if not m:
            raise ValueError(f'Invalid frontmatter line: {line}')

        key = m.group(1)
        value = m.group(2) or ''
        if key in frontmatter:
            raise ValueError(f'Duplicate frontmatter key: {key}')

        # Check for block scalar indicators
        if value.strip() in ('>', '>-', '|', '|-'):
            # Block scalar: collect indented lines
            indicator = value.strip()
            scalar_lines = []
            i += 1
            while i < len(frontmatter_lines):
                scalar_line = frontmatter_lines[i]
                if scalar_line and not scalar_line.startswith('  '):
                    break
                if scalar_line.startswith('  '):
                    scalar_lines.append(scalar_line[2:])
                elif not scalar_line.strip():
                    scalar_lines.append('')
                i += 1

            # Process based on indicator
            if indicator in ('>', '>-'):
                # Fold: join with spaces, strip trailing if '-'
                result = ' '.join(line for line in scalar_lines if line.strip())
                if indicator == '>':
                    result += '\n'
            else:  # | or |-
                # Join with newlines, strip trailing if '-'
                result = '\n'.join(scalar_lines)
                if indicator == '|':
                    result += '\n'

            frontmatter[key] = result
            continue

        # Parse scalar value
        parsed_value = parse_scalar(value)
        frontmatter[key] = parsed_value
        i += 1

    return frontmatter, body


def parse_scalar(value):
    """Parse a scalar value (quoted, plain, or type-coerced)."""
    value = value.strip()

    if not value:
        return ''

    # Double-quoted
    if value.startswith('"') and value.endswith('"'):
        inner = value[1:-1]
        inner = inner.replace('\\\"', '"')
        inner = inner.replace('\\\\', '\\')
        return inner

    # Single-quoted
    if value.startswith("'") and value.endswith("'"):
        inner = value[1:-1]
        inner = inner.replace("''", "'")
        return inner

    # Plain: try type coercion
    # Integer
    if re.match(r'^[-+]?[0-9]+$', value):
        return int(value)

    # Float
    if re.match(r'^[-+]?[0-9]*\.[0-9]+$', value) or re.match(r'^[-+]?[0-9]+\.[0-9]*$', value):
        return float(value)

    # Boolean
    if value.lower() in ('true', 'false'):
        return value.lower() == 'true'

    # Null
    if value.lower() in ('null', '~'):
        return None

    # String
    return value


def load_schema(schema_path=None):
    """Load the frontmatter schema."""
    if schema_path is None:
        schema_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..',
            'schema',
            'frontmatter.schema.json'
        )

    with open(schema_path) as f:
        return json.load(f)


def lint_skill(skill_dir, schema=None):
    """Lint a single skill directory.

    Returns:
        list[str]: List of findings as 'CAT-N: message'
    """
    if schema is None:
        try:
            schema = load_schema()
        except Exception:
            return ['CAT-4: Could not load schema']

    skill_file = os.path.join(skill_dir, 'SKILL.md')

    # Check file exists
    if not os.path.isfile(skill_file):
        return ['CAT-4: SKILL.md is missing']

    # Parse frontmatter
    try:
        with open(skill_file) as f:
            text = f.read()
        frontmatter, body = parse_frontmatter(text)
    except ValueError as e:
        return [f'CAT-4: {e}']

    findings = []

    # CAT-2: name validation
    name = frontmatter.get('name')
    if not isinstance(name, str):
        findings.append('CAT-2: name must be a string')
    elif not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', name):
        findings.append('CAT-2: name does not match ^[a-z0-9]+(-[a-z0-9]+)*$')
    elif len(name) > 64:
        findings.append('CAT-2: name exceeds 64 characters')
    else:
        # Check folder name matches
        folder_name = os.path.basename(os.path.abspath(skill_dir))
        if folder_name != name:
            findings.append(f'CAT-2: folder name "{folder_name}" does not match name "{name}"')

    # CAT-3: description validation
    desc = frontmatter.get('description')
    if not isinstance(desc, str):
        findings.append('CAT-3: description must be a string')
    elif not desc.strip():
        findings.append('CAT-3: description is empty')
    elif len(desc) > 1024:
        findings.append('CAT-3: description exceeds 1024 characters')
    elif not re.search(r'\bUse (when|after|before|for)\b', desc):
        findings.append('CAT-3: description does not contain "Use when/after/before/for"')

    # CAT-4: top-level keys validation
    metadata = frontmatter.get('metadata')
    if metadata is None:
        findings.append('CAT-4: metadata is missing')
    elif not isinstance(metadata, dict):
        findings.append('CAT-4: metadata must be a dict')
    # Check for disallowed top-level keys whether or not metadata parsed
    catalog_targets = metadata.get('catalog-targets') if isinstance(metadata, dict) else None
    for key in frontmatter:
        if key not in schema['properties']:
            # Check if it's in x-claude-only and catalog-targets is 'claude'
            if key not in schema['x-claude-only']:
                findings.append(f'CAT-4: unknown key "{key}"')
            elif catalog_targets != 'claude':
                findings.append(f'CAT-4: key "{key}" requires catalog-targets to be "claude"')

    # CAT-5: metadata validation
    if isinstance(metadata, dict):
        # Check required keys
        required_keys = schema['properties']['metadata']['required']
        for req_key in required_keys:
            if req_key not in metadata:
                findings.append(f'CAT-5: required metadata key "{req_key}" is missing')
        # Every metadata value must be a string, required or not
        for meta_key, value in metadata.items():
            if not isinstance(value, str):
                findings.append(f'CAT-5: metadata key "{meta_key}" must be a string, got {type(value).__name__}')

        # Validate specific metadata values
        if 'catalog-version' in metadata:
            val = metadata['catalog-version']
            if isinstance(val, str) and not re.match(r'^[0-9]+\.[0-9]+\.[0-9]+$', val):
                findings.append(f'CAT-5: catalog-version must match X.Y.Z pattern')

        if 'catalog-last-verified' in metadata:
            val = metadata['catalog-last-verified']
            if isinstance(val, str) and not re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', val):
                findings.append(f'CAT-5: catalog-last-verified must match YYYY-MM-DD pattern')

    # CAT-6: promoted source validation (only when catalog-provenance == 'promoted')
    if isinstance(metadata, dict) and metadata.get('catalog-provenance') == 'promoted':
        source = metadata.get('catalog-source')
        source_sha = metadata.get('catalog-source-sha')
        source_license = metadata.get('catalog-source-license')

        # Check source URL starts with https://
        if not isinstance(source, str) or not source.startswith('https://'):
            findings.append('CAT-6: catalog-source must be https:// URL when promoted')

        # Check SHA is 40 hex chars
        if not isinstance(source_sha, str) or not re.match(r'^[0-9a-f]{40}$', source_sha):
            findings.append('CAT-6: catalog-source-sha must be 40 hex characters when promoted')

        # Check license is in allowlist
        if source_license not in schema['x-license-allowlist']:
            findings.append('CAT-6: catalog-source-license not in allowed list when promoted')

        # Check LICENSE file exists
        if not os.path.isfile(os.path.join(skill_dir, 'LICENSE')):
            findings.append('CAT-6: LICENSE file missing when promoted')

    # CAT-7: body validation
    # Line count
    lines = text.splitlines()
    if len(lines) > 500:
        findings.append('CAT-7: file exceeds 500 lines')

    # Check for template syntax
    if '{{' in text:
        findings.append('CAT-7: file contains template syntax {{')

    # Check file references
    # Collect markdown link targets and backticked paths
    references = set()

    # Markdown links: ](...)
    for match in re.finditer(r'\]\(([^\s)]+)', text):
        target = match.group(1)
        # Remove fragment
        if '#' in target:
            target = target.split('#')[0]
        # Skip URL schemes
        if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', target):
            continue
        # Skip anchors (empty after removing the fragment) and absolute paths
        if not target or target.startswith('/'):
            continue
        references.add(target)

    # Backticked paths
    for match in re.finditer(r'`((?:references|scripts|assets|examples)/[^`\s]+)`', text):
        references.add(match.group(1))

    # Validate references
    for ref in references:
        ref_path = os.path.realpath(os.path.join(skill_dir, ref))
        skill_real = os.path.realpath(skill_dir)

        # Check file exists
        if not os.path.isfile(ref_path):
            findings.append(f'CAT-7: reference "{ref}" does not resolve to a file')
        else:
            # Check path is within skill directory
            try:
                common = os.path.commonpath([ref_path, skill_real])
                if common != skill_real:
                    findings.append(f'CAT-7: reference "{ref}" is outside skill directory')
            except ValueError:
                findings.append(f'CAT-7: reference "{ref}" is outside skill directory')

    # CAT-14: changelog validation
    # Find last ## heading (ignoring code fences)
    in_fence = False
    last_changelog_line = None
    for i, line in enumerate(lines):
        if line.startswith('```'):
            in_fence = not in_fence
        elif not in_fence and line.lstrip().startswith('## '):
            last_changelog_line = i

    if last_changelog_line is None or lines[last_changelog_line].rstrip() != '## Changelog':
        findings.append('CAT-14: no "## Changelog" heading found')
    else:
        # The section holds a `| Version | Date | Change |` table, and its
        # first data row names the source commit or content hash (SPEC CAT-14).
        section = lines[last_changelog_line + 1:]
        header_re = re.compile(r'^\|\s*Version\s*\|\s*Date\s*\|\s*Change\s*\|\s*$')
        sep_re = re.compile(r'^\|(\s*:?-+:?\s*\|)+\s*$')
        source_re = re.compile(r'at `[0-9a-f]{7,40}`|sha256 `[0-9a-f]{12,64}`')
        header_at = next((j for j, l in enumerate(section) if header_re.match(l.strip())), None)
        if header_at is None:
            findings.append('CAT-14: changelog has no | Version | Date | Change | table')
        else:
            rows = []
            for l in section[header_at + 1:]:
                s = l.strip()
                if not s.startswith('|'):
                    break
                if not sep_re.match(s):
                    rows.append(s)
            if not rows:
                findings.append('CAT-14: changelog table has no rows')
            elif not source_re.search(rows[0]):
                findings.append('CAT-14: first changelog row does not name a source commit or sha256 hash')

    # CAT-22: catalog-consumers validation
    held = os.path.basename(os.path.dirname(os.path.abspath(skill_dir))) == '.held'

    if isinstance(metadata, dict):
        consumers = metadata.get('catalog-consumers', 'all')

        if consumers not in ('all', 'none'):
            findings.append('CAT-22: catalog-consumers must be "all" or "none"')

        if held and consumers != 'none':
            findings.append('CAT-22: held skills must have catalog-consumers "none"')

        if not held and consumers == 'none':
            findings.append('CAT-22: non-held skills cannot have catalog-consumers "none"')

    return findings


def lint_tree(root='.'):
    """Lint all skills in a tree.

    Returns:
        dict: mapping 'skills/<x>' or '.held/<x>' to list of findings
    """
    schema = load_schema()
    results = {}

    # Find all skills
    skills_pattern = os.path.join(root, 'skills', '*', 'SKILL.md')
    held_pattern = os.path.join(root, '.held', '*', 'SKILL.md')

    all_skills = sorted(glob.glob(skills_pattern)) + sorted(glob.glob(held_pattern))

    for skill_file in all_skills:
        skill_dir = os.path.dirname(skill_file)
        rel_path = os.path.relpath(skill_dir, root)
        # Normalize to forward slashes for consistency
        rel_path = rel_path.replace(os.sep, '/')

        findings = lint_skill(skill_dir, schema)
        results[rel_path] = findings

    return results


if __name__ == '__main__':
    root = sys.argv[1] if len(sys.argv) > 1 else '.'

    results = lint_tree(root)

    total_findings = 0
    for key in sorted(results.keys()):
        findings = results[key]
        for finding in findings:
            print(f'{key}: {finding}')
            total_findings += 1

    print(f'lint: {len(results)} skills, {total_findings} findings')
    sys.exit(1 if total_findings > 0 else 0)
