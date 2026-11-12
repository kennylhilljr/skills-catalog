#!/usr/bin/env python3
"""Local checks run by the pre-commit and pre-push hooks (CAT-10, CAT-19).

    python3 tools/local_checks.py

Run from anywhere inside the repository. Two checks run, and both always run:

- Exclusion check (CAT-19). Reads the owner's local exclusion list from the
  file named by SKILLS_CATALOG_EXCLUDE_FILE, else from
  ~/.config/skills-catalog/exclude.txt. One name per line, `#` starts a
  comment; a missing file skips the check. Each name is matched as a fixed
  string, in any letter case, against the content staged in the index. A
  finding prints `path:line: excluded skill name`, never the name or the line.
- Scrub (CAT-10). Runs tools/scrub.py's scan_tree on the repository with the
  same rules as CI, including the private repo list default, and prints
  `path:line: kind`.

Exit status: 0 clean, 1 findings, 2 error. Diagnostics never quote either
list. Python 3.9+, standard library only.
"""

import importlib.util
import os
import subprocess
import sys

EXCLUDE_ENV = 'SKILLS_CATALOG_EXCLUDE_FILE'
DEFAULT_EXCLUDE_FILE = os.path.join('~', '.config', 'skills-catalog', 'exclude.txt')
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


class CheckError(Exception):
    """An operational failure; its message never quotes a list entry."""


def _load_scrub():
    spec = importlib.util.spec_from_file_location(
        'skills_catalog_scrub', os.path.join(TOOLS_DIR, 'scrub.py')
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _parse_grep_z(output):
    """(path, line) pairs from `git grep -z -n` output: path NUL line NUL text LF."""
    found = set()
    pos = 0
    size = len(output)
    while pos < size:
        path_end = output.find(b'\0', pos)
        if path_end < 0:
            break
        line_end = output.find(b'\0', path_end + 1)
        if line_end < 0:
            raise CheckError('unexpected git grep output')
        text_end = output.find(b'\n', line_end + 1)
        if text_end < 0:
            text_end = size
        path = os.fsdecode(output[pos:path_end])
        try:
            line = int(output[path_end + 1:line_end])
        except ValueError:
            raise CheckError('unexpected git grep output')
        found.add((path, line))
        pos = text_end + 1
    return sorted(found)


def exclusion_findings(root, names):
    """Sorted (path, line) pairs where a staged file contains a listed name.

    Matching is case-insensitive and by fixed string. Binary files are
    skipped. Patterns go to git through stdin, so they never appear in a
    process listing.
    """
    if not names:
        return []
    patterns = ''.join(name + '\n' for name in names).encode('utf-8')
    try:
        result = subprocess.run(
            [
                'git', '-C', root,
                '-c', 'grep.patternType=fixed',
                '-c', 'grep.fallbackToNoIndex=false',
                'grep', '--cached', '--no-color', '--no-textconv', '--full-name',
                '-n', '-i', '-F', '-I', '-z', '-f', '-',
            ],
            input=patterns,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CheckError('git is not installed')
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise CheckError('git grep failed')
    return _parse_grep_z(result.stdout)


def excluded_path_count(root, names):
    """Number of staged paths whose name contains a listed name (any case)."""
    if not names:
        return 0
    try:
        listing = subprocess.run(
            ['git', '-C', root, 'ls-files', '--cached', '-z'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise CheckError('git is not installed')
    if listing.returncode != 0:
        raise CheckError('git ls-files failed')
    lowered = [name.lower() for name in names]
    count = 0
    for raw in listing.stdout.split(b'\0'):
        if not raw:
            continue
        path = os.fsdecode(raw).lower()
        if any(name in path for name in lowered):
            count += 1
    return count


def scrub_findings(root, scrub=None):
    """Sorted (path, line, kind) scrub findings, with CI's private list default."""
    if scrub is None:
        scrub = _load_scrub()
    names = []
    default = os.path.expanduser(scrub.DEFAULT_PRIVATE_LIST)
    if os.path.exists(default):
        try:
            names = scrub.read_private_list(default)
        except (OSError, UnicodeDecodeError):
            raise CheckError('cannot read the private repo list')
    try:
        return scrub.scan_tree(root, names)
    except scrub.ScrubError as err:
        raise CheckError('scrub: ' + str(err))


def main(argv=None):
    if argv:
        print('usage: python3 tools/local_checks.py', file=sys.stderr)
        return 2
    try:
        root = repo_root()
    except CheckError as err:
        print('local checks: error: ' + str(err), file=sys.stderr)
        return 2

    failed = False
    errored = False

    try:
        names = read_names(exclude_path())
        if names is None:
            print('exclusion check: skipped, no exclusion list')
        else:
            hits = exclusion_findings(root, names)
            for path, line in hits:
                print('%s:%d: excluded skill name' % (path, line))
            paths = excluded_path_count(root, names)
            if paths:
                print('exclusion check: %d staged path(s) contain an excluded skill name' % paths)
            print('exclusion check: %d findings' % (len(hits) + paths))
            failed = failed or bool(hits) or bool(paths)
    except CheckError as err:
        print('exclusion check: error: ' + str(err), file=sys.stderr)
        errored = True

    try:
        findings = scrub_findings(root)
        for path, line, kind in findings:
            print('%s:%d: %s' % (path, line, kind))
        print('scrub: %d findings' % len(findings))
        failed = failed or bool(findings)
    except CheckError as err:
        print('scrub: error: ' + str(err), file=sys.stderr)
        errored = True

    if errored:
        return 2
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
