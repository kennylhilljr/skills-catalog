"""Shared shapes of the freshness bot: Finding, Action and the fingerprint.

Requirements: FRS-13, FRS-22 (docs/SPEC.md). Contract: docs/DESIGN.md 7.5
and 7.8.

Findings and actions are plain dicts, so the parts that produce them do not
need to import this module. The shapes, restated for those parts:

Finding::

    {"kind": str,            # one of FINDING_KINDS
     "skill": str,           # skill folder name, "catalog" or an upstream source id
     "subject": str,         # stable identity, no line numbers
     "file": str | None,     # repo-relative path
     "line": int | None,     # one-based
     "evidence": str,
     "fix": None | {"file": str, "line": int, "old": str, "new": str}}

Action::

    {"op": one of ACTION_OPS,
     "fingerprint": str,
     "title": str, "body": str, "labels": [str],   # open_issue, open_pr
     "number": int,                                # comment, reopen, close, update_pr
     "body": str,                                  # also the text of a comment
     "files": {repo_path: new_text},               # open_pr, update_pr
     "branch": "freshness/<first 12 hex>",         # open_pr, update_pr
     "text": str}                                  # send_ops

Python 3.9+, standard library only. Importing has no side effects.
"""

import hashlib
import re

FINDING_KINDS = (
    'deprecated-model',
    'dead-link',
    'missing-path',
    'description',
    'frontmatter',
    'missing-command',
    'unknown-tool',
    'unknown-flag',
    'cli-lag',
    'held-drift',
    'upstream-change',
    'past-max-age',
)

ALERT_KINDS = (
    'source-failing',
    'heartbeat-actions',
    'heartbeat-mac',
    'canary',
    'dwell',
    'storm',
)

ACTION_OPS = ('open_issue', 'comment', 'reopen', 'close', 'open_pr', 'update_pr', 'send_ops')

FINDING_KEYS = ('kind', 'skill', 'subject', 'file', 'line', 'evidence', 'fix')
FIX_KEYS = ('file', 'line', 'old', 'new')

MARKER_PREFIX = '<!-- freshness-fingerprint: '
MARKER_SUFFIX = ' -->'
_MARKER_RE = re.compile(r'<!-- freshness-fingerprint: ([0-9a-f]{64}) -->')
_ANY_MARKER_RE = re.compile(r'[ \t]*<!-- freshness-fingerprint:[^>]*-->[ \t]*\n?')
_HEX64 = re.compile(r'[0-9a-f]{64}')

# Keys each op needs besides "op" and "fingerprint".
_ACTION_REQUIRED = {
    'open_issue': ('title', 'body', 'labels'),
    'comment': ('number', 'body'),
    'reopen': ('number',),
    'close': ('number',),
    'open_pr': ('title', 'body', 'labels', 'files', 'branch'),
    'update_pr': ('number', 'files', 'branch'),
    'send_ops': ('text',),
}


def fingerprint(kind, subject):
    """Lowercase hex sha256 of `kind:subject` (DESIGN 7.8)."""
    return hashlib.sha256((kind + ':' + subject).encode('utf-8')).hexdigest()


def finding_fingerprint(finding):
    return fingerprint(finding['kind'], finding['subject'])


def evidence_sha(evidence):
    """Lowercase hex sha256 of an evidence string."""
    return hashlib.sha256((evidence or '').encode('utf-8')).hexdigest()


def branch_name(fp):
    """The PR branch for a fingerprint: `freshness/<first 12 hex>`."""
    return 'freshness/' + fp[:12]


def marker(fp):
    """The hidden deduplication line carried by issue and PR bodies."""
    return MARKER_PREFIX + fp + MARKER_SUFFIX


def parse_marker(body):
    """The fingerprint in an issue or PR body, or None."""
    m = _MARKER_RE.search(body or '')
    return m.group(1) if m else None


def strip_markers(body):
    """`body` without any fingerprint marker line."""
    return _ANY_MARKER_RE.sub('', body or '')


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate(finding):
    """Shape errors of a Finding, as a list of strings (empty when valid)."""
    if not isinstance(finding, dict):
        return ['finding is not a dict']
    errors = []
    for key in FINDING_KEYS:
        if key not in finding:
            errors.append('missing key: ' + key)
    for key in sorted(set(finding) - set(FINDING_KEYS)):
        errors.append('unknown key: ' + str(key))
    kind = finding.get('kind')
    if 'kind' in finding and kind not in FINDING_KINDS:
        errors.append('unknown kind: ' + repr(kind))
    for key in ('skill', 'subject'):
        if key in finding and not (isinstance(finding[key], str) and finding[key]):
            errors.append(key + ' must be a non-empty string')
    if 'evidence' in finding and not isinstance(finding['evidence'], str):
        errors.append('evidence must be a string')
    if 'file' in finding and not (finding['file'] is None or isinstance(finding['file'], str)):
        errors.append('file must be a string or None')
    if 'line' in finding and not (finding['line'] is None or _is_int(finding['line'])):
        errors.append('line must be an int or None')
    fix = finding.get('fix')
    if 'fix' in finding and fix is not None:
        if not isinstance(fix, dict):
            errors.append('fix must be a dict or None')
        else:
            for key in FIX_KEYS:
                if key not in fix:
                    errors.append('fix missing key: ' + key)
            for key in sorted(set(fix) - set(FIX_KEYS)):
                errors.append('fix unknown key: ' + str(key))
            for key in ('file', 'old', 'new'):
                if key in fix and not isinstance(fix[key], str):
                    errors.append('fix.' + key + ' must be a string')
            if 'line' in fix and not _is_int(fix['line']):
                errors.append('fix.line must be an int')
    return errors


def validate_action(action):
    """Shape errors of an Action, as a list of strings (empty when valid)."""
    if not isinstance(action, dict):
        return ['action is not a dict']
    errors = []
    op = action.get('op')
    if op not in ACTION_OPS:
        return ['unknown op: ' + repr(op)]
    fp = action.get('fingerprint')
    if not (isinstance(fp, str) and _HEX64.fullmatch(fp)):
        errors.append('fingerprint must be 64 lowercase hex')
    for key in _ACTION_REQUIRED[op]:
        if key not in action:
            errors.append(op + ' missing key: ' + key)
    if 'number' in action and not _is_int(action['number']):
        errors.append('number must be an int')
    if 'labels' in action:
        labels = action['labels']
        if not (isinstance(labels, list) and all(isinstance(x, str) for x in labels)):
            errors.append('labels must be a list of strings')
    if 'files' in action and not isinstance(action['files'], dict):
        errors.append('files must be a dict')
    if 'branch' in action and isinstance(fp, str) and action['branch'] != branch_name(fp):
        errors.append('branch must be ' + branch_name(fp if isinstance(fp, str) else ''))
    return errors
