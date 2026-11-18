"""Temp catalog repositories for the server tests. M4 parts 2 to 4 reuse these.

Every repository lives under the temp folder the caller passes in. Git runs
with an empty global config, so a test never reads or writes the real home.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

_DAY = 86400
_BASE_TIME = 4000000000  # a fixed date, so tag order never depends on the clock


def git_env(stamp=_BASE_TIME):
    env = dict(os.environ)
    who = 'catalog-test'
    mail = who + '@' + 'example.invalid'
    date = '%d +0000' % stamp
    env.update({
        'GIT_CONFIG_GLOBAL': os.devnull,
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_AUTHOR_NAME': who,
        'GIT_AUTHOR_EMAIL': mail,
        'GIT_COMMITTER_NAME': who,
        'GIT_COMMITTER_EMAIL': mail,
        'GIT_AUTHOR_DATE': date,
        'GIT_COMMITTER_DATE': date,
    })
    return env


def git(repo, *args, stamp=_BASE_TIME):
    """Run git in repo with the test identity and return its stdout as text."""
    proc = subprocess.run(
        ['git', '-C', os.fspath(repo), *args],
        env=git_env(stamp),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.decode('utf-8')


def skill_md(name, description=None):
    description = description or 'Use when testing the catalog server with %s.' % name
    return (
        '---\n'
        'name: %s\n'
        'description: %s\n'
        'metadata:\n'
        '  catalog-version: "1.0.0"\n'
        '---\n\n'
        '# %s\n\nBody of %s.\n'
    ) % (name, description, name, name)


def _as_bytes(content):
    return content if isinstance(content, bytes) else content.encode('utf-8')


def _normalize(skills):
    """Accept a list of names or a dict name -> {relpath: content}; add SKILL.md if missing."""
    if skills is None:
        return {}
    if not isinstance(skills, dict):
        skills = {name: {} for name in skills}
    out = {}
    for name, files in skills.items():
        files = dict(files or {})
        files.setdefault('SKILL.md', skill_md(name))
        out[name] = {rel: _as_bytes(c) for rel, c in files.items()}
    return out


def _entry(section, name, files, consumers):
    return {
        'name': name,
        'path': '%s/%s' % (section, name),
        'version': '1.0.0',
        'owner': 'catalog-test',
        'targets': ['claude', 'codex'],
        'consumers': consumers,
        'last_verified': '2099-01-01',
        'files': {rel: hashlib.sha256(files[rel]).hexdigest() for rel in sorted(files)},
    }


def catalog_json(skills, held=None):
    """Return catalog.json text in the shape tools/catalog.py writes."""
    skills = _normalize(skills)
    held = _normalize(held)
    manifest = {
        'version': 1,
        'skills': [_entry('skills', n, skills[n], 'all') for n in sorted(skills)],
        'held': [_entry('.held', n, held[n], 'none') for n in sorted(held)],
    }
    return json.dumps(manifest, indent=2) + '\n'


def write_files(repo, files):
    """Write {repo-relative path: str or bytes} into repo."""
    for rel, content in files.items():
        full = os.path.join(os.fspath(repo), *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as handle:
            handle.write(_as_bytes(content))


def commit_all(repo, message, tag=None, stamp=_BASE_TIME):
    """Commit every change in repo and optionally tag it (annotated)."""
    git(repo, 'add', '-A', stamp=stamp)
    git(repo, 'commit', '-q', '--allow-empty', '-m', message, stamp=stamp)
    if tag:
        git(repo, 'tag', '-a', tag, '-m', tag, stamp=stamp)
    return git(repo, 'rev-parse', 'HEAD', stamp=stamp).strip()


def make_catalog_repo(tmp, skills=('alpha-skill', 'beta-skill'), held=None, extra_files=None,
                      tag='v2099.1.1.1', manifest=None):
    """Build a git repo under tmp with skills/, .held/, catalog.json and a tag; return its path.

    skills and held are lists of names or dicts name -> {relpath: content}.
    extra_files maps repo-relative paths to content and is written after the
    manifest, so it can add files the manifest does not list. manifest
    replaces the generated catalog.json text. tag=None leaves the repo untagged.
    """
    repo = os.path.join(os.fspath(tmp), 'catalog')
    os.makedirs(repo)
    git(repo, 'init', '-q', '-b', 'main')
    files = {}
    for name, content in _normalize(skills).items():
        for rel, data in content.items():
            files['skills/%s/%s' % (name, rel)] = data
    for name, content in _normalize(held).items():
        for rel, data in content.items():
            files['.held/%s/%s' % (name, rel)] = data
    files['catalog.json'] = manifest if manifest is not None else catalog_json(skills, held)
    files.update(extra_files or {})
    write_files(repo, files)
    commit_all(repo, 'catalog', tag=tag)
    return repo


def next_stamp(n):
    """A commit time n days after the base, for later commits and tags."""
    return _BASE_TIME + n * _DAY
