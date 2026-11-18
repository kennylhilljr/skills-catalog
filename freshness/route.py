"""Finding router: a mechanical fix becomes a PR, everything else an issue (SPEC FRS-12, SDD-1; DESIGN 7.1, 7.5, 10).

`route(finding, root, models)` turns one Finding (freshness/finding.py) into
the routed item that `alerts.plan_findings` takes::

    {"finding": Finding, "as": "pr" | "issue", "title": str, "body": str,
     "labels": [str], "files": {repo_path: new_text}}

Mechanical, a PR labeled `freshness`:

- a `deprecated-model`, `moved-link` (a 301 or 308), `dead-link` or
  `missing-path` finding with a `fix`, in a skill under `skills/`. The PR
  changes the `fix` line, bumps the patch of `catalog-version`, adds one
  `## Changelog` row and carries the regenerated `catalog.json`, which the
  catalog's own checks require (CAT-11, CAT-14, DESIGN 10);
- an `upstream-change` of the Claude Code skills docs or the Agent Skills
  spec whose diff only adds optional frontmatter fields. The PR adds them to
  `x-claude-only` or `properties` of `schema/frontmatter.schema.json`.

Held skills (`.held/`) never get a PR: an `sdd-*` body changes only through a
sync PR from its source (SDD-1), and `zz-canary` must keep its known
findings (FRS-18). Their findings, every other per-skill finding and every
non-mechanical `upstream-change` become issues: labeled `freshness` for a
skill, `upstream-change` for an upstream diff, which lists the affected
skills (from freshness/classify.py) and the diff in a fenced block.

`models` is the model table (`freshness/models.json`), used to name the
replacement in the PR text. Python 3.9+, standard library only. Importing
has no side effects beyond loading `tools/catalog.py`.
"""

import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CATALOG_PATH = os.path.join(_ROOT, 'tools', 'catalog.py')
_catalog = None

LABEL_FRESHNESS = 'freshness'
LABEL_UPSTREAM = 'upstream-change'
TITLE_MAX = 100
SCHEMA_PATH = 'schema/frontmatter.schema.json'
CATALOG_PATH = 'catalog.json'

MECHANICAL_KINDS = ('deprecated-model', 'moved-link', 'dead-link', 'missing-path')
# Upstream sources whose field table can add an optional frontmatter field,
# and where the schema records it.
SCHEMA_SOURCES = {
    'claude-code-skills-frontmatter': 'x-claude-only',
    'agent-skills-spec': 'properties',
}

_VERSION = re.compile(r'^(\s*catalog-version:\s*)(["\']?)(\d+)\.(\d+)\.(\d+)\2(\s*)$')
_FIELD = re.compile(r'^[a-z][a-z0-9_-]*$')
_SEPARATOR = re.compile(r'^\|(\s*:?-+:?\s*\|)+\s*$')
_CHANGELOG_HEADER = re.compile(r'^\|\s*Version\s*\|\s*Date\s*\|\s*Change\s*\|\s*$')

_HINTS = {
    'deprecated-model': 'replace the model ID with its current replacement, or remove it',
    'moved-link': 'point the link at its new location',
    'dead-link': 'replace or remove the dead link',
    'missing-path': 'fix or remove the reference to the missing path',
    'description': 'rewrite the description so it says when to use the skill',
    'frontmatter': 'fix the frontmatter so `python3 tools/lint.py` passes',
    'missing-command': 'install the command, or remove it from the skill and `catalog-requires`',
    'unknown-tool': 'use a current tool name',
    'unknown-flag': 'use a flag the current CLI accepts',
    'cli-lag': 'update the host CLI',
    'held-drift': 'sync the held mirror from its source',
    'past-max-age': 're-review the skill and advance `catalog-last-verified`',
}


def _catalog_module():
    global _catalog
    if _catalog is None:
        spec = importlib.util.spec_from_file_location('freshness_route_catalog', _CATALOG_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _catalog = module
    return _catalog


def _title(prefix, text):
    return (prefix + text)[:TITLE_MAX]


def _fence(text):
    longest = max([len(run) for run in re.findall(r'`+', text or '')] + [2])
    return '`' * (longest + 1)


def _fenced(text):
    fence = _fence(text)
    return fence + '\n' + (text or '').rstrip('\n') + '\n' + fence


def _read(root, rel):
    with open(os.path.join(root, *rel.split('/')), encoding='utf-8', newline='') as handle:
        return handle.read()


def _today(today):
    if today is None:
        return datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if isinstance(today, (datetime.date, datetime.datetime)):
        return today.isoformat()[:10]
    return str(today)[:10]


def is_held(finding, root):
    skill = finding.get('skill') or ''
    for path in (finding.get('file'), (finding.get('fix') or {}).get('file')):
        if isinstance(path, str) and path.startswith('.held/'):
            return True
    return bool(skill) and os.path.isdir(os.path.join(root, '.held', skill))


# ---------------------------------------------------------------------------
# Skill PRs


def apply_fix(text, fix):
    """`text` with `fix.old` replaced by `fix.new` on line `fix.line`, or None if it no longer applies."""
    lines = text.split('\n')
    index = fix['line'] - 1
    if index < 0 or index >= len(lines) or not fix['old'] or fix['old'] not in lines[index]:
        return None
    lines[index] = lines[index].replace(fix['old'], fix['new'])
    return '\n'.join(lines)


def bump_patch(text):
    """`(new text, old version, new version)` with the patch of `catalog-version` bumped, or None."""
    lines = text.split('\n')
    if not lines or lines[0] != '---':
        return None
    for index in range(1, len(lines)):
        if lines[index] == '---':
            return None
        m = _VERSION.match(lines[index])
        if m:
            old = '%s.%s.%s' % (m.group(3), m.group(4), m.group(5))
            new = '%s.%s.%d' % (m.group(3), m.group(4), int(m.group(5)) + 1)
            lines[index] = m.group(1) + m.group(2) + new + m.group(2) + m.group(6)
            return '\n'.join(lines), old, new
    return None


def add_changelog_row(text, row):
    """`text` with `row` as the first row of the `## Changelog` table, or None."""
    lines = text.split('\n')
    heading, in_fence = None, False
    for index, line in enumerate(lines):
        if line.startswith('```'):
            in_fence = not in_fence
        elif not in_fence and line.lstrip().startswith('## '):
            heading = index
    if heading is None or lines[heading].rstrip() != '## Changelog':
        return None
    for index in range(heading + 1, len(lines) - 1):
        if _CHANGELOG_HEADER.match(lines[index].strip()) and _SEPARATOR.match(lines[index + 1].strip()):
            lines.insert(index + 2, row)
            return '\n'.join(lines)
    return None


def _render_catalog(root, changes):
    """catalog.json after `changes`, built on a copy of `skills/` and `.held/`."""
    with tempfile.TemporaryDirectory() as tmp:
        for section in ('skills', '.held'):
            source = os.path.join(root, section)
            target = os.path.join(tmp, section)
            if os.path.isdir(source):
                shutil.copytree(source, target, symlinks=True)
            else:
                os.makedirs(target)
        for rel, text in changes.items():
            path = os.path.join(tmp, *rel.split('/'))
            with open(path, 'w', encoding='utf-8', newline='') as handle:
                handle.write(text)
        return _catalog_module().render(tmp)


def _replacement(models, finding):
    for model in models or ():
        old = model.get('id')
        if old and (finding.get('subject') or '').endswith(':' + old):
            return old, model.get('replacement')
    return None, None


def _change_text(finding, models):
    kind = finding['kind']
    if kind == 'deprecated-model':
        old, new = _replacement(models, finding)
        if old and new:
            return 'replace deprecated model ID `%s` with `%s`' % (old, new)
        return 'replace a deprecated model ID'
    if kind == 'moved-link':
        return 'update a link that moved permanently'
    if kind == 'dead-link':
        return 'replace a dead link'
    return 'fix a missing path'


def _skill_pr(finding, root, models, today):
    """The PR files for a mechanical per-skill fix, or None when it cannot be built."""
    fix = finding.get('fix')
    skill = finding.get('skill') or ''
    if not fix or not skill or finding['kind'] not in MECHANICAL_KINDS:
        return None
    prefix = 'skills/' + skill + '/'
    rel = fix.get('file') or ''
    if not rel.startswith(prefix) or '..' in rel.split('/') or rel == CATALOG_PATH:
        return None
    skill_md = prefix + 'SKILL.md'
    try:
        original = _read(root, rel)
        skill_text = _read(root, skill_md)
    except (OSError, UnicodeDecodeError):
        return None
    fixed = apply_fix(original, fix)
    if fixed is None or fixed == original:
        return None
    files = {rel: fixed}
    if rel == skill_md:
        skill_text = fixed
    bumped = bump_patch(skill_text)
    if bumped is None:
        return None
    skill_text, _old_version, new_version = bumped
    change = _change_text(finding, models)
    before = hashlib.sha256(_read(root, skill_md).encode('utf-8')).hexdigest()[:12]
    row = '| %s | %s | Freshness bot: %s in `%s` (FRS-12); previous SKILL.md sha256 `%s` |' % (
        new_version, today, change, rel[len(prefix):], before)
    skill_text = add_changelog_row(skill_text, row)
    if skill_text is None:
        return None
    files[skill_md] = skill_text
    files[CATALOG_PATH] = _render_catalog(root, files)
    return files, change


def _skill_pr_item(finding, files, change):
    fix = finding['fix']
    body = '\n\n'.join([
        '## What happened\n\n`%s` finding in skill `%s`.' % (finding['kind'], finding['skill']),
        '## Evidence\n\n' + (finding.get('evidence') or 'none'),
        '## Change\n\nOn line %d of `%s`: %s. Also a patch bump of `catalog-version`, one '
        '`## Changelog` row and the regenerated `catalog.json`, which the catalog checks require '
        '(CAT-11, CAT-14, DESIGN 10).' % (fix['line'], fix['file'], change),
        '## Files\n\n' + '\n'.join('- `%s`' % path for path in sorted(files)),
        '## Acceptance\n\n```\npython3 tools/lint.py\npython3 tools/catalog.py --check\n```',
    ])
    return {'finding': finding, 'as': 'pr', 'labels': [LABEL_FRESHNESS], 'files': files, 'body': body,
            'title': _title('freshness: %s: ' % finding['skill'], change)}


# ---------------------------------------------------------------------------
# Schema PRs


def new_optional_fields(diff):
    """Fields a diff only adds as optional rows of a field table, or [] when it does more."""
    fields = []
    for line in (diff or '').split('\n'):
        if line.startswith(('+++', '---', '@@', ' ')) or line == '':
            continue
        if not line.startswith('+'):
            return []
        row = line[1:].strip()
        if not row:
            continue
        if not row.startswith('|') or _SEPARATOR.match(row):
            return []
        cells = [c.strip().strip('`').strip() for c in row.strip('|').split('|')]
        if len(cells) < 2 or not _FIELD.match(cells[0]) or cells[1].lower() not in ('no', 'optional'):
            return []
        fields.append(cells[0])
    return fields


def _schema_pr(finding, root):
    key = SCHEMA_SOURCES.get(finding.get('skill'))
    if key is None:
        return None
    fields = new_optional_fields(finding.get('evidence'))
    if not fields:
        return None
    try:
        original = _read(root, SCHEMA_PATH)
        schema = json.loads(original)
    except (OSError, ValueError):
        return None
    known = set(schema.get('properties') or {}) | set(schema.get('x-claude-only') or [])
    known |= set(((schema.get('properties') or {}).get('metadata') or {}).get('required') or [])
    added = [f for f in dict.fromkeys(fields) if f not in known]
    if not added:
        return None
    if key == 'x-claude-only':
        schema.setdefault('x-claude-only', []).extend(added)
    else:
        for field in added:
            schema.setdefault('properties', {})[field] = {'type': 'string'}
    text = json.dumps(schema, indent=2, ensure_ascii=False) + '\n'
    names = ', '.join('`%s`' % f for f in added)
    where = ('`x-claude-only` (Claude Code-only fields)' if key == 'x-claude-only'
             else '`properties` (optional string fields)')
    body = '\n\n'.join([
        '## What happened\n\nThe upstream source `%s` added the optional frontmatter field(s) %s.'
        % (finding['skill'], names),
        '## Evidence\n\n' + _fenced(finding.get('evidence')),
        '## Change\n\nAdds %s to %s of `%s`.' % (names, where, SCHEMA_PATH),
        '## Files\n\n- `%s`' % SCHEMA_PATH,
        '## Acceptance\n\n```\npython3 tools/lint.py\n```',
    ])
    return {'finding': finding, 'as': 'pr', 'labels': [LABEL_FRESHNESS], 'files': {SCHEMA_PATH: text},
            'body': body, 'title': _title('freshness: schema: ', 'add optional frontmatter field ' + ', '.join(added))}


# ---------------------------------------------------------------------------
# Issues


def _skill_issue(finding, root):
    held = is_held(finding, root)
    kind = finding['kind']
    what = _HINTS.get(kind, 'investigate; a no-code answer that names the fix is acceptable')
    fix = finding.get('fix')
    if held and (finding.get('skill') or '').startswith('sdd-'):
        what = ('this is a held mirror of an `sdd-*` skill, so its body changes only through a sync PR '
                'from the source repository (SDD-1). Make the change there: ' + what)
    elif held:
        what = 'this is a held skill and gets no bot PR. ' + what
    if fix:
        what += '.\n\nSuggested change on line %d of `%s`:\n\n%s' % (
            fix['line'], fix['file'], _fenced('- ' + fix['old'] + '\n+ ' + fix['new']))
    files = [finding['file']] if finding.get('file') else []
    body = '\n\n'.join([
        '## What happened\n\n`%s` finding in skill `%s`.' % (kind, finding.get('skill')),
        '## Evidence\n\n' + (finding.get('evidence') or 'none'),
        '## What to change\n\n' + what,
        '## Files\n\n' + ('\n'.join('- `%s`' % f for f in files) if files else 'none'),
        '## Acceptance\n\nThe next freshness run no longer reports this finding.',
    ])
    return {'finding': finding, 'as': 'issue', 'labels': [LABEL_FRESHNESS], 'files': {}, 'body': body,
            'title': _title('freshness: %s: ' % kind, finding.get('subject') or '')}


def _upstream_issue(finding, classification, unclassified_reason):
    if classification is None:
        affected = ('Not classified: %s. Check the diff against the skills by hand.'
                    % (unclassified_reason or 'no classification was made'))
    elif classification.get('affects'):
        affected = '\n'.join('- `%s`' % name for name in classification['affects'])
        affected += '\n\nReason: ' + (classification.get('reason') or 'none given')
    else:
        affected = 'None found. Reason: ' + (classification.get('reason') or 'none given')
    body = '\n\n'.join([
        '## What happened\n\nThe upstream source `%s` changed, and the change has no mechanical fix.'
        % finding.get('skill'),
        '## Affected skills\n\n' + affected,
        '## Diff\n\n' + _fenced(finding.get('evidence')),
        '## What to change\n\nDecide whether the listed skills need an update; a no-code answer '
        'that says why none is needed is acceptable.',
        '## Acceptance\n\nEach affected skill is updated through a PR, or the issue says why none is needed.',
    ])
    return {'finding': finding, 'as': 'issue', 'labels': [LABEL_UPSTREAM], 'files': {}, 'body': body,
            'title': _title('upstream-change: ', finding.get('skill') or '')}


def route(finding, root, models, classification=None, unclassified_reason=None, today=None, excluded=()):
    """The routed item for one Finding (FRS-12), or None for an excluded skill.

    `classification` and `unclassified_reason` come from freshness/classify.py
    and are used for non-mechanical `upstream-change` issues. `today` is the
    changelog date (default: today in UTC).
    """
    excluded = set(excluded)
    if finding.get('skill') in excluded:
        return None
    if classification is not None:
        # Excluded skills are never named (DESIGN 5.1), even by the classifier.
        reason = classification.get('reason') or ''
        if any(re.search(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', reason) for name in excluded):
            reason = 'withheld'
        classification = dict(classification, reason=reason, affects=[
            name for name in classification.get('affects') or () if name not in excluded])
    if finding['kind'] == 'upstream-change':
        item = _schema_pr(finding, root)
        return item or _upstream_issue(finding, classification, unclassified_reason)
    if not is_held(finding, root):
        built = _skill_pr(finding, root, models, _today(today))
        if built is not None:
            return _skill_pr_item(finding, *built)
    return _skill_issue(finding, root)
