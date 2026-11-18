"""Rules-first classifier for upstream diffs, with a daily call cap (SPEC FRS-21; DESIGN 7.1, 7.7).

`classify(diff, skills, state, call, today)` maps an upstream diff to the
skills it affects. Rules run first: a changed diff line that names a model
ID, a tool name or a frontmatter key found in a skill marks that skill.
Only when the rules find nothing is `call(prompt) -> str` used: the Mac job
passes a `claude -p` runner, the Actions half passes None.

`state["classifier"] = {"day": "YYYY-MM-DD", "calls": int}` counts the calls
of one day. At CALLS_PER_DAY calls `classify` returns None without calling,
and the router (freshness/route.py) opens an unclassified `upstream-change`
issue that says why (`unclassified_reason`).

`skills` maps a skill folder name to its SKILL.md text. The caller drops
excluded skills (DESIGN 5.1) before passing them; `excluded` is dropped here
as well. Python 3.9+, standard library only. Importing has no side effects.
"""

import datetime
import json
import re

CALLS_PER_DAY = 10
DIFF_PROMPT_MAX = 8000
REASON_MAX = 500

_MODEL_ID = re.compile(r'(?<![\w.-])(?:claude-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|gpt-[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|o[1-9](?:-[a-z0-9]+)*)(?![\w-]|\.\d)')
_BACKTICK = re.compile(r'`([A-Za-z][A-Za-z0-9_-]{1,63})`')
_TOOL = re.compile(r'^[A-Z][A-Za-z0-9]+$')
_KEY_LINE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_-]*):')


def _day(today):
    if isinstance(today, (datetime.date, datetime.datetime)):
        return today.isoformat()[:10]
    return str(today)[:10]


def _frontmatter(text):
    lines = (text or '').split('\n')
    if not lines or lines[0] != '---':
        return []
    for index in range(1, len(lines)):
        if lines[index] == '---':
            return lines[1:index]
    return []


def _description(text):
    for line in _frontmatter(text):
        if line.startswith('description:'):
            return line[len('description:'):].strip().strip('"\'>-').strip()
    return ''


def vocabulary(text):
    """`(model IDs, names)` a skill uses; names are tool names and frontmatter keys."""
    models = set(_MODEL_ID.findall(text or ''))
    names = set()
    for line in _frontmatter(text):
        m = _KEY_LINE.match(line)
        if m:
            names.add(m.group(1))
        if line.startswith('allowed-tools:'):
            for tool in re.split(r'[\s,]+', line[len('allowed-tools:'):]):
                tool = tool.strip('"\'').split('(', 1)[0]
                if _TOOL.match(tool):
                    names.add(tool)
    for word in _BACKTICK.findall(text or ''):
        if _TOOL.match(word):
            names.add(word)
    return models, names


def changed_lines(diff):
    """Added and removed lines of a unified diff, without the +/- mark."""
    out = []
    for line in (diff or '').split('\n'):
        if line.startswith(('+++', '---')):
            continue
        if line.startswith(('+', '-')):
            out.append(line[1:])
    return out


def _mentions(lines, word):
    """A model ID anywhere as a whole token."""
    pattern = re.compile(r'(?<![\w.-])' + re.escape(word) + r'(?![\w-]|\.\d)')
    return any(pattern.search(line) for line in lines)


def _names(lines, word):
    """A tool name or key only in key form: `word`, word: or a whole table cell.

    Plain prose words such as "name" or "Read" would otherwise mark every skill.
    """
    w = re.escape(word)
    pattern = re.compile(r'`' + w + r'`|(?<![\w-])' + w + r':|(?:^|\|)\s*' + w + r'\s*(?:\||$)')
    return any(pattern.search(line) for line in lines)


def rules(diff, skills):
    """`{"affects", "reason"}` from the rules, or None when nothing matched."""
    lines = changed_lines(diff)
    affects, why = [], {}
    for name in sorted(skills):
        models, names = vocabulary(skills[name])
        hits = sorted({w for w in models if _mentions(lines, w)} | {w for w in names if _names(lines, w)})
        if hits:
            affects.append(name)
            for word in hits:
                why.setdefault(word, []).append(name)
    if not affects:
        return None
    reason = 'rules: the diff names ' + '; '.join(
        '`%s` (used by %s)' % (word, ', '.join(names)) for word, names in sorted(why.items()))
    return {'affects': affects, 'reason': reason[:REASON_MAX]}


def _counter(state, today):
    entry = state.setdefault('classifier', {'day': '', 'calls': 0})
    day = _day(today)
    if entry.get('day') != day:
        entry['day'] = day
        entry['calls'] = 0
    return entry


def capped(state, today):
    """True when today's calls have reached the cap."""
    entry = (state or {}).get('classifier') or {}
    return entry.get('day') == _day(today) and int(entry.get('calls') or 0) >= CALLS_PER_DAY


def unclassified_reason(state, call, today):
    """Why `classify` returned None, for the unclassified issue body."""
    if capped(state, today):
        return ('the classifier cap of %d calls a day was reached (FRS-21), so the affected '
                'skills are not known' % CALLS_PER_DAY)
    if call is None:
        return 'the classifier runs only in the Mac job, and the rules matched no skill'
    return 'the classifier call failed or its reply was not understood'


def prompt(diff, skills):
    listing = '\n'.join('- %s: %s' % (name, _description(skills[name])[:300]) for name in sorted(skills))
    return (
        'An upstream source watched by the skills catalog changed. Decide which skills the '
        'change affects. Reply with only a JSON object: {"affects": [skill names], '
        '"reason": "one sentence"}. Use an empty list when no skill is affected.\n\n'
        'Skills:\n' + listing + '\n\nDiff:\n' + (diff or '')[:DIFF_PROMPT_MAX] + '\n')


def _parse(reply, names):
    text = reply or ''
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get('affects'), list):
        return None
    affects = sorted({a for a in data['affects'] if isinstance(a, str) and a in names})
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    return {'affects': affects, 'reason': ('classifier: ' + ' '.join(reason.split()))[:REASON_MAX]}


def classify(diff, skills, state, call, today, excluded=()):
    """`{"affects": [names], "reason": str}` or None (FRS-21). Updates `state["classifier"]`."""
    skills = {name: text for name, text in (skills or {}).items() if name not in set(excluded)}
    found = rules(diff, skills)
    if found is not None:
        return found
    entry = _counter(state, today)
    if call is None or entry['calls'] >= CALLS_PER_DAY:
        return None
    entry['calls'] += 1
    try:
        reply = call(prompt(diff, skills))
    except Exception:
        return None
    return _parse(reply, set(skills))
