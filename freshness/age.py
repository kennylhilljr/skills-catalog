"""Skill age lifecycle: `re-review` issues, the `stale` flag and advancing `catalog-last-verified`.

Requirements: FRS-8, FRS-9 (docs/SPEC.md). Contract: docs/DESIGN.md 7.2 (the
Age row and the last-verified rule) and 7.6 (14 days in `re-review`).

The `stale` flag
----------------

`freshness/stale.json` is a sorted JSON list of skill names, changed only by
the PRs this module plans. `tools/catalog.py` reads it and adds
`"stale": true` to those entries of `catalog.json` (in `skills` or `held`).
A skill that is not stale has no `stale` key at all, so readers such as the
MCP server's `skill_status` (M4) must treat a missing key as not stale::

    stale = entry.get("stale") is True

Lifecycle
---------

1. `age_findings(skills, today)` (FRS-8) reports kind `past-max-age` for every
   skill whose `catalog-last-verified` plus `catalog-max-age-days` is before
   today. `route_rereview(finding)` turns one into the routed item
   `alerts.plan_findings` takes: an issue titled `re-review: <skill>` and
   labeled `re-review`, deduplicated by its fingerprint like any finding.
   The runner (part 9) uses it instead of `route.route` for this kind.
2. `rereview_since(state, open_issues, skills, now)` records, in
   `state["rereview"]`, when each open `re-review` issue was first seen.
3. `stale_actions(open_rereview, stale, now)` plans one PR that sets
   `freshness/stale.json` to its target: every skill whose `re-review` issue
   has been open 14 days, minus skills whose issue closed or that are no
   longer past their max age (verified again). The PR also carries the
   regenerated `catalog.json`.
4. `advance_actions(results, today, policy)` (FRS-9) plans one PR that sets
   `catalog-last-verified` to today on that one line of each eligible
   skill, drops those skills from `freshness/stale.json` and regenerates
   `catalog.json`. This is the only code that writes `catalog-last-verified`.

At most one stale PR and one advance PR are open at a time. Each body
carries a hidden key line with its target and a hash of `main`'s
`catalog.json`; when either changes, the open PR is closed with a comment
and a fresh one is opened on the current `main`, so a PR never goes stale
against another bot PR that merged first.

Held skills (`.held/`) are reported by `age_findings` (the canary relies on
that, FRS-18) but are never advanced: an `sdd-*` mirror changes only through
a sync PR from its source (SDD-1), and `zz-canary` must stay past its age.

Python 3.9+, standard library only. Importing has no side effects beyond
loading `tools/catalog.py` and `tools/scrub.py`.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import tempfile

from . import alerts as amod
from . import finding as fmod
from . import state as smod

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CATALOG_PATH = os.path.join(_ROOT, 'tools', 'catalog.py')
_catalog = None

STALE_PATH = 'freshness/stale.json'
CATALOG_PATH = 'catalog.json'
SECTIONS = ('skills', '.held')

LABEL_REREVIEW = 're-review'
LABEL_FRESHNESS = 'freshness'
TITLE_MAX = 100

DEFAULT_MAX_AGE_DAYS = 90
STALE_AFTER = datetime.timedelta(days=14)
MAC_MAX_AGE = datetime.timedelta(hours=26)
NEAR_MAX_AGE_DAYS = 7

# FRS-9 policy (ops decision on the M2 tracker): advance only a skill that is
# past, or within NEAR_MAX_AGE_DAYS of, its max age. The alternative is one
# constant away.
POLICY_AT_MAX_AGE = 'at-max-age'
POLICY_EVERY_CLEAN_RUN = 'every-clean-run'
POLICIES = (POLICY_AT_MAX_AGE, POLICY_EVERY_CLEAN_RUN)
DEFAULT_POLICY = POLICY_AT_MAX_AGE

STATE_KEY = 'rereview'  # state["rereview"][skill] = first-seen ISO time (owned here)

_KEY_PREFIX = '<!-- freshness-'
_STALE_KEY = 'stale'
_ADVANCE_KEY = 'advance'
_KEY_RE = re.compile(r'<!-- freshness-(stale|advance): (.*?) -->')

_LAST_KEY = 'catalog-last-verified'
_LAST_RE = re.compile(r'^(\s*' + _LAST_KEY + r':\s*)(["\']?)(\d{4}-\d{2}-\d{2})\2(\s*)$')
_MAX_RE = re.compile(r'^\s*catalog-max-age-days:\s*(["\']?)(\d+)\1\s*$')


def _catalog_module():
    global _catalog
    if _catalog is None:
        spec = importlib.util.spec_from_file_location('freshness_age_catalog', _CATALOG_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _catalog = module
    return _catalog


def _date(value):
    if isinstance(value, datetime.datetime):
        return smod.require_utc(value).date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def _title(text):
    return text[:TITLE_MAX]


# ---------------------------------------------------------------------------
# Reading skills


def _is_real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _frontmatter_lines(text):
    lines = text.split('\n')
    if not lines or lines[0].rstrip('\r') != '---':
        return []
    for index in range(1, len(lines)):
        if lines[index].rstrip('\r') == '---':
            return lines[1:index]
    return []


def read_skills(root, excluded=()):
    """Age fields of every skill under `skills/` and `.held/`.

    Returns `[{"name", "section", "file", "line", "last_verified",
    "max_age_days"}]`: `file` is the repo-relative SKILL.md, `line` the
    one-based line of `catalog-last-verified` (None when missing or not a
    date, which `tools/lint.py` reports), `last_verified` an ISO date or
    None. Names in `excluded` are skipped before their folder is touched
    (DESIGN 5.1).
    """
    excluded = set(excluded or ())
    out = []
    for section in SECTIONS:
        base = os.path.join(root, section)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if name in excluded:
                continue
            folder = os.path.join(base, name)
            path = os.path.join(folder, 'SKILL.md')
            if not (_is_real_dir(folder) and _is_regular(path)):
                continue
            try:
                with open(path, encoding='utf-8', newline='') as handle:
                    text = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            entry = {'name': name, 'section': section, 'file': section + '/' + name + '/SKILL.md',
                     'line': None, 'last_verified': None, 'max_age_days': DEFAULT_MAX_AGE_DAYS}
            for offset, line in enumerate(_frontmatter_lines(text)):
                line = line.rstrip('\r')
                m = _LAST_RE.match(line)
                if m:
                    try:
                        datetime.date.fromisoformat(m.group(3))
                    except ValueError:
                        continue
                    entry['line'] = offset + 2
                    entry['last_verified'] = m.group(3)
                    continue
                m = _MAX_RE.match(line)
                if m and int(m.group(2)) > 0:
                    entry['max_age_days'] = int(m.group(2))
            out.append(entry)
    return out


def due_date(skill):
    """The last day a skill is within its max age, or None without a date."""
    if not skill.get('last_verified'):
        return None
    return _date(skill['last_verified']) + datetime.timedelta(days=int(skill['max_age_days']))


# ---------------------------------------------------------------------------
# FRS-8: past max age, re-review issues


def age_findings(skills, today, excluded=()):
    """One `past-max-age` Finding per skill whose last-verified + max age is before `today`."""
    today = _date(today)
    excluded = set(excluded or ())
    out = []
    for skill in skills or ():
        if skill['name'] in excluded:
            continue
        due = due_date(skill)
        if due is None or not due < today:
            continue
        evidence = ('`catalog-last-verified` is %s and `catalog-max-age-days` is %d, so the skill was due '
                    'for re-review by %s; today is %s (%d days past).'
                    % (skill['last_verified'], skill['max_age_days'], due.isoformat(), today.isoformat(),
                       (today - due).days))
        out.append({'kind': 'past-max-age', 'skill': skill['name'], 'subject': skill['name'],
                    'file': skill['file'], 'line': skill['line'], 'evidence': evidence, 'fix': None})
    return out


def rereview_fingerprint(skill):
    """The fingerprint marker of a skill's `re-review` issue."""
    return fmod.fingerprint('past-max-age', skill)


def route_rereview(finding):
    """The routed item (for `alerts.plan_findings`) of one `past-max-age` finding."""
    skill = finding['skill']
    held = (finding.get('file') or '').startswith('.held/')
    what = ('Re-read the skill against the current Claude Code and Codex docs and fix what is out of '
            'date. When every freshness check passes, the bot advances `catalog-last-verified` '
            'through a PR (FRS-9). If this issue stays open 14 days, the bot marks the skill `stale` '
            'in `catalog.json` (FRS-8).')
    if held:
        what = ('This is a held skill: it gets no bot PR, and a held `sdd-*` mirror changes only through '
                'a sync PR from its source (SDD-1). ' + what)
    body = '\n\n'.join([
        '## What happened\n\nSkill `%s` is past its max age (FRS-8).' % skill,
        '## Evidence\n\n' + (finding.get('evidence') or 'none'),
        '## What to change\n\n' + what,
        '## Files\n\n' + ('- `%s`' % finding['file'] if finding.get('file') else 'none'),
        '## Acceptance\n\n`catalog-last-verified` of the skill is within its max age on `main`.',
    ])
    return {'finding': finding, 'as': 'issue', 'labels': [LABEL_REREVIEW], 'files': {}, 'body': body,
            'title': _title('re-review: ' + skill)}


def rereview_since(state, open_issues, skills, now):
    """`[{"skill", "number", "opened_at"}]` for the open `re-review` issues.

    `open_issues`: open bot items `{"number", "fingerprint", ...}` (from
    `github.list_bot_items`). An issue is matched to its skill through
    `rereview_fingerprint`. `state["rereview"][skill]` keeps the first time
    the issue was seen open, and entries whose issue is no longer open are
    dropped, so a reopened issue starts its 14 days again. `state` is
    changed in place.
    """
    now_iso = smod.iso(now)
    by_fp = {rereview_fingerprint(s['name']): s['name'] for s in skills or ()}
    table = state.setdefault(STATE_KEY, {})
    seen = {}
    for item in open_issues or ():
        if item.get('state', 'open') != 'open' or item.get('is_pr'):
            continue
        skill = by_fp.get(item.get('fingerprint'))
        if skill and skill not in seen:
            seen[skill] = item['number']
    for skill in list(table):
        if skill not in seen:
            del table[skill]
    out = []
    for skill in sorted(seen):
        table.setdefault(skill, now_iso)
        out.append({'skill': skill, 'number': seen[skill], 'opened_at': table[skill]})
    return out


# ---------------------------------------------------------------------------
# freshness/stale.json and catalog.json


def read_stale(root):
    """The names in `freshness/stale.json` (sorted, unique), or [] when the file is missing."""
    return _catalog_module().read_stale(root)


def render_stale(names):
    """The text of `freshness/stale.json`: a sorted, unique JSON list."""
    return json.dumps(sorted(set(names)), indent=2) + '\n'


def _catalog_sha(root):
    try:
        with open(os.path.join(root, CATALOG_PATH), 'rb') as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:12]
    except FileNotFoundError:
        return 'none'


def _render(root, changes):
    """`catalog.json` after `changes` ({repo path: text}), built with `tools/catalog.py` `render`."""
    with tempfile.TemporaryDirectory() as tmp:
        for section in SECTIONS:
            source = os.path.join(root, section)
            target = os.path.join(tmp, section)
            if os.path.isdir(source):
                shutil.copytree(source, target, symlinks=True)
            else:
                os.makedirs(target)
        stale = os.path.join(root, *STALE_PATH.split('/'))
        if os.path.isfile(stale):
            os.makedirs(os.path.join(tmp, 'freshness'), exist_ok=True)
            shutil.copyfile(stale, os.path.join(tmp, *STALE_PATH.split('/')))
        for rel, text in changes.items():
            path = os.path.join(tmp, *rel.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='') as handle:
                handle.write(text)
        return _catalog_module().render(tmp)


def _manifest_names(root):
    names = set()
    for section in SECTIONS:
        base = os.path.join(root, section)
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for name in entries:
            if section == '.held' and name == _catalog_module().CANARY:
                continue
            if _is_real_dir(os.path.join(base, name)) and _is_regular(os.path.join(base, name, 'SKILL.md')):
                names.add(name)
    return names


# ---------------------------------------------------------------------------
# One open bot PR per purpose, keyed by its target and the base catalog.json


def _key_line(purpose, names, base):
    return '%s%s: names=%s base=%s -->' % (_KEY_PREFIX, purpose, ','.join(sorted(names)) or '-', base)


def _open_prs(open_items, purpose):
    out = []
    for item in open_items or ():
        if not item.get('is_pr') or item.get('state', 'open') != 'open':
            continue
        for m in _KEY_RE.finditer(item.get('body') or ''):
            if m.group(1) == purpose:
                out.append((item, _KEY_PREFIX + purpose + ': ' + m.group(2) + ' -->'))
                break
    return out


def _plan_pr(purpose, key, files, title, body, open_items, superseded):
    """Keep an open PR with the same key, else close the others and open a new one."""
    actions = []
    keep = False
    for item, item_key in _open_prs(open_items, purpose):
        if files is not None and item_key == key and not keep:
            keep = True
            continue
        actions.append({'op': 'comment', 'fingerprint': item['fingerprint'], 'number': item['number'],
                        'body': superseded})
        actions.append({'op': 'close', 'fingerprint': item['fingerprint'], 'number': item['number']})
    if files is not None and not keep:
        fp = fmod.fingerprint('freshness-' + purpose, key)
        body = body.rstrip('\n') + '\n\n' + key + '\n' + fmod.marker(fp) + '\n'
        actions.append({'op': 'open_pr', 'fingerprint': fp, 'title': _title(title), 'body': body,
                        'labels': [LABEL_FRESHNESS], 'files': files, 'branch': fmod.branch_name(fp)})
    return amod._scrub_actions(actions)


def _files_section(files):
    return '## Files\n\n' + '\n'.join('- `%s`' % path for path in sorted(files))


# ---------------------------------------------------------------------------
# FRS-8: the stale flag


def stale_actions(open_rereview, stale, now, root=None, aged=None, open_items=(), excluded=()):
    """Actions that keep `freshness/stale.json` in line with the `re-review` issues (FRS-8).

    `open_rereview`: `[{"skill", "number", "opened_at"}]` from `rereview_since`.
    `stale`: the names in `main`'s `freshness/stale.json`. `aged`: the
    skills `age_findings` reported this run; None means every open issue
    still counts. A skill is in the target list when its issue has been
    open at least 14 days and it is still past its max age. A listed skill
    whose issue closed, or that is no longer past its max age (verified
    again), is dropped. An open issue of a skill that was verified again is
    closed with a comment. `open_items`: open bot items with `body` and
    `is_pr`, used to find the open stale PR.
    """
    now = smod.require_utc(now)
    root = _ROOT if root is None else root
    excluded = set(excluded or ())
    stale = set(stale or ()) - excluded
    aged_set = None if aged is None else set(aged)
    known = _manifest_names(root)
    actions = []
    target = set()
    open_by_skill = {}
    for item in open_rereview or ():
        if item['skill'] in excluded:
            continue
        open_by_skill[item['skill']] = item
    for skill, item in sorted(open_by_skill.items()):
        if aged_set is not None and skill not in aged_set:
            fp = rereview_fingerprint(skill)
            actions.append({'op': 'comment', 'fingerprint': fp, 'number': item['number'],
                            'body': 'Skill `%s` is within its max age again, so this re-review is done (FRS-8).'
                            % skill})
            actions.append({'op': 'close', 'fingerprint': fp, 'number': item['number']})
            continue
        opened = smod.parse_iso(item.get('opened_at'))
        if skill in stale or (opened is not None and now - opened >= STALE_AFTER):
            target.add(skill)
    target &= known
    added, removed = sorted(target - stale), sorted(stale - target)
    files = None
    key = _key_line(_STALE_KEY, target, _catalog_sha(root))
    if added or removed:
        files = {STALE_PATH: render_stale(target)}
        files[CATALOG_PATH] = _render(root, files)
    parts = (['mark %s stale' % ', '.join(added)] if added else []) + \
            (['clear stale on %s' % ', '.join(removed)] if removed else [])
    title = 'freshness: ' + '; '.join(parts) if parts else 'freshness: stale list'
    body = '\n\n'.join([
        '## What happened\n\n'
        + ('Marked stale: %s. These skills have had a `re-review` issue open for 14 days (FRS-8).\n\n'
           % ', '.join('`%s`' % s for s in added) if added else '')
        + ('Stale flag cleared: %s. Their `re-review` issue closed or they were verified again (FRS-8).'
           % ', '.join('`%s`' % s for s in removed) if removed else ''),
        '## Change\n\n`freshness/stale.json` lists the stale skills, and `catalog.json`, regenerated with '
        '`tools/catalog.py`, carries `"stale": true` for each of them.',
        _files_section(files or {}),
        '## Acceptance\n\n```\npython3 tools/catalog.py --check\n```',
    ])
    superseded = 'Superseded: the stale list or `catalog.json` on `main` changed, so the bot opens a fresh PR.'
    return actions + _plan_pr(_STALE_KEY, key, files, title, body, open_items, superseded)


# ---------------------------------------------------------------------------
# FRS-9: advancing catalog-last-verified


def eligible(result, now, evals_required=False):
    """True when every Actions check passed this run and the Mac run is under 26 h old and passed."""
    if not isinstance(result, dict):
        return False
    if result.get('actions_ok') is not True or not result.get('actions_run'):
        return False
    if not result.get('mac_run') or result.get('mac_ok') is not True:
        return False
    mac_at = smod.parse_iso(result.get('mac_at'))
    if mac_at is None or not (datetime.timedelta(0) <= now - mac_at < MAC_MAX_AGE):
        return False
    if (evals_required or 'evals_ok' in result) and result.get('evals_ok') is not True:
        return False  # from M6, evals must pass too
    return True


def _wanted(skill, today, policy):
    if policy == POLICY_EVERY_CLEAN_RUN:
        return True
    if policy == POLICY_AT_MAX_AGE:
        due = due_date(skill)
        return due is not None and due - datetime.timedelta(days=NEAR_MAX_AGE_DAYS) <= today
    raise ValueError('unknown policy: %r' % (policy,))


def set_last_verified(text, line, today):
    """`text` with `catalog-last-verified` on one-based `line` set to `today`, or None."""
    lines = text.split('\n')
    index = line - 1
    if index < 0 or index >= len(lines):
        return None
    m = _LAST_RE.match(lines[index].rstrip('\r'))
    if not m:
        return None
    cr = '\r' if lines[index].endswith('\r') else ''
    lines[index] = m.group(1) + m.group(2) + today + m.group(2) + m.group(4) + cr
    return '\n'.join(lines)


def advance_actions(results, today, policy=DEFAULT_POLICY, now=None, root=None, skills=None,
                    open_items=(), excluded=(), evals_required=False):
    """Actions for one PR that advances `catalog-last-verified` (FRS-9).

    `results[skill] = {"actions_run": run_id, "actions_ok": bool,
    "mac_run": run_id | None, "mac_ok": bool | None, "mac_at": iso,
    "evals_ok"?: bool}`. A skill under `skills/` is eligible when
    `eligible()` holds; `policy` then picks which eligible skills get the
    PR (`at-max-age`: past or within 7 days of the max age;
    `every-clean-run`: all). The PR sets `catalog-last-verified` to `today`
    on that one line of each picked skill, drops them from
    `freshness/stale.json` and regenerates `catalog.json`. `now` (aware UTC,
    default: the current time) dates the Mac run.
    """
    if policy not in POLICIES:
        raise ValueError('unknown policy: %r' % (policy,))
    today = _date(today)
    now = smod.require_utc(now) if now is not None else datetime.datetime.now(smod.UTC)
    root = _ROOT if root is None else root
    excluded = set(excluded or ())
    if skills is None:
        skills = read_skills(root, excluded)
    picked = []
    for skill in skills:
        if skill['name'] in excluded or skill['section'] != 'skills' or not skill.get('line'):
            continue
        if _date(skill['last_verified']) >= today:
            continue
        if not eligible((results or {}).get(skill['name']), now, evals_required):
            continue
        if _wanted(skill, today, policy):
            picked.append(skill)
    files = {}
    for skill in picked:
        with open(os.path.join(root, *skill['file'].split('/')), encoding='utf-8', newline='') as handle:
            text = handle.read()
        new = set_last_verified(text, skill['line'], today.isoformat())
        if new is not None and new != text:
            files[skill['file']] = new
    names = sorted(rel.split('/')[1] for rel in files)
    if not files:
        key = _key_line(_ADVANCE_KEY, (), _catalog_sha(root))
        return _plan_pr(_ADVANCE_KEY, key, None, '', '', open_items,
                        'Superseded: no skill needs `catalog-last-verified` advanced now.')
    stale = read_stale(root)
    if set(stale) & set(names):
        files[STALE_PATH] = render_stale(set(stale) - set(names))
    files[CATALOG_PATH] = _render(root, files)
    key = _key_line(_ADVANCE_KEY, names, _catalog_sha(root))
    rows = []
    for skill in picked:
        if skill['file'] in files:
            result = results[skill['name']]
            rows.append('| `%s` | %s | %s | %s | %s |' % (
                skill['name'], skill['last_verified'], today.isoformat(), result['actions_run'],
                result['mac_run']))
    body = '\n\n'.join([
        '## What happened\n\nEvery freshness check passed for these skills on this run, and the Mac run '
        'is under 26 hours old (FRS-9). Policy: `%s`.' % policy,
        '## Evidence\n\n| Skill | Last verified | New | Actions run | Mac run |\n| --- | --- | --- | --- | --- |\n'
        + '\n'.join(rows),
        '## Change\n\nSets `catalog-last-verified` to %s on that one line of each skill, drops them from '
        '`freshness/stale.json` and regenerates `catalog.json`.' % today.isoformat(),
        _files_section(files),
        '## Acceptance\n\n```\npython3 tools/lint.py\npython3 tools/catalog.py --check\n```',
    ])
    title = 'freshness: advance catalog-last-verified: ' + ', '.join(names)
    return _plan_pr(_ADVANCE_KEY, key, files, title, body, open_items,
                    'Superseded: the skills to advance or `catalog.json` on `main` changed, so the bot opens a fresh PR.')
