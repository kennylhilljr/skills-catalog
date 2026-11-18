"""Per-skill static checks that need no local CLI (DESIGN 7.2).

Requirements: FRS-2 (deprecated model IDs), FRS-6 (missing paths), FRS-7
(description quality) and the "Frontmatter, Claude Code" and "Catalog
metadata" rows of DESIGN 7.2. Links (FRS-5) live in `freshness/links.py`.

Every function returns Findings as plain dicts (the shape of part 1):
`{"kind", "skill", "subject", "file", "line", "evidence", "fix"}`, where `fix`
is None or `{"file", "line", "old", "new"}`. `file` is repo-relative
(`skills/<name>/...` or `.held/<name>/...`); `subject` has no line number.
A `fix` means: on line `line` of `file`, replace the text `old` with `new`;
here `old` and `new` are the whole line before and after the change.

This module does no network or subprocess work, and importing it has no side
effects beyond loading `tools/lint.py` (as `tools/catalog.py` does).
"""

import importlib.util
import json
import math
import os
import re
import stat

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LINT_PATH = os.path.join(_ROOT, 'tools', 'lint.py')
_spec = importlib.util.spec_from_file_location('freshness_checks_lint', _LINT_PATH)
_lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lint)

SECTIONS = ('skills', '.held')
MODELS_PATH = os.path.join(_ROOT, 'freshness', 'models.json')

# FRS-7 collision threshold, tuned once from the issue's 0.35. Measured
# maximum pairwise score on the catalog (skills/ and .held/) when this was
# set: 0.479 (new-agent vs new-mcp); next highest 0.293. The default sits just
# above the maximum so the current catalog has no collision.
DEFAULT_THRESHOLD = 0.5
FIRST_SENTENCE_MAX = 200
LISTING_CAP = 1536

_USE_CLAUSE = re.compile(r'\bUse (when|after|before|for)\b')
_FIRST_SENTENCE_END = re.compile(r'[.?!] ')
_WORD = re.compile(r'[a-z0-9]+')

# A lint CAT-7 reference finding, reused by check_paths.
_LINT_REFERENCE = re.compile(r'^CAT-7: reference "(.+)" (does not resolve to a file|is outside skill directory)$')

STOP_WORDS = frozenset("""
a about above after again against all am an and any are as at be because been
before being below between both but by can could did do does doing down during
each few for from further had has have having he her here hers herself him
himself his how i if in into is it its itself just me more most my myself no
nor not now of off on once only or other our ours ourselves out over own same
she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours yourself
yourselves use used using
""".split())


def load_models(path=MODELS_PATH):
    """The model ID table (`freshness/models.json`)."""
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def finding(kind, skill, subject, file, line, evidence, fix=None):
    return {'kind': kind, 'skill': skill, 'subject': subject, 'file': file,
            'line': line, 'evidence': evidence, 'fix': fix}


def _rel_folder(folder):
    """`<section>/<name>` for a skill folder, from its last two path parts."""
    real = os.path.abspath(folder)
    return os.path.basename(os.path.dirname(real)) + '/' + os.path.basename(real)


def _rel_file(folder, path):
    rel = os.path.relpath(path, folder).replace(os.sep, '/')
    return _rel_folder(folder) + '/' + rel


def _is_regular(path):
    """True for a regular file that is not a symlink (lstat, like tools/catalog.py)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _read_text(path):
    """File text, or None for a binary, unreadable or non-regular file.

    Symlinks and special files are never read, so a link cannot pull in a file
    outside the skill folder or a device that never ends. A regular file is
    read whole, so no text file is skipped for its size.
    """
    if not _is_regular(path):
        return None
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError:
        return None
    if b'\0' in data[:8000]:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def iter_skills(root, excluded=()):
    """`[(section, name, folder)]` for every `skills/*` and `.held/*` with a SKILL.md.

    Names in `excluded` are skipped before their folder is touched (DESIGN 5.1).
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
            # A symlinked folder or SKILL.md is skipped, as in tools/catalog.py.
            if _is_real_dir(folder) and _is_regular(os.path.join(folder, 'SKILL.md')):
                out.append((section, name, folder))
    return out


def _model_files(folder):
    """SKILL.md plus every text file under scripts/, in a stable order."""
    files = [os.path.join(folder, 'SKILL.md')]
    scripts = os.path.join(folder, 'scripts')
    found = []
    if not _is_real_dir(scripts):
        return files
    for dirpath, dirnames, filenames in os.walk(scripts):
        dirnames.sort()
        for filename in sorted(filenames):
            found.append(os.path.join(dirpath, filename))
    return files + found


def _id_pattern(model_id):
    # Whole token, as in issue #65: `(?<![\w.-])id(?![\w-])`, plus one
    # narrowing: not followed by `.<digit>`, so `gpt-5` does not match inside
    # the longer ID `gpt-5.1`. `gpt-5.` at a sentence end and `gpt-5.json`
    # still match.
    return re.compile(r'(?<![\w.-])' + re.escape(model_id) + r'(?![\w-]|\.\d)')


def check_models(folder, skill, models):
    """FRS-2: deprecated or retired model IDs in SKILL.md and scripts/."""
    flagged = [(m['id'], m.get('status'), m.get('replacement'), _id_pattern(m['id']))
               for m in models if m.get('status') in ('deprecated', 'retired')]
    out = []
    for path in _model_files(folder):
        text = _read_text(path)
        if text is None:
            continue
        rel = _rel_file(folder, path)
        for number, line in enumerate(text.split('\n'), 1):
            for model_id, status, replacement, pattern in flagged:
                if not pattern.search(line):
                    continue
                if replacement:
                    evidence = '{}:{} mentions {} model ID {}; replacement: {}'.format(
                        rel, number, status, model_id, replacement)
                    fix = {'file': rel, 'line': number, 'old': line,
                           'new': pattern.sub(replacement, line)}
                else:
                    evidence = '{}:{} mentions {} model ID {}; no replacement listed'.format(
                        rel, number, status, model_id)
                    fix = None
                out.append(finding('deprecated-model', skill, rel + ':' + model_id,
                                   rel, number, evidence, fix))
    return out


def _lint_findings(folder):
    return _lint.lint_skill(folder)


def _line_of(text, needle):
    for number, line in enumerate(text.split('\n'), 1):
        if needle in line:
            return number
    return None


def check_paths(folder, skill):
    """FRS-6: relative references in SKILL.md that do not resolve inside the folder.

    Reuses `tools/lint.py`'s CAT-7 reference rule through `lint_skill`.
    """
    skill_md = os.path.join(folder, 'SKILL.md')
    text = _read_text(skill_md) or ''
    rel = _rel_file(folder, skill_md)
    out = []
    for message in _lint_findings(folder):
        m = _LINT_REFERENCE.match(message)
        if not m:
            continue
        ref = m.group(1)
        reason = 'is missing' if m.group(2).startswith('does not') else 'resolves outside the skill folder'
        out.append(finding('missing-path', skill, rel + ':' + ref, rel, _line_of(text, ref),
                           '{} references {}, which {}'.format(rel, ref, reason)))
    return sorted(out, key=lambda f: f['subject'])


def _stem(word):
    for suffix in ('ing', 'ed', 'es', 's', 'ly'):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def _terms(text):
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in STOP_WORDS]


def _tfidf(docs):
    """Unit-length TF-IDF vectors (dicts) for a list of term lists."""
    n = len(docs)
    df = {}
    for terms in docs:
        for term in set(terms):
            df[term] = df.get(term, 0) + 1
    vectors = []
    for terms in docs:
        tf = {}
        for term in terms:
            tf[term] = tf.get(term, 0) + 1
        vec = {t: c * (math.log((1 + n) / (1 + df[t])) + 1) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        vectors.append({t: v / norm for t, v in vec.items()} if norm else {})
    return vectors


def _cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def similarities(descriptions):
    """`{(a, b): score}` for every pair of names in `{name: description}`, a < b."""
    names = sorted(descriptions)
    vectors = _tfidf([_terms(descriptions[n]) for n in names])
    out = {}
    for i, a in enumerate(names):
        for j in range(i + 1, len(names)):
            out[(a, names[j])] = _cosine(vectors[i], vectors[j])
    return out


def first_sentence(text):
    m = _FIRST_SENTENCE_END.search(text)
    return text[:m.start() + 1] if m else text


def check_description(skills, threshold=DEFAULT_THRESHOLD):
    """FRS-7 over `skills`, a list of `(name, description, file)`.

    `file` is the repo-relative SKILL.md path. A collision is reported once,
    on the alphabetically first skill, naming the other.
    """
    out = []
    by_name = {}
    for name, desc, file in skills:
        desc = desc if isinstance(desc, str) else ''
        by_name[name] = (desc, file)
        if not _USE_CLAUSE.search(desc):
            out.append(finding('description', name, file + ':use-clause', file, None,
                               '{}: description has no "Use when/after/before/for" clause'.format(file)))
        first = first_sentence(desc)
        if len(first) > FIRST_SENTENCE_MAX:
            out.append(finding('description', name, file + ':first-sentence', file, None,
                               '{}: first sentence of the description is {} characters (max {})'.format(
                                   file, len(first), FIRST_SENTENCE_MAX)))
    scores = similarities({n: d for n, (d, _f) in by_name.items()})
    for (a, b), score in sorted(scores.items()):
        if score > threshold:
            file = by_name[a][1]
            out.append(finding('description', a, file + ':collision:' + b, file, None,
                               '{}: description of {} collides with {} (score {:.3f} > {})'.format(
                                   file, a, b, score, threshold)))
    return out


def read_description(folder):
    """The frontmatter description of a skill, or '' when it cannot be parsed."""
    text = _read_text(os.path.join(folder, 'SKILL.md')) or ''
    try:
        frontmatter, _body = _lint.parse_frontmatter(text)
    except ValueError:
        return ''
    desc = frontmatter.get('description')
    return desc if isinstance(desc, str) else ''


def catalog_descriptions(root, excluded=()):
    """`[(name, description, file)]` for `check_description` over a catalog root."""
    return [(name, read_description(folder), section + '/' + name + '/SKILL.md')
            for section, name, folder in iter_skills(root, excluded)]


def check_frontmatter(folder, skill, field_table=None, listing_cap=LISTING_CAP):
    """Frontmatter (Claude Code) and catalog metadata rows of DESIGN 7.2.

    Every `lint_skill` finding becomes kind `frontmatter` (CAT-7 references
    and the CAT-3 use clause also appear as `missing-path` and `description`
    findings, with their own subjects).
    """
    skill_md = os.path.join(folder, 'SKILL.md')
    rel = _rel_file(folder, skill_md)
    out = []
    for message in _lint_findings(folder):
        out.append(finding('frontmatter', skill, rel + ':' + message, rel, None,
                           '{}: {}'.format(rel, message)))
    text = _read_text(skill_md)
    frontmatter = None
    if text is not None:
        try:
            frontmatter, _body = _lint.parse_frontmatter(text)
        except ValueError:
            frontmatter = None
    if frontmatter is None:
        return out
    if field_table is not None:
        allowed = set(field_table)
        for key in frontmatter:
            if key not in allowed:
                out.append(finding('frontmatter', skill, rel + ':field:' + key, rel,
                                   _line_of(text, key + ':'),
                                   '{}: top-level key "{}" is not in the Claude Code field table'.format(rel, key)))
    desc = frontmatter.get('description')
    if isinstance(desc, str) and len(desc) > listing_cap:
        out.append(finding('frontmatter', skill, rel + ':listing-cap', rel, None,
                           '{}: description is {} characters, above the listing cap of {}'.format(
                               rel, len(desc), listing_cap)))
    return out
