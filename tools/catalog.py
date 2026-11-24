#!/usr/bin/env python3
"""Build catalog.json, the manifest of skills and held skills (CAT-11, CAT-23).

Usage: python3 tools/catalog.py [--root DIR] [--check]

The `stale` flag (FRS-8): `freshness/stale.json`, when present, is a sorted
JSON list of skill names that only freshness bot PRs change. Each listed
skill gets `"stale": true` as the last key of its entry, in `skills` or
`held`. Other entries have no `stale` key, so readers treat a missing key as
not stale (`entry.get("stale") is True`). A listed name that is not in the
manifest is an error.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys

_LINT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lint.py')
_spec = importlib.util.spec_from_file_location('catalog_lint_for_manifest', _LINT_PATH)
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)

STALE_MESSAGE = 'catalog.json is out of date; run python3 tools/catalog.py'
STALE_LIST = os.path.join('freshness', 'stale.json')
CANARY = 'zz-canary'

# (entry key, metadata key, default when the metadata key is absent)
_FIELDS = (
    ('version', 'catalog-version', ''),
    ('owner', 'catalog-owner', ''),
    ('targets', 'catalog-targets', None),
    ('consumers', 'catalog-consumers', 'all'),
    ('last_verified', 'catalog-last-verified', ''),
)


def _raise(err):
    """os.walk error handler: never skip a folder silently."""
    raise err


def _split_targets(value):
    return [part.strip() for part in value.split(',') if part.strip()]


IGNORED_FILES = ('.DS_Store',)


def _refuse(path, mode):
    """Raise ValueError for a symlink or special file (CAT-11).

    The server and tools/install-local refuse a whole tag that holds one, so
    the manifest check fails on it instead of skipping it.
    """
    kind = 'symlink' if stat.S_ISLNK(mode) else 'special file'
    raise ValueError('%s: %s is not allowed under skills/ or .held/' % (path, kind))


def _hash_files(folder, label):
    """Map each regular file under folder (relative, '/'-separated) to its sha256.

    A regular file named .DS_Store is ignored, as the server and
    tools/install-local do. A symlink or special file anywhere in the folder
    raises ValueError naming its repository-relative path (label + '/' + rel).
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(folder, onerror=_raise, followlinks=False):
        # os.walk decides "symlink or not" for subfolders with calls that hide
        # OSErrors; lstat each one so such an error makes build() raise.
        for name in dirnames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if not stat.S_ISDIR(mode):
                _refuse(label + '/' + os.path.relpath(full, folder).replace(os.sep, '/'), mode)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, folder).replace(os.sep, '/')
            # lstat, not os.path.isfile/islink: those hide OSErrors.
            mode = os.lstat(full).st_mode
            if not stat.S_ISREG(mode):
                _refuse(label + '/' + rel, mode)
            if name in IGNORED_FILES:
                continue
            with open(full, 'rb') as handle:
                found[rel] = hashlib.sha256(handle.read()).hexdigest()
    return {rel: found[rel] for rel in sorted(found)}


def _entry(root, section, name):
    path = section + '/' + name
    folder = os.path.join(root, section, name)
    with open(os.path.join(folder, 'SKILL.md'), encoding='utf-8') as handle:
        frontmatter, _body = _lint.parse_frontmatter(handle.read())
    metadata = frontmatter.get('metadata') or {}
    entry = {'name': name, 'path': path}
    for key, meta_key, default in _FIELDS:
        if meta_key not in metadata:
            entry[key] = [] if key == 'targets' else default
            continue
        value = metadata[meta_key]
        if not isinstance(value, str):
            raise ValueError('%s: metadata %s must be a string, got %r' % (path, meta_key, value))
        entry[key] = _split_targets(value) if key == 'targets' else value
    entry['files'] = _hash_files(folder, path)
    return entry


def _has_skill_md(folder, label):
    """True if folder holds a regular SKILL.md.

    The caller has already refused a symlinked or special folder. Only "does
    not exist" counts as no; other OSErrors propagate, and a SKILL.md that is
    a symlink or special file raises ValueError.
    """
    path = os.path.join(folder, 'SKILL.md')
    try:
        mode = os.lstat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    if stat.S_ISREG(mode):
        return True
    if stat.S_ISDIR(mode):
        return False
    _refuse(label + '/SKILL.md', mode)


def _check_tree(root, section):
    """Refuse a symlink or special file anywhere under section (CAT-11, CAT-23).

    Walks every folder, including ones without a SKILL.md and the canary,
    because the server and tools/install-local refuse a tag for any of them.
    A missing section is fine.
    """
    base = os.path.join(root, section)
    try:
        mode = os.lstat(base).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(mode):
        _refuse(section, mode)
    for dirpath, dirnames, filenames in os.walk(base, onerror=_raise, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                rel = os.path.relpath(full, root).replace(os.sep, '/')
                _refuse(rel, mode)


def _section(root, section):
    _check_tree(root, section)
    names = []
    base = os.path.join(root, section)
    for name in os.listdir(base):
        full = os.path.join(base, name)
        if section == '.held' and name == CANARY:
            continue  # FRS-19: the canary is never published
        if stat.S_ISDIR(os.lstat(full).st_mode) and _has_skill_md(full, section + '/' + name):
            names.append(name)
    return [_entry(root, section, name) for name in sorted(names)]


def read_stale(root='.'):
    """The names in freshness/stale.json, or [] when the file is missing."""
    try:
        with open(os.path.join(root, STALE_LIST), encoding='utf-8') as handle:
            names = json.load(handle)
    except FileNotFoundError:
        return []
    if not (isinstance(names, list) and all(isinstance(name, str) for name in names)):
        raise ValueError('freshness/stale.json must be a JSON list of skill names')
    if names != sorted(set(names)):
        raise ValueError('freshness/stale.json must be sorted and without duplicates')
    return names


def build(root='.'):
    """Return the catalog manifest for the repository at root."""
    doc = {
        'version': 1,
        'skills': _section(root, 'skills'),
        'held': _section(root, '.held'),
    }
    stale = set(read_stale(root))
    for entry in doc['skills'] + doc['held']:
        if entry['name'] in stale:
            entry['stale'] = True
            stale.discard(entry['name'])
    if stale:
        raise ValueError('freshness/stale.json names %s, not a skill in the catalog'
                         % ', '.join(sorted(stale)))
    return doc


def render(root='.'):
    return json.dumps(build(root), indent=2) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build or check catalog.json.')
    parser.add_argument('--root', default='.', help='repository root (default: .)')
    parser.add_argument('--check', action='store_true',
                        help='exit 1 if catalog.json is missing or out of date')
    args = parser.parse_args(argv)
    expected = render(args.root)
    target = os.path.join(args.root, 'catalog.json')
    if not args.check:
        with open(target, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(expected)
        return 0
    try:
        with open(target, encoding='utf-8', newline='') as handle:
            current = handle.read()
    except FileNotFoundError:
        current = None
    if current != expected:
        print(STALE_MESSAGE)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
