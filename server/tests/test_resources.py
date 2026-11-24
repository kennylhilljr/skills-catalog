"""skill:// resources of one catalog tag (MCP-1, MCP-6, CAT-20, INV-7, INV-8)."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
import unittest.mock

from fastmcp import Client
from fastmcp.skills.client import SkillsClientExtension
from mcp.shared.exceptions import MCPError

from skills_catalog_mcp import __main__ as cli
from skills_catalog_mcp.app import build_server
from skills_catalog_mcp.snapshot import SnapshotError, materialize
from tests.fixtures import catalog_json, commit_all, git, git_env, make_catalog_repo, next_stamp, skill_md, write_files

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 1, 2, 255, 254])
SKILLS = {
    'alpha-skill': {'references/notes.md': 'Alpha notes.\n', 'assets/logo.png': PNG},
    'beta-skill': {'scripts/run.py': 'print("beta")\n'},
}


def _uris(resources):
    return sorted(str(r.uri) for r in resources if not str(r.uri).endswith('/_manifest'))


def _tag_files(repo, tag):
    """Map 'name/relpath' to bytes for every file under skills/ at tag."""
    names = git(repo, 'ls-tree', '-r', '--name-only', tag, '--', 'skills').split()
    out = {}
    for path in names:
        data = subprocess.run(['git', '-C', repo, 'show', '%s:%s' % (tag, path)],
                              env=git_env(), stdout=subprocess.PIPE, check=True).stdout
        out[path[len('skills/'):]] = data
    return out


class _Temp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.exclude = os.path.join(self.tmp, 'exclude.txt')

    def snap(self, repo, ref=None):
        snap = materialize(repo, ref, exclude_file=self.exclude)
        self.addCleanup(snap.cleanup)
        return snap

    async def read_text(self, client, uri):
        contents = await client.read_resource(uri)
        return contents[0].text


class ResourceTests(_Temp):
    async def test_lists_every_skill_and_every_file(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        async with Client(build_server(self.snap(repo))) as client:
            skills = sorted(str(r.uri) for r in await client.list_skill_resources())
            uris = _uris(await client.list_resources())
        self.assertEqual(skills, ['skill://alpha-skill/SKILL.md', 'skill://beta-skill/SKILL.md'])
        self.assertEqual(uris, sorted('skill://' + p for p in _tag_files(repo, 'v2099.1.1.1')))

    async def test_read_bytes_equal_the_tag(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        tagged = _tag_files(repo, 'v2099.1.1.1')
        served = {}
        async with Client(build_server(self.snap(repo))) as client:
            for name in SKILLS:
                skill = await client.read_skill('skill://%s/SKILL.md' % name)
                for rel, data in skill.files.items():
                    served['%s/%s' % (name, rel)] = data
            text = await self.read_text(client, 'skill://alpha-skill/references/notes.md')
            skill = await client.read_skill('skill://alpha-skill/SKILL.md')
        out = skill.to_directory(os.path.join(self.tmp, 'out', 'alpha-skill'))
        for rel in ('SKILL.md', 'references/notes.md', 'assets/logo.png'):
            self.assertEqual((out / rel).read_bytes(), tagged['alpha-skill/' + rel])
        self.assertEqual(served, tagged)
        self.assertEqual(text.encode('utf-8'), tagged['alpha-skill/references/notes.md'])

    async def test_serves_the_tag_not_a_later_commit(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        write_files(repo, {'skills/beta-skill/scripts/run.py': 'print("changed")\n'})
        commit_all(repo, 'untagged change', stamp=next_stamp(1))
        snap = self.snap(repo)
        self.assertEqual(snap.tag, 'v2099.1.1.1')
        async with Client(build_server(snap)) as client:
            text = await self.read_text(client, 'skill://beta-skill/scripts/run.py')
        self.assertEqual(text, 'print("beta")\n')

    async def test_ref_none_picks_the_highest_v_tag(self):
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill'])
        write_files(repo, {'skills/alpha-skill/extra.md': 'second\n'})
        write_files(repo, {'catalog.json': catalog_json({'alpha-skill': {'extra.md': 'second\n'}})})
        commit_all(repo, 'second', tag='v2099.1.1.2', stamp=next_stamp(2))
        commit_all(repo, 'not a release', tag='other-tag', stamp=next_stamp(3))
        snap = self.snap(repo)
        self.assertEqual(snap.tag, 'v2099.1.1.2')
        self.assertEqual(snap.commit, git(repo, 'rev-parse', 'v2099.1.1.2^{commit}').strip())
        self.assertEqual(self.snap(repo, 'v2099.1.1.1').tag, 'v2099.1.1.1')
        async with Client(build_server(snap)) as client:
            self.assertEqual(await self.read_text(client, 'skill://alpha-skill/extra.md'), 'second\n')

    def test_ref_none_picks_the_highest_version_not_the_latest_date(self):
        # The highest version is tagged first; a hotfix on an older line and
        # a lightweight tag come later. Version order decides, not date, and
        # 10 sorts above 9 (version order, not text order).
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill'], tag='v2099.1.1.9')
        commit_all(repo, 'highest', tag='v2099.1.1.10', stamp=next_stamp(1))
        commit_all(repo, 'hotfix on an older line', tag='v2099.1.1.2', stamp=next_stamp(5))
        commit_all(repo, 'later lightweight', stamp=next_stamp(6))
        git(repo, 'tag', 'v2099.1.1.3', stamp=next_stamp(6))
        snap = self.snap(repo)
        self.assertEqual(snap.tag, 'v2099.1.1.10')
        self.assertEqual(snap.commit, git(repo, 'rev-parse', 'v2099.1.1.10^{commit}').strip())

    def test_ref_none_ranks_a_pre_release_below_its_release(self):
        # The release is tagged first, its pre-releases later.
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill'], tag='v2099.1.1.1')
        commit_all(repo, 'rc', tag='v2099.1.1.1-rc2', stamp=next_stamp(1))
        commit_all(repo, 'older rc', tag='v2099.1.1.1-rc1', stamp=next_stamp(2))
        self.assertEqual(self.snap(repo).tag, 'v2099.1.1.1')
        commit_all(repo, 'next line beta', tag='v2099.1.1.2-beta', stamp=next_stamp(3))
        self.assertEqual(self.snap(repo).tag, 'v2099.1.1.2-beta')

    def test_snapshot_is_private_and_cleaned_up(self):
        repo = make_catalog_repo(self.tmp)
        snap = materialize(repo, exclude_file=self.exclude)
        top = os.path.dirname(snap.root)
        self.assertEqual(stat.S_IMODE(os.stat(top).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(top)), ['skills'])
        self.assertEqual([e['name'] for e in snap.catalog], ['alpha-skill', 'beta-skill'])
        snap.cleanup()
        self.assertFalse(os.path.exists(top))


class RefTests(_Temp):
    def test_branch_name_is_not_a_tag(self):
        repo = make_catalog_repo(self.tmp)
        with self.assertRaisesRegex(SnapshotError, '^ref is not a tag$'):
            materialize(repo, 'main', exclude_file=self.exclude)

    def test_revision_expressions_are_not_tags(self):
        repo = make_catalog_repo(self.tmp)
        commit_all(repo, 'later', tag='v2099.1.1.2', stamp=next_stamp(1))
        for ref in ('v2099.1.1.2~1', 'v2099.1.1.2^', 'v2099.1.1.2^{commit}', 'v2099.1.1.2:catalog.json',
                    'v2099.1.1.2@{0}', '-v2099.1.1.2', 'tags/v2099.1.1.2', 'HEAD', ''):
            with self.assertRaisesRegex(SnapshotError, '^ref is not a tag$', msg=ref):
                materialize(repo, ref, exclude_file=self.exclude)

    def test_default_exclusion_list(self):
        import inspect
        from skills_catalog_mcp import snapshot
        default = inspect.signature(materialize).parameters['exclude_file'].default
        self.assertEqual(default, snapshot.DEFAULT_EXCLUDE_FILE)
        self.assertEqual(default, os.path.join('~', '.config', 'skills-catalog', 'exclude.txt'))
        self.assertEqual(cli.parse_args([]).exclude_file, default)

    def test_inherited_git_dir_is_ignored(self):
        repo = make_catalog_repo(self.tmp)
        other = os.path.join(self.tmp, 'other')
        os.makedirs(other)
        git(other, 'init', '-q')
        with unittest.mock.patch.dict(os.environ, {'GIT_DIR': os.path.join(repo, '.git')}):
            with self.assertRaisesRegex(SnapshotError, '^no catalog tag$'):
                materialize(other, exclude_file=self.exclude)

    def test_raw_sha_is_not_a_tag(self):
        repo = make_catalog_repo(self.tmp)
        sha = git(repo, 'rev-parse', 'HEAD').strip()
        for ref in (sha, sha[:12]):
            with self.assertRaisesRegex(SnapshotError, '^ref is not a tag$'):
                materialize(repo, ref, exclude_file=self.exclude)

    def test_repo_without_tags(self):
        repo = make_catalog_repo(self.tmp, tag=None)
        with self.assertRaisesRegex(SnapshotError, '^no catalog tag$'):
            materialize(repo, exclude_file=self.exclude)

    def test_cli_exits_2(self):
        repo = make_catalog_repo(self.tmp, tag=None)
        for extra, message in (([], 'no catalog tag'), (['--ref', 'main'], 'ref is not a tag')):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli.main(['--repo', repo, '--exclude-file', self.exclude] + extra)
            self.assertEqual(code, 2)
            self.assertEqual(err.getvalue().strip(), 'error: ' + message)


class OnlyDeliveredSkillsTests(_Temp):
    async def test_mcp6_held_and_other_folders_are_not_served(self):
        held = {'zz-' + 'canary': {}, 'sdd-' + 'x': {}}
        extra = {
            'inventory/x.json': '{"x": 1}\n',
            'freshness/state.json': '{}\n',
            'README.md': '# readme\n',
            'docs/SKILL.md': skill_md('docs'),
            'tools/x.py': 'x = 1\n',
        }
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill'], held=held, extra_files=extra)
        snap = self.snap(repo)
        on_disk = sorted(str(p.relative_to(snap.root)) for p in pathlib.Path(snap.root).rglob('*'))
        self.assertEqual(on_disk, ['alpha-skill', 'alpha-skill/SKILL.md'])
        async with Client(build_server(snap)) as client:
            uris = _uris(await client.list_resources())
            self.assertEqual(uris, ['skill://alpha-skill/SKILL.md'])
            for uri in ('skill://zz-canary/SKILL.md', 'skill://sdd-x/SKILL.md',
                        'skill://alpha-skill/../../inventory/x.json',
                        'skill://alpha-skill/../../catalog.json',
                        'skill://docs/SKILL.md'):
                with self.assertRaises(MCPError, msg=uri):
                    await client.read_resource(uri)
            with self.assertRaises(Exception):
                await client.read_skill('skill://sdd-x/SKILL.md')

    def test_folder_not_in_manifest_refuses_to_start(self):
        manifest = json.loads(catalog_json(['alpha-skill']))
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill', 'stray-skill'],
                                 manifest=json.dumps(manifest))
        with self.assertRaisesRegex(SnapshotError, r'^skills/ does not match catalog\.json: skills/stray-skill '):
            materialize(repo, exclude_file=self.exclude)

    def test_changed_hash_refuses_to_start(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS, extra_files={
            'skills/beta-skill/scripts/run.py': 'print("tampered")\n'})
        with self.assertRaises(SnapshotError) as ctx:
            materialize(repo, exclude_file=self.exclude)
        self.assertRegex(str(ctx.exception), r'^skills/ does not match catalog\.json: skills/beta-skill ')
        self.assertNotIn('tampered', str(ctx.exception))

    def test_unsafe_file_name_refuses_to_start(self):
        odd = 'a' + chr(92) + 'b.md'
        repo = make_catalog_repo(self.tmp, skills={'alpha-skill': {odd: 'x\n'}})
        with self.assertRaisesRegex(SnapshotError, '^unsafe path in archive$'):
            materialize(repo, exclude_file=self.exclude)

    def test_unsafe_paths_are_refused(self):
        from skills_catalog_mcp.snapshot import _inside
        sep = chr(92)
        for name in ('skills/../x', '/etc/x', 'skills/a' + sep + '..' + sep + 'x', 'C:/x',
                     'skills/a/b:c', 'skills/a/./b', 'skills/a/' + chr(0), '', '..'):
            self.assertFalse(_inside(self.tmp, name), repr(name))
        for name in ('catalog.json', 'skills', 'skills/', 'skills/a/b.md'):
            self.assertTrue(_inside(self.tmp, name), repr(name))

    def _without_data_filter(self):
        """Run as Python 3.11.0 to 3.11.3 would, which have no tarfile.data_filter."""
        import tarfile
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        if hasattr(tarfile, 'data_filter'):
            saved = tarfile.data_filter
            del tarfile.data_filter
            stack.callback(setattr, tarfile, 'data_filter', saved)
            # 3.12+ warns when no filter is given; keep the default explicit.
            stack.enter_context(unittest.mock.patch.object(
                tarfile.TarFile, 'extraction_filter',
                staticmethod(tarfile.fully_trusted_filter), create=True))
        return stack

    def test_extraction_without_the_data_filter(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        with self._without_data_filter():
            snap = self.snap(repo)
        self.assertEqual(sorted(os.listdir(snap.root)), ['alpha-skill', 'beta-skill'])

    def test_unsafe_name_refused_without_the_data_filter(self):
        odd = 'a' + chr(92) + '..' + chr(92) + 'b.md'
        repo = make_catalog_repo(self.tmp, skills={'alpha-skill': {odd: 'x\n'}})
        before = sorted(os.listdir(self.tmp))
        with self._without_data_filter():
            with self.assertRaisesRegex(SnapshotError, '^unsafe path in archive$'):
                materialize(repo, exclude_file=self.exclude)
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_symlink_refuses_to_start(self):
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill'])
        os.symlink('../../catalog.json', os.path.join(repo, 'skills', 'alpha-skill', 'link.json'))
        commit_all(repo, 'symlink', tag='v2099.1.1.2', stamp=next_stamp(1))
        with self.assertRaisesRegex(SnapshotError, 'symlink in skills/alpha-skill'):
            materialize(repo, exclude_file=self.exclude)

    def test_committed_ds_store_is_ignored(self):
        """CAT-26: a regular .DS_Store is left out of the manifest by tools/catalog.py; the tag still serves."""
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        write_files(repo, {'skills/alpha-skill/.DS_Store': b'\x00\x01',
                           'skills/alpha-skill/sub/.DS_Store': b'\x00\x02',
                           'skills/.DS_Store': b'\x00\x03'})
        git(repo, 'add', '-f', '--', 'skills', stamp=next_stamp(1))
        commit_all(repo, 'ds_store', tag='v2099.1.1.2', stamp=next_stamp(1))
        self.assertIn('alpha-skill/.DS_Store', _tag_files(repo, 'v2099.1.1.2'))
        snap = self.snap(repo)
        self.assertEqual(sorted(os.listdir(snap.root)), ['alpha-skill', 'beta-skill'])
        for dirpath, _dirs, files in os.walk(snap.root):
            self.assertNotIn('.DS_Store', files, dirpath)


class ExclusionTests(_Temp):
    async def test_excluded_skill_is_invisible(self):
        hidden = 'priv' + 'ate-' + 'tool'
        missing = 'nothere-' + 'skil'
        repo = make_catalog_repo(self.tmp, skills={'alpha-skill': {}, hidden: {'notes.md': 'secret body\n'}})
        with open(self.exclude, 'w', encoding='utf-8') as handle:
            handle.write('# local list\n' + hidden.upper() + '  # case-insensitive\n')

        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        # FastMCP's own logger does not propagate; capture it at the level
        # the CLI runs it (INFO, see __main__.quiet_fastmcp_debug).
        fastmcp_logger = logging.getLogger('fastmcp')
        old_level = fastmcp_logger.level
        fastmcp_logger.addHandler(handler)
        cli.quiet_fastmcp_debug()
        self.addCleanup(fastmcp_logger.removeHandler, handler)
        self.addCleanup(fastmcp_logger.setLevel, old_level)

        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertLogs(level='DEBUG') as logs:
            snap = self.snap(repo)
            async with Client(build_server(snap)) as client:
                listed = [str(r.uri) for r in await client.list_resources()]
                answers = {}
                for name in (hidden, missing):
                    for rel in ('SKILL.md', 'notes.md'):
                        with self.assertRaises(MCPError) as ctx:
                            await client.read_resource('skill://%s/%s' % (name, rel))
                        e = ctx.exception
                        answer = (e.code, str(e), repr(getattr(e, 'data', None)))
                        self.assertNotIn(name, repr(answer))
                        answers.setdefault(rel, []).append(answer)
        for rel, pair in answers.items():
            self.assertEqual(pair[0], pair[1], rel)
        self.assertEqual(listed, [u for u in listed if hidden not in u])
        self.assertIn('skill://alpha-skill/SKILL.md', listed)
        self.assertNotIn(hidden, [e['name'] for e in snap.catalog])
        on_disk = [str(p) for p in pathlib.Path(snap.root).parent.rglob('*')]
        self.assertFalse([p for p in on_disk if hidden in p.lower()])
        output = '\n'.join(logs.output) + err.getvalue() + captured.getvalue()
        self.assertNotIn(hidden, output.lower())
        self.assertIn('skipped 1 skill(s) from the local exclusion list', output)

    async def test_skills_get_does_not_echo_a_name(self):
        hidden = 'priv' + 'ate-' + 'tool'
        missing = 'nothere-' + 'skil'
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill', hidden])
        with open(self.exclude, 'w', encoding='utf-8') as handle:
            handle.write(hidden + '\n')
        async with Client(build_server(self.snap(repo)), extensions=[SkillsClientExtension()]) as client:
            listed = [s.uri for s in await client.list_skills()]
            self.assertEqual(listed, ['skill://alpha-skill/SKILL.md'])
            entry = await client.get_skill('skill://alpha-skill/SKILL.md')
            self.assertEqual(entry.uri, 'skill://alpha-skill/SKILL.md')
            answers = []
            for name in (hidden, missing):
                with self.assertRaises(MCPError) as ctx:
                    await client.get_skill('skill://%s/SKILL.md' % name)
                e = ctx.exception
                answer = (e.code, str(e), repr(getattr(e, 'data', None)))
                self.assertNotIn(name, repr(answer))
                answers.append(answer)
        self.assertEqual(answers[0], answers[1])

    def test_no_count_logged_without_exclusions(self):
        repo = make_catalog_repo(self.tmp)
        logger = logging.getLogger('skills_catalog_mcp')
        with self.assertNoLogs(logger, level='DEBUG'):
            self.snap(repo)

    def test_excluded_skill_is_dropped_before_checks(self):
        hidden = 'hidden-' + 'one'
        repo = make_catalog_repo(self.tmp, skills=['alpha-skill', hidden], extra_files={
            'skills/%s/SKILL.md' % hidden: 'changed after hashing\n'})
        with open(self.exclude, 'w', encoding='utf-8') as handle:
            handle.write(hidden + '\n')
        snap = self.snap(repo)
        self.assertEqual(sorted(os.listdir(snap.root)), ['alpha-skill'])


class RealRepositoryTests(_Temp):
    async def test_serves_exactly_the_catalog_skills(self):
        clone = os.path.join(self.tmp, 'real')
        git(self.tmp, 'init', '-q', clone)
        git(clone, 'fetch', '-q', '--depth', '1', os.fspath(REPO_ROOT), '+HEAD:refs/tags/v2099.9.9.9')
        tagged = json.loads(git(clone, 'show', 'v2099.9.9.9:catalog.json'))
        names = sorted(e['name'] for e in tagged['skills'])
        self.assertEqual(len(names), 12)
        snap = self.snap(clone)
        async with Client(build_server(snap)) as client:
            served = sorted(str(r.uri)[len('skill://'):-len('/SKILL.md')]
                            for r in await client.list_skill_resources())
        self.assertEqual(served, names)
        self.assertFalse([n for n in served if n.startswith('sdd-') or n.startswith('zz-')])


if __name__ == '__main__':
    unittest.main()
