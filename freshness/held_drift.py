"""Held mirror drift for the Mac half of the freshness bot (SDD-1).

A held `.held/sdd-*` folder mirrors a private source skill. The catalog copy
keeps the source body verbatim; only the frontmatter and the first
`## Changelog` row may differ (DESIGN 3.2). The source paths come from the
untracked local config (part 9), never from the repo. Findings carry only the
folder name and two sha256 values: no path, line or text from the source.
"""

import hashlib
import os
import re

HELD_DIR = '.held'
PREFIX = 'sdd-'

_CHANGELOG = re.compile(r'^#{2}\s+Changelog\s*$')
_TABLE_ROW = re.compile(r'^\s*\|')
_SEPARATOR = re.compile(r'^\s*\|[\s:|-]+\|\s*$')


def _finding(name, evidence):
    return {'kind': 'held-drift', 'skill': name, 'subject': '%s/%s' % (HELD_DIR, name),
            'file': '%s/%s/SKILL.md' % (HELD_DIR, name), 'line': None,
            'evidence': evidence, 'fix': None}


def body(text):
    """Return the text after the frontmatter, with LF line endings."""
    text = text.replace('\r\n', '\n')
    lines = text.split('\n')
    if lines and lines[0].strip() == '---':
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                return '\n'.join(lines[i + 1:])
    return text


def drop_first_changelog_row(text):
    """Remove the first data row of the `## Changelog` table, if any."""
    lines = text.split('\n')
    in_changelog = False
    for i, line in enumerate(lines):
        if _CHANGELOG.match(line):
            in_changelog = True
            continue
        if not in_changelog:
            continue
        if line.startswith('#'):
            break
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1]):
            # Header row; the first data row follows the separator.
            j = i + 2
            if j < len(lines) and _TABLE_ROW.match(lines[j]):
                return '\n'.join(lines[:j] + lines[j + 1:])
            break
    return text


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _normal(text):
    return text


def held_matches(catalog_text, source_text):
    """Return (matches, catalog sha256, source sha256) for two SKILL.md texts.

    The catalog's first changelog row is always dropped. The source's first
    row is dropped too when that is what makes the bodies agree, so both a
    replaced first row and an added import row on top of the source's rows
    count as clean.
    """
    cat = _normal(drop_first_changelog_row(body(catalog_text)))
    src_full = _normal(body(source_text))
    src_dropped = _normal(drop_first_changelog_row(body(source_text)))
    if cat == src_full:
        return True, _sha(cat), _sha(src_full)
    if cat == src_dropped:
        return True, _sha(cat), _sha(src_dropped)
    return False, _sha(cat), _sha(src_full)


def check_held(root, sources, excluded):
    """Return `held-drift` findings for mapped `.held/sdd-*` folders.

    `sources` maps a held folder name to its source `SKILL.md` path (from the
    local config). `excluded` folder names are skipped before any read.
    Folders without a mapped source are not checked.
    """
    held = os.path.join(root, HELD_DIR)
    if not os.path.isdir(held):
        return []
    findings = []
    for name in sorted(os.listdir(held)):
        if name in excluded or not name.startswith(PREFIX) or name not in sources:
            continue
        catalog_path = os.path.join(held, name, 'SKILL.md')
        if not os.path.isfile(catalog_path):
            continue
        source_path = os.path.expanduser(sources[name])
        if not os.path.isfile(source_path):
            findings.append(_finding(name, '%s: source missing' % name))
            continue
        with open(catalog_path, encoding='utf-8', errors='replace') as fh:
            catalog_text = fh.read()
        with open(source_path, encoding='utf-8', errors='replace') as fh:
            source_text = fh.read()
        ok, cat_sha, src_sha = held_matches(catalog_text, source_text)
        if not ok:
            findings.append(_finding(
                name, '%s: catalog body sha256 %s, source body sha256 %s' % (name, cat_sha, src_sha)))
    return findings
