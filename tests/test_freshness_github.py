"""Tests for the GitHub writer, the finding router and the classifier cap.

Requirements: INV-5, FRS-12, FRS-21 (docs/SPEC.md). No test touches the
network: GitHub is a fake transport that records every request.
"""

import datetime
import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest

from freshness import alerts
from freshness import classify as cmod
from freshness import finding as fmod
from freshness import github as gmod
from freshness import route as rmod

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)
# Built at runtime so the repo never holds a token-like literal.
TOKEN = 'test' + '-token-' + 'abc123'
FP = fmod.fingerprint('deprecated-model', 'skills/demo/SKILL.md:old-model')


class FakeGitHub:
    """In-memory GitHub API: refs, objects, issues and pulls. Records requests."""

    def __init__(self, default_branch='main'):
        self.requests = []
        self.default_branch = default_branch
        self.objects = {}
        self.issues = {}
        self.next_number = 1
        self.conflicts = 0      # PATCHes of the state ref to reject
        self.error_message = None
        self.label_failures = 0
        base_tree = self._put({'type': 'tree', 'tree': [
            {'path': 'skills/demo/SKILL.md', 'mode': '100644', 'type': 'blob', 'sha': 'b' * 40},
            {'path': 'skills/demo/scripts/run.sh', 'mode': '100755', 'type': 'blob', 'sha': 'c' * 40}]})
        main = self._put({'type': 'commit', 'tree': base_tree, 'parents': []})
        self.refs = {'refs/heads/' + default_branch: main, 'refs/heads/main': main}

    def _put(self, obj):
        sha = hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        self.objects[sha] = obj
        return sha

    def writes(self):
        return [r for r in self.requests if r[0] != 'GET']

    def __call__(self, method, path, body):
        self.requests.append((method, path, body))
        if self.error_message is not None:
            return 500, {'message': self.error_message}
        prefix = '/repos/o/r'
        assert path.startswith(prefix), path
        rel, _, query = path[len(prefix):].partition('?')
        if method == 'GET' and rel == '':
            return 200, {'default_branch': self.default_branch}
        m = re.match(r'^/git/ref/(heads/.+)$', rel)
        if method == 'GET' and m:
            ref = 'refs/' + m.group(1)
            if ref not in self.refs:
                return 404, {'message': 'Not Found'}
            return 200, {'ref': ref, 'object': {'sha': self.refs[ref]}}
        m = re.match(r'^/git/commits/(\w+)$', rel)
        if method == 'GET' and m:
            obj = self.objects[m.group(1)]
            return 200, {'sha': m.group(1), 'tree': {'sha': obj['tree']}}
        m = re.match(r'^/git/trees/(\w+)$', rel)
        if method == 'GET' and m:
            return 200, {'tree': self.objects[m.group(1)]['tree']}
        m = re.match(r'^/git/blobs/(\w+)$', rel)
        if method == 'GET' and m:
            return 200, {'content': self.objects[m.group(1)]['content'], 'encoding': 'utf-8'}
        if method == 'POST' and rel == '/git/blobs':
            return 201, {'sha': self._put({'type': 'blob', 'content': body['content']})}
        if method == 'POST' and rel == '/git/trees':
            entries = {}
            if body.get('base_tree'):
                entries = {e['path']: e for e in self.objects[body['base_tree']]['tree']}
            for e in body['tree']:
                entries[e['path']] = e
            return 201, {'sha': self._put({'type': 'tree', 'tree': [entries[k] for k in sorted(entries)]})}
        if method == 'POST' and rel == '/git/commits':
            return 201, {'sha': self._put({'type': 'commit', 'tree': body['tree'], 'parents': body['parents'],
                                           'message': body['message']})}
        if method == 'POST' and rel == '/git/refs':
            if body['ref'] in self.refs:
                return 422, {'message': 'Reference already exists'}
            self.refs[body['ref']] = body['sha']
            return 201, {'ref': body['ref']}
        m = re.match(r'^/git/(refs/heads/.+)$', rel)
        if method == 'PATCH' and m:
            ref = m.group(1)
            if ref == 'refs/heads/freshness-state' and self.conflicts:
                self.conflicts -= 1
                return 422, {'message': 'Update is not a fast forward'}
            if not body.get('force') and self.refs.get(ref) not in self.objects[body['sha']]['parents']:
                return 422, {'message': 'Update is not a fast forward'}
            self.refs[ref] = body['sha']
            return 200, {'ref': ref}
        if method == 'POST' and rel in ('/issues', '/pulls'):
            number = self.next_number
            self.next_number += 1
            item = dict(body, number=number, state='open', closed_at=None,
                        user={'login': 'freshness[bot]', 'type': 'Bot'}, author_association='NONE',
                        html_url='https://github.com/o/r/%s/%d' % ('pull' if rel == '/pulls' else 'issues', number))
            if rel == '/pulls':
                item['pull_request'] = {}
                item['head'] = {'ref': body['head'], 'repo': {'full_name': 'o/r'}}
            self.issues[number] = item
            return 201, item
        m = re.match(r'^/pulls/(\d+)$', rel)
        if method == 'GET' and m:
            return 200, self.issues[int(m.group(1))]
        m = re.match(r'^/issues/(\d+)$', rel)
        if method == 'PATCH' and m:
            self.issues[int(m.group(1))]['state'] = body['state']
            return 200, self.issues[int(m.group(1))]
        if method == 'POST' and re.match(r'^/issues/\d+/labels$', rel) and self.label_failures:
            self.label_failures -= 1
            return 502, {'message': 'Bad Gateway'}
        if method == 'POST' and re.match(r'^/issues/\d+/(comments|labels)$', rel):
            return 201, {}
        if method == 'GET' and rel == '/issues':
            params = dict(p.split('=', 1) for p in query.split('&'))
            items = [i for i in self.issues.values() if i['state'] == params['state']]
            if params.get('page') != '1':
                items = []
            return 200, items
        return 404, {'message': 'unhandled ' + method + ' ' + rel}

    def tree_of(self, ref):
        tree = self.objects[self.objects[self.refs[ref]]['tree']]['tree']
        return {e['path']: e for e in tree}


def make_github(default_branch='main'):
    fake = FakeGitHub(default_branch)
    return gmod.GitHub('o/r', TOKEN, transport=fake, bot_login='freshness[bot]'), fake


SKILL = '''---
name: demo
description: Demo skill for router tests. Use when testing the freshness router.
allowed-tools: Bash Read
metadata:
  catalog-version: "1.2.3"
  catalog-owner: "kennylhilljr"
  catalog-provenance: "own"
  catalog-targets: "claude,codex"
  catalog-last-verified: "2026-11-11"
  catalog-max-age-days: "90"
---

Call the API with model old-model for a summary.
Docs live at https://example.com/old-docs for reference.
Use the `Bash` tool to run checks.

## Changelog

| Version | Date | Change |
| --- | --- | --- |
| 1.2.3 | 2026-11-11 | Imported at `abcdef1` |
'''

MODELS = [{'id': 'old-model', 'vendor': 'anthropic', 'status': 'deprecated', 'replacement': 'new-model'}]


class RootMixin:
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        for folder, text in (('skills/demo', SKILL), ('.held/sdd-demo', SKILL.replace('name: demo', 'name: sdd-demo'))):
            os.makedirs(os.path.join(self.root, folder))
            with open(os.path.join(self.root, folder, 'SKILL.md'), 'w', encoding='utf-8') as handle:
                handle.write(text)
        os.makedirs(os.path.join(self.root, 'schema'))
        shutil.copy(os.path.join(REPO_ROOT, 'schema', 'frontmatter.schema.json'),
                    os.path.join(self.root, 'schema', 'frontmatter.schema.json'))
        with open(os.path.join(self.root, 'catalog.json'), 'w', encoding='utf-8') as handle:
            handle.write(rmod._catalog_module().render(self.root))

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding='utf-8') as handle:
            return handle.read()


def model_finding(skill='demo', section='skills'):
    rel = section + '/' + skill + '/SKILL.md'
    old = 'Call the API with model old-model for a summary.'
    return {'kind': 'deprecated-model', 'skill': skill, 'subject': rel + ':old-model', 'file': rel, 'line': 14,
            'evidence': rel + ':14 mentions deprecated model ID old-model; replacement: new-model',
            'fix': {'file': rel, 'line': 14, 'old': old, 'new': old.replace('old-model', 'new-model')}}


def changed(old, new):
    return [line for line in difflib.ndiff(old.split('\n'), new.split('\n')) if line[:2] in ('- ', '+ ')]


# ---------------------------------------------------------------------------
# INV-5


class Inv5Test(unittest.TestCase):
    def test_write_to_main_is_refused_and_sends_nothing(self):
        gh, fake = make_github()
        with self.assertRaises(gmod.RefusedWrite):
            gh._request('POST', '/git/refs', {'ref': 'refs/heads/main', 'sha': 'a' * 40})
        with self.assertRaises(gmod.RefusedWrite):
            gh._request('PATCH', '/git/refs/heads/main', {'sha': 'a' * 40, 'force': True})
        self.assertEqual(fake.requests, [])

    def test_default_branch_under_another_name_is_refused(self):
        gh, fake = make_github(default_branch='trunk')
        with self.assertRaises(gmod.RefusedWrite):
            gh._request('PATCH', '/git/refs/heads/trunk', {'sha': 'a' * 40, 'force': True})
        # A default branch that even looks like a freshness branch is refused.
        gh, fake = make_github(default_branch='freshness/0123456789ab')
        with self.assertRaises(gmod.RefusedWrite):
            gh._request('POST', '/git/refs', {'ref': 'refs/heads/freshness/0123456789ab', 'sha': 'a' * 40})
        self.assertEqual(fake.writes(), [])
        self.assertEqual([r[1] for r in fake.requests], ['/repos/o/r'])

    def test_tag_and_other_refs_are_refused(self):
        gh, fake = make_github()
        for ref in ('refs/tags/v2026.11.18.1', 'refs/heads/feature', 'refs/heads/freshness/XYZ',
                    'refs/heads/freshness/0123456789abc', 'refs/heads/freshness-state2'):
            with self.assertRaises(gmod.RefusedWrite, msg=ref):
                gh._check_ref(ref)
            with self.assertRaises(gmod.RefusedWrite, msg=ref):
                gh._request('POST', '/git/refs', {'ref': ref, 'sha': 'a' * 40})
        self.assertEqual(fake.requests, [])

    def test_merge_and_contents_writes_are_refused(self):
        gh, fake = make_github()
        self.assertFalse(any('merge' in name.lower() for name in dir(gh)))
        for method, path in (('PUT', '/pulls/3/merge'), ('POST', '/merges'),
                             ('PUT', '/contents/skills/demo/SKILL.md'), ('DELETE', '/contents/README.md'),
                             ('DELETE', '/git/refs/heads/freshness/0123456789ab'), ('PUT', '/collaborators/x')):
            with self.assertRaises(gmod.RefusedWrite, msg=path):
                gh._request(method, path, {'branch': 'main'})
        self.assertEqual(fake.requests, [])

    def test_open_pr_writes_only_its_freshness_branch(self):
        gh, fake = make_github()
        main_before = fake.refs['refs/heads/main']
        action = {'op': 'open_pr', 'fingerprint': FP, 'title': 't', 'body': 'b', 'labels': ['freshness'],
                  'files': {'skills/demo/scripts/run.sh': 'echo new\n'}, 'branch': fmod.branch_name(FP)}
        results = gh.apply([action])
        self.assertTrue(results[0]['ok'], results)
        ref_writes = [r for r in fake.writes() if '/git/refs' in r[1]]
        self.assertEqual(len(ref_writes), 1)
        self.assertEqual(ref_writes[0][2]['ref'], 'refs/heads/freshness/' + FP[:12])
        pulls = [r for r in fake.writes() if r[1].endswith('/pulls')]
        self.assertEqual(pulls[0][2]['base'], 'main')
        self.assertEqual(pulls[0][2]['head'], 'freshness/' + FP[:12])
        self.assertEqual(fake.refs['refs/heads/main'], main_before)
        tree = fake.tree_of('refs/heads/freshness/' + FP[:12])
        self.assertEqual(tree['skills/demo/scripts/run.sh']['mode'], '100755')
        self.assertIn('skills/demo/SKILL.md', tree)
        commit = fake.objects[fake.refs['refs/heads/freshness/' + FP[:12]]]
        self.assertEqual(commit['parents'], [main_before])
        self.assertIn('FRS-12', commit['message'])

    def test_update_pr_moves_only_its_own_branch(self):
        gh, fake = make_github()
        branch = fmod.branch_name(FP)
        gh.apply([{'op': 'open_pr', 'fingerprint': FP, 'title': 't', 'body': 'b', 'labels': [],
                   'files': {'a.txt': '1\n'}, 'branch': branch}])
        results = gh.apply([{'op': 'update_pr', 'fingerprint': FP, 'number': 1,
                             'files': {'a.txt': '2\n'}, 'branch': branch}])
        self.assertTrue(results[0]['ok'], results)
        other = fmod.fingerprint('dead-link', 'x')
        fake.issues[1]['head']['ref'] = fmod.branch_name(other)
        with self.assertRaises(gmod.RefusedWrite):
            gh.apply([{'op': 'update_pr', 'fingerprint': FP, 'number': 1,
                       'files': {'a.txt': '3\n'}, 'branch': branch}])

    def test_write_state_creates_orphan_once_then_updates(self):
        gh, fake = make_github()
        gh.write_state('{"a": 1}\n')
        first = fake.refs['refs/heads/freshness-state']
        self.assertEqual(fake.objects[first]['parents'], [])
        self.assertEqual(sorted(fake.tree_of('refs/heads/freshness-state')), [gmod.STATE_PATH])
        gh.write_state('{"a": 2}\n')
        second = fake.refs['refs/heads/freshness-state']
        self.assertEqual(fake.objects[second]['parents'], [first])
        creates = [r for r in fake.writes() if r[1].endswith('/git/refs')]
        self.assertEqual(len(creates), 1)
        self.assertEqual(gh.read_state(), ('{"a": 2}\n', second))

    def test_write_state_retries_once_on_conflict(self):
        gh, fake = make_github()
        gh.write_state('1\n')
        fake.conflicts = 1
        gh.write_state('2\n')
        self.assertEqual(gh.read_state()[0], '2\n')
        fake.conflicts = 2
        with self.assertRaises(gmod.GitHubError):
            gh.write_state('3\n')
        self.assertEqual(gh.read_state()[0], '2\n')

    def test_token_never_in_errors(self):
        gh, fake = make_github()
        fake.error_message = 'bad credentials for ' + TOKEN
        with self.assertRaises(gmod.GitHubError) as ctx:
            gh.list_bot_items(now=NOW)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, repr(gh))
        results = gh.apply([{'op': 'comment', 'fingerprint': FP, 'number': 1, 'body': 'x'}])
        self.assertFalse(results[0]['ok'])
        self.assertNotIn(TOKEN, json.dumps(results))

        def broken(method, path, body):
            raise RuntimeError('header Authorization: Bearer ' + TOKEN)
        gh = gmod.GitHub('o/r', TOKEN, transport=broken)
        with self.assertRaises(gmod.GitHubError) as ctx:
            gh.default_branch()
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_apply_fills_issue_url_and_keeps_fingerprint(self):
        gh, fake = make_github()
        afp = fmod.fingerprint('canary', 'nightly')
        actions = [
            {'op': 'open_issue', 'fingerprint': afp, 'title': 't', 'body': 'b', 'labels': ['freshness-alert']},
            {'op': 'send_ops', 'fingerprint': afp, 'text': 'skills-catalog freshness-alert: canary nightly: '
                                                         + alerts.ISSUE_URL_PLACEHOLDER},
            {'op': 'send_ops', 'fingerprint': FP, 'text': 'x: ' + alerts.ISSUE_URL_PLACEHOLDER},
        ]
        results = gh.apply(actions)
        self.assertEqual(results[1]['text'], 'skills-catalog freshness-alert: canary nightly: '
                                             'https://github.com/o/r/issues/1')
        self.assertEqual(results[1]['fingerprint'], afp)
        self.assertTrue(results[1]['ok'])
        self.assertFalse(results[2]['ok'])
        self.assertEqual(gmod.ISSUE_URL_PLACEHOLDER, alerts.ISSUE_URL_PLACEHOLDER)

    def test_list_bot_items_keeps_marked_items_only(self):
        gh, fake = make_github()
        gh.apply([{'op': 'open_issue', 'fingerprint': FP, 'title': 't',
                   'body': 'x\n' + fmod.marker(FP) + '\n', 'labels': []}])
        bot = {'login': 'freshness[bot]', 'type': 'Bot'}
        fake.issues[2] = {'number': 2, 'state': 'open', 'body': 'no marker', 'closed_at': None, 'user': bot}
        # Anyone else copying a marker, another App included, does not suppress the finding.
        fake.issues[5] = {'number': 5, 'state': 'open', 'body': fmod.marker(fmod.fingerprint('canary', 'x')),
                          'closed_at': None, 'user': {'login': 'someone', 'type': 'User'}}
        fake.issues[6] = {'number': 6, 'state': 'open', 'body': fmod.marker(fmod.fingerprint('canary', 'y')),
                          'closed_at': None, 'user': {'login': 'other-app[bot]', 'type': 'Bot'}}
        old_fp = fmod.fingerprint('dead-link', 'old')
        fake.issues[3] = {'number': 3, 'state': 'closed', 'body': fmod.marker(old_fp),
                          'closed_at': '2026-10-01T00:00:00Z', 'pull_request': {}, 'user': bot}
        new_fp = fmod.fingerprint('dead-link', 'new')
        fake.issues[4] = {'number': 4, 'state': 'closed', 'body': fmod.marker(new_fp),
                          'closed_at': '2026-11-17T00:00:00Z', 'pull_request': {}, 'user': bot}
        items = gh.list_bot_items(now=NOW)
        self.assertEqual([(i['number'], i['fingerprint'], i['is_pr']) for i in items],
                         [(1, FP, False), (4, new_fp, True)])
        self.assertTrue(all(r[0] == 'GET' for r in fake.requests[1:]))
        gh.bot_login = None
        with self.assertRaises(ValueError):
            gh.list_bot_items(now=NOW)

    def test_open_pr_closes_unlabeled_pr_for_retry(self):
        gh, fake = make_github()
        action = {'op': 'open_pr', 'fingerprint': FP, 'title': 't', 'body': 'b', 'labels': ['freshness'],
                  'files': {'a.txt': '1\n'}, 'branch': fmod.branch_name(FP)}
        fake.label_failures = 2
        result = gh.apply([action])[0]
        self.assertFalse(result['ok'])
        self.assertEqual(fake.issues[1]['state'], 'closed')
        result = gh.apply([action])[0]   # the next run opens it again on the same branch
        self.assertTrue(result['ok'], result)
        self.assertEqual(fake.issues[2]['head']['ref'], fmod.branch_name(FP))

    def test_open_pr_retries_labels_once(self):
        gh, fake = make_github()
        action = {'op': 'open_pr', 'fingerprint': FP, 'title': 't', 'body': 'b', 'labels': ['freshness'],
                  'files': {'a.txt': '1\n'}, 'branch': fmod.branch_name(FP)}
        fake.label_failures = 1
        result = gh.apply([action])[0]
        self.assertTrue(result['ok'])
        self.assertNotIn('label_error', result)
        self.assertEqual(len([r for r in fake.writes() if r[1].endswith('/labels')]), 2)


# ---------------------------------------------------------------------------
# FRS-12


class RouteTest(RootMixin, unittest.TestCase):
    def test_deprecated_model_pr_changes_only_fix_version_changelog_catalog(self):
        item = rmod.route(model_finding(), self.root, MODELS, today='2026-11-18')
        self.assertEqual(item['as'], 'pr')
        self.assertEqual(item['labels'], ['freshness'])
        self.assertEqual(sorted(item['files']), ['catalog.json', 'skills/demo/SKILL.md'])
        lines = changed(self.read('skills/demo/SKILL.md'), item['files']['skills/demo/SKILL.md'])
        self.assertEqual(len(lines), 5, lines)
        self.assertIn('-   catalog-version: "1.2.3"', lines)
        self.assertIn('+   catalog-version: "1.2.4"', lines)
        self.assertIn('- Call the API with model old-model for a summary.', lines)
        self.assertIn('+ Call the API with model new-model for a summary.', lines)
        rows = [l for l in lines if l.startswith('+ | 1.2.4 | 2026-11-18 |')]
        self.assertEqual(len(rows), 1)
        self.assertIn('new-model', rows[0])
        # catalog.json is the regenerated manifest of the changed tree.
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(os.path.join(self.root, 'skills'), os.path.join(tmp, 'skills'))
            shutil.copytree(os.path.join(self.root, '.held'), os.path.join(tmp, '.held'))
            with open(os.path.join(tmp, 'skills/demo/SKILL.md'), 'w', encoding='utf-8') as handle:
                handle.write(item['files']['skills/demo/SKILL.md'])
            self.assertEqual(item['files']['catalog.json'], rmod._catalog_module().render(tmp))
            # The changed skill still passes lint (CAT-14 changelog row, schema),
            # with the fixture's allowed-tools granted as main's allowlist
            # would have to (CAT-28).
            lint = rmod._catalog_module()._lint
            schema = lint.load_schema()
            schema['x-privileged-allowlist'] = {'demo': {'allowed-tools': 'Bash Read'}}
            self.assertEqual(lint.lint_skill(os.path.join(tmp, 'skills', 'demo'), schema), [])
        # plan_findings turns it into an open_pr on the finding's branch.
        actions = alerts.plan_findings([item], [], {})
        self.assertEqual(actions[0]['op'], 'open_pr')
        self.assertEqual(actions[0]['branch'], fmod.branch_name(fmod.finding_fingerprint(item['finding'])))

    def test_moved_link_gives_link_pr(self):
        rel = 'skills/demo/SKILL.md'
        found = {'kind': 'moved-link', 'skill': 'demo', 'subject': rel + ':https://example.com/old-docs',
                 'file': rel, 'line': 15, 'evidence': 'moved permanently (301)',
                 'fix': {'file': rel, 'line': 15, 'old': 'https://example.com/old-docs',
                         'new': 'https://example.com/new-docs'}}
        self.assertEqual(fmod.validate(found), [])
        item = rmod.route(found, self.root, MODELS, today='2026-11-18')
        self.assertEqual(item['as'], 'pr')
        lines = changed(self.read(rel), item['files'][rel])
        self.assertIn('+ Docs live at https://example.com/new-docs for reference.', lines)
        self.assertEqual(len(lines), 5, lines)

    def test_new_optional_field_gives_schema_pr(self):
        diff = ('--- claude-code-skills-frontmatter (stored)\n+++ claude-code-skills-frontmatter (fetched)\n'
                '@@ -3,2 +3,3 @@\n | `when_to_use` | No | Extra trigger phrases. |\n'
                '+| `max-turns` | No | Turn limit. |\n | `allowed-tools` | No | Tools. |')
        found = {'kind': 'upstream-change', 'skill': 'claude-code-skills-frontmatter',
                 'subject': 'claude-code-skills-frontmatter:' + 'a' * 64, 'file': None, 'line': None,
                 'evidence': diff, 'fix': None}
        item = rmod.route(found, self.root, MODELS)
        self.assertEqual(item['as'], 'pr')
        self.assertEqual(list(item['files']), ['schema/frontmatter.schema.json'])
        lines = changed(self.read('schema/frontmatter.schema.json'), item['files']['schema/frontmatter.schema.json'])
        self.assertEqual(lines, ['-     "disallowed-tools"', '+     "disallowed-tools",', '+     "max-turns"'])
        # The Agent Skills spec source adds to properties instead.
        found['skill'] = 'agent-skills-spec'
        item = rmod.route(found, self.root, MODELS)
        self.assertEqual(json.loads(item['files']['schema/frontmatter.schema.json'])['properties']['max-turns'],
                         {'type': 'string'})
        # A required field or a removed line is not mechanical.
        found['evidence'] = diff.replace('| No | Turn', '| Yes | Turn')
        self.assertEqual(rmod.route(found, self.root, MODELS)['as'], 'issue')

    def test_held_sdd_finding_gives_issue(self):
        item = rmod.route(model_finding('sdd-demo', '.held'), self.root, MODELS)
        self.assertEqual(item['as'], 'issue')
        self.assertEqual(item['files'], {})
        self.assertIn('SDD-1', item['body'])
        self.assertEqual(alerts.plan_findings([item], [], {})[0]['op'], 'open_issue')

    def test_non_mechanical_diff_gives_upstream_issue_listing_skills(self):
        diff = '--- s (stored)\n+++ s (fetched)\n@@ -1 +1 @@\n-Skills load from one folder.\n+Skills load from two folders.'
        found = {'kind': 'upstream-change', 'skill': 'claude-code-skills-locations',
                 'subject': 'claude-code-skills-locations:' + 'b' * 64, 'file': None, 'line': None,
                 'evidence': diff, 'fix': None}
        item = rmod.route(found, self.root, MODELS,
                          classification={'affects': ['demo', 'sdd-demo'], 'reason': 'classifier: both load skills'})
        self.assertEqual(item['as'], 'issue')
        self.assertEqual(item['labels'], ['upstream-change'])
        self.assertIn('- `demo`\n- `sdd-demo`', item['body'])
        self.assertIn('```\n' + diff + '\n```', item['body'])
        unclassified = rmod.route(found, self.root, MODELS, classification=None,
                                  unclassified_reason=cmod.unclassified_reason(
                                      {'classifier': {'day': '2026-11-18', 'calls': 10}}, lambda p: '', '2026-11-18'))
        self.assertIn('Not classified', unclassified['body'])
        self.assertIn('cap of 10 calls', unclassified['body'])
        hidden = rmod.route(found, self.root, MODELS, excluded={'sdd-demo'},
                            classification={'affects': ['demo', 'sdd-demo'], 'reason': 'sdd-demo loads skills'})
        self.assertNotIn('sdd-demo', hidden['body'])
        self.assertIn('- `demo`', hidden['body'])
        # The diff itself may name an excluded skill; that line is withheld.
        found = dict(found, evidence=diff + '\n+See the secret-skill notes.')
        hidden = rmod.route(found, self.root, MODELS, excluded={'SECRET-skill'}, classification=None)
        self.assertNotIn('secret-skill', json.dumps(hidden).lower())
        self.assertIn('[line withheld', hidden['body'])
        # A finding whose subject or file names an excluded skill is dropped whole.
        self.assertIsNone(rmod.route(model_finding(), self.root, MODELS, excluded={'DEMO'}))
        found = model_finding()
        found['fix'] = dict(found['fix'], new=found['fix']['new'] + ' see hidden-one')
        self.assertIsNone(rmod.route(found, self.root, MODELS, excluded={'Hidden-One'}))

    def test_stale_fix_or_excluded_skill_is_not_a_pr(self):
        found = model_finding()
        found['fix'] = dict(found['fix'], old='a line that is not there')
        self.assertEqual(rmod.route(found, self.root, MODELS)['as'], 'issue')
        found = model_finding()
        found['fix'] = dict(found['fix'], file='skills/other/SKILL.md')
        self.assertEqual(rmod.route(found, self.root, MODELS)['as'], 'issue')
        self.assertIsNone(rmod.route(model_finding(), self.root, MODELS, excluded={'demo'}))


# ---------------------------------------------------------------------------
# FRS-21


class ClassifyTest(unittest.TestCase):
    SKILLS = {'demo': SKILL, 'other': SKILL.replace('name: demo', 'name: other')
              .replace('old-model', 'other-model').replace('allowed-tools: Bash Read\n', '')
              .replace('Use the `Bash` tool to run checks.\n', '')}
    UNMATCHED = '--- s\n+++ s\n@@ -1 +1 @@\n-Plugins are listed.\n+Plugins are listed by name.'

    def counting(self, reply='{"affects": ["other", "ghost"], "reason": "plugins"}'):
        calls = []

        def call(prompt):
            calls.append(prompt)
            return reply
        return call, calls

    def test_rules_match_makes_no_call(self):
        call, calls = self.counting()
        state = {}
        result = cmod.classify('--- s\n+++ s\n@@\n-| `Bash` | runs commands |\n+| `Bash` | runs shell commands |',
                               self.SKILLS, state, call, '2026-11-18')
        self.assertEqual(result['affects'], ['demo'])
        self.assertEqual(calls, [])
        self.assertNotIn('classifier', state)
        claude = 'claude-' + 'sonnet-4-5-20250929'
        result = cmod.classify('+' + claude + ' is deprecated', {'x': 'Use ' + claude + ' here.'}, state, call, '2026-11-18')
        self.assertEqual(result['affects'], ['x'])
        result = cmod.classify('-| gpt-5 | deprecated |', {'y': 'Runs on gpt-5 by default.'}, state, call, '2026-11-18')
        self.assertEqual(result['affects'], ['y'])
        self.assertEqual(calls, [])
        self.assertEqual(cmod.rules('-o4-mini-2025-04-16 retires', {'z': 'Pin o4-mini-2025-04-16.'})['affects'], ['z'])
        # gpt-5.1 in a diff does not mark a skill that uses only gpt-5.
        self.assertIsNone(cmod.rules('+| gpt-5.1 | deprecated |', {'y': 'Runs on gpt-5 by default.'}))

    def test_prose_words_do_not_match(self):
        call, calls = self.counting()
        result = cmod.classify('+Read the name and description of each skill.', self.SKILLS, {}, call, '2026-11-18')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['affects'], ['other'])  # unknown "ghost" dropped

    def test_eleventh_call_of_a_day_makes_no_call(self):
        call, calls = self.counting()
        state = {}
        for _ in range(10):
            self.assertIsNotNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, call, '2026-11-18'))
        self.assertEqual(len(calls), 10)
        self.assertIsNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, call, '2026-11-18'))
        self.assertEqual(len(calls), 10)
        self.assertEqual(state['classifier'], {'day': '2026-11-18', 'calls': 10})
        self.assertTrue(cmod.capped(state, '2026-11-18'))

    def test_counter_resets_next_day(self):
        call, calls = self.counting()
        state = {'classifier': {'day': '2026-11-18', 'calls': 10}}
        self.assertIsNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, call, '2026-11-18'))
        self.assertIsNotNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, call, datetime.date(2026, 11, 19)))
        self.assertEqual(state['classifier'], {'day': '2026-11-19', 'calls': 1})
        self.assertFalse(cmod.capped(state, '2026-11-19'))

    def test_no_runner_or_bad_reply_returns_none(self):
        state = {}
        self.assertIsNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, None, '2026-11-18'))
        self.assertIn('Mac job', cmod.unclassified_reason(state, None, '2026-11-18'))
        call, calls = self.counting()
        cmod.classify(self.UNMATCHED, self.SKILLS, {}, call, '2026-11-18', excluded={'OTHER'})
        self.assertNotIn('other', calls[0].lower().split('skills:')[1].split('diff:')[0])
        call, calls = self.counting('not json')
        self.assertIsNone(cmod.classify(self.UNMATCHED, self.SKILLS, state, call, '2026-11-18'))
        self.assertEqual(state['classifier']['calls'], 1)


if __name__ == '__main__':
    unittest.main()
