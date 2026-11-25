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
import posixpath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile

log = logging.getLogger('skills_catalog_mcp')

DEFAULT_EXCLUDE_FILE = os.path.join('~', '.config', 'skills-catalog', 'exclude.txt')
TAG_PATTERN = 'refs/tags/v*'
DEFAULT_MAIN_REF = 'origin/main'
# Fixed messages for the reviewed-tag check: they never echo the tag or ref.
NOT_REVIEWED = 'tag is not reviewed: it must be an annotated tag on the main branch'
NO_MAIN_REF = 'main ref not found'
_NAME = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$')
_MISMATCH = 'skills/ does not match catalog.json'
# Regular files that tools/catalog.py leaves out of the manifest.
IGNORED_FILES = ('.DS_Store',)


class SnapshotError(Exception):
    """The tag cannot be served. The message never holds file content."""


@dataclasses.dataclass
class Snapshot:
    tag: str
    commit: str
    root: str
    catalog: list
    _tmp: str = dataclasses.field(repr=False, default='')
    # The lowercased local exclusion list, kept so the tools can drop any
    # text that names an excluded skill (INV-7). Never shown in repr.
    excluded: frozenset = dataclasses.field(repr=False, default=frozenset())

    def cleanup(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = ''


def load_exclusions(path):
    """Return the lowercased folder names in an exclusion file.

    One name per line, `#` starts a comment, case-insensitive. A missing file
    means an empty list. The names are never logged or returned in an error.
    """
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


def _git_env():
    """The environment with git's global and system config switched off.

    The snapshot then depends only on the repository, never on the user's
    git config (filters, attributes, aliases).
    """
    # Drop every inherited GIT_* variable: GIT_DIR, GIT_WORK_TREE and the
    # like would override `git -C <repo>` and read another repository.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0'})
    return env


def _git(repo, *args):
    proc = subprocess.run(
        ['git', '-C', os.fspath(repo), *args],
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode, proc.stdout


# Release tag order (MCP-1, MCP-9). Keep this block identical in
# tools/install-local and server/skills_catalog_mcp/snapshot.py;
# tests/test_tag_order.py checks that both copies agree.
_TAG_RE = re.compile(r'^v([0-9]+(?:\.[0-9]+)*)(.*)$', re.DOTALL)
_CHUNK_RE = re.compile(r'[0-9]+|[^0-9]+', re.DOTALL)


def _number_key(digits):
    """Compare a run of ASCII digits as a number, without int() size limits."""
    digits = digits.lstrip('0')
    return (len(digits), digits)


def tag_sort_key(tag):
    """Sort key for a release tag, higher is newer; None if tag is not v<digit>...

    The tag is v<numbers><rest>. Numbers compare as integers, so v1.10 is newer
    than v1.9. An empty rest, or one that starts with `+` (build metadata), is
    a release. Any other rest (v1.0-rc1, v1.0rc1) is a pre-release, older than
    the release with the same numbers. Pre-release suffixes compare piece by
    piece, digit runs as numbers, so rc10 is newer than rc2. The full name
    breaks ties. Git's versionsort config is never consulted.
    """
    match = _TAG_RE.match(tag)
    if match is None:
        return None
    numbers = tuple(_number_key(part) for part in match.group(1).split('.'))
    rest = match.group(2)
    release = 1 if (not rest or rest.startswith('+')) else 0
    pieces = tuple((0, _number_key(piece)) if piece[0] in '0123456789' else (1, piece)
                   for piece in _CHUNK_RE.findall(rest))
    return (numbers, release, pieces, tag)


def newest_tag(names):
    """The newest name by tag_sort_key, skipping names that do not parse; None if none do."""
    keyed = [(tag_sort_key(name), name) for name in names]
    keyed = [pair for pair in keyed if pair[0] is not None]
    return max(keyed)[1] if keyed else None


def _resolve_tag(repo, ref):
    """Return (tag, commit) for ref, which must name a tag."""
    if ref is None:
        # Version order from tag_sort_key, never tag dates or git's
        # versionsort config, so this picks the same tag as install-local.
        code, out = _git(repo, 'for-each-ref', '--format=%(refname:strip=2)', TAG_PATTERN)
        if code != 0:
            raise SnapshotError('no catalog tag')
        ref = newest_tag(out.decode('utf-8', 'replace').split())
        if ref is None:
            raise SnapshotError('no catalog tag')
    # The ref is a literal tag name, never a revision expression such as
    # `v1~1` or `v1^{tree}`, which would resolve to an untagged commit.
    if not isinstance(ref, str) or not ref or ref.startswith('-'):
        raise SnapshotError('ref is not a tag')
    code, _out = _git(repo, 'check-ref-format', 'refs/tags/' + ref)
    if code != 0:
        raise SnapshotError('ref is not a tag')
    code, out = _git(repo, 'show-ref', '--verify', '--hash', 'refs/tags/' + ref)
    if code != 0:
        raise SnapshotError('ref is not a tag')
    oid = out.decode('ascii').strip()
    code, out = _git(repo, 'rev-parse', '--verify', '--quiet', oid + '^{commit}')
    if code != 0:
        raise SnapshotError('ref is not a tag')
    return ref, out.decode('ascii').strip()


def _check_reviewed(repo, tag, commit, main_ref):
    """Refuse a tag that is not annotated or whose commit is not on main_ref.

    Every catalog commit on main passed the `gate` check before it merged, so
    a tag on an ancestor of main_ref serves reviewed text. The manifest check
    alone only proves the tag is self-consistent. A lightweight tag, or a tag
    on a local or unmerged commit, is refused with a fixed message.
    """
    code, out = _git(repo, 'cat-file', '-t', 'refs/tags/' + tag)
    if code != 0 or out.strip() != b'tag':
        raise SnapshotError(NOT_REVIEWED)
    if not isinstance(main_ref, str) or not main_ref or main_ref.startswith('-'):
        raise SnapshotError(NO_MAIN_REF)
    code, out = _git(repo, 'rev-parse', '--verify', '--quiet', '--end-of-options', main_ref + '^{commit}')
    if code != 0:
        raise SnapshotError(NO_MAIN_REF)
    main = out.decode('ascii').strip()
    code, _out = _git(repo, 'merge-base', '--is-ancestor', commit, main)
    if code != 0:
        raise SnapshotError(NOT_REVIEWED)


def _inside(tmp, name):
    """True if archive member `name` is a plain relative path that stays inside tmp.

    Refuses `\\`, `:` and NUL on every platform (some read them as separators
    or drive letters), absolute paths and `.` or `..` parts, then checks the
    resolved target too.
    """
    if not name or any(c in name for c in '\\:\x00') or name.startswith('/'):
        return False
    parts = [p for p in name.split('/') if p]
    if not parts or any(p in ('.', '..') for p in parts):
        return False
    base = os.path.realpath(tmp)
    target = os.path.realpath(os.path.join(base, *parts))
    return os.path.commonpath([base, target]) == base


def _skill_of(member_name):
    """Return the skill folder name of a tar member under skills/, else None."""
    parts = member_name.rstrip('/').split('/')
    if len(parts) >= 2 and parts[0] == 'skills':
        return parts[1]
    return None


def _hash_files(folder):
    """Map each file under folder (relative, '/'-separated) to its sha256, like tools/catalog.py.

    A regular file named in IGNORED_FILES is left out, as tools/catalog.py
    leaves it out of the manifest.
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in dirnames + filenames:
            if stat.S_ISLNK(os.lstat(os.path.join(dirpath, name)).st_mode):
                raise SnapshotError('symlink in skills/' + os.path.basename(folder))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(full).st_mode):
                raise SnapshotError('special file in skills/' + os.path.basename(folder))
            if name in IGNORED_FILES:
                continue
            rel = os.path.relpath(full, folder).replace(os.sep, '/')
            with open(full, 'rb') as handle:
                found[rel] = hashlib.sha256(handle.read()).hexdigest()
    return found


def materialize(repo, ref=None, exclude_file=DEFAULT_EXCLUDE_FILE, require_reviewed=False,
               main_ref=DEFAULT_MAIN_REF):
    """Extract the delivered skills of tag `ref` (default: highest `v*` tag) of `repo`.

    `exclude_file` is the local exclusion list, by default
    `~/.config/skills-catalog/exclude.txt`; a missing file is an empty list.
    Tests pass their own file. With `require_reviewed`, the tag must be an
    annotated tag whose commit is an ancestor of `main_ref` (default
    `origin/main`). Raises SnapshotError when the ref is not a tag, the tag
    is not reviewed, or the tree does not match the tag's catalog.json.
    """
    excluded = load_exclusions(exclude_file)
    tag, commit = _resolve_tag(repo, ref)
    if require_reviewed:
        _check_reviewed(repo, tag, commit, main_ref)
    code, data = _git(repo, 'archive', '--format=tar', commit, '--', 'skills', 'catalog.json')
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
            if not _inside(tmp, member.name):
                raise SnapshotError('unsafe path in archive')
            if member.issym() or member.islnk():
                raise SnapshotError('symlink in skills/' + skill if skill else 'symlink in archive')
            if not (member.isfile() or member.isdir()):
                raise SnapshotError('special file in skills/' + (skill or ''))
            if member.isfile() and skill is not None and posixpath.basename(member.name) in IGNORED_FILES:
                continue  # not in the manifest, so never served
            members.append(member)
        try:
            # filter='data' exists from Python 3.11.4. Before that, the
            # checks above (regular files and folders only, every path
            # resolving inside tmp) are what keep the extraction inside tmp.
            if hasattr(tarfile, 'data_filter'):
                archive.extractall(tmp, members=members, filter='data')
            else:
                archive.extractall(tmp, members=members)
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
    return Snapshot(tag=tag, commit=commit, root=root, catalog=catalog, _tmp=tmp, excluded=excluded)
