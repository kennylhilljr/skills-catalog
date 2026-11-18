"""Mac-only CLI checks for the freshness bot (FRS-3, FRS-4, FRS-15).

These checks need the installed `claude` and `codex` CLIs, so they run only
in the Mac launchd half (DESIGN 7.6). Every command runner is injected:
`run(argv, timeout) -> (returncode, stdout)`. `make_run` builds the real one
for the CLI entry point; importing this module has no side effects.

Findings use the part 1 shape:
{"kind", "skill", "subject", "file", "line", "evidence", "fix"}.
"""

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

TIMEOUT = 10
LAG_DAYS = 30
NPM_PACKAGES = {'claude': '@anthropic-ai/claude-code', 'codex': '@openai/codex'}
CLIS = ('claude', 'codex')

SHELL_TAGS = frozenset(('sh', 'bash', 'shell', 'zsh', 'console'))

# Shell builtins and keywords: never checked as commands (FRS-4).
BUILTINS = frozenset((
    '.', ':', '[', '[[', ']]', '{', '}', '(', ')', '!', 'alias', 'bg', 'bind',
    'break', 'builtin', 'case', 'cd', 'command', 'continue', 'declare', 'do',
    'done', 'echo', 'elif', 'else', 'esac', 'eval', 'exec', 'exit', 'export',
    'false', 'fg', 'fi', 'for', 'function', 'getopts', 'hash', 'if', 'in',
    'jobs', 'kill', 'let', 'local', 'popd', 'printf', 'pushd', 'pwd', 'read',
    'readonly', 'return', 'select', 'set', 'shift', 'source', 'test', 'then',
    'time', 'trap', 'true', 'type', 'typeset', 'ulimit', 'umask', 'unalias',
    'unset', 'until', 'wait', 'while',
))

# sudo options that take an argument (`sudo -u root git`, `sudo -Eu root git`).
_SUDO_ARG_SHORT = frozenset('ugCDhprtUTR')
_SUDO_ARG_OPTIONS = frozenset((
    '--user', '--group', '--close-from', '--chdir', '--host', '--prompt',
    '--role', '--type', '--other-user', '--command-timeout', '--chroot',
))
_SHELL_SCRIPT_EXTS = ('.sh', '.bash', '.zsh')

_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)')
_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_COMMAND_WORD = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]*$')
_HEREDOC = re.compile(r'<<-?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\1')
_VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)')
_BACKTICK = re.compile(r'`([^`\n]+)`')
_CAMEL = re.compile(r'^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)*$')
_ACRONYM = re.compile(r'^[A-Z]{2,}$')
_FLAG = re.compile(r'^(--[A-Za-z0-9][A-Za-z0-9-]*|-[A-Za-z])(?:=.*)?$')
_ALIASES = re.compile(r'\[aliases?:\s*([^\]]+)\]')
_SUBCOMMAND = re.compile(r'^[a-z][a-z0-9-]*$')
# A bare word in subcommand position: any case, underscores allowed, so
# `codex Teleport` and `codex teleport_now` are checked, not skipped.
_SUBCOMMAND_WORD = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
_SHELL_OPERATORS = ('&&', '||', ';', '|', '&')
_COMMANDS_HEADER = re.compile(r'^Commands:\s*$', re.MULTILINE)
_TOOL_WORD = re.compile(r'\btools?\b', re.IGNORECASE)
# CamelCase words with these endings are code names, not tools.
_NOT_TOOL_SUFFIXES = ('Error', 'Exception', 'Warning')


def _finding(kind, skill, subject, file, line, evidence, fix=None):
    return {'kind': kind, 'skill': skill, 'subject': subject, 'file': file,
            'line': line, 'evidence': evidence, 'fix': fix}


# ---------------------------------------------------------------------------
# Runners


def make_run(path=None, timeout=TIMEOUT):
    """Return the real runner: no shell, `~/.local/bin` first on PATH.

    `path` is iwb's PATH from the local config (DESIGN 7.3); None uses the
    current PATH. Only the CLI entry point (part 9) calls this.
    """
    local_bin = os.path.expanduser(os.path.join('~', '.local', 'bin'))
    parts = [p for p in (path if path is not None else os.environ.get('PATH', '')).split(os.pathsep) if p]
    parts = [local_bin] + [p for p in parts if p != local_bin]
    env = dict(os.environ)
    env['PATH'] = os.pathsep.join(parts)

    def run(argv, timeout=timeout):
        try:
            proc = subprocess.run(
                list(argv), shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, timeout=timeout, check=False)
        except FileNotFoundError:
            return 127, ''
        except PermissionError:
            return 126, ''
        except subprocess.TimeoutExpired:
            return 124, ''
        out = proc.stdout.decode('utf-8', 'replace') if proc.stdout else ''
        return proc.returncode, out

    return run


class CachedRun:
    """Wrap a runner so each argv runs once per job (FRS-4, FRS-3)."""

    def __init__(self, run):
        self._run = run
        self._results = {}

    def __call__(self, argv, timeout=TIMEOUT):
        key = tuple(argv)
        if key not in self._results:
            self._results[key] = self._run(list(argv), timeout)
        return self._results[key]


_CACHES = {}


def cached(run):
    """Return the job-wide cache for `run`, creating it on first use."""
    if isinstance(run, CachedRun):
        return run
    if run not in _CACHES:
        _CACHES[run] = CachedRun(run)
    return _CACHES[run]


def reset_cache():
    """Forget every cached result (start of a new job)."""
    _CACHES.clear()


# ---------------------------------------------------------------------------
# Skill reading


def _rel(folder, *parts):
    """Repo-relative path: the last two folder components plus `parts`."""
    norm = os.path.normpath(folder)
    head, name = os.path.split(norm)
    parent = os.path.basename(head)
    pieces = [p for p in (parent, name) if p] + list(parts)
    return '/'.join(pieces)


def _read(path):
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _frontmatter(lines):
    """Return (frontmatter lines with 1-based numbers, body start index)."""
    if not lines or lines[0].strip() != '---':
        return [], 0
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            return [(n + 2, lines[n + 1]) for n in range(i - 1)], i + 1
    return [], 0


def _unquote(value):
    value = value.strip()
    if '#' in value and not value.startswith(('"', "'")):
        value = value.split('#', 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        value = value[1:-1]
    return value


def _meta_value(fm, key):
    """Return (line, value) for a `metadata` sub-key, or (None, None)."""
    pat = re.compile(r'^\s+' + re.escape(key) + r':\s*(.*)$')
    for number, line in fm:
        m = pat.match(line)
        if m:
            return number, _unquote(m.group(1))
    return None, None


def _allowed_tools(fm):
    """Return [(line, tool name)] from top-level `allowed-tools`."""
    out = []
    in_list = False
    for number, line in fm:
        m = re.match(r'^allowed-tools:\s*(.*)$', line)
        if m:
            value = m.group(1).strip()
            in_list = value == ''
            if value.startswith('['):
                value = value.strip('[]')
            for item in re.split(r'[,\s]+(?![^(]*\))', _unquote(value)):
                if item:
                    out.append((number, item))
            continue
        if in_list:
            lm = re.match(r'^\s+-\s*(.+)$', line)
            if lm:
                out.append((number, _unquote(lm.group(1))))
            elif line.strip():
                in_list = False
    names = []
    for number, item in out:
        name = item.split('(', 1)[0].strip().strip('"\'')
        if name and not name.startswith('mcp__'):
            names.append((number, name))
    return names


def _markdown_files(folder):
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for name in sorted(filenames):
            if name.endswith('.md'):
                found.append(os.path.join(dirpath, name))
    skill_md = os.path.join(folder, 'SKILL.md')
    found.sort(key=lambda p: (p != skill_md, p))
    return found


def _fenced_blocks(lines, start=0):
    """Yield (tag, [(1-based line, text)]) for each fenced block."""
    i = start
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        marker, tag = m.group(1), m.group(2).lower()
        body = []
        i += 1
        while i < len(lines):
            close = re.match(r'^ {0,3}(`{3,}|~{3,})\s*$', lines[i])
            if close and close.group(1)[0] == marker[0] and len(close.group(1)) >= len(marker):
                break
            body.append((i + 1, lines[i]))
            i += 1
        yield tag, body
        i += 1


def _strip_wrappers(tokens):
    """Drop leading `VAR=value` words and `sudo` with its options."""
    tokens = list(tokens)
    while tokens and _ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    while tokens and tokens[0] == 'sudo':
        tokens.pop(0)
        while tokens and tokens[0].startswith('-'):
            opt = tokens.pop(0)
            if opt == '--':
                break
            if _sudo_option_takes_next(opt) and tokens:
                tokens.pop(0)
        while tokens and _ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
    return tokens


def _sudo_option_takes_next(opt):
    """True when sudo option `opt` takes the next word as its value.

    `--user root` does; `--user=root` does not. In a short-option group
    (`-Eu root`) the first letter that takes an argument uses the rest of
    the group (`-uroot`) or, when it is the last letter, the next word.
    """
    if opt.startswith('--'):
        return '=' not in opt and opt in _SUDO_ARG_OPTIONS
    letters = opt[1:]
    for i, ch in enumerate(letters):
        if ch in _SUDO_ARG_SHORT:
            return i == len(letters) - 1
    return False


def _tokens(text):
    """Split a shell line into words and operator tokens.

    Quotes are kept, so a quoted word (`"a b"`, `--prompt="Pass word: "`)
    stays one token. Unquoted `;`, `|`, `||`, `&&` and a lone `&` become
    their own tokens even when stuck to a word (`codex teleport;`); `&` in
    a redirection (`2>&1`, `&>f`) stays part of the word. A `$(...)`,
    `${...}` or backtick expansion stays inside its word, operators and
    spaces included. An unquoted `#` that starts a word ends the line. An
    unclosed quote falls back to splitting on whitespace.
    """
    tokens = []
    word = []
    quote = None
    nest = []  # open expansion closers: ')', '}' or '`'
    i = 0
    n = len(text)

    def flush():
        if word:
            tokens.append(''.join(word))
            del word[:]

    while i < n:
        ch = text[i]
        if quote:
            word.append(ch)
            if ch == '\\' and quote == '"' and i + 1 < n:
                word.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in '"\'':
            quote = ch
            word.append(ch)
        elif ch == '$' and text[i + 1:i + 2] in ('(', '{'):
            nest.append(')' if text[i + 1] == '(' else '}')
            word.append(text[i:i + 2])
            i += 2
            continue
        elif ch == '`':
            if nest and nest[-1] == '`':
                nest.pop()
            else:
                nest.append('`')
            word.append(ch)
        elif nest:
            if ch == '(' and nest[-1] == ')':
                nest.append(')')
            elif ch == nest[-1]:
                nest.pop()
            word.append(ch)
        elif ch == '#' and not word:
            break
        elif ch == '\\' and i + 1 < n:
            word.append(ch)
            word.append(text[i + 1])
            i += 1
        elif ch.isspace():
            flush()
        elif ch in ';|&':
            if ch == '&' and ((word and word[-1] in '<>') or text[i + 1:i + 2] == '>'):
                word.append(ch)
            else:
                op = text[i:i + 2] if text[i:i + 2] in ('&&', '||') else ch
                flush()
                tokens.append(op)
                i += len(op)
                continue
        else:
            word.append(ch)
        i += 1
    if quote or nest:
        return text.split()
    flush()
    return tokens


def _first_word(text):
    """Return the command word of one shell line, or None."""
    tokens = _strip_wrappers(_tokens(text))
    if not tokens:
        return None
    word = tokens[0].rstrip(';&|')
    if not word or word in BUILTINS or not _COMMAND_WORD.match(word):
        return None
    return word


def _shell_commands(body, tag):
    """Yield (line, word, text) for the command lines of one shell block.

    A line ending in a backslash is joined with the lines it continues, and
    the joined text is reported at its first line.
    """
    heredoc = None
    pending = None
    for number, raw in body:
        line = raw.rstrip()
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        if pending is not None:
            first, text = pending
            joined = text + ' ' + line.strip()
            if line.endswith('\\'):
                pending = (first, joined[:-1].rstrip())
                continue
            pending = None
            number, line = first, joined
        elif line.endswith('\\'):
            pending = (number, line[:-1].rstrip())
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if tag == 'console':
            if not stripped.startswith('$ '):
                continue
        if stripped.startswith('$ '):
            stripped = stripped[2:].strip()
        if stripped.startswith(('|', '&&', '||', '>', '<', ')', '}')):
            continue
        h = _HEREDOC.search(stripped)
        if h:
            heredoc = h.group(2)
        word = _first_word(stripped)
        if word:
            yield number, word, stripped


def commands_for(folder):
    """Return [(file, line, cmd)] a skill needs on PATH (FRS-4).

    `file` is repo-relative. Commands come from `metadata.catalog-requires`
    and the first word of each command line in sh, bash, shell, zsh and
    console fenced blocks of the skill's Markdown files.
    """
    out = []
    for path in _markdown_files(folder):
        rel = _rel(folder, os.path.relpath(path, folder).replace(os.sep, '/'))
        lines = _read(path).split('\n')
        start = 0
        if os.path.basename(path) == 'SKILL.md' and os.path.dirname(path) == os.path.normpath(folder):
            fm, start = _frontmatter(lines)
            number, value = _meta_value(fm, 'catalog-requires')
            if value:
                for name in value.split(','):
                    name = name.strip()
                    if name:
                        out.append((rel, number, name))
        for tag, body in _fenced_blocks(lines, start):
            if tag in SHELL_TAGS:
                for number, word, _ in _shell_commands(body, tag):
                    out.append((rel, number, word))
    return out


def command_ok(cmd, run):
    """Return (ok, evidence) for one command, cached per job."""
    run = cached(run)
    rc, _ = run([cmd, '--version'], TIMEOUT)
    if rc == 0:
        return True, ''
    rc2, _ = run([cmd, '--help'], TIMEOUT)
    if rc2 == 0:
        return True, ''
    if rc == 127 and rc2 == 127:
        return False, '`%s` was not found on PATH' % cmd
    return False, '`%s --version` exited %d and `%s --help` exited %d' % (cmd, rc, cmd, rc2)


def check_commands(skill, folder, run):
    """Return `missing-command` findings, one per command per skill (FRS-4)."""
    findings = []
    seen = set()
    for file, line, cmd in commands_for(folder):
        if cmd in seen:
            continue
        seen.add(cmd)
        ok, evidence = command_ok(cmd, run)
        if not ok:
            findings.append(_finding(
                'missing-command', skill, '%s:%s' % (skill, cmd), file, line,
                evidence + ' (named in %s line %s)' % (file, line)))
    return findings


# ---------------------------------------------------------------------------
# FRS-3: tool names and CLI flags


def _help(cli, sub, run, nested=None):
    argv = [cli] + [w for w in (sub, nested) if w] + ['--help']
    rc, out = cached(run)(argv, TIMEOUT)
    return out if rc == 0 else None


def _commands(text):
    """Return the command names listed under `Commands:`, or None."""
    entries = []
    aliases = set()
    found = False
    in_section = False
    for line in text.split('\n'):
        if _COMMANDS_HEADER.match(line):
            found = in_section = True
            continue
        if in_section:
            if line.strip() and not line[:1].isspace():
                in_section = False
                continue
            for group in _ALIASES.findall(line):
                aliases.update(a.strip() for a in group.split(',') if a.strip())
            m = re.match(r'^(\s+)([a-z][a-z0-9-]*)(?:[,|]\s*([a-z][a-z0-9-]*))?', line)
            if m:
                entries.append((len(m.group(1)), m.group(2), m.group(3)))
    if not found:
        return None
    # Command names sit at the shallowest indent; wrapped descriptions
    # are indented further.
    indent = min((e[0] for e in entries), default=0)
    names = {n for depth, name, alias in entries if depth == indent for n in (name, alias) if n}
    return names | aliases


def _has_command(word, text):
    """True when `word` is a listed command (or, with no list, named)."""
    listed = _commands(text)
    return word in listed if listed is not None else _in_help(word, text)


def _in_help(token, text):
    return re.search(r'(?<![A-Za-z0-9-])' + re.escape(token) + r'(?![A-Za-z0-9-])', text) is not None


def _cli_spans(lines, start):
    """Yield (line, text) for backtick spans and shell command lines."""
    shell_lines = set()
    for tag, body in _fenced_blocks(lines, start):
        if tag in SHELL_TAGS:
            for number, _, text in _shell_commands(body, tag):
                shell_lines.add(number)
                yield number, text
    in_fence = None
    for index in range(start, len(lines)):
        line = lines[index]
        m = _FENCE.match(line)
        if m:
            in_fence = None if in_fence else m.group(1)[0]
            continue
        if in_fence:
            continue
        for span in _BACKTICK.findall(line):
            yield index + 1, span


def _cli_refs(text):
    """Yield (cli, [args]) for each `claude`/`codex` reference in `text`.

    Quoted words (prompts) stay single tokens with their quotes, so they are
    never taken for subcommands or flags. Arguments stop at a shell operator
    or at `--`, after which words belong to another program.
    """
    tokens = _strip_wrappers(_tokens(text))
    for i, tok in enumerate(tokens):
        if tok not in CLIS:
            continue
        if i > 0 and not re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tokens[i - 1]) \
                and tokens[i - 1] not in _SHELL_OPERATORS + ('sudo', '$', 'run', 'exec'):
            continue
        rest = []
        for t in tokens[i + 1:]:
            if t in _SHELL_OPERATORS or t == '--' or t in CLIS:
                break
            rest.append(t)
        yield tok, rest


def _takes_value(flag, text):
    """True when help `text` shows `flag` with a value (`--model <m>`)."""
    pat = (r'(?<![A-Za-z0-9-])' + re.escape(flag)
           + r'(?:,\s*-{1,2}[A-Za-z0-9][A-Za-z0-9-]*)?(?:[ =]+|\s*,?\s*)[<\[]')
    return re.search(pat, text) is not None


def _split_ref(rest, top):
    """Return (sub, nested, [flags]) for the arguments of one reference.

    Global options may come before the subcommand (`codex --profile p exec`).
    A word after an option that `top` shows with a value (`--profile <P>`)
    is that option's value; the first other word is the subcommand, known
    or not.
    """
    flags = []
    for t in rest:
        fm = _FLAG.match(t)
        if fm:
            flags.append(fm.group(1))
    sub_idx = None
    after_option = False
    for idx, t in enumerate(rest):
        if t.startswith('-'):
            fm = _FLAG.match(t)
            after_option = bool(fm) and '=' not in t and _takes_value(fm.group(1), top)
            continue
        if after_option:
            after_option = False
            continue
        if _SUBCOMMAND_WORD.match(t):
            sub_idx = idx
        break
    if sub_idx is None:
        return None, None, flags
    sub = rest[sub_idx]
    nested = None
    if sub_idx + 1 < len(rest) and _SUBCOMMAND.match(rest[sub_idx + 1]):
        nested = rest[sub_idx + 1]
    return sub, nested, flags


def _tool_candidates(lines, start):
    """Yield (line, name) for backticked CamelCase tool-shaped words.

    Multi-word CamelCase (`WebFetch`) always counts. A single capitalized
    word (`Read`) or an all-caps name (`LSP`) counts only on a line that
    mentions a tool, since prose uses such words for other things. Names ending in Error, Exception or
    Warning are code names and are skipped.
    """
    in_fence = None
    for index in range(start, len(lines)):
        line = lines[index]
        m = _FENCE.match(line)
        if m:
            in_fence = None if in_fence else m.group(1)[0]
            continue
        if in_fence:
            continue
        for span in _BACKTICK.findall(line):
            word = span.strip()
            if _ACRONYM.match(word):
                if _TOOL_WORD.search(line):
                    yield index + 1, word
                continue
            if not _CAMEL.match(word) or word.endswith(_NOT_TOOL_SUFFIXES):
                continue
            humps = len(re.findall(r'[A-Z]', word))
            if humps >= 2 or _TOOL_WORD.search(line):
                yield index + 1, word


def check_cli_refs(skill, folder, tools, run):
    """Return `unknown-tool` and `unknown-flag` findings (FRS-3).

    `tools` is the current tools reference list (part 3). CLI references are
    `claude <sub>` or `codex <sub>` with `--flag` tokens in one backtick span
    or shell command line; each is checked against `<cli> --help` and
    `<cli> <sub> --help`, cached per job. A CLI whose `--help` fails is
    skipped here; `check_commands` reports it.
    """
    known = set(tools)
    findings = []
    reported = set()
    for path in _markdown_files(folder):
        rel = _rel(folder, os.path.relpath(path, folder).replace(os.sep, '/'))
        lines = _read(path).split('\n')
        start = 0
        candidates = []
        if os.path.basename(path) == 'SKILL.md' and os.path.dirname(path) == os.path.normpath(folder):
            fm, start = _frontmatter(lines)
            candidates.extend(_allowed_tools(fm))
        candidates.extend(_tool_candidates(lines, start))
        for line, name in candidates:
            key = ('unknown-tool', name)
            if name in known or key in reported:
                continue
            reported.add(key)
            findings.append(_finding(
                'unknown-tool', skill, '%s:%s' % (skill, name), rel, line,
                '`%s` is not in the current Claude Code tools reference' % name))
        for line, text in _cli_spans(lines, start):
            _check_cli_ref(skill, rel, line, text, run, reported, findings)
    for path in _shell_scripts(folder):
        rel = _rel(folder, os.path.relpath(path, folder).replace(os.sep, '/'))
        body = list(enumerate(_read(path).split('\n'), 1))
        for line, _, text in _shell_commands(body, 'sh'):
            _check_cli_ref(skill, rel, line, text, run, reported, findings)
    return findings


def _shell_scripts(folder):
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for name in sorted(filenames):
            if name.endswith(_SHELL_SCRIPT_EXTS):
                found.append(os.path.join(dirpath, name))
    return found


def _check_cli_ref(skill, rel, line, text, run, reported, findings):
    """Append `unknown-flag` findings for one span or command line."""
    for cli, rest in _cli_refs(text):
        top = _help(cli, None, run)
        if top is None:
            continue
        sub, nested, flags = _split_ref(rest, top)
        sub_help = None
        if sub is not None:
            if not _has_command(sub, top):
                key = ('unknown-flag', cli, sub, None)
                if key not in reported:
                    reported.add(key)
                    findings.append(_finding(
                        'unknown-flag', skill, '%s:%s %s' % (skill, cli, sub), rel, line,
                        '`%s %s` is not a subcommand in `%s --help`' % (cli, sub, cli)))
                continue
            sub_help = _help(cli, sub, run)
            if nested is not None and sub_help is not None:
                if _has_command(nested, sub_help):
                    # `claude mcp add --scope`: the flag lives on
                    # the nested subcommand's help.
                    sub_help = sub_help + '\n' + (_help(cli, sub, run, nested) or '')
                elif _COMMANDS_HEADER.search(sub_help):
                    # The subcommand lists its own commands, so a
                    # word it does not list is an unknown command,
                    # not a positional argument.
                    key = ('unknown-flag', cli, sub, nested)
                    if key not in reported:
                        reported.add(key)
                        label = '%s %s %s' % (cli, sub, nested)
                        findings.append(_finding(
                            'unknown-flag', skill, '%s:%s' % (skill, label), rel, line,
                            '`%s` is not a subcommand in `%s %s --help`' % (label, cli, sub)))
                    continue
        for flag in flags:
            if _in_help(flag, top) or (sub_help is not None and _in_help(flag, sub_help)):
                continue
            key = ('unknown-flag', cli, sub, flag)
            if key in reported:
                continue
            reported.add(key)
            where = '`%s --help`' % cli if sub is None else '`%s --help` or `%s %s --help`' % (cli, cli, sub)
            label = cli if sub is None else '%s %s' % (cli, sub)
            findings.append(_finding(
                'unknown-flag', skill, '%s:%s %s' % (skill, label, flag), rel, line,
                '`%s` for `%s` is not in %s' % (flag, label, where)))


# ---------------------------------------------------------------------------
# FRS-15: host CLI lag


def _version(text):
    m = _VERSION.search(text or '')
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _vstr(v):
    return '%d.%d.%d' % v if v else None


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(now):
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def check_cli_lag(run, fetch_npm, now, state=None):
    """Record host CLI versions and return `cli-lag` findings (FRS-15).

    `fetch_npm(package) -> {"latest": str, "time": {version: iso}}`.
    Sets `state["cli"] = {"claude": {"installed", "latest", "checked_at"},
    "codex": {...}}` when `state` is given. A finding is raised when the
    installed minor is one or more behind the latest (or the major is
    behind), or the latest release is more than 30 days newer than the
    installed one's release.
    """
    run = cached(run)
    cli_state = {}
    findings = []
    for cli in CLIS:
        rc, out = run([cli, '--version'], TIMEOUT)
        installed = _version(out) if rc == 0 else None
        latest = None
        times = {}
        try:
            info = fetch_npm(NPM_PACKAGES[cli]) or {}
            latest = _version(info.get('latest') or '')
            times = info.get('time') or {}
        except Exception:  # a failed fetch is reported by the upstream watch
            latest = None
        cli_state[cli] = {'installed': _vstr(installed), 'latest': _vstr(latest),
                          'checked_at': _iso(now)}
        if installed is None or latest is None or installed >= latest:
            continue
        reasons = []
        if latest[0] > installed[0] or (latest[0] == installed[0] and latest[1] - installed[1] >= 1):
            reasons.append('at least one minor version behind')
        t_inst = _parse_time(times.get(_vstr(installed)))
        t_latest = _parse_time(times.get(_vstr(latest)))
        if t_inst and t_latest and t_latest - t_inst > timedelta(days=LAG_DAYS):
            reasons.append('latest release is %d days newer' % (t_latest - t_inst).days)
        if reasons:
            findings.append(_finding(
                'cli-lag', 'catalog', 'cli:%s' % cli, None, None,
                '%s installed %s, latest %s: %s' % (cli, _vstr(installed), _vstr(latest), '; '.join(reasons))))
    if state is not None:
        state['cli'] = cli_state
    return findings
