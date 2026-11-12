#!/usr/bin/env python3
"""Public-content scrub for the skills catalog (CAT-24, CAT-9).

Finds secrets, personal data, internal hosts, home paths and private repo
names in catalog files. It reports the file, line and kind of each finding
and never the matched text.

Library use:

    scan_text(text, path='', private_names=()) -> [(line, kind), ...]
    scan_tree(root='.', private_names=()) -> [(relpath, line, kind), ...]

CLI use:

    python3 tools/scrub.py [--private-repos FILE] [ROOT]

The private repo list has one name per line; `#` starts a comment. Without
--private-repos, the scrub reads ~/.config/skills-catalog/private-repos.txt
when that file exists. Exit status: 0 clean, 1 findings, 2 error.

Importing this module has no side effects. Python 3.9+, standard library only.
"""

import argparse
import math
import os
import re
import subprocess
import sys

DEFAULT_PRIVATE_LIST = os.path.join('~', '.config', 'skills-catalog', 'private-repos.txt')

# Kind patterns that need no extra validation. Each prefix has a left
# boundary so that ordinary words such as "risk-..." or "high_..." do not
# start a match.
_SIMPLE_PATTERNS = (
    ('github-token', re.compile(r'(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,}')),
    ('github-token', re.compile(r'(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}')),
    ('anthropic-key', re.compile(r'(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai-key', re.compile(r'(?<![A-Za-z0-9])sk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,}')),
    ('aws-key', re.compile(r'(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])')),
    ('slack-token', re.compile(r'(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}')),
    ('private-key', re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----')),
    (
        'internal-host',
        re.compile(
            r'(?<![A-Za-z0-9.-])'
            r'(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+'
            r'(?:local|internal|lan|ts\.net)'
            r'(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])',
            re.IGNORECASE,
        ),
    ),
    ('home-path', re.compile(r'(?<![A-Za-z0-9_.~-])/(?:Users|home)/[A-Za-z0-9._-]+/')),
)

# A dot-atom local part (no leading, trailing or doubled dot) and a dotted
# domain. Single-label domains are left out on purpose: `actions/checkout@v4`
# and `pkg@latest` are common in skill docs and are not addresses.
_EMAIL = re.compile(
    r'(?<![A-Za-z0-9._%+-])([A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*)@'
    r'((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})(?![A-Za-z0-9-])'
)
_EMAIL_EXEMPT_LOCAL = 'noreply'
_EMAIL_EXEMPT_DOMAIN = 'users.noreply.github.com'

# Four dotted numbers that are not part of a longer dotted sequence.
_IPV4 = re.compile(
    r'(?<![A-Za-z0-9_])(?<!\d\.)'
    r'(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})'
    r'(?![A-Za-z0-9_])(?!\.\d)'
)

_ENTROPY_RUN = re.compile(r'[A-Za-z0-9+=_-]+')
_ENTROPY_MIN_LENGTH = 33
_ENTROPY_MIN_BITS = 4.0
_LOWER_HEX = re.compile(r'[0-9a-f]+')

_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_FRONTMATTER_DELIMITER = re.compile(r'^---\s*$')
_CATALOG_OWNER = re.compile(r'^  catalog-owner:')
# The value of a `  catalog-owner:` line: a quoted scalar, or a plain scalar
# that ends before an inline ` #` comment. Only this span is exempt.
_CATALOG_OWNER_VALUE = re.compile(
    r'^(  catalog-owner:[ \t]*)'
    r'("(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'|[^ \t#"\'].*?|)'
    r'(?=[ \t]+#|[ \t]*$)'
)

_PRIVATE_PREFIXES = ('skills/', '.held/')
_HELD_PREFIX = '.held/'


class ScrubError(Exception):
    """An enumeration or read failure. The message never holds file content."""


def _normalize_path(path):
    path = (path or '').replace(os.sep, '/')
    while path.startswith('./'):
        path = path[2:]
    return path


def _is_private_ip(octets):
    if any(o > 255 for o in octets):
        return False
    a, b = octets[0], octets[1]
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254)


def _shannon_entropy(s):
    counts = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    n = float(len(s))
    return -sum((k / n) * math.log2(k / n) for k in counts.values())


def _is_high_entropy(run):
    if len(run) < _ENTROPY_MIN_LENGTH:
        return False
    if not any(c.isdigit() for c in run) or not any(c.isalpha() for c in run):
        return False
    if _LOWER_HEX.fullmatch(run):
        return False
    return _shannon_entropy(run) >= _ENTROPY_MIN_BITS


def _private_name_patterns(private_names):
    patterns = []
    for name in private_names:
        name = (name or '').strip()
        if name:
            patterns.append(
                re.compile(r'(?<![A-Za-z0-9_-])' + re.escape(name) + r'(?![A-Za-z0-9_-])', re.IGNORECASE)
            )
    return patterns


def _frontmatter_end(lines):
    """Index of the first line after the leading frontmatter block.

    A text that opens with `---` has frontmatter up to the closing `---`.
    Otherwise there is none, except for a bare fragment whose non-blank
    lines are all `  catalog-owner:` lines, which counts as frontmatter.
    """
    if lines and _FRONTMATTER_DELIMITER.match(lines[0]):
        for i in range(1, len(lines)):
            if _FRONTMATTER_DELIMITER.match(lines[i]):
                return i
        return len(lines)
    if all(_CATALOG_OWNER.match(line) or not line.strip() for line in lines):
        return len(lines)
    return 0


def _without_owner_value(line):
    """The catalog-owner line with only its value blanked out.

    When the value cannot be parsed, the whole line is checked, so the
    exemption never widens past the value.
    """
    m = _CATALOG_OWNER_VALUE.match(line)
    if m is None:
        return line
    return m.group(1) + ' ' * len(m.group(2)) + line[m.end(2):]


def _scan_line(line, kinds_out, check_entropy):
    for kind, pattern in _SIMPLE_PATTERNS:
        if kind not in kinds_out and pattern.search(line):
            kinds_out.add(kind)
    for m in _EMAIL.finditer(line):
        local, domain = m.group(1), m.group(2)
        if local.lower() == _EMAIL_EXEMPT_LOCAL or domain.lower() == _EMAIL_EXEMPT_DOMAIN:
            continue
        kinds_out.add('email')
        break
    for m in _IPV4.finditer(line):
        if _is_private_ip([int(g) for g in m.groups()]):
            kinds_out.add('private-ip')
            break
    if check_entropy:
        for m in _ENTROPY_RUN.finditer(line):
            if _is_high_entropy(m.group(0)):
                kinds_out.add('high-entropy')
                break


def scan_text(text, path='', private_names=()):
    """Return sorted, unique (one-based line, kind) pairs for `text`.

    `path` is repo-relative. Private names count only under skills/ and
    .held/. In a held skill's SKILL.md, the `  catalog-owner:` frontmatter
    line is exempt from the private-name check (other kinds still apply).
    """
    rel = _normalize_path(path)
    name_patterns = _private_name_patterns(private_names) if rel.startswith(_PRIVATE_PREFIXES) else []
    held = rel.startswith(_HELD_PREFIX) and rel.rsplit('/', 1)[-1] == 'SKILL.md'

    lines = [line[:-1] if line.endswith('\r') else line for line in text.split('\n')]
    owner_limit = _frontmatter_end(lines) if held else 0

    findings = set()
    fence_char = None
    fence_len = 0
    fence_is_example = False

    for index, line in enumerate(lines):
        number = index + 1
        kinds = set()
        fence = _FENCE.match(line)
        if fence_char is None:
            check_entropy = True
            if fence:
                marker, info = fence.group(1), fence.group(2)
                if not (marker[0] == '`' and '`' in info):
                    fence_char, fence_len = marker[0], len(marker)
                    fence_is_example = info.strip() == 'example'
        else:
            closing = (
                fence is not None
                and fence.group(1)[0] == fence_char
                and len(fence.group(1)) >= fence_len
                and fence.group(2).strip() == ''
            )
            if closing:
                fence_char = None
                fence_is_example = False
                check_entropy = True
            else:
                check_entropy = not fence_is_example

        _scan_line(line, kinds, check_entropy)

        if name_patterns:
            checked = line
            if held and index < owner_limit and _CATALOG_OWNER.match(line):
                checked = _without_owner_value(line)
            if any(p.search(checked) for p in name_patterns):
                kinds.add('private-repo')

        for kind in kinds:
            findings.add((number, kind))
    return sorted(findings)


def _git_prefix(root):
    """Path of root relative to its work tree top, with a trailing slash."""
    probe = subprocess.run(
        ['git', '-C', root, 'rev-parse', '--show-prefix'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        raise ScrubError('git rev-parse failed')
    return os.fsdecode(probe.stdout.strip())


def _git_files(root):
    """Tracked files under root, or None when root is not in a git work tree."""
    try:
        probe = subprocess.run(
            ['git', '-C', root, 'rev-parse', '--is-inside-work-tree'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    if probe.returncode != 0 or probe.stdout.strip() != b'true':
        return None
    listing = subprocess.run(
        ['git', '-C', root, 'ls-files', '-z'],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if listing.returncode != 0:
        raise ScrubError('git ls-files failed')
    return [os.fsdecode(p) for p in listing.stdout.split(b'\0') if p]


def _walk_files(root):
    def fail(err):
        raise ScrubError('cannot list a directory under the scan root')

    files = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        # os.walk lists a symlink to a directory under dirnames and does not
        # descend into it; keep it as an entry so its target text is scanned.
        links = [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]
        dirnames[:] = sorted(d for d in dirnames if d != '.git' and d not in links)
        for name in list(filenames) + links:
            files.append(os.path.relpath(os.path.join(dirpath, name), root))
    return files


def scan_tree(root='.', private_names=()):
    """Return sorted (relpath, line, kind) findings for every file under root.

    Uses `git ls-files` in a git work tree, else walks the tree skipping
    `.git/`. A symlink is not followed; its target text is scanned, since
    git publishes it. Skips files that are not UTF-8 and tracked paths that
    are missing or not regular files. Raises ScrubError on other enumeration
    or read failures.
    """
    if not os.path.isdir(root):
        raise ScrubError('scan root is not a directory')
    files = _git_files(root)
    # Private names count by repo-relative path, so a scan rooted at skills/
    # or .held/ inside a work tree still classifies its files correctly.
    prefix = ''
    if files is None:
        files = _walk_files(root)
    else:
        prefix = _git_prefix(root)

    findings = []
    for rel in sorted(set(files)):
        full = os.path.join(root, rel)
        try:
            if os.path.islink(full):
                data = os.fsencode(os.readlink(full))
            elif not os.path.isfile(full):
                continue
            else:
                with open(full, 'rb') as handle:
                    data = handle.read()
        except OSError:
            raise ScrubError('cannot read ' + rel)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        for line, kind in scan_text(text, prefix + rel, private_names):
            findings.append((rel, line, kind))
    return findings


def read_private_list(path):
    """Names from a private repo list: `#` comments, blanks and spaces dropped."""
    names = []
    with open(path, 'r', encoding='utf-8') as handle:
        for raw in handle:
            name = raw.split('#', 1)[0].strip()
            if name and name not in names:
                names.append(name)
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Scan the catalog for secrets, personal data and private names (CAT-24). '
        'Prints file, line and kind only (CAT-9).'
    )
    parser.add_argument('root', nargs='?', default='.', help='directory to scan (default: .)')
    parser.add_argument(
        '--private-repos',
        metavar='FILE',
        help='private repo list, one name per line (default: ' + DEFAULT_PRIVATE_LIST + ' if present)',
    )
    args = parser.parse_args(argv)

    list_path = args.private_repos
    if list_path is None:
        default = os.path.expanduser(DEFAULT_PRIVATE_LIST)
        if os.path.exists(default):
            list_path = default
    names = []
    if list_path is not None:
        try:
            names = read_private_list(list_path)
        except (OSError, UnicodeDecodeError):
            print('scrub: error: cannot read the private repo list', file=sys.stderr)
            return 2

    try:
        findings = scan_tree(args.root, names)
    except ScrubError as err:
        print('scrub: error: ' + str(err), file=sys.stderr)
        return 2

    for rel, line, kind in findings:
        print('%s:%d: %s' % (rel, line, kind))
    print('scrub: %d findings' % len(findings))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
