"""Materialize the delivered skills of one catalog tag (MCP-1, MCP-6, INV-8, CAT-20).

The server never reads a working tree. `materialize()` extracts `skills/` and
`catalog.json` of one tag with `git archive` into a private temp folder,
checks every served file against that tag's manifest, and drops the skills on
the local exclusion list (DESIGN 5.1) before any provider sees the folder.
Nothing outside `skills/` is extracted, so `.held/`, `inventory/`,
`freshness/`, `docs/` and `tools/` cannot be served.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

log = logging.getLogger('skills_catalog_mcp')

DEFAULT_EXCLUDE_FILE = os.path.join('~', '.config', 'skills-catalog', 'exclude.txt')
TAG_PATTERN = 'refs/tags/v*'
_NAME = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$')
_MISMATCH = 'skills/ does not match catalog.json'


class SnapshotError(Exception):
    """The tag cannot be served. The message never holds file content."""


@dataclasses.dataclass
class Snapshot:
    tag: str
    commit: str
    root: str
    catalog: list
    _tmp: str = dataclasses.field(repr=False, default='')

    def cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = ''


def load_exclusions(path):
    """Return the lowercased folder names in an exclusion file.

    One name per line, `#` starts a comment, case-insensitive. A missing file
    means an empty list. The names are never logged or returned in an error.
    """
    if path is None:
        return frozenset()
    try:
        with open(os.path.expanduser(path), encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        return frozenset()
    names = set()
    for line in lines:
        name = line.split('#', 1)[0].strip()
        if name:
            names.add(name.lower())
    return frozenset(names)


def _git(repo, *args):
    proc = subprocess.run(
        ['git', '-C', os.fspath(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout


def _resolve_tag(repo, ref):
    """Return (tag, commit) for ref, which must name a tag."""
    if ref is None:
        code, out = _git(
            repo, 'for-each-ref', '--sort=-v:refname', '--sort=-creatordate',
            '--format=%(refname:strip=2)', TAG_PATTERN,
        )
        if code != 0:
            raise SnapshotError('no catalog tag')
        tags = out.decode('utf-8', 'replace').split()
        if not tags:
            raise SnapshotError('no catalog tag')
        ref = tags[0]
    if not ref or ref.startswith('-') or any(c.isspace() for c in ref):
        raise SnapshotError('ref is not a tag')
    code, out = _git(repo, 'rev-parse', '--verify', '--quiet', 'refs/tags/%s^{commit}' % ref)
    if code != 0:
        raise SnapshotError('ref is not a tag')
    return ref, out.decode('ascii').strip()


def _skill_of(member_name):
    """Return the skill folder name of a tar member under skills/, else None."""
    parts = member_name.rstrip('/').split('/')
    if len(parts) >= 2 and parts[0] == 'skills':
        return parts[1]
    return None


def _hash_files(folder):
    """Map each file under folder (relative, '/'-separated) to its sha256, like tools/catalog.py."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in dirnames + filenames:
            if stat.S_ISLNK(os.lstat(os.path.join(dirpath, name)).st_mode):
                raise SnapshotError('symlink in skills/' + os.path.basename(folder))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(full).st_mode):
                raise SnapshotError('special file in skills/' + os.path.basename(folder))
            rel = os.path.relpath(full, folder).replace(os.sep, '/')
            with open(full, 'rb') as handle:
                found[rel] = hashlib.sha256(handle.read()).hexdigest()
    return found


def materialize(repo, ref=None, exclude_file=None):
    """Extract the delivered skills of tag `ref` (default: newest `v*` tag) of `repo`.

    `exclude_file` is the local exclusion list; None means no list. Raises
    SnapshotError when the ref is not a tag or the tree does not match the
    tag's catalog.json.
    """
    excluded = load_exclusions(exclude_file)
    tag, commit = _resolve_tag(repo, ref)
    code, data = _git(repo, 'archive', '--format=tar', 'refs/tags/' + tag, '--', 'skills', 'catalog.json')
    if code != 0:
        raise SnapshotError('tag has no skills/ or catalog.json')

    tmp = tempfile.mkdtemp(prefix='skills-catalog-mcp-')
    os.chmod(tmp, 0o700)
    try:
        return _extract(tag, commit, data, tmp, excluded)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _extract(tag, commit, data, tmp, excluded):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        members = []
        for member in archive.getmembers():
            skill = _skill_of(member.name)
            if skill is not None and skill.lower() in excluded:
                continue  # never written to disk, never named
            if member.name not in ('catalog.json', 'skills', 'skills/') and skill is None:
                raise SnapshotError(_MISMATCH + ': unexpected entry under skills/')
            if member.issym() or member.islnk():
                raise SnapshotError('symlink in skills/' + skill if skill else 'symlink in archive')
            if not (member.isfile() or member.isdir()):
                raise SnapshotError('special file in skills/' + (skill or ''))
            members.append(member)
        try:
            archive.extractall(tmp, members=members, filter='data')
        except tarfile.TarError as exc:
            raise SnapshotError('cannot extract the tag: ' + type(exc).__name__) from None

    try:
        with open(os.path.join(tmp, 'catalog.json'), encoding='utf-8') as handle:
            manifest = json.load(handle)
        entries = manifest['skills']
        if not isinstance(entries, list):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        raise SnapshotError('tag has no valid catalog.json') from None

    root = os.path.join(tmp, 'skills')
    os.makedirs(root, mode=0o700, exist_ok=True)
    skipped = 0
    expected = {}
    for entry in entries:
        name = entry.get('name') if isinstance(entry, dict) else None
        if not isinstance(name, str):
            raise SnapshotError(_MISMATCH + ': entry without a name')
        if name.lower() in excluded:
            skipped += 1
            continue
        if not _NAME.match(name) or entry.get('path') != 'skills/' + name or not isinstance(entry.get('files'), dict):
            raise SnapshotError(_MISMATCH + ': bad entry skills/' + name)
        expected[name] = entry

    present = set()
    for name in sorted(os.listdir(root)):
        mode = os.lstat(os.path.join(root, name)).st_mode
        if not stat.S_ISDIR(mode) or name not in expected:
            raise SnapshotError(_MISMATCH + ': skills/' + name + ' is not in the manifest')
        present.add(name)
    for name, entry in expected.items():
        if name not in present:
            raise SnapshotError(_MISMATCH + ': skills/' + name + ' is missing')
        if _hash_files(os.path.join(root, name)) != entry['files']:
            raise SnapshotError(_MISMATCH + ': skills/' + name + ' differs from its hashes')

    if skipped:
        log.info('skipped %d skill(s) from the local exclusion list', skipped)
    os.remove(os.path.join(tmp, 'catalog.json'))
    catalog = [expected[name] for name in sorted(expected)]
    return Snapshot(tag=tag, commit=commit, root=root, catalog=catalog, _tmp=tmp)
