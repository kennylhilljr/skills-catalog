"""Upstream watch for the freshness bot (SPEC FRS-10, FRS-11, FRS-14; DESIGN 7.1).

Each source in `schema/upstream.json` is fetched, reduced to a deterministic
text extract by `normalize`, and hashed. `watch` compares the hash with the
one stored in `state.sources` (kept on the `freshness-state` branch, never on
`main`, INV-5) and returns `upstream-change` findings with the diff of the
extracts. A source that fails to fetch 3 runs in a row gives a
`source-failing` alert on every run until it recovers: silence is a fault,
not "no change".

Importing this module has no side effects. Fetching is injected:
`fetch(url) -> (status, text)`; the real fetcher lives in the runner
(part 9). Standard library only.
"""

import copy
import datetime
import difflib
import hashlib
import importlib.util
import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_PATH = os.path.join('schema', 'upstream.json')

EXTRACT_KINDS = frozenset({
    'markdown-sections',
    'npm-version',
    'github-releases',
    'pypi-version',
    'table-column',
})

# Consecutive failed runs before a source raises a `source-failing` alert.
FAILURE_ALERT_THRESHOLD = 3
# Errors kept per source, for the alert evidence.
ERRORS_KEPT = 3
# Evidence diffs are cut to this many lines.
DIFF_MAX_LINES = 200
# Default number of releases kept by `github-releases`.
RELEASES_DEFAULT_LIMIT = 10
# Longest error message stored in state.
ERROR_MAX_CHARS = 200

TOOLS_SOURCE = 'claude-code-tools'
FIELDS_SOURCE = 'claude-code-skills-frontmatter'

_ID = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')


class ExtractError(ValueError):
    """The fetched page does not hold what the extract kind looks for."""


# ---------------------------------------------------------------------------
# Loading the source list


def load_sources(path=DEFAULT_PATH):
    """Read and validate `schema/upstream.json`; return the list of sources.

    Raises ValueError on a bad version, a duplicate or malformed id, a
    non-https url, an unknown extract kind or missing kind parameters.
    """
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    return validate_sources(data)


def validate_sources(data):
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('upstream.json: version must be 1')
    sources = data.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('upstream.json: sources must be a non-empty list')
    seen = set()
    for index, source in enumerate(sources):
        where = 'upstream.json: sources[%d]' % index
        if not isinstance(source, dict):
            raise ValueError(where + ' is not an object')
        unknown = set(source) - {'id', 'url', 'extract', 'params'}
        if unknown:
            raise ValueError('%s: unknown keys %s' % (where, sorted(unknown)))
        sid = source.get('id')
        if not isinstance(sid, str) or not _ID.match(sid):
            raise ValueError(where + ': id must be lowercase words joined by hyphens')
        if sid in seen:
            raise ValueError('%s: duplicate id %s' % (where, sid))
        seen.add(sid)
        url = source.get('url')
        if not isinstance(url, str) or not url.startswith('https://'):
            raise ValueError('%s (%s): url must be https' % (where, sid))
        kind = source.get('extract')
        if kind not in EXTRACT_KINDS:
            raise ValueError('%s (%s): unknown extract kind %r' % (where, sid, kind))
        params = source.get('params', {})
        if not isinstance(params, dict):
            raise ValueError('%s (%s): params must be an object' % (where, sid))
        if kind == 'markdown-sections':
            headings = params.get('headings')
            if (not isinstance(headings, list) or not headings
                    or not all(isinstance(h, str) and h.strip() for h in headings)):
                raise ValueError('%s (%s): markdown-sections needs params.headings' % (where, sid))
        if kind == 'table-column':
            column = params.get('column')
            columns = column if isinstance(column, list) else [column]
            if (not isinstance(params.get('heading'), str) or not columns
                    or not all(isinstance(c, str) and c.strip() for c in columns)):
                raise ValueError('%s (%s): table-column needs params.heading and params.column' % (where, sid))
        if kind == 'github-releases':
            limit = params.get('limit', RELEASES_DEFAULT_LIMIT)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError('%s (%s): params.limit must be a positive integer' % (where, sid))
    return sources


# ---------------------------------------------------------------------------
# Normalizing


_BLOCK_TAGS = frozenset({
    'p', 'div', 'section', 'article', 'main', 'ul', 'ol', 'li', 'br', 'pre',
    'blockquote', 'table', 'thead', 'tbody', 'dl', 'dt', 'dd', 'hr', 'tr',
})
_SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'svg', 'template', 'footer', 'nav', 'head'})
_VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
    'source', 'track', 'wbr',
})


class _TextExtractor(HTMLParser):
    """HTML to plain text. Headings become `#` lines and table rows `| a | b |`."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.pre = 0
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_TAGS:
            if tag in ('br', 'hr') and not self.skip:
                self.parts.append('\n')
            return
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == 'pre':
            self.pre += 1
        if len(tag) == 2 and tag[0] == 'h' and tag[1] in '123456':
            self.parts.append('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag == 'tr':
            self.row = []
        elif tag in ('td', 'th') and self.row is not None:
            self.row.append([])
        elif tag in _BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == 'pre' and self.pre:
            self.pre -= 1
        if len(tag) == 2 and tag[0] == 'h' and tag[1] in '123456':
            self.parts.append('\n\n')
        elif tag == 'tr' and self.row is not None:
            cells = [' '.join(''.join(c).split()) for c in self.row]
            self.parts.append('\n| ' + ' | '.join(cells) + ' |')
            self.row = None
        elif tag in _BLOCK_TAGS and tag != 'tr':
            self.parts.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        if self.row is not None:
            if self.row:
                self.row[-1].append(data)
            return
        # Source line breaks inside text are layout, not content, except in <pre>.
        self.parts.append(data if self.pre else re.sub(r'\s+', ' ', data))

    def text(self):
        return ''.join(self.parts)


def _looks_like_html(raw):
    head = raw.lstrip()[:500].lower()
    return head.startswith('<!doctype html') or head.startswith('<html') or '<body' in head


def html_to_text(raw):
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return parser.text()


_TRACKING_PARAM = re.compile(r'^(?:utm_[a-z_]+|fbclid|gclid|msclkid|mc_cid|mc_eid|_ga|_gl|ref_src)$', re.IGNORECASE)
_URL = re.compile(r'https?://[^\s)\]>"\'`]+')
_VOLATILE_LINE = re.compile(
    r'^\W*(?:'
    r'last\s+(?:updated|modified|edited|reviewed|changed)\b'
    r'|(?:page\s+)?(?:updated|modified|edited)\s+(?:on|at)\b'
    r'|edit\s+this\s+page\b'
    r'|edit\s+on\s+github\b'
    r'|was\s+this\s+page\s+helpful\b'
    r'|(?:©|\(c\)|copyright)\s*(?:©\s*)?\d{4}'
    r')',
    re.IGNORECASE,
)
_EDIT_LINK = re.compile(r'\[[^\]]*\bedit\s+(?:this\s+page|on\s+github)\b[^\]]*\]\([^)]*\)', re.IGNORECASE)


def _strip_tracking(match):
    url = match.group(0)
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not _TRACKING_PARAM.match(k)]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def clean_text(raw):
    """Collapse whitespace, drop volatile lines, edit links and tracking params."""
    if _looks_like_html(raw):
        raw = html_to_text(raw)
    raw = raw.replace('\r\n', '\n').replace('\r', '\n').replace(' ', ' ')
    out = []
    blank = False
    for line in raw.split('\n'):
        line = ' '.join(line.split())
        line = _EDIT_LINK.sub('', line).strip()
        line = _URL.sub(_strip_tracking, line)
        if _VOLATILE_LINE.match(line):
            continue
        if not line:
            if out and not blank:
                out.append('')
            blank = True
            continue
        blank = False
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return '\n'.join(out)


_HEADING = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
_FENCE = re.compile(r'^(`{3,}|~{3,})')
_MD_LINK = re.compile(r'\[([^\]]*)\]\([^)]*\)')


def _plain(text):
    """Heading or cell text without markdown links, code marks or emphasis."""
    text = _MD_LINK.sub(r'\1', text)
    text = text.replace('`', '').replace('**', '').replace('__', '')
    return ' '.join(text.split())


def _headings(lines):
    """(index, level, plain text) for each heading line outside code fences.

    A fence closes only on a bare marker of the same character that is at
    least as long as the opening one (CommonMark).
    """
    found = []
    fence = None
    for index, line in enumerate(lines):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
                continue
        else:
            if (m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)
                    and not line[m.end():].strip()):
                fence = None
            continue
        h = _HEADING.match(line)
        if h:
            found.append((index, len(h.group(1)), _plain(h.group(2)).lower()))
    return found


def _section(lines, heads, wanted):
    """Lines of the first section whose heading matches `wanted`, or None.

    `wanted` is heading text compared without markdown and case; a leading
    `#` run pins the level (`## Create a marketplace`). The section runs to
    the next heading of the same or a higher level.
    """
    level = None
    m = re.match(r'^(#{1,6})\s+(.*)$', wanted.strip())
    if m:
        level, wanted = len(m.group(1)), m.group(2)
    wanted = _plain(wanted).lower()
    for pos, (index, lvl, text) in enumerate(heads):
        if text != wanted or (level is not None and lvl != level):
            continue
        end = len(lines)
        for index2, lvl2, _ in heads[pos + 1:]:
            if lvl2 <= lvl:
                end = index2
                break
        return lines[index:end]
    return None


def _markdown_sections(text, params):
    lines = text.split('\n')
    heads = _headings(lines)
    out = []
    found = 0
    for wanted in params['headings']:
        section = _section(lines, heads, wanted)
        if out:
            out.append('')
        if section is None:
            out.append('[missing section: %s]' % _plain(wanted.lstrip('#').strip()))
            continue
        found += 1
        while section and not section[-1]:
            section = section[:-1]
        out.extend(section)
    if not found:
        raise ExtractError('none of the headings found: %s' % ', '.join(params['headings']))
    return '\n'.join(out)


def _split_row(line):
    line = line.strip()
    if line.startswith('|'):
        line = line[1:]
    if line.endswith('|') and not line.endswith('\\|'):
        line = line[:-1]
    cells = re.split(r'(?<!\\)\|', line)
    return [c.strip().replace('\\|', '|') for c in cells]


_SEPARATOR_CELL = re.compile(r'^:?-+:?$')


def _tables(lines):
    """Yield (header cells, body rows) for each pipe table in `lines`."""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith('|') and i + 1 < len(lines):
            header = _split_row(line)
            sep = _split_row(lines[i + 1])
            is_md = lines[i + 1].lstrip().startswith('|') and all(_SEPARATOR_CELL.match(c) for c in sep if c)
            raw_rows = []
            j = i + (2 if is_md else 1)
            while j < len(lines):
                nxt = lines[j]
                if nxt.lstrip().startswith('|'):
                    raw_rows.append(nxt.strip())
                elif raw_rows and nxt.strip() and not _HEADING.match(nxt) and not _FENCE.match(nxt):
                    # A row wrapped onto the next line: rejoin the physical
                    # lines before splitting, so later cells stay in place.
                    raw_rows[-1] = raw_rows[-1] + ' ' + nxt.strip()
                else:
                    break
                j += 1
            yield header, [_split_row(r) for r in raw_rows]
            i = j
            continue
        i += 1


def _table_column(text, params):
    lines = text.split('\n')
    heads = _headings(lines)
    section = _section(lines, heads, params['heading'])
    if section is None:
        raise ExtractError('heading not found: %s' % params['heading'])
    column = params['column']
    columns = column if isinstance(column, list) else [column]
    wanted = [_plain(c).lower() for c in columns]
    for header, rows in _tables(section):
        names = [_plain(h).lower() for h in header]
        if not all(w in names for w in wanted):
            continue
        idx = [names.index(w) for w in wanted]
        out = []
        for row in rows:
            cells = [_plain(row[k]) if k < len(row) else '' for k in idx]
            if any(cells):
                out.append(' | '.join(cells))
        return '\n'.join(out)
    raise ExtractError('no table with column(s) %s under %s' % (', '.join(columns), params['heading']))


def _npm_version(text, params):
    data = _json(text)
    version = data.get('version')
    if not isinstance(version, str):
        version = (data.get('dist-tags') or {}).get('latest')
    if not isinstance(version, str) or not version:
        raise ExtractError('no version in npm response')
    return 'latest: %s' % version


_RELEASE_TERMS = re.compile(
    r'(?i)\b(?:skills?|plugins?|tools?|slash[ -]commands?|flags?)\b'
    r'|(?<![\w-])--[a-z][a-z0-9-]+'
)


def _github_releases(text, params):
    data = _json(text)
    # `/releases/latest` returns one release object, not a list.
    if isinstance(data, dict) and 'tag_name' in data:
        data = [data]
    if not isinstance(data, list):
        raise ExtractError('GitHub releases response is not a list')
    include_pre = params.get('include_prereleases', True)
    releases = []
    for rel in data:
        if not isinstance(rel, dict) or rel.get('draft'):
            continue
        if rel.get('prerelease') and not include_pre:
            continue
        tag = rel.get('tag_name')
        if not isinstance(tag, str) or not tag:
            continue
        date = (rel.get('published_at') or rel.get('created_at') or '')[:10]
        releases.append((date, tag, bool(rel.get('prerelease')), rel.get('body') or ''))
    releases.sort(key=lambda r: (r[0], r[1]), reverse=True)
    out = []
    for date, tag, pre, body in releases[:params.get('limit', RELEASES_DEFAULT_LIMIT)]:
        if out:
            out.append('')
        out.append('%s %s%s' % (tag, date or 'undated', ' (pre-release)' if pre else ''))
        for line in body.replace('\r\n', '\n').split('\n'):
            line = ' '.join(line.split())
            if line and _RELEASE_TERMS.search(line):
                out.append('  ' + line)
    if not out:
        raise ExtractError('no releases')
    return '\n'.join(out)


_PRE_RELEASE = re.compile(r'(?i)(?:a|alpha|b|beta|c|rc|pre|preview|dev)\d*')


def _version_key(version):
    key = []
    for part in re.findall(r'\d+|[A-Za-z]+', version):
        if part.isdigit():
            key.append((2, int(part), ''))
        else:
            key.append((0, 0, part.lower()))
    # The end sorts between a letter part and a number part, so
    # 4.2.0rc1 < 4.2.0 < 4.2.0.1.
    key.append((1, 0, ''))
    return key


def _is_pre_release(version):
    tail = re.sub(r'^[vV]?\d+(?:\.\d+)*', '', version)
    tail = re.sub(r'\+.*$', '', tail).lstrip('.-_')
    return bool(tail) and bool(_PRE_RELEASE.match(tail))


def _pypi_version(text, params):
    data = _json(text)
    releases = data.get('releases')
    if not isinstance(releases, dict) or not releases:
        raise ExtractError('no releases in PyPI response')
    out = []
    latest = (data.get('info') or {}).get('version')
    if isinstance(latest, str) and latest:
        out.append('latest: %s' % latest)
    for version in sorted(releases, key=_version_key, reverse=True):
        files = releases[version]
        marks = []
        if _is_pre_release(version):
            marks.append('pre-release')
        if isinstance(files, list) and files and all(isinstance(f, dict) and f.get('yanked') for f in files):
            marks.append('yanked')
        out.append(version + (' (%s)' % ', '.join(marks) if marks else ''))
    return '\n'.join(out)


def _json(text):
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ExtractError('response is not JSON: %s' % exc.__class__.__name__)


_EXTRACTORS = {
    'markdown-sections': _markdown_sections,
    'table-column': _table_column,
    'npm-version': _npm_version,
    'github-releases': _github_releases,
    'pypi-version': _pypi_version,
}


def normalize(source, raw):
    """Deterministic text extract of `raw` for the source's extract kind.

    HTML is reduced to text with html.parser, whitespace is collapsed, and
    volatile parts (footer dates, edit links, tracking query strings) are
    dropped. Raises ExtractError when the page lacks what the kind needs.
    """
    kind = source['extract']
    params = source.get('params') or {}
    if kind in ('markdown-sections', 'table-column'):
        raw = clean_text(raw)
    return _EXTRACTORS[kind](raw, params)


# ---------------------------------------------------------------------------
# Watching


def _sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _iso(now):
    if isinstance(now, datetime.datetime):
        if now.tzinfo is not None:
            now = now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return now.replace(microsecond=0).isoformat() + 'Z'
    return str(now)


_scrub_module = None


def _scrub():
    """tools/scrub.py, loaded by path on first use (no import side effects)."""
    global _scrub_module
    if _scrub_module is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'scrub.py')
        spec = importlib.util.spec_from_file_location('_freshness_scrub', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _scrub_module = module
    return _scrub_module


# At least two segments, none longer than 16 characters: `gpt-4o-2024-08-06`,
# `2026-04-22-legacy-models`. A single long run is not a slug.
_SLUG = re.compile(r'[a-z0-9]{1,16}(?:[.-][a-z0-9]{1,16})+')


def _only_slug_entropy(line):
    """True when every high-entropy run on `line` is a lowercase slug.

    Dated model IDs (`name-2025-03-11`) and URL anchors trip the entropy
    rule; a token or key payload has capitals, `_`, `+` or `=` and does not
    match.
    """
    scrub = _scrub()
    runs = [m.group(0) for m in scrub._ENTROPY_RUN.finditer(line) if scrub._is_high_entropy(m.group(0))]
    return bool(runs) and all(_SLUG.fullmatch(r) for r in runs)


def scrub_lines(text, allow_slugs=False):
    """`text` with every line that `tools/scrub.scan_text` flags withheld.

    With `allow_slugs`, a line whose only finding is `high-entropy` is kept
    when every high-entropy run on it is a lowercase slug (see
    `_only_slug_entropy`). Every other finding withholds the line.
    """
    lines = text.split('\n')

    def flagged(scan):
        found = {}
        for number, kind in scan:
            found.setdefault(number, set()).add(kind)
        if allow_slugs:
            for number in list(found):
                if found[number] == {'high-entropy'} and _only_slug_entropy(lines[number - 1]):
                    del found[number]
        return found

    found = flagged(_scrub().scan_text(text))
    if not found:
        return text
    for number, kinds in found.items():
        lines[number - 1] = '[line withheld by scrub: %s]' % ', '.join(sorted(kinds))
    out = '\n'.join(lines)
    # A withheld line can still change how a fence pairs up; rescan once and
    # withhold everything rather than publish a flagged line.
    if flagged(_scrub().scan_text(out)):
        return '[text withheld by scrub]'
    return out


def _diff(source_id, old, new):
    lines = list(difflib.unified_diff(
        old.split('\n'), new.split('\n'),
        fromfile='%s (stored)' % source_id, tofile='%s (fetched)' % source_id,
        lineterm='', n=2,
    ))
    if len(lines) > DIFF_MAX_LINES:
        rest = len(lines) - (DIFF_MAX_LINES - 1)
        lines = lines[:DIFF_MAX_LINES - 1] + ['[diff cut: %d more lines]' % rest]
    return scrub_lines('\n'.join(lines))


def _error_text(exc):
    msg = ' '.join(str(exc).split())[:ERROR_MAX_CHARS]
    text = '%s: %s' % (exc.__class__.__name__, msg) if msg else exc.__class__.__name__
    if _scrub().scan_text(text):
        return exc.__class__.__name__
    return text


def _upstream_finding(source_id, sha, evidence):
    return {
        'kind': 'upstream-change',
        'skill': source_id,
        'subject': '%s:%s' % (source_id, sha),
        'file': None,
        'line': None,
        'evidence': evidence,
        'fix': None,
    }


def watch(sources, state_sources, fetch, now, run_id):
    """Fetch every source and compare it with its stored extract.

    Extracts are scrubbed with `tools/scrub.scan_text` before they are hashed
    and stored, since `state.sources` is published on `freshness-state`
    (lowercase slugs such as dated model IDs are kept, see `scrub_lines`).
    Evidence gets the full scrub.

    `state_sources` maps a source id to
    `{"sha256", "extract", "fetched_at", "failures", "last_error", "errors"}`
    (`errors` keeps the last 3 `{"run_id", "at", "error"}`). It is not
    modified; the updated copy is returned. Returns
    `(findings, alerts, state_sources)`.

    - First successful fetch: stores the baseline, no finding.
    - Changed sha256: one `upstream-change` finding with the diff.
    - A fetch error, a status of 400 or more, or a page the extract kind
      cannot read: `failures` goes up and the stored extract is kept. At 3
      consecutive failures, a `source-failing` alert, every run until a
      success resets the count (FRS-14).

    Each URL is fetched once per run, even when several sources share it.
    """
    state = copy.deepcopy(state_sources) if state_sources else {}
    at = _iso(now)
    findings = []
    alerts = []
    pages = {}
    for source in sources:
        sid = source['id']
        entry = state.get(sid)
        if not isinstance(entry, dict):
            entry = {}
        entry.setdefault('sha256', None)
        entry.setdefault('extract', None)
        entry.setdefault('fetched_at', None)
        entry.setdefault('failures', 0)
        entry.setdefault('last_error', None)
        entry.setdefault('errors', [])
        state[sid] = entry

        url = source['url']
        if url not in pages:
            try:
                status, text = fetch(url)
                if status is None or int(status) >= 400:
                    pages[url] = (None, 'HTTP %s' % status)
                elif not isinstance(text, str):
                    pages[url] = (None, 'empty response')
                else:
                    pages[url] = (text, None)
            except Exception as exc:  # any fetch failure counts (FRS-14)
                pages[url] = (None, _error_text(exc))
        text, error = pages[url]

        extract = None
        if error is None:
            try:
                # The extract is stored on the public freshness-state branch,
                # so it is scrubbed before it is hashed or stored.
                extract = scrub_lines(normalize(source, text), allow_slugs=True)
            except ExtractError as exc:
                error = _error_text(exc)
            except Exception as exc:  # a parser fault counts as a failure too
                error = _error_text(exc)

        if error is not None:
            entry['failures'] = int(entry.get('failures') or 0) + 1
            entry['last_error'] = error
            entry['errors'] = (list(entry.get('errors') or []) + [
                {'run_id': str(run_id), 'at': at, 'error': error},
            ])[-ERRORS_KEPT:]
            if entry['failures'] >= FAILURE_ALERT_THRESHOLD:
                alerts.append(_failing_alert(sid, entry))
            continue

        sha = _sha256(extract)
        old_sha, old_extract = entry.get('sha256'), entry.get('extract')
        if old_sha and sha != old_sha:
            findings.append(_upstream_finding(sid, sha, _diff(sid, old_extract or '', extract)))
        entry['sha256'] = sha
        entry['extract'] = extract
        entry['fetched_at'] = at
        entry['failures'] = 0
        entry['last_error'] = None
        entry['errors'] = []
    return findings, alerts, state


def _failing_alert(source_id, entry):
    lines = ['Source %s failed %d consecutive runs. Last errors:' % (source_id, entry['failures'])]
    for err in entry['errors'][-ERRORS_KEPT:]:
        lines.append('- run %s at %s: %s' % (err['run_id'], err['at'], err['error']))
    if entry.get('fetched_at'):
        lines.append('Last successful fetch: %s. The stored extract is kept.' % entry['fetched_at'])
    else:
        lines.append('No successful fetch yet.')
    return {
        'kind': 'source-failing',
        'subject': source_id,
        'evidence': scrub_lines('\n'.join(lines)),
    }


# ---------------------------------------------------------------------------
# Parsed lists for other parts


_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')


def _extract(state_sources, source_id):
    entry = (state_sources or {}).get(source_id)
    if not isinstance(entry, dict):
        return ''
    return entry.get('extract') or ''


def tool_names(state_sources, source_id=TOOLS_SOURCE):
    """Tool names from the stored tools reference extract, in table order."""
    names = []
    for line in _extract(state_sources, source_id).split('\n'):
        name = _plain(line.split(' | ')[0])
        if _NAME.match(name) and name not in names:
            names.append(name)
    return names


def field_table(state_sources, source_id=FIELDS_SOURCE):
    """Frontmatter field names from the stored skills docs extract.

    Reads the first table whose first column is `Field`.
    """
    for header, rows in _tables(_extract(state_sources, source_id).split('\n')):
        if not header or _plain(header[0]).lower() != 'field':
            continue
        names = []
        for row in rows:
            name = _plain(row[0]) if row else ''
            if _NAME.match(name) and name not in names:
                names.append(name)
        return names
    return []
