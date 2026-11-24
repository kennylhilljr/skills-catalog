"""Tests of the freshness runner, part 9 (FRS-16, FRS-26).

Everything outside the repository is a fake: the GitHub transport, the link,
text and npm fetchers, the CLI runner, the classifier and the notifier. No
test touches the network, GitHub, `gh`, `openclaw`, `claude` or `codex`.
"""

import builtins
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

from freshness import age
from freshness import alerts as amod
from freshness import canary
from freshness import checks
from freshness import classify as cmod
from freshness import cli_checks
from freshness import dwell
from freshness import finding as fmod
from freshness import github as ghmod
from freshness import run as runmod
from freshness import state as smod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = runmod.REPO
BOT = 'skills-freshness' + '[bot]'
NOW = datetime.datetime(2026, 11, 18, 11, 0, tzinfo=datetime.timezone.utc)


def _skill_folders(root=ROOT):
    out = []
    for section in ('skills', '.held'):
        for name in sorted(os.listdir(os.path.join(root, section))):
            if os.path.isfile(os.path.join(root, section, name, 'SKILL.md')):
                out.append(name)
    return out


class FakeGitHub:
    """A transport for `github.GitHub`: answers reads, records every request."""

    def __init__(self, state=None, items=()):
        self.requests = []
        self.state_text = smod.dump(state) if state is not None else None
        self.items = list(items)
        self.blobs = []
        self.number = 500
        self.state_sha = 'a' * 40
        self.concurrent = []  # states the other half writes, one per conflicting ref move

    def writes(self):
        return [(m, p, b) for m, p, b in self.requests if m != 'GET']

    def written_state(self):
        texts = [b['content'] for m, p, b in self.requests
                 if m == 'POST' and p.endswith('/git/blobs') and '"heartbeats"' in b['content']]
        return json.loads(texts[-1]) if texts else None

    def _last_blob(self):
        return self.blobs[-1] if self.blobs else self.state_text

    def _conflict(self):
        if not self.concurrent:
            return False
        self._other_half_writes()
        return True

    def _other_half_writes(self):
        self.state_text = smod.dump(self.concurrent.pop(0))
        self.state_sha = format(int(self.state_sha, 16) + 1, '040x')

    def __call__(self, method, path, body):
        self.requests.append((method, path, body))
        prefix = '/repos/' + REPO
        assert path.startswith(prefix), path
        rel = path[len(prefix):]
        if method == 'GET':
            if rel == '':
                return 200, {'default_branch': 'main'}
            if rel == '/git/ref/heads/freshness-state':
                if self.state_text is None:
                    return 404, {'message': 'Not Found'}
                return 200, {'object': {'sha': self.state_sha}}
            if rel == '/git/ref/heads/main':
                return 200, {'object': {'sha': 'c' * 40}}
            if rel.startswith('/git/commits/'):
                return 200, {'tree': {'sha': 'd' * 40}}
            if rel.startswith('/git/trees/'):
                return 200, {'tree': [{'path': ghmod.STATE_PATH, 'type': 'blob', 'sha': 'e' * 40}]}
            if rel.startswith('/git/blobs/'):
                return 200, {'content': self.state_text or '', 'encoding': 'utf-8'}
            if rel.startswith('/issues?'):
                if 'page=1' not in rel:
                    return 200, []
                want = 'open' if 'state=open' in rel else 'closed'
                return 200, [i for i in self.items if i['state'] == want]
            if rel.startswith('/pulls/'):
                return 200, {'head': {'ref': 'x'}}
            return 404, {'message': 'Not Found'}
        if rel == '/git/blobs':
            self.blobs.append(body['content'])
            return 201, {'sha': 'b' * 40}
        if rel in ('/git/trees', '/git/commits'):
            return 201, {'sha': 'f' * 40}
        if rel == '/git/refs':
            if self._conflict():
                return 422, {'message': 'Reference already exists'}
            self.state_text = self._last_blob()
            return 201, {}
        if rel.startswith('/git/refs/'):
            if rel == '/git/refs/heads/freshness-state':
                if self._conflict():
                    return 422, {'message': 'Update is not a fast forward'}
                self.state_text = self._last_blob()
            return 200, {}
        if rel in ('/issues', '/pulls'):
            self.number += 1
            return 201, {'number': self.number,
                         'html_url': 'https://github.com/%s/issues/%d' % (REPO, self.number)}
        return 200, {}


def _item(number, kind, subject, state='open', is_pr=False, closed_at=None):
    item = {'number': number, 'state': state, 'closed_at': closed_at, 'user': {'login': BOT},
            'body': 'x\n\n' + fmod.marker(fmod.fingerprint(kind, subject)) + '\n'}
    if is_pr:
        item['pull_request'] = {}
    return item


class FakeCli:
    """The CLI runner of the Mac half: every command exists, at version 1.0.0."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout=10):
        self.calls.append(list(argv))
        if argv[-1] == '--version':
            return 0, '1.0.0'
        return 0, 'Usage: tool [options]\n'


def _no_cli(argv, timeout=10):
    raise AssertionError('the Actions half must never run a CLI: %r' % (argv,))


def _no_call(prompt):
    raise AssertionError('no classifier call expected')


def _fetch_text(url):
    return 200, '# Page\n\nunchanged text\n'


def _dead(url):
    return 404, None


def _alive(url):
    return 200, None


def _beat(last_success, result='ok'):
    return {'run_id': 'r0', 'start': last_success, 'end': last_success, 'result': result,
            'counts': {}, 'last_success': last_success}


def _deps(fake, half, out=None, **kw):
    params = dict(
        github=ghmod.GitHub(REPO, 'tok' + 'en-value', transport=fake, bot_login=BOT),
        clock=lambda: NOW, fetch_link=_dead, fetch_text=_fetch_text,
        fetch_npm=lambda pkg: {'latest': '1.0.0', 'time': {}},
        cli_run=_no_cli, classifier=_no_call,
        notify_run=lambda argv, **k: types.SimpleNamespace(returncode=0),
        attention_fn=lambda *a: None,
        local_config={'slack_channel_id': 'C' + '0123', 'prbot_path': 'prbot', 'held_sources': {}},
        excluded=(), out=out if out is not None else io.StringIO(),
        run_id='%s-1' % half)
    params.update(kw)
    return runmod.Deps(**params)


def _execute(half, fake, root=ROOT, dry_run=False, **kw):
    deps = _deps(fake, half, **kw)
    job = runmod.Run(half, deps, root=root, dry_run=dry_run)
    ok = job.execute()
    return ok, job, deps


def _opened_titles(fake):
    return [b['title'] for m, p, b in fake.requests if m == 'POST' and p.endswith('/issues')]


class ActionsHalfTests(unittest.TestCase):
    def test_runs_every_cli_free_check_over_every_skill_folder(self):
        """FRS-26: every check of parts 2 and 6, over skills/ and .held/, zz-canary included."""
        seen = {name: [] for name in ('check_frontmatter', 'check_models', 'check_paths')}
        patches = []
        for name in seen:
            real = getattr(checks, name)

            def wrapper(folder, skill, *args, _real=real, _name=name, **kwargs):
                seen[_name].append(skill)
                return _real(folder, skill, *args, **kwargs)
            patches.append(mock.patch.object(checks, name, wrapper))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            spies = {name: stack.enter_context(mock.patch.object(mod, name, wraps=getattr(mod, name)))
                     for mod, name in ((checks, 'check_description'), (runmod.links, 'check_links'),
                                       (age, 'age_findings'), (age, 'stale_actions'), (age, 'advance_actions'),
                                       (dwell, 'check_dwell'), (runmod.upstream, 'watch'),
                                       (canary, 'verify'))}
            ok, job, _ = _execute('actions', FakeGitHub())
        self.assertTrue(ok, job.errors)
        folders = _skill_folders()
        self.assertIn('zz-canary', folders)
        for name, skills in seen.items():
            self.assertEqual(sorted(skills), sorted(folders), name)
        for name, spy in spies.items():
            self.assertTrue(spy.called, name)
        described = [n for n, _d, _f in spies['check_description'].call_args[0][0]]
        self.assertEqual(sorted(described), sorted(folders))
        linked = {item[0] for item in spies['check_links'].call_args[0][0]}
        self.assertIn('zz-canary', linked)
        states = {c[0][0] for c in spies['check_dwell'].call_args_list}
        self.assertTrue({'mac-job', 'freshness-pr-unreviewed', 're-review'} <= states)
        for name in runmod.ACTIONS_CHECKS:
            self.assertIn(name, job.calls)

    def test_never_calls_a_cli_runner(self):
        with mock.patch.object(cli_checks, 'make_run', side_effect=AssertionError('no CLI')), \
                mock.patch.object(runmod.subprocess, 'run', side_effect=AssertionError('no subprocess')):
            ok, job, _ = _execute('actions', FakeGitHub(), cli_run=_no_cli, classifier=_no_call)
        self.assertTrue(ok, job.errors)
        for name in ('check_commands', 'check_cli_refs', 'check_cli_lag', 'check_held', 'announce'):
            self.assertNotIn(name, job.calls)

    def test_writes_a_heartbeat_to_the_state_branch_only(self):
        """FRS-16: start, end, result and counts, committed to freshness-state, no PR."""
        fake = FakeGitHub()
        ok, job, _ = _execute('actions', fake)
        self.assertTrue(ok, job.errors)
        written = fake.written_state()
        beat = written['heartbeats']['actions']
        self.assertEqual(beat['result'], 'ok')
        self.assertEqual(beat['run_id'], 'actions-1')
        self.assertEqual(beat['start'], smod.iso(NOW))
        self.assertEqual(beat['end'], smod.iso(NOW))
        self.assertEqual(beat['last_success'], smod.iso(NOW))
        self.assertEqual(beat['counts'].get('past-max-age'), 1)
        self.assertEqual(beat['counts'].get('deprecated-model'), 1)
        refs = [b['ref'] for m, p, b in fake.requests if m == 'POST' and p.endswith('/git/refs')]
        self.assertEqual(refs, [ghmod.STATE_REF])
        self.assertNotIn('freshness-state', json.dumps([b for m, p, b in fake.requests
                                                        if m == 'POST' and p.endswith('/pulls')]))

    def test_no_write_targets_main(self):
        fake = FakeGitHub()
        _execute('actions', fake)
        _execute('mac', fake, cli_run=FakeCli())
        for method, path, body in fake.writes():
            self.assertNotIn('heads/main', path)
            self.assertNotIn('merge', path)
            if isinstance(body, dict):
                self.assertNotEqual(body.get('ref'), 'refs/heads/main')

    def test_canary_mismatch_records_a_failed_run_and_a_canary_alert(self):
        state = smod.empty_state()
        state['heartbeats'] = {'actions': _beat('2026-11-17T11:00:00+00:00')}
        fake = FakeGitHub(state)
        ok, job, _ = _execute('actions', fake, fetch_link=_alive)  # the canary link answers: no dead-link
        self.assertFalse(ok)
        beat = fake.written_state()['heartbeats']['actions']
        self.assertEqual(beat['result'], 'failed')
        self.assertEqual(beat['last_success'], '2026-11-17T11:00:00+00:00')
        self.assertEqual(beat['counts'].get('canary'), 1)
        self.assertIn('freshness-alert: canary: zz-canary', _opened_titles(fake))

    def test_first_night_canary_counts_its_recorded_link_failure(self):
        fake = FakeGitHub()
        ok, job, _ = _execute('actions', fake, fetch_link=_dead)
        self.assertTrue(ok, job.errors)
        entry = fake.written_state()['links'][canary.CANARY_URL]
        self.assertEqual(entry['first_fail_at'], smod.iso(NOW))

    def test_stale_mac_heartbeat_raises_heartbeat_mac(self):
        state = smod.empty_state()
        state['heartbeats'] = {'mac': _beat(smod.iso(NOW - datetime.timedelta(hours=27)))}
        fake = FakeGitHub(state)
        ok, job, _ = _execute('actions', fake)
        self.assertTrue(ok, job.errors)
        self.assertIn('freshness-alert: heartbeat-mac: mac-job:mac', _opened_titles(fake))

    def test_fresh_mac_heartbeat_raises_nothing(self):
        state = smod.empty_state()
        state['heartbeats'] = {'mac': _beat(smod.iso(NOW - datetime.timedelta(hours=25)))}
        fake = FakeGitHub(state)
        _execute('actions', fake)
        self.assertFalse([t for t in _opened_titles(fake) if 'heartbeat' in t])

    def test_mac_never_seen_starts_the_watch_clock(self):
        fake = FakeGitHub()
        _execute('actions', fake)
        self.assertEqual(fake.written_state()['heartbeat_watch'], {'mac': smod.iso(NOW)})
        self.assertFalse([t for t in _opened_titles(fake) if 'heartbeat' in t])

    def test_dry_run_sends_no_write_request(self):
        fake = FakeGitHub()
        out = io.StringIO()
        deps = _deps(fake, 'actions', out=out)
        with mock.patch.object(runmod, 'default_deps', side_effect=AssertionError('deps are injected')):
            code = runmod.main(['--half', 'actions', '--dry-run'], deps=deps)
        self.assertEqual(code, 0)
        self.assertTrue(fake.requests)
        self.assertEqual(fake.writes(), [])
        self.assertIn('planned: ', out.getvalue())
        self.assertIn('heartbeat not written', out.getvalue())

    def test_dry_run_transport_refuses_writes(self):
        sent = []
        guarded = runmod.read_only(lambda m, p, b: sent.append(m) or (200, {}))
        guarded('GET', '/repos/' + REPO, None)
        with self.assertRaises(ghmod.RefusedWrite):
            guarded('POST', '/repos/' + REPO + '/issues', {})
        self.assertEqual(sent, ['GET'])

    def test_excluded_skill_is_never_read_or_named(self):
        hidden = 'zz-hidden-' + 'skill'
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        root = os.path.join(tmp, 'catalog')
        shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns('.git', '__pycache__', 'server', 'logos',
                                                                  'assets', 'inventory'))
        folder = os.path.join(root, 'skills', hidden)
        os.makedirs(folder)
        with open(os.path.join(folder, 'SKILL.md'), 'w', encoding='utf-8') as handle:
            handle.write('---\nname: %s\ndescription: bad\n---\n\nclaude-3-opus-20240229 https://example.invalid/x\n'
                         % hidden)
        opened = []
        real_open = builtins.open

        def spy(file, *args, **kwargs):
            opened.append(str(file))
            return real_open(file, *args, **kwargs)
        fake = FakeGitHub()
        out = io.StringIO()
        with mock.patch.object(builtins, 'open', spy):
            _execute('actions', fake, root=root, excluded={hidden}, out=out)
        self.assertFalse([p for p in opened if hidden in p])
        everything = json.dumps(fake.requests) + out.getvalue()
        self.assertNotIn(hidden, everything)
        self.assertNotIn(fmod.fingerprint('past-max-age', hidden), everything)

    def test_unplaced_upstream_change_waits_for_the_mac_classifier(self):
        finding = {'kind': 'upstream-change', 'skill': 'claude-code-releases-github', 'subject': 'x',
                   'file': None, 'line': None, 'evidence': '+ nothing that names a skill', 'fix': None}
        fake = FakeGitHub()
        with mock.patch.object(runmod.upstream, 'watch', return_value=([finding], [], {})):
            ok, job, _ = _execute('actions', fake, classifier=_no_call)
        pending = fake.written_state()['upstream_pending']
        self.assertEqual(list(pending), [fmod.finding_fingerprint(finding)])
        self.assertFalse([t for t in _opened_titles(fake) if t.startswith('upstream-change')])

        replies = []

        def classifier(prompt):
            replies.append(prompt)
            return '{"affects": [], "reason": "no skill uses it"}'
        fake2 = FakeGitHub(fake.written_state())
        ok, job, _ = _execute('mac', fake2, cli_run=FakeCli(), classifier=classifier)
        self.assertEqual(len(replies), 1)
        self.assertEqual(fake2.written_state()['upstream_pending'], {})
        self.assertIn('upstream-change: claude-code-releases-github', _opened_titles(fake2))


class RobustnessTests(unittest.TestCase):
    def test_results_are_recorded_before_the_advance_rule_reads_them(self):
        state = smod.empty_state()
        state['results'] = {'zz-canary': {'actions_run': 'old', 'actions_ok': True, 'mac_run': None,
                                          'mac_ok': None, 'mac_at': None}}
        with mock.patch.object(age, 'advance_actions', wraps=age.advance_actions) as spy:
            _execute('actions', FakeGitHub(state))
        results = spy.call_args[0][0]
        self.assertEqual(results['zz-canary']['actions_run'], 'actions-1')
        self.assertFalse(results['zz-canary']['actions_ok'])  # it has findings this run

    def test_a_refused_write_still_writes_a_failed_heartbeat(self):
        state = smod.empty_state()
        state['heartbeats'] = {'actions': _beat('2026-11-17T11:00:00+00:00')}
        fake = FakeGitHub(state)
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        with mock.patch.object(deps.github, 'apply', side_effect=ghmod.RefusedWrite('refused: planner bug')):
            self.assertFalse(job.execute())
        beat = fake.written_state()['heartbeats']['actions']
        self.assertEqual(beat['result'], 'failed')
        self.assertEqual(beat['last_success'], '2026-11-17T11:00:00+00:00')
        self.assertTrue(any(e.startswith('apply: RefusedWrite') for e in job.errors))

    def test_a_late_failure_records_every_result_as_failing(self):
        """Results are recorded after routing, alert planning and apply, so a late failure is never `ok`."""
        fake = FakeGitHub(smod.empty_state())
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        with mock.patch.object(deps.github, 'apply', side_effect=ghmod.GitHubError('boom')):
            self.assertFalse(job.execute())
        results = fake.written_state()['results']
        self.assertEqual(sorted(results), sorted(_skill_folders()))
        self.assertEqual({e['actions_ok'] for e in results.values()}, {False})

    def test_an_alert_planning_failure_applies_nothing(self):
        fake = FakeGitHub(smod.empty_state())
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        with mock.patch.object(amod, 'plan_alerts', side_effect=RuntimeError('planner bug')), \
                mock.patch.object(deps.github, 'apply', wraps=deps.github.apply) as apply:
            self.assertFalse(job.execute())
        self.assertFalse(apply.called)
        self.assertEqual(_opened_titles(fake), [])
        self.assertEqual({e['actions_ok'] for e in fake.written_state()['results'].values()}, {False})
        self.assertEqual(fake.written_state()['heartbeats']['actions']['result'], 'failed')
        self.assertTrue(any(e.startswith('alerts: RuntimeError') for e in job.errors))

    def test_a_mac_alert_planning_failure_announces_nothing(self):
        fake = FakeGitHub(smod.empty_state())
        deps = _deps(fake, 'mac', cli_run=FakeCli())
        job = runmod.Run('mac', deps)
        with mock.patch.object(amod, 'plan_alerts', side_effect=RuntimeError('planner bug')):
            self.assertFalse(job.execute())
        self.assertNotIn('announce', job.calls)
        self.assertEqual({e['mac_ok'] for e in fake.written_state()['results'].values()}, {False})

    def _advance_run(self, apply_results):
        """Run the Actions half with one planned advance PR; `apply_results(actions)` fakes each apply call."""
        fake = FakeGitHub(smod.empty_state())
        deps = _deps(fake, 'actions', private_names=[PRIVATE])
        job = runmod.Run('actions', deps)
        advance = [{'op': 'open_pr', 'fingerprint': 'f' * 64, 'title': 'advance', 'branch': 'b', 'files': {}}]
        calls = []

        def apply(actions):
            calls.append(list(actions))
            return apply_results(actions)
        with mock.patch.object(age, 'advance_actions', return_value=advance), \
                mock.patch.object(deps.github, 'apply', side_effect=apply):
            ok = job.execute()
        return ok, job, calls, advance

    def test_an_advance_waits_for_every_other_action(self):
        ok, job, calls, advance = self._advance_run(lambda actions: [{'op': a['op'], 'ok': True} for a in actions])
        self.assertTrue(ok, job.errors)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(advance[0], calls[0])
        self.assertEqual(calls[1], advance)

    def test_a_partial_apply_failure_advances_nothing(self):
        def apply(actions):
            return [{'op': 'open_issue', 'ok': False, 'error': 'boom'}]
        ok, job, calls, advance = self._advance_run(apply)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(advance[0], calls[0])

    def test_an_undelivered_upstream_change_is_not_consumed(self):
        finding = {'kind': 'upstream-change', 'skill': 'claude-code-releases-github', 'subject': 'x',
                   'file': None, 'line': None, 'evidence': '+ nothing that names a skill', 'fix': None}
        base = smod.empty_state()
        old = {'sha256': 'old', 'extract': 'a', 'fetched_at': 't0', 'failures': 0, 'last_error': None, 'errors': []}
        base['sources'] = {'src': old, 'failing': dict(old, failures=1)}
        new = {'src': dict(old, sha256='new', extract='b', fetched_at='t1'),
               'failing': dict(old, failures=2, last_error='HTTP 500'),
               'first': dict(old, sha256='first')}
        fake = FakeGitHub(base)
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        with mock.patch.object(runmod.upstream, 'watch', return_value=([], [], new)), \
                mock.patch.object(deps.github, 'apply', side_effect=ghmod.GitHubError('boom')):
            self.assertFalse(job.execute())
        sources = fake.written_state()['sources']
        self.assertEqual(sources['src'], old)
        self.assertEqual(sources['failing']['failures'], 2)  # failure counters keep counting
        self.assertEqual(sources['first']['sha256'], 'first')

        parked = smod.empty_state()
        parked['upstream_pending'] = {fmod.finding_fingerprint(finding): finding}
        fake = FakeGitHub(parked)
        deps = _deps(fake, 'mac', cli_run=FakeCli(), classifier=lambda prompt: '{"affects": [], "reason": "r"}')
        job = runmod.Run('mac', deps)
        failed = [{'op': 'open_issue', 'ok': False, 'error': 'boom'}]
        with mock.patch.object(deps.github, 'apply', return_value=failed) as apply:
            self.assertFalse(job.execute())
        self.assertTrue(apply.called)
        self.assertIn('classify', job.calls)  # the entry was classified, then kept
        self.assertEqual(list(fake.written_state()['upstream_pending']), [fmod.finding_fingerprint(finding)])

    def test_a_delivered_upstream_change_moves_the_source_on(self):
        fake = FakeGitHub(smod.empty_state())
        with mock.patch.object(runmod.upstream, 'watch', return_value=([], [], {'src': {'sha': 'new'}})):
            ok, job, _ = _execute('actions', fake)
        self.assertTrue(ok, job.errors)
        self.assertEqual(fake.written_state()['sources'], {'src': {'sha': 'new'}})

    def test_private_names_come_from_the_private_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'private.txt')
            self.assertEqual(runmod.read_private_names(path), ())
            name = 'secret' + '-repo'
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('# private\n' + name + '\n')
            self.assertEqual(runmod.read_private_names(path), (name,))

    def test_listing_failure_plans_nothing_and_records_a_failed_run(self):
        fake = FakeGitHub()
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        with mock.patch.object(deps.github, 'list_bot_items', side_effect=ghmod.GitHubError('boom')):
            self.assertFalse(job.execute())
        self.assertEqual(_opened_titles(fake), [])
        self.assertEqual(fake.written_state()['heartbeats']['actions']['result'], 'failed')

    def _other_half(self, base, at):
        fresh = json.loads(json.dumps(base))
        fresh.setdefault('heartbeats', {})['mac'] = _beat(at)
        fresh.setdefault('results', {}).setdefault('zz-canary', {}).update(mac_run='m-' + at, mac_at=at)
        fresh['cli'] = {'claude': {'installed': at}}
        return fresh

    def _check_merged(self, fake, at):
        written = json.loads(fake.state_text)
        self.assertEqual(written['heartbeats']['mac']['last_success'], at)
        self.assertEqual(written['heartbeats']['actions']['run_id'], 'actions-1')
        self.assertEqual(written['results']['zz-canary']['mac_run'], 'm-' + at)
        self.assertEqual(written['results']['zz-canary']['actions_run'], 'actions-1')
        self.assertEqual(written['cli'], {'claude': {'installed': at}})

    def test_a_write_by_the_other_half_during_the_run_is_merged(self):
        base = smod.empty_state()
        base['heartbeats'] = {'mac': _beat('2026-11-17T11:30:00+00:00')}
        fake = FakeGitHub(base)
        deps = _deps(fake, 'actions')
        job = runmod.Run('actions', deps)
        real = deps.github.read_state
        calls = []

        def read_state():
            calls.append(1)
            if len(calls) == 2:  # the Mac commits while this run works
                fake.concurrent.append(self._other_half(base, '2026-11-18T11:00:30+00:00'))
                fake._other_half_writes()
            return real()
        with mock.patch.object(deps.github, 'read_state', side_effect=read_state):
            self.assertTrue(job.execute(), job.errors)
        self._check_merged(fake, '2026-11-18T11:00:30+00:00')

    def test_a_write_by_the_other_half_during_the_commit_is_merged(self):
        """The ref move is a compare-and-swap: a lost race re-reads and merges, never overwrites."""
        base = smod.empty_state()
        base['heartbeats'] = {'mac': _beat('2026-11-17T11:30:00+00:00')}
        fake = FakeGitHub(base)
        fake.concurrent.append(self._other_half(base, '2026-11-18T11:00:40+00:00'))
        ok, job, _ = _execute('actions', fake)
        self.assertTrue(ok, job.errors)
        self._check_merged(fake, '2026-11-18T11:00:40+00:00')
        moves = [b for m, p, b in fake.requests if m == 'PATCH' and p.endswith('/git/refs/heads/freshness-state')]
        self.assertEqual([b['force'] for b in moves], [False, False])

    def test_the_first_write_races_too(self):
        fake = FakeGitHub()
        fake.concurrent.append(self._other_half(smod.empty_state(), '2026-11-18T11:00:50+00:00'))
        ok, job, _ = _execute('actions', fake)
        self.assertTrue(ok, job.errors)
        self._check_merged(fake, '2026-11-18T11:00:50+00:00')

    def test_state_write_gives_up_after_repeated_conflicts(self):
        fake = FakeGitHub(smod.empty_state())
        fake.concurrent.extend(self._other_half(smod.empty_state(), '2026-11-18T11:0%d:00+00:00' % i)
                               for i in range(runmod.STATE_WRITE_ATTEMPTS))
        ok, job, _ = _execute('actions', fake)
        self.assertFalse(ok)
        self.assertTrue(any(e.startswith('state write') for e in job.errors))

    def test_merge_state_rules(self):
        base = {'a': 1, 'b': {'x': 1, 'y': 1}, 'gone': 1, 'kept': {'k': 1}, 'parked': {'e': 1}}
        ours = {'a': 2, 'b': {'x': 2, 'y': 1}, 'kept': {'k': 1}}
        fresh = {'a': 3, 'b': {'x': 1, 'y': 3}, 'gone': 1, 'kept': {'k': 1}, 'new': 4, 'parked': {'e': 2}}
        self.assertEqual(runmod.merge_state(base, ours, fresh),
                         {'a': 2, 'b': {'x': 2, 'y': 3}, 'kept': {'k': 1}, 'new': 4, 'parked': {'e': 2}})

    def test_a_reparked_upstream_finding_survives_the_mac_clearing_it(self):
        base = {'upstream_pending': {'fp': {'evidence': 'old'}}}
        mac = {'upstream_pending': {}}
        actions = {'upstream_pending': {'fp': {'evidence': 'new'}}}
        self.assertEqual(runmod.merge_state(base, mac, actions), actions)

    def test_a_failed_check_advances_nothing(self):
        state = smod.empty_state()
        state['results'] = {'zz-canary': {'actions_run': 'old', 'actions_ok': True}}
        fake = FakeGitHub(state)
        with mock.patch.object(checks, 'check_models', side_effect=RuntimeError('broken')), \
                mock.patch.object(age, 'advance_actions', wraps=age.advance_actions) as advance:
            ok, job, _ = _execute('actions', fake)
        self.assertFalse(ok)
        self.assertFalse(advance.called)
        self.assertIs(fake.written_state()['results']['zz-canary']['actions_ok'], False)

    def test_notify_runner_uses_the_configured_path(self):
        run = runmod.make_notify_run('/opt/tools/bin' + os.pathsep + '/usr/bin')
        path = run.keywords['env']['PATH'].split(os.pathsep)
        self.assertEqual(path[1:], ['/opt/tools/bin', '/usr/bin'])
        self.assertTrue(path[0].endswith(os.path.join('.local', 'bin')))


PRIVATE = 'zz-' + 'hidden-' + 'repo'
OTHER_PRIVATE = 'zz-' + 'second-' + 'repo'


def _finding(subject):
    return {'kind': 'model-retired', 'skill': 'zz-canary', 'subject': subject, 'file': 'skills/x/SKILL.md',
            'line': 1, 'evidence': 'synthetic evidence', 'fix': None}


def _pr_item(found, text):
    return {'finding': found, 'as': 'pr', 'labels': ['freshness'], 'title': 'fix ' + found['subject'],
            'body': 'Generated fix.', 'files': {'skills/x/SKILL.md': text}}


def _age_pr(text, fp='a' * 64):
    return {'op': 'open_pr', 'fingerprint': fp, 'title': 'freshness: advance', 'body': 'b\n' + fmod.marker(fp),
            'labels': ['freshness'], 'files': {'skills/x/SKILL.md': text}, 'branch': fmod.branch_name(fp)}


class PrivateListTests(unittest.TestCase):
    """FRS-29, FRS-30: a bot PR is published only with the private repo list (issue #99)."""

    def _route(self, half, deps_kw, texts):
        out = io.StringIO()
        fake = FakeGitHub(smod.empty_state())
        deps = _deps(fake, half, out=out, cli_run=FakeCli(), **deps_kw)
        job = runmod.Run(half, deps)
        job.now = NOW
        job.load_state()
        job.items = []
        job.findings = [_finding('s%d' % i) for i in range(len(texts))]
        by_subject = {f['subject']: t for f, t in zip(job.findings, texts)}

        def route(found, *a, **k):
            return _pr_item(found, by_subject[found['subject']])
        with mock.patch.object(runmod.rmod, 'route', side_effect=route):
            job.route_findings()
        return job, out.getvalue(), fake

    def _assert_no_names(self, *texts):
        for text in texts:
            self.assertNotIn(PRIVATE, text)
            self.assertNotIn(OTHER_PRIVATE, text)

    def test_list_text_is_parsed_like_the_scrub(self):
        text = '# private repos\n%s\n\n  %s  # trailing\r\n%s\n#only a comment\n' % (
            PRIVATE, OTHER_PRIVATE, PRIVATE)
        self.assertEqual(runmod.parse_private_names(text), (PRIVATE, OTHER_PRIVATE))

    def test_missing_empty_or_comment_only_secret_is_unavailable(self):
        for environ in ({}, {'FRESHNESS_PRIVATE_REPOS': ''}, {'FRESHNESS_PRIVATE_REPOS': ' \n'},
                        {'FRESHNESS_PRIVATE_REPOS': '# none yet\n  # still none\n'}):
            self.assertEqual(runmod.private_names_from_env(environ), ())
        self.assertEqual(runmod.private_names_from_env({'FRESHNESS_PRIVATE_REPOS': PRIVATE + '\n' + OTHER_PRIVATE}),
                         (PRIVATE, OTHER_PRIVATE))

    def test_omitted_or_empty_names_fail_closed(self):
        fake = FakeGitHub(smod.empty_state())
        self.assertFalse(_deps(fake, 'actions').private_list_ok)
        self.assertFalse(_deps(fake, 'actions', private_names=()).private_list_ok)
        self.assertFalse(_deps(fake, 'actions', private_names=['', '  ']).private_list_ok)
        self.assertTrue(_deps(fake, 'actions', private_names=[PRIVATE]).private_list_ok)

    def _default_deps(self, half, environ):
        with mock.patch.object(runmod, 'read_excluded', return_value=set()), \
                mock.patch.object(runmod, 'load_local_config', return_value={}):
            return runmod.default_deps(half, dry_run=True, environ=environ, clock=lambda: NOW)

    def test_actions_half_reads_only_the_injected_environment(self):
        environ = {'FRESHNESS_PRIVATE_REPOS': '# list\n' + PRIVATE + '\n' + OTHER_PRIVATE + '\n'}
        with mock.patch.object(runmod, 'read_private_names', side_effect=AssertionError('no Mac file')), \
                mock.patch.dict(os.environ, {'FRESHNESS_PRIVATE_REPOS': 'zz-not-this-one'}):
            deps = self._default_deps('actions', environ)
            self.assertEqual(deps.private_names, (PRIVATE, OTHER_PRIVATE))
            self.assertTrue(deps.private_list_ok)
            deps = self._default_deps('actions', {})
            self.assertEqual(deps.private_names, ())
            self.assertFalse(deps.private_list_ok)

    def test_mac_half_reads_the_private_list_file(self):
        with mock.patch.object(runmod, 'read_private_names', return_value=(PRIVATE,)) as read:
            deps = self._default_deps('mac', {'FRESHNESS_PRIVATE_REPOS': OTHER_PRIVATE})
        read.assert_called_once_with()
        self.assertEqual(deps.private_names, (PRIVATE,))
        with mock.patch.object(runmod, 'read_private_names', return_value=()):
            self.assertFalse(self._default_deps('mac', {}).private_list_ok)

    def test_missing_and_unreadable_mac_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'private.txt')
            self.assertEqual(runmod.read_private_names(path), ())
            with open(path, 'wb') as handle:
                handle.write(b'\xff\xfe' + PRIVATE.encode() + b'\n')
            with self.assertRaises(runmod.RunError) as caught:
                runmod.read_private_names(path)
        self._assert_no_names(str(caught.exception))

    def test_actions_generated_file_with_a_private_name_plans_no_open_pr(self):
        """Issue #99 acceptance: the Actions half, given a private name in a PR file, plans no open_pr."""
        job, out, _ = self._route('actions', {'private_names': [PRIVATE]},
                                  ['clean text\n', 'see ' + PRIVATE + ' for details\n'])
        ops = sorted(a['op'] for a in job.actions)
        self.assertEqual(ops, ['open_issue', 'open_pr'])
        issue = [a for a in job.actions if a['op'] == 'open_issue'][0]
        self.assertNotIn('files', issue)
        self._assert_no_names(out, json.dumps(job.actions), json.dumps(job.state))

    def test_configured_clean_files_keep_open_pr(self):
        for half in runmod.HALVES:
            job, out, _ = self._route(half, {'private_names': [PRIVATE]}, ['clean text\n'])
            self.assertEqual([a['op'] for a in job.actions], ['open_pr'], half)
            self.assertNotIn('notice', out)

    def test_unavailable_list_routes_prs_to_issues_on_both_halves(self):
        for half in runmod.HALVES:
            job, out, _ = self._route(half, {}, ['clean text\n', 'more clean text\n'])
            self.assertEqual([a['op'] for a in job.actions], ['open_issue', 'open_issue'], half)
            for action in job.actions:
                self.assertNotIn('files', action)
                self.assertNotIn('branch', action)

    def test_routed_fallback_dedupes_against_open_items(self):
        found = _finding('s0')
        out = io.StringIO()
        fake = FakeGitHub(smod.empty_state())
        job = runmod.Run('actions', _deps(fake, 'actions', out=out))
        job.now = NOW
        job.load_state()
        job.items = [{'number': 7, 'state': 'open', 'is_pr': False,
                      'fingerprint': fmod.finding_fingerprint(found)}]
        job.findings = [found, dict(found)]
        with mock.patch.object(runmod.rmod, 'route', side_effect=lambda f, *a, **k: _pr_item(f, 'x\n')):
            job.route_findings()
        self.assertEqual(job.actions, [])

    def _age_job(self, deps_kw, items=()):
        out = io.StringIO()
        fake = FakeGitHub(smod.empty_state())
        job = runmod.Run('actions', _deps(fake, 'actions', out=out, **deps_kw))
        job.now = NOW
        job.load_state()
        job.items = list(items)
        job.rereview, job.age_skills, job.checked_skills = [], [], []
        return job, out

    def _age_actions(self, job, stale, advance):
        with mock.patch.object(age, 'stale_actions', return_value=stale), \
                mock.patch.object(age, 'advance_actions', return_value=advance):
            job.age_actions()

    def test_age_prs_follow_the_same_policy(self):
        clean, leaky = _age_pr('clean\n', 'a' * 64), _age_pr('see ' + PRIVATE + '\n', 'b' * 64)
        close = {'op': 'close', 'fingerprint': 'c' * 64, 'number': 3}

        job, out = self._age_job({'private_names': [PRIVATE]})
        self._age_actions(job, [close, dict(clean)], [dict(leaky)])
        self.assertEqual([a['op'] for a in job.actions], ['close', 'open_pr'])
        self.assertEqual([a['op'] for a in job.advance], ['open_issue'])
        self.assertNotIn('files', job.advance[0])
        self.assertNotIn('branch', job.advance[0])
        self.assertEqual(job.advance[0]['fingerprint'], 'b' * 64)
        self._assert_no_names(out, json.dumps(job.advance))

        job, out = self._age_job({})
        self._age_actions(job, [dict(clean)], [dict(clean, fingerprint='d' * 64)])
        self.assertEqual([a['op'] for a in job.actions + job.advance], ['open_issue', 'open_issue'])

    def test_age_fallback_dedupes_against_open_items(self):
        job, _ = self._age_job({}, items=[{'number': 9, 'state': 'open', 'is_pr': False, 'fingerprint': 'a' * 64}])
        self._age_actions(job, [_age_pr('clean\n', 'a' * 64)], [])
        self.assertEqual(job.actions, [])

    def test_list_added_later_opens_no_pr_beside_the_fallback_issue(self):
        fallback = {'number': 9, 'state': 'open', 'is_pr': False, 'fingerprint': 'a' * 64}
        job, _ = self._age_job({'private_names': [PRIVATE]}, items=[fallback])
        self._age_actions(job, [_age_pr('clean\n', 'a' * 64)], [_age_pr('clean\n', 'b' * 64)])
        self.assertEqual(job.actions, [])
        self.assertEqual([a['op'] for a in job.advance], ['open_pr'])

    def test_unconfigured_dry_run_prints_no_open_pr(self):
        fake = FakeGitHub(smod.empty_state())
        out = io.StringIO()
        with mock.patch.object(runmod.rmod, 'route', side_effect=lambda f, *a, **k: _pr_item(f, 'x\n')):
            ok, job, _ = _execute('actions', fake, dry_run=True, out=out)
        planned = [json.loads(line[len('planned: '):]) for line in out.getvalue().splitlines()
                   if line.startswith('planned: {')]
        self.assertTrue(planned)
        self.assertNotIn('open_pr', {p['op'] for p in planned})
        self.assertEqual(out.getvalue().count(runmod.PRIVATE_LIST_NOTICE['actions']), 1)
        self.assertEqual(fake.writes(), [])

    def test_notice_once_per_unconfigured_run_even_without_prs(self):
        for half, kw in (('actions', {}), ('mac', {'cli_run': FakeCli()})):
            out = io.StringIO()
            with mock.patch.object(runmod.Run, 'route_findings', lambda self: None):
                _execute(half, FakeGitHub(smod.empty_state()), out=out, **kw)
            self.assertEqual(out.getvalue().count(runmod.PRIVATE_LIST_NOTICE[half]), 1, half)
            out = io.StringIO()
            _execute(half, FakeGitHub(smod.empty_state()), out=out, private_names=[PRIVATE], **kw)
            self.assertNotIn(runmod.PRIVATE_LIST_NOTICE[half], out.getvalue())

    def test_configured_run_never_prints_or_persists_the_list(self):
        fake = FakeGitHub(smod.empty_state())
        out = io.StringIO()
        with mock.patch.object(runmod.rmod, 'route',
                               side_effect=lambda f, *a, **k: _pr_item(f, 'see %s\n' % PRIVATE)):
            _execute('actions', fake, out=out, private_names=[PRIVATE, OTHER_PRIVATE])
        self._assert_no_names(out.getvalue(), json.dumps(fake.written_state()),
                              json.dumps([b for _m, _p, b in fake.requests]))
        self.assertFalse([p for m, p, b in fake.requests if m == 'POST' and p.endswith('/pulls')])


class MacHalfTests(unittest.TestCase):
    def test_runs_the_cli_checks_and_announces(self):
        cli = FakeCli()
        sends = []
        state = smod.empty_state()
        state['heartbeats'] = {'actions': _beat(smod.iso(NOW - datetime.timedelta(hours=27)))}
        fake = FakeGitHub(state)
        with mock.patch.object(cli_checks, 'check_commands', wraps=cli_checks.check_commands) as commands, \
                mock.patch.object(cli_checks, 'check_cli_refs', wraps=cli_checks.check_cli_refs) as refs, \
                mock.patch.object(runmod.held_drift, 'check_held', wraps=runmod.held_drift.check_held) as held:
            ok, job, _ = _execute('mac', fake, cli_run=cli,
                                  notify_run=lambda argv, **k: sends.append(argv) or types.SimpleNamespace(returncode=0))
        self.assertTrue(ok, job.errors)
        self.assertEqual(sorted(c[0][0] for c in commands.call_args_list), sorted(_skill_folders()))
        self.assertEqual(sorted(c[0][0] for c in refs.call_args_list), sorted(_skill_folders()))
        self.assertTrue(held.called)
        self.assertIn(['claude', '--version'], cli.calls)
        for name in ('check_commands', 'check_cli_refs', 'check_cli_lag', 'check_held', 'announce'):
            self.assertIn(name, job.calls)
        for name in ('check_frontmatter', 'check_links', 'verify'):
            self.assertNotIn(name, job.calls)
        # FRS-16, reverse direction: the Mac raises heartbeat-actions, and part 7 sends it.
        self.assertIn('freshness-alert: heartbeat-actions: actions-run:actions', _opened_titles(fake))
        self.assertTrue(any('heartbeat-actions' in argv[-1] for argv in sends))
        written = fake.written_state()
        self.assertEqual(written['heartbeats']['mac']['result'], 'ok')
        self.assertEqual(written['heartbeats']['actions']['run_id'], 'r0')

    def test_fresh_actions_heartbeat_raises_nothing(self):
        state = smod.empty_state()
        state['heartbeats'] = {'actions': _beat(smod.iso(NOW - datetime.timedelta(hours=2)))}
        fake = FakeGitHub(state)
        ok, job, _ = _execute('mac', fake, cli_run=FakeCli())
        self.assertTrue(ok, job.errors)
        self.assertFalse([t for t in _opened_titles(fake) if 'heartbeat' in t])

    def test_announces_open_alert_issue_missing_from_state(self):
        """FRS-23: an open alert issue absent from state.alerts is still announced and recorded."""
        sends = []
        state = smod.empty_state()
        state['heartbeats'] = {'actions': _beat(smod.iso(NOW - datetime.timedelta(hours=2)))}
        alert = _item(41, 'dead-link', 'zz-demo')
        alert['title'] = 'freshness-alert: dead-link: zz-demo'
        alert['labels'] = [{'name': 'freshness-alert'}, {'name': 'dead-link'}]
        other = _item(42, 'stale', 'zz-other')
        other['labels'] = [{'name': 'freshness'}]
        fake = FakeGitHub(state, items=[alert, other])
        ok, job, _ = _execute('mac', fake, cli_run=FakeCli(),
                              notify_run=lambda argv, **k: sends.append(argv) or types.SimpleNamespace(returncode=0))
        self.assertTrue(ok, job.errors)
        self.assertTrue(any('/issues/41' in argv[-1] for argv in sends))
        self.assertFalse(any('/issues/42' in argv[-1] for argv in sends))
        written = fake.written_state()['alerts']
        entry = written[fmod.fingerprint('dead-link', 'zz-demo')]
        self.assertTrue(entry['announced'] is True)
        self.assertEqual(entry['kind'], 'dead-link')
        self.assertEqual(entry['issue'], 41)
        self.assertNotIn(fmod.fingerprint('stale', 'zz-other'), written)

    def test_mac_results_feed_the_advance_rule(self):
        fake = FakeGitHub()
        _execute('mac', fake, cli_run=FakeCli())
        results = fake.written_state()['results']
        self.assertEqual(sorted(results), sorted(_skill_folders()))
        entry = results['zz-canary']
        self.assertEqual(entry['mac_run'], 'mac-1')
        self.assertEqual(entry['mac_at'], smod.iso(NOW))


class ConfigTests(unittest.TestCase):
    def test_tracked_config_matches_the_module_defaults(self):
        config = runmod.load_config()
        self.assertEqual(config['description_collision_threshold'], checks.DEFAULT_THRESHOLD)
        self.assertEqual(config['link_min_gap_hours'], 20)
        self.assertEqual(config['issues_per_day'], amod.ISSUES_PER_DAY)
        self.assertEqual(config['ops_messages_per_day'], amod.OPS_PER_DAY)
        self.assertEqual(config['classifier_calls_per_day'], cmod.CALLS_PER_DAY)
        self.assertEqual(config['advance_policy'], age.DEFAULT_POLICY)
        for row in dwell.LIMITS:
            unit = 'max_runs' if 'max_runs' in row else 'max_hours'
            self.assertEqual(config['dwell_limits'][row['state']], {unit: row[unit]}, row['state'])

    def test_overrides_are_applied_and_restored(self):
        config = runmod.load_config()
        config = dict(config, issues_per_day=2, classifier_calls_per_day=3,
                      dwell_limits=dict(config['dwell_limits'], **{'mac-job': {'max_hours': 30}}))
        previous = runmod.apply_config(config)
        try:
            self.assertEqual(amod.ISSUES_PER_DAY, 2)
            self.assertEqual(cmod.CALLS_PER_DAY, 3)
            self.assertEqual(dwell.limit('mac-job')['max_hours'], 30)
        finally:
            runmod.restore_config(previous)
        self.assertEqual(amod.ISSUES_PER_DAY, 5)
        self.assertEqual(cmod.CALLS_PER_DAY, 10)
        self.assertEqual(dwell.limit('mac-job')['max_hours'], 26)

    def test_bad_config_is_refused(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        config = runmod.load_config()
        del config['dwell_limits']['re-review']
        path = os.path.join(tmp, 'config.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(config, handle)
        with self.assertRaises(runmod.RunError):
            runmod.load_config(path)


class CredentialTests(unittest.TestCase):
    KEY = ('-----BEGIN RSA ' + 'PRIVATE KEY-----\nfake\n-----END RSA ' + 'PRIVATE KEY-----\n').encode()

    def test_live_run_without_a_token_fails_before_any_request(self):
        with self.assertRaises(runmod.RunError) as caught:
            runmod.default_deps('actions', dry_run=False, environ={})
        self.assertIn('FRESHNESS_TOKEN', str(caught.exception))

    def test_jwt_and_installation_token(self):
        signed = []

        def sign(key, data):
            signed.append((key, data))
            return b'sig'
        calls = []

        def api(method, path, bearer, body=None):
            calls.append((method, path, body))
            if path == '/app':
                return 200, {'slug': 'skills-freshness'}
            return 201, {'token': 'minted'}
        config = {'keychain_service': 'svc', 'app_id': '42', 'installation_id': '7'}
        token, login = runmod.mint_installation_token(config, NOW, key_reader=lambda s: self.KEY, sign=sign,
                                                      api=api)
        self.assertEqual((token, login), ('minted', BOT))
        header, payload, sig = signed[0][1].decode().split('.') + ['']
        claims = json.loads(runmod.base64.urlsafe_b64decode(payload + '=='))
        self.assertEqual(claims['iss'], '42')
        self.assertLessEqual(claims['exp'] - claims['iat'], 600)
        self.assertEqual(calls[1][1], '/app/installations/7/access_tokens')
        self.assertEqual(calls[1][2]['repositories'], ['skills-catalog'])
        self.assertEqual(calls[1][2]['permissions'],
                         {'contents': 'write', 'issues': 'write', 'pull_requests': 'write'})

    def test_mint_error_never_holds_the_key(self):
        config = {'keychain_service': 'svc', 'app_id': '42', 'installation_id': '7'}
        with self.assertRaises(runmod.RunError) as caught:
            runmod.mint_installation_token(config, NOW, key_reader=lambda s: self.KEY,
                                           sign=lambda k, d: b'sig', api=lambda *a, **k: (401, None))
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_openssl_gets_the_key_through_a_pipe(self):
        seen = {}

        def run(argv, **kwargs):
            fd = kwargs['pass_fds'][0]
            seen['argv'] = argv
            seen['key'] = os.read(fd, 4096)
            return types.SimpleNamespace(returncode=0, stdout=b'sig')
        self.assertEqual(runmod.openssl_sign(self.KEY, b'data', run=run), b'sig')
        self.assertEqual(seen['argv'][:4], ['openssl', 'dgst', '-sha256', '-sign'])
        self.assertTrue(seen['argv'][4].startswith('/dev/fd/'))
        self.assertEqual(seen['key'], self.KEY)
        self.assertNotIn(self.KEY.decode(), ' '.join(seen['argv']))

    def test_keychain_lookup(self):
        def run(argv, **kwargs):
            self.assertEqual(argv, ['security', 'find-generic-password', '-s', 'svc', '-w'])
            return types.SimpleNamespace(returncode=0, stdout=self.KEY)
        self.assertEqual(runmod.keychain_key('svc', run=run), self.KEY)
        with self.assertRaises(runmod.RunError):
            runmod.keychain_key('svc', run=lambda argv, **k: types.SimpleNamespace(returncode=44, stdout=b''))


class FetcherTests(unittest.TestCase):
    def test_link_fetch_falls_back_to_get_and_keeps_the_location(self):
        methods = []

        class Response:
            def __init__(self, status, location=None):
                self.status = status
                self.headers = {'Location': location} if location else {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, request, timeout):
                methods.append((request.get_method(), timeout, request.get_header('User-agent')))
                if request.get_method() == 'HEAD':
                    return Response(405)
                return Response(301, 'https://example.org/new')
        self.assertEqual(runmod.fetch_link('https://example.org/old', opener=Opener()),
                         (301, 'https://example.org/new'))
        self.assertEqual([m[0] for m in methods], ['HEAD', 'GET'])
        self.assertEqual(methods[0][1], 10)
        self.assertEqual(methods[0][2], 'skills-catalog-freshness')


def _load_installer():
    path = os.path.join(ROOT, 'tools', 'install-freshness-agent')
    loader = importlib.machinery.SourceFileLoader('install_freshness_agent', path)
    spec = importlib.util.spec_from_loader('install_freshness_agent', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_installer()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)

    def test_prints_without_writing(self):
        out = io.StringIO()
        code = self.mod.main([], home=self.home, uid=501, out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        plist = os.path.join(self.home, 'Library', 'LaunchAgents', 'com.kennylhilljr.skills-freshness.plist')
        self.assertIn('launchctl bootstrap gui/501 ' + plist, text)
        self.assertIn('<string>freshness.run</string>', text)
        self.assertIn('<string>mac</string>', text)
        self.assertIn('<key>Hour</key>\n    <integer>6</integer>', text)
        self.assertIn('<key>Minute</key>\n    <integer>30</integer>', text)
        self.assertIn(os.path.join(self.home, 'Library', 'Logs', 'skills-freshness'), text)
        self.assertNotIn('@ROOT@', text)
        self.assertEqual(os.listdir(self.home), [])

    def test_write_puts_the_plist_in_launch_agents_and_never_runs_launchctl(self):
        out = io.StringIO()
        with mock.patch('subprocess.run', side_effect=AssertionError('no launchctl')), \
                mock.patch('os.system', side_effect=AssertionError('no launchctl')):
            code = self.mod.main(['--write'], home=self.home, uid=501, out=out)
        self.assertEqual(code, 0)
        plist = os.path.join(self.home, 'Library', 'LaunchAgents', 'com.kennylhilljr.skills-freshness.plist')
        with open(plist, encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('<string>%s</string>' % ROOT, text)
        self.assertTrue(os.path.isdir(os.path.join(self.home, 'Library', 'Logs', 'skills-freshness')))
        self.assertIn('launchctl bootstrap gui/501', out.getvalue())


if __name__ == '__main__':
    unittest.main()
