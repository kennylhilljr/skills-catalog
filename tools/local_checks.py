#!/usr/bin/env python3
"""Local checks run by the pre-commit and pre-push hooks (CAT-10, CAT-19).

    python3 tools/local_checks.py                 # the staged index
    python3 tools/local_checks.py --pre-push [REMOTE [URL]] < refs

Run from anywhere inside the repository. Both checks read git objects, never
the working tree, so they see exactly what will be committed or pushed.

- Without arguments (pre-commit) they read every file staged in the index.
- With --pre-push they read the ref lines git gives a pre-push hook on stdin
  (`<local ref> <local sha> <remote ref> <remote sha>`) and check every
  commit being pushed: each file the commit adds or changes, read at that
  commit, plus the commit message for the exclusion check. Commits reachable
  from the remote sha git reports for that ref are skipped, but only when
  this clone has that commit. For a new ref, or a remote sha this clone
  lacks, the pushed ref's whole history is checked: tracking refs can be
  stale, so they are never trusted. That can recheck historical commits.
  Deleting a ref checks nothing.

Two checks run, and both always run:

- Exclusion check (CAT-19). Reads the owner's local exclusion list from the
  file named by SKILLS_CATALOG_EXCLUDE_FILE, else from
  ~/.config/skills-catalog/exclude.txt. One name per line, `#` starts a
  comment; a missing file skips the check. Each name is matched as a fixed
  string, in any letter case, line by line; binary files are skipped. A
  finding prints `path:line: excluded skill name`, never the name or the
  line. A listed name inside a printed path shows as `[excluded]`. Checked
  paths that contain a listed name are counted, not printed.
- Scrub (CAT-10). Runs tools/scrub.py's scan_text with the same rules as CI,
  including the private repo list default, and prints `path:line: kind`.

In pre-push mode each finding starts with the short commit id, as in
`1a2b3c4:notes.md:2: excluded skill name`.

Exit status: 0 clean, 1 findings, 2 error. Diagnostics never quote either
list. Python 3.9+, standard library only.
"""

import importlib.util
import os
import re
import subprocess
import sys

EXCLUDE_ENV = 'SKILLS_CATALOG_EXCLUDE_FILE'
DEFAULT_EXCLUDE_FILE = os.path.join('~', '.config', 'skills-catalog', 'exclude.txt')
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REDACTED = '[excluded]'
MESSAGE_PATH = '(commit message)'
# Regular files, executables and symlinks; gitlinks (submodules) have no blob.
BLOB_MODES = (b'100644', b'100755', b'120000')
# git's binary heuristic: a NUL byte in the first 8000 bytes.
BINARY_PROBE = 8000


class CheckError(Exception):
    """An operational failure; its message never quotes a list entry."""


def _load_scrub():
    spec = importlib.util.spec_from_file_location(
        'skills_catalog_scrub', os.path.join(TOOLS_DIR, 'scrub.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root, args, data=None, ok=(0,)):
    """(returncode, stdout bytes) of `git -C root args`; CheckError otherwise."""
    try:
        result = subprocess.run(
            ['git', '-C', root] + list(args),
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CheckError('git is not installed')
    if result.returncode not in ok:
        raise CheckError('git ' + args[0] + ' failed')
    return result.returncode, result.stdout


def repo_root(start='.'):
    """Top of the work tree that contains start."""
    try:
        probe = subprocess.run(
            ['git', '-C', start, 'rev-parse', '--show-toplevel'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CheckError('git is not installed')
    if probe.returncode != 0:
        raise CheckError('not inside a git work tree')
    return os.fsdecode(probe.stdout.rstrip(b'\n'))


def exclude_path():
    """The exclusion list path: SKILLS_CATALOG_EXCLUDE_FILE, else the default."""
    override = os.environ.get(EXCLUDE_ENV)
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser(DEFAULT_EXCLUDE_FILE)


def read_names(path):
    """Names from a list file: `#` comments, surrounding spaces and blanks dropped.

    Returns None when the file does not exist.
    """
    if not os.path.exists(path):
        return None
    names = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for raw in handle:
                name = raw.split('#', 1)[0].strip()
                if name and name not in names:
                    names.append(name)
    except (OSError, UnicodeDecodeError):
        raise CheckError('cannot read the exclusion list')
    return names


def redact(path, names):
    """path with every listed name, in any letter case, replaced by [excluded]."""
    if not names:
        return path
    # Longest first, so a name that contains another is replaced whole.
    ordered = sorted(names, key=len, reverse=True)
    pattern = re.compile('|'.join(re.escape(name) for name in ordered), re.IGNORECASE)
    return pattern.sub(REDACTED, path)


def read_blobs(root, wanted):
    """{oid: bytes} for wanted, a list of (path, oid) pairs, via one cat-file call.

    A missing or unreadable object raises CheckError naming its path.
    """
    oids = []
    path_of = {}
    for path, oid in wanted:
        if oid not in path_of:
            path_of[oid] = path
            oids.append(oid)
    if not oids:
        return {}
    request = ''.join(oid + '\n' for oid in oids).encode('ascii')
    _, out = _git(root, ['cat-file', '--batch'], data=request)
    blobs = {}
    pos = 0
    for oid in oids:
        header_end = out.find(b'\n', pos)
        if header_end < 0:
            raise CheckError('cannot read ' + path_of[oid])
        header = out[pos:header_end].split()
        if len(header) != 3 or header[1] != b'blob':
            raise CheckError('cannot read ' + path_of[oid])
        try:
            size = int(header[2])
        except ValueError:
            raise CheckError('cannot read ' + path_of[oid])
        start = header_end + 1
        blobs[oid] = out[start:start + size]
        pos = start + size + 1
    return blobs


def index_items(root):
    """[(label, path, oid)] for every blob staged in the index."""
    _, out = _git(root, ['ls-files', '--stage', '-z'])
    items = []
    for record in out.split(b'\0'):
        if not record:
            continue
        meta, _, raw_path = record.partition(b'\t')
        fields = meta.split()
        if len(fields) != 3 or fields[0] not in BLOB_MODES:
            continue
        items.append(('', os.fsdecode(raw_path), fields[1].decode('ascii')))
    return items


def _is_zero(sha):
    return set(sha) == {'0'}


def _commit_exists(root, sha):
    code, _ = _git(root, ['cat-file', '-e', sha + '^{commit}'], ok=(0, 1, 128))
    return code == 0


def pushed_commits(root, ref_lines, remote=None):
    """Commit ids being pushed, oldest first, without repeats.

    For an update whose remote sha this clone has, the range is
    remote_sha..local_sha. For a new ref, or a remote sha this clone lacks,
    every commit reachable from local_sha. Remote-tracking refs are never
    used to skip commits, because a stale one (after `git remote set-url`,
    say) would hide commits the remote does not have. The cost is that a
    new ref rechecks historical commits. `remote` is accepted for the hook's
    calling convention and does not change the result.
    """
    seen = set()
    commits = []
    for raw in ref_lines:
        fields = raw.split()
        if not fields:
            continue
        if len(fields) != 4:
            raise CheckError('unexpected pre-push input')
        local_sha, remote_sha = fields[1], fields[3]
        if _is_zero(local_sha):
            continue
        args = ['rev-list', '--reverse', local_sha]
        if not _is_zero(remote_sha) and _commit_exists(root, remote_sha):
            args += ['--not', remote_sha]
        _, out = _git(root, args)
        for line in out.decode('ascii').split():
            if line not in seen:
                seen.add(line)
                commits.append(line)
    return commits


def commit_items(root, commit):
    """[(label, path, oid)] for each blob the commit adds or changes.

    A merge is compared with its first parent; a root commit with nothing.
    """
    _, parents = _git(root, ['rev-list', '--parents', '-n', '1', commit])
    ids = parents.decode('ascii').split()
    args = ['diff-tree', '-r', '-z', '--no-renames', '--diff-filter=d']
    if len(ids) > 1:
        args += [ids[1], commit]
    else:
        args += ['--root', '--no-commit-id', commit]
    _, out = _git(root, args)
    parts = out.split(b'\0')
    label = commit[:7] + ':'
    items = []
    index = 0
    while index + 1 < len(parts):
        meta = parts[index]
        if not meta.startswith(b':'):
            index += 1
            continue
        fields = meta[1:].split()
        path = os.fsdecode(parts[index + 1])
        index += 2
        if len(fields) < 4 or fields[1] not in BLOB_MODES:
            continue
        items.append((label, path, fields[3].decode('ascii')))
    return items


def commit_message(root, commit):
    _, out = _git(root, ['cat-file', 'commit', commit])
    _, _, message = out.partition(b'\n\n')
    return message


def exclusion_line_hits(data, lowered):
    """Sorted one-based line numbers of data that contain a listed name."""
    if b'\0' in data[:BINARY_PROBE]:
        return []
    text = data.decode('utf-8', 'replace').lower()
    return [
        number
        for number, line in enumerate(text.split('\n'), 1)
        if any(name in line for name in lowered)
    ]


def run_checks(root, items, messages, names, scrub, private_names, path_word, report_skip=True):
    """Print findings for items [(label, path, oid)]; return (failed, errored).

    names is None when there is no exclusion list; report_skip says so.
    """
    failed = False
    wanted = []
    seen = set()
    for label, path, oid in items:
        if (path, oid) not in seen:
            seen.add((path, oid))
            wanted.append((label, path, oid))
    try:
        blobs = read_blobs(root, [(path, oid) for _, path, oid in wanted])
    except CheckError as err:
        print('local checks: error: ' + redact(str(err), names), file=sys.stderr)
        return failed, True

    if names is None:
        if report_skip:
            print('exclusion check: skipped, no exclusion list')
    else:
        lowered = [name.lower() for name in names]
        hits = []
        for label, path, oid in wanted:
            for line in exclusion_line_hits(blobs[oid], lowered):
                hits.append((label, path, line))
        for label, message in messages:
            for line in exclusion_line_hits(message, lowered):
                hits.append((label, MESSAGE_PATH, line))
        for label, path, line in hits:
            print('%s%s:%d: excluded skill name' % (label, redact(path, names), line))
        paths = len({path for _, path, _ in wanted if any(n in path.lower() for n in lowered)})
        if paths:
            print('exclusion check: %d %s path(s) contain an excluded skill name' % (paths, path_word))
        print('exclusion check: %d findings' % (len(hits) + paths))
        failed = failed or bool(hits) or bool(paths)

    findings = []
    for label, path, oid in wanted:
        try:
            text = blobs[oid].decode('utf-8')
        except UnicodeDecodeError:
            continue
        for line, kind in scrub.scan_text(text, path, private_names):
            findings.append((label, path, line, kind))
    for label, path, line, kind in findings:
        print('%s%s:%d: %s' % (label, redact(path, names), line, kind))
    print('scrub: %d findings' % len(findings))
    failed = failed or bool(findings)
    return failed, False


def private_names_for(scrub):
    """CI's private repo list default, or [] when it does not exist."""
    default = os.path.expanduser(scrub.DEFAULT_PRIVATE_LIST)
    if not os.path.exists(default):
        return []
    try:
        return scrub.read_private_list(default)
    except (OSError, UnicodeDecodeError):
        raise CheckError('cannot read the private repo list')


def main(argv=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    push = bool(argv) and argv[0] == '--pre-push'
    if (argv and not push) or len(argv) > 3:
        print(
            'usage: python3 tools/local_checks.py [--pre-push [REMOTE [URL]]]',
            file=sys.stderr,
        )
        return 2
    try:
        root = repo_root()
    except CheckError as err:
        print('local checks: error: ' + str(err), file=sys.stderr)
        return 2

    errored = False
    names = None
    try:
        names = read_names(exclude_path())
    except CheckError as err:
        print('exclusion check: error: ' + str(err), file=sys.stderr)
        errored = True

    try:
        scrub = _load_scrub()
        private_names = private_names_for(scrub)
        messages = []
        if push:
            remote = argv[1] if len(argv) > 1 else None
            lines = (stdin or sys.stdin).read().splitlines()
            items = []
            for commit in pushed_commits(root, lines, remote):
                items.extend(commit_items(root, commit))
                messages.append((commit[:7] + ':', commit_message(root, commit)))
            path_word = 'pushed'
        else:
            items = index_items(root)
            path_word = 'staged'
    except CheckError as err:
        print('local checks: error: ' + redact(str(err), names), file=sys.stderr)
        return 2

    # When the exclusion list could not be read, the scrub still runs.
    failed, read_errored = run_checks(
        root, items, messages, names, scrub, private_names, path_word,
        report_skip=not errored,
    )
    if errored or read_errored:
        return 2
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
