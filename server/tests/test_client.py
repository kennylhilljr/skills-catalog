"""Skills extension listing and verified client sync (MCP-2, MCP-7)."""

from __future__ import annotations

import filecmp
import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import unittest.mock

from fastmcp import Client
from fastmcp.skills.client import SkillsClientExtension
from fastmcp.skills.provider import SkillsDirectoryProvider

from skills_catalog_mcp import app
from skills_catalog_mcp.app import build_server
from skills_catalog_mcp.client import sync_skills
from skills_catalog_mcp.snapshot import materialize
from tests.fixtures import commit_all, git, git_env, make_catalog_repo, next_stamp, write_files

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TAG = 'v2099.1.1.1'
PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 1, 2, 255, 254])
SKILLS = {
    'alpha-skill': {'references/notes.md': 'Alpha notes.\n', 'assets/logo.png': PNG},
    'beta-skill': {'scripts/run.py': 'print("beta")\n'},
    'gamma-skill': {},
}
HELD = {'zz-' + 'canary': {}, 'sdd-' + 'x': {}}
EXCLUDED = 'private-' + 'thing'


def _archive_skills(repo, ref, dest):
    """Extract `git archive <ref> skills/` of repo into dest and return dest/skills."""
    data = subprocess.run(['git', '-C', os.fspath(repo), 'archive', '--format=tar', ref, '--', 'skills'],
                          env=git_env(), stdout=subprocess.PIPE, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        if hasattr(tarfile, 'data_filter'):
            archive.extractall(dest, filter='data')
        else:
            archive.extractall(dest)
    return os.path.join(dest, 'skills')


def _tree(root):
    """Map each file under root (relative, '/'-separated) to its bytes."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full, 'rb') as handle:
                out[os.path.relpath(full, root).replace(os.sep, '/')] = handle.read()
    return out


def _catalog_at(repo, ref):
    text = git(repo, 'show', ref + ':catalog.json')
    return json.loads(text)


class _Temp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.exclude = os.path.join(self.tmp, 'exclude.txt')
        with open(self.exclude, 'w', encoding='utf-8') as handle:
            handle.write('# local exclusion list for the test\n')

    def snap(self, repo, ref=None):
        snap = materialize(repo, ref, exclude_file=self.exclude)
        self.addCleanup(snap.cleanup)
        return snap

    def assertSameTree(self, left, right):
        """Recursive dircmp with no left-only, right-only, diff or funny entries, plus a byte check."""
        def walk(cmp, where):
            self.assertEqual(cmp.left_only, [], where)
            self.assertEqual(cmp.right_only, [], where)
            self.assertEqual(cmp.diff_files, [], where)
            self.assertEqual(cmp.funny_files, [], where)
            self.assertEqual(cmp.common_funny, [], where)
            for name, sub in cmp.subdirs.items():
                walk(sub, where + '/' + name)
        walk(filecmp.dircmp(left, right), '.')
        self.assertEqual(_tree(left), _tree(right))


class ExtensionListingTests(_Temp):
    async def test_list_skills_entries_match_the_tag_manifest(self):
        with open(self.exclude, 'a', encoding='utf-8') as handle:
            handle.write(EXCLUDED.upper() + '\n')
        skills = dict(SKILLS)
        skills[EXCLUDED] = {'notes.md': 'never served\n'}
        repo = make_catalog_repo(self.tmp, skills=skills, held=HELD)
        manifest = {e['name']: e for e in _catalog_at(repo, TAG)['skills']}
        async with Client(build_server(self.snap(repo)), extensions=[SkillsClientExtension()]) as client:
            entries = await client.list_skills()
        names = sorted(e.frontmatter.name for e in entries)
        self.assertEqual(names, sorted(SKILLS))
        for entry in entries:
            name = entry.frontmatter.name
            self.assertEqual(str(entry.uri), 'skill://%s/SKILL.md' % name)
            self.assertEqual(entry.frontmatter.description, 'Use when testing the catalog server with %s.' % name)
            prefix = 'skill://%s/' % name
            digests = {str(r.uri)[len(prefix):]: r.digest for r in entry.resources}
            self.assertEqual(digests, {rel: 'sha256:' + h for rel, h in manifest[name]['files'].items()})
        for hidden in list(HELD) + [EXCLUDED]:
            self.assertNotIn(hidden, names)
            self.assertNotIn(hidden, json.dumps([e.model_dump(mode='json') for e in entries]))

    async def test_list_skills_needs_the_extension_but_resources_do_not(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        async with Client(build_server(self.snap(repo))) as client:
            with self.assertRaises(RuntimeError):
                await client.list_skills()
            uris = sorted(str(r.uri) for r in await client.list_skill_resources())
            text = (await client.read_resource('skill://alpha-skill/SKILL.md'))[0].text
        self.assertEqual(uris, sorted('skill://%s/SKILL.md' % n for n in SKILLS))
        self.assertIn('name: alpha-skill', text)


class SyncTests(_Temp):
    async def test_sync_is_byte_identical_to_the_tag(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS, held=HELD)
        # A change on main after the tag must not reach the sync.
        write_files(repo, {'skills/alpha-skill/references/notes.md': 'Changed on main.\n',
                           'skills/beta-skill/extra.md': 'New on main.\n'})
        commit_all(repo, 'after the tag', stamp=next_stamp(1))
        snap = self.snap(repo, TAG)
        target = pathlib.Path(self.tmp, 'out')
        for extensions in ([], [SkillsClientExtension()]):
            with self.subTest(extension=bool(extensions)):
                async with Client(build_server(snap), extensions=extensions) as client:
                    written = await sync_skills(client, target)
                self.assertEqual(written, [target / n for n in sorted(SKILLS)])
                expected = _archive_skills(repo, TAG, os.path.join(self.tmp, 'archive-%d' % len(extensions)))
                self.assertSameTree(target, expected)
                self.assertEqual((target / 'alpha-skill/references/notes.md').read_bytes(), b'Alpha notes.\n')
                self.assertFalse((target / 'beta-skill/extra.md').exists())
                self.assertEqual((target / 'alpha-skill/assets/logo.png').read_bytes(), PNG)
                shutil.rmtree(target)

    async def test_sync_into_an_existing_empty_folder(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        target = pathlib.Path(self.tmp, 'empty')
        target.mkdir()
        async with Client(build_server(self.snap(repo))) as client:
            await sync_skills(client, target)
        self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(SKILLS))
        self.assertEqual(sorted(p.name for p in pathlib.Path(self.tmp).iterdir() if p.name.startswith('.')), [])

    async def test_tampered_digest_fails_and_writes_nothing(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)

        class Tampering(SkillsDirectoryProvider):
            """Announces the true digests but serves other bytes for one file."""

            async def read_file(self, resource):
                data = await super().read_file(resource)
                if resource.skill_path == 'beta-skill' and resource.file_path == 'scripts/run.py':
                    return data.replace(b'beta', b'evil')
                return data

        snap = self.snap(repo)
        with unittest.mock.patch.object(app, 'SkillsDirectoryProvider', Tampering):
            server = build_server(snap)
        for extensions in ([], [SkillsClientExtension()]):
            with self.subTest(extension=bool(extensions)):
                target = pathlib.Path(self.tmp, 'out-%d' % len(extensions))
                async with Client(server, extensions=extensions) as client:
                    with self.assertRaisesRegex(ValueError, 'digest'):
                        await client.read_skill('skill://beta-skill/SKILL.md')
                    alpha = await client.read_skill('skill://alpha-skill/SKILL.md')
                    self.assertEqual(alpha.name, 'alpha-skill')
                    with self.assertRaises(ValueError) as caught:
                        await sync_skills(client, target)
                self.assertIn('skill://beta-skill/SKILL.md', str(caught.exception))
                self.assertFalse(target.exists())
        self.assertEqual([p.name for p in pathlib.Path(self.tmp).iterdir() if p.name.startswith('.')], [])

    async def test_non_empty_target_is_refused(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        target = pathlib.Path(self.tmp, 'busy')
        target.mkdir()
        (target / 'keep.txt').write_text('mine\n', encoding='utf-8')
        afile = pathlib.Path(self.tmp, 'afile')
        afile.write_text('x', encoding='utf-8')
        async with Client(build_server(self.snap(repo))) as client:
            with self.assertRaises(ValueError):
                await sync_skills(client, target)
            with self.assertRaises(ValueError):
                await sync_skills(client, afile)
        self.assertEqual([p.name for p in target.iterdir()], ['keep.txt'])
        self.assertEqual(afile.read_text(encoding='utf-8'), 'x')


class RealRepositoryTests(_Temp):
    async def test_sync_of_this_repository_equals_skills_at_the_tag(self):
        clone = os.path.join(self.tmp, 'clone')
        subprocess.run(['git', 'clone', '-q', '--no-hardlinks', os.fspath(REPO_ROOT), clone],
                       env=git_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        tag = 'v2099.12.31.1'
        git(clone, 'tag', '-a', tag, '-m', tag)
        names = sorted(e['name'] for e in _catalog_at(clone, tag)['skills'])
        self.assertGreaterEqual(len(names), 12)
        target = pathlib.Path(self.tmp, 'out')
        async with Client(build_server(self.snap(clone, tag)), extensions=[SkillsClientExtension()]) as client:
            written = await sync_skills(client, target)
        self.assertEqual([p.name for p in written], names)
        self.assertSameTree(target, _archive_skills(clone, tag, os.path.join(self.tmp, 'archive')))


if __name__ == '__main__':
    unittest.main()
