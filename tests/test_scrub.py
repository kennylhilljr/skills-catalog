"""Tests for tools/scrub.py, the public-content scrub (CAT-24, CAT-9).

Every value that looks like a secret, an address, a host, a home path or a
private repo name is built at runtime by concatenation, so this file itself
scans clean (the repository is public).
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRUB_PATH = os.path.join(REPO, 'tools', 'scrub.py')


def _load():
    spec = importlib.util.spec_from_file_location('catalog_scrub_under_test', SCRUB_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scrub = _load()

AT = chr(64)
FENCE = chr(96) * 3
TILDES = '~' * 3
NL = chr(10)
GH_TOKEN = 'gh' + 'p_' + 'A1b2C3d4' * 5
ENTROPY = 'Q7xK2mP9vL4n' + 'R8tW1zB6cY3h' + 'J5dF0gSaE2uI9oX'
EMAIL = 'someone' + AT + 'example.com'
IP = '10.' + '20.30.40'
PRIVATE = 'zz-' + 'private-repo'


def kinds(text, path='docs/x.md', names=()):
    return [k for _, k in scrub.scan_text(text, path, names)]


def write(root, rel, content, mode='w'):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, mode) as handle:
        handle.write(content)
    return full


def run_cli(args, env=None):
    return subprocess.run(
        [sys.executable, SCRUB_PATH] + list(args),
        capture_output=True,
        text=True,
        env=env,
    )


def git(root, *args):
    subprocess.run(['git', '-C', root] + list(args), check=True, capture_output=True)


class DetectionKinds(unittest.TestCase):
    def test_every_kind_is_reported(self):
        samples = [
            ('github-token', 'token ' + GH_TOKEN),
            ('github-token', 'token ' + 'gh' + 'o_' + 'Zz9' * 12),
            ('github-token', 'github' + '_pat_' + 'A1b2C3d4_' * 4),
            ('anthropic-key', 'key ' + 'sk-' + 'ant-' + 'api03-' + 'Zx9Yw8Vu7' * 3),
            ('openai-key', 'key ' + 'sk-' + 'proj-' + 'Zx9Yw8Vu7' * 3),
            ('openai-key', 'key ' + 'sk-' + 'Zx9Yw8Vu7' * 3),
            ('aws-key', 'id ' + 'AK' + 'IA' + 'Q2W3E4R5T6Y7U8I9'),
            ('aws-key', 'id ' + 'AS' + 'IA' + 'Q2W3E4R5T6Y7U8I9'),
            ('slack-token', 'xo' + 'xb-' + '123456789012-' + 'abcdefABCDEF'),
            ('slack-token', 'xo' + 'xp-' + '1234567890'),
            ('private-key', '-----BEGIN ' + 'RSA PRIVATE KEY-----'),
            ('private-key', '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'),
            ('private-key', '-----BEGIN ' + 'PRIVATE KEY-----'),
            ('email', 'mail ' + EMAIL + ' now'),
            ('private-ip', 'host ' + IP + ' up'),
            ('private-ip', 'host 192.' + '168.1.20 up'),
            ('private-ip', 'host 172.' + '16.0.1 up'),
            ('private-ip', 'host 172.' + '31.255.255 up'),
            ('private-ip', 'host 169.' + '254.1.1 up'),
            ('internal-host', 'see build-box' + '.internal'),
            ('internal-host', 'see mac-mini' + '.local now'),
            ('internal-host', 'see printer' + '.lan.'),
            ('internal-host', 'see node' + '.tail1234.ts' + '.net'),
            ('home-path', 'at /Users/' + 'alice/project'),
            ('home-path', 'at /home/' + 'bob/project'),
            ('high-entropy', 'blob ' + ENTROPY),
        ]
        for kind, text in samples:
            with self.subTest(kind=kind, index=samples.index((kind, text))):
                self.assertIn(kind, kinds(text))

    def test_thresholds_and_boundaries(self):
        # One character short of each minimum length is not reported.
        self.assertNotIn('github-token', kinds('gh' + 'p_' + 'A' * 35))
        self.assertIn('github-token', kinds('gh' + 'p_' + 'A' * 36))
        self.assertNotIn('github-token', kinds('github' + '_pat_' + 'A' * 21))
        self.assertNotIn('anthropic-key', kinds('sk-' + 'ant-' + 'a' * 19))
        self.assertNotIn('openai-key', kinds('sk-' + 'a' * 19))
        self.assertNotIn('aws-key', kinds('AK' + 'IA' + 'Q' * 15))
        # An AWS key id is exactly 16 characters after the prefix.
        self.assertIn('aws-key', kinds('id ' + 'AS' + 'IA' + 'Q2W3E4R5T6Y7U8I9' + '.'))
        self.assertNotIn('aws-key', kinds('AK' + 'IA' + 'Q' * 17))
        self.assertNotIn('aws-key', kinds('AK' + 'IA' + 'Q' * 16 + 'z'))
        self.assertNotIn('slack-token', kinds('xo' + 'xb-' + '1' * 9))
        # An Anthropic key is not also an OpenAI key.
        self.assertNotIn('openai-key', kinds('sk-' + 'ant-' + 'api03-' + 'Zx9Yw8Vu7' * 3))
        # Ordinary hyphenated words do not start a key match.
        self.assertEqual(kinds('risk-assessment-and-mitigation-plan-steps'), [])
        # Public and out-of-range addresses are not private.
        for text in ['8.8.8.8', '172.' + '15.0.1', '172.' + '32.0.1', '10.' + '1.2.256', '1' + '10.' + '1.2.3']:
            with self.subTest(text=text):
                self.assertNotIn('private-ip', kinds('ip ' + text))
        # The address must stand alone, not inside a longer dotted run.
        self.assertNotIn('private-ip', kinds('v ' + IP + '.5'))
        self.assertNotIn('private-ip', kinds('v 1.' + IP))
        self.assertIn('private-ip', kinds('ends at ' + IP + '.'))
        # Entropy: 32 characters is too short, 33 is enough; letters-only,
        # digits-only and low entropy runs pass.
        self.assertNotIn('high-entropy', kinds('x ' + ENTROPY[:32]))
        self.assertIn('high-entropy', kinds('x ' + ENTROPY[:33]))
        self.assertEqual(kinds('x ' + 'QwErTyUiOpAsDfGhJkLzXcVbNmQwErTyUiOp'), [])
        self.assertEqual(kinds('x ' + '1234567890' * 4), [])
        self.assertEqual(kinds('x ' + 'a1' * 20), [])
        # Email local parts are dot-atoms; domains need a dot.
        self.assertIn('email', kinds('mail first.last' + AT + 'example.com'))
        for text in ['first.' + AT + 'example.com', 'a..b' + AT + 'example.com',
                     'uses actions/checkout' + AT + 'v4', 'npm i pkg' + AT + 'latest']:
            with self.subTest(text=text):
                self.assertNotIn('email', kinds(text))
        # A slash breaks a run, so long repo-relative paths are fine.
        self.assertEqual(kinds('docs/' + ENTROPY[:20] + '/' + ENTROPY[20:]), [])

    def test_results_are_sorted_unique_and_keep_kinds_per_line(self):
        text = NL.join([
            'clean',
            'token ' + GH_TOKEN + ' and ' + GH_TOKEN + ' mail ' + EMAIL,
            'clean',
            'ip ' + IP,
        ])
        self.assertEqual(
            scrub.scan_text(text, 'docs/x.md'),
            [(2, 'email'), (2, 'github-token'), (4, 'private-ip')],
        )
        self.assertEqual(scrub.scan_text('a' + '\r' + NL + 'ip ' + IP + '\r' + NL), [(2, 'private-ip')])


class Exemptions(unittest.TestCase):
    def test_allowlisted_text_is_clean(self):
        clean = [
            'reply as noreply' + AT + 'github.com',
            'reply as NoReply' + AT + 'example.org',
            'bot 123+claude[bot]' + AT + 'users.noreply.github.com',
            'paths ~/.local/bin and ~/Documents/GitHub/x and /Users/<name>/ in prose',
            'the bare word .local and .internal in prose',
            'files .env.local and ~/.lan',
            'repo path src' + '/home/' + 'views/index.py and /home/ alone',
            'sha ' + 'a1' * 20 + ' and sha256 ' + '0f' * 32,
            'version 10.2.3 is not an address, nor is 1.2.3.4.5',
            'a long path tools/inventory/own-skills-generated-output-file.json',
        ]
        for text in clean:
            with self.subTest(text=text):
                self.assertEqual(scrub.scan_text(text, 'docs/x.md'), [])

    def test_example_fence_skips_only_entropy(self):
        inside = FENCE + 'example' + NL + ENTROPY + NL + FENCE + NL
        self.assertEqual(scrub.scan_text(inside, 'docs/x.md'), [])
        tilde = TILDES + 'example' + NL + ENTROPY + NL + TILDES + NL
        self.assertEqual(scrub.scan_text(tilde, 'docs/x.md'), [])
        # Other kinds stay active inside the fence.
        mixed = FENCE + 'example' + NL + ENTROPY + ' ' + EMAIL + NL + FENCE + NL
        self.assertEqual(scrub.scan_text(mixed, 'docs/x.md'), [(2, 'email')])
        # Detection resumes after the closing fence.
        after = inside + ENTROPY + NL
        self.assertEqual(scrub.scan_text(after, 'docs/x.md'), [(4, 'high-entropy')])
        # Only an info string of exactly `example` counts.
        for info in ['', 'sh', 'example extra', 'examples']:
            with self.subTest(info=info):
                text = FENCE + info + NL + ENTROPY + NL + FENCE + NL
                self.assertEqual(scrub.scan_text(text, 'docs/x.md'), [(2, 'high-entropy')])
        # An inner fence of another kind does not close the example fence.
        nested = FENCE * 2 + 'example' + NL + FENCE + NL + ENTROPY + NL + FENCE * 2 + NL
        self.assertEqual(scrub.scan_text(nested, 'docs/x.md'), [])

    def test_private_names_only_under_skills_and_held(self):
        line = 'see ' + PRIVATE + ' here'
        names = [PRIVATE]
        self.assertEqual(kinds(line, 'docs/x.md', names), [])
        self.assertEqual(kinds(line, 'README.md', names), [])
        self.assertEqual(kinds(line, 'skills/a/SKILL.md', names), ['private-repo'])
        self.assertEqual(kinds(line, '.held/a/SKILL.md', names), ['private-repo'])
        self.assertEqual(kinds(line, './skills/a/SKILL.md', names), ['private-repo'])
        self.assertEqual(kinds(line, os.path.join('skills', 'a', 'x.md'), names), ['private-repo'])
        # Case-insensitive, whole word.
        self.assertEqual(kinds('see ' + PRIVATE.upper(), 'skills/a/SKILL.md', names), ['private-repo'])
        self.assertEqual(kinds('see ' + PRIVATE + '.', 'skills/a/SKILL.md', names), ['private-repo'])
        self.assertEqual(kinds('see x' + PRIVATE, 'skills/a/SKILL.md', names), [])
        self.assertEqual(kinds('see ' + PRIVATE + '-two', 'skills/a/SKILL.md', names), [])
        # Regex characters in a name are literal.
        self.assertEqual(kinds('see a.b', 'skills/a/SKILL.md', ['a+b']), [])
        self.assertEqual(kinds('see a+b', 'skills/a/SKILL.md', ['a+b']), ['private-repo'])
        # No names, no private-repo findings.
        self.assertEqual(kinds(line, 'skills/a/SKILL.md', []), [])

    def test_catalog_owner_exemption_is_narrow(self):
        names = [PRIVATE]
        owner = '  catalog-owner: ' + chr(34) + PRIVATE + chr(34)
        self.assertEqual(scrub.scan_text(owner, '.held/a/SKILL.md', names), [])
        doc = NL.join(['---', 'name: a', 'metadata:', owner, '---', 'body', owner, ''])
        # Exempt in held frontmatter, not in the body.
        self.assertEqual(scrub.scan_text(doc, '.held/a/SKILL.md', names), [(7, 'private-repo')])
        # Not exempt under skills/.
        self.assertEqual(
            scrub.scan_text(doc, 'skills/a/SKILL.md', names), [(4, 'private-repo'), (7, 'private-repo')]
        )
        # Other kinds stay active on the owner line.
        owner_mail = '  catalog-owner: ' + chr(34) + PRIVATE + ' ' + EMAIL + chr(34)
        self.assertEqual(scrub.scan_text(owner_mail, '.held/a/SKILL.md', names), [(1, 'email')])
        # Without frontmatter, a body line is not exempt.
        body = NL.join(['# Title', owner, ''])
        self.assertEqual(scrub.scan_text(body, '.held/a/SKILL.md', names), [(2, 'private-repo')])
        # Only a held SKILL.md is exempt, not other files of the skill.
        for rel in ['.held/a/references/note.md', '.held/a/README.md']:
            with self.subTest(rel=rel):
                self.assertEqual(kinds(owner, rel, names), ['private-repo'])
        # Only the value is exempt, not an inline comment or a second name.
        other_name = 'yy-' + 'other-repo'
        both = [PRIVATE, other_name]
        for value in [chr(34) + PRIVATE + chr(34), chr(39) + PRIVATE + chr(39), PRIVATE]:
            with self.subTest(value=value):
                line = '  catalog-owner: ' + value
                self.assertEqual(kinds(line, '.held/a/SKILL.md', both), [])
                self.assertEqual(kinds(line + '  # ' + other_name, '.held/a/SKILL.md', both), ['private-repo'])
                self.assertEqual(kinds(line + ' # ' + PRIVATE, '.held/a/SKILL.md', both), ['private-repo'])
        # Text after a closed quoted value is not part of the value.
        trailing = owner + ' ' + other_name
        self.assertEqual(kinds(trailing, '.held/a/SKILL.md', both), ['private-repo'])
        # Only the exact two-space key is exempt.
        other = '    catalog-owner: ' + PRIVATE
        self.assertEqual(kinds(other, '.held/a/SKILL.md', names), ['private-repo'])


class ScanTree(unittest.TestCase):
    def test_git_tree_scans_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as root:
            git(root, 'init', '-q')
            write(root, 'tracked.md', 'one' + NL + 'mail ' + EMAIL + NL)
            write(root, 'skills/a/SKILL.md', 'uses ' + PRIVATE + NL)
            write(root, 'untracked.md', 'token ' + GH_TOKEN + NL)
            git(root, 'add', 'tracked.md', 'skills/a/SKILL.md')
            self.assertEqual(
                scrub.scan_tree(root, [PRIVATE]),
                [('skills/a/SKILL.md', 1, 'private-repo'), ('tracked.md', 2, 'email')],
            )
            # A tracked file deleted from the work tree is skipped.
            os.remove(os.path.join(root, 'tracked.md'))
            self.assertEqual(scrub.scan_tree(root, []), [])

    def test_git_subdirectory_root_keeps_repo_relative_classification(self):
        with tempfile.TemporaryDirectory() as root:
            git(root, 'init', '-q')
            write(root, 'skills/a/SKILL.md', 'uses ' + PRIVATE + NL)
            write(root, '.held/b/SKILL.md', 'uses ' + PRIVATE + NL)
            write(root, 'docs/x.md', 'uses ' + PRIVATE + NL)
            git(root, 'add', '-A')
            sub = os.path.join(root, 'skills')
            self.assertEqual(
                scrub.scan_tree(sub, [PRIVATE]), [(os.path.join('a', 'SKILL.md'), 1, 'private-repo')]
            )
            self.assertEqual(
                scrub.scan_tree(os.path.join(root, '.held'), [PRIVATE]),
                [(os.path.join('b', 'SKILL.md'), 1, 'private-repo')],
            )
            self.assertEqual(scrub.scan_tree(os.path.join(root, 'docs'), [PRIVATE]), [])

    def test_plain_tree_walks_all_files_but_git_dir_and_skips_non_utf8(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, '.git/config', 'mail ' + EMAIL + NL)
            write(root, 'sub/.git/x', 'mail ' + EMAIL + NL)
            write(root, 'a.md', 'ip ' + IP + NL)
            write(root, 'bin.dat', b'\xff\xfe' + ('mail ' + EMAIL).encode(), mode='wb')
            write(root, 'z/b.md', 'clean' + NL)
            self.assertEqual(scrub.scan_tree(root, []), [('a.md', 1, 'private-ip')])

    def test_symlink_target_text_is_scanned_not_followed(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            secret = write(outside, 'secret.md', 'mail ' + EMAIL + NL)
            os.symlink('/Users/' + 'alice/notes.md', os.path.join(root, 'dangling.md'))
            os.symlink(secret, os.path.join(root, 'outside.md'))
            os.symlink('docs/a.md', os.path.join(root, 'relative.md'))
            found = scrub.scan_tree(root, [])
            self.assertIn(('dangling.md', 1, 'home-path'), found)
            self.assertNotIn('email', [k for _, _, k in found])
            self.assertNotIn('relative.md', [r for r, _, _ in found])
            os.symlink('/home/' + 'bob/dir', os.path.join(root, 'linkdir'))
            os.symlink(outside, os.path.join(root, 'outdir'))
            found = scrub.scan_tree(root, [])
            self.assertIn(('linkdir', 1, 'home-path'), found)
            self.assertNotIn('email', [k for _, _, k in found])
            git(root, 'init', '-q')
            git(root, 'add', 'dangling.md', 'relative.md')
            self.assertEqual(scrub.scan_tree(root, []), [('dangling.md', 1, 'home-path')])

    def test_unreadable_file_is_an_error_not_clean(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, 'a.md', 'clean' + NL)
            with mock.patch('builtins.open', side_effect=PermissionError('denied')):
                with self.assertRaises(scrub.ScrubError):
                    scrub.scan_tree(root, [])
            with self.assertRaises(scrub.ScrubError):
                scrub.scan_tree(os.path.join(root, 'missing'), [])

    def test_catalog_scans_clean(self):
        # With no list, with a synthetic list, and over this change's new
        # source files explicitly, so untracked status cannot hide a leak.
        self.assertEqual(scrub.scan_tree(REPO, []), [])
        self.assertEqual(scrub.scan_tree(REPO, [PRIVATE, 'zz-' + 'other']), [])
        for rel in ['tools/scrub.py', 'tests/test_scrub.py', 'tests/__init__.py']:
            with self.subTest(rel=rel):
                with open(os.path.join(REPO, rel), encoding='utf-8') as handle:
                    self.assertEqual(scrub.scan_text(handle.read(), rel, [PRIVATE]), [])


class PrivateList(unittest.TestCase):
    def test_parsing(self):
        with tempfile.TemporaryDirectory() as root:
            path = write(root, 'list.txt', '# header' + NL + '  ' + PRIVATE + '  # trailing' + NL + NL + 'zz-b' + NL + 'zz-b' + NL)
            self.assertEqual(scrub.read_private_list(path), [PRIVATE, 'zz-b'])

    def test_default_list_is_used_only_when_present(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            write(root, 'skills/a/SKILL.md', 'uses ' + PRIVATE + NL)
            env = dict(os.environ, HOME=home)
            r = run_cli([root], env=env)
            self.assertEqual((r.returncode, r.stdout), (0, 'scrub: 0 findings' + NL))
            write(home, '.config/skills-catalog/private-repos.txt', PRIVATE + ' # note' + NL)
            r = run_cli([root], env=env)
            self.assertEqual(r.returncode, 1)
            self.assertIn('skills/a/SKILL.md:1: private-repo', r.stdout)
            # An explicit list replaces the default one.
            other = write(root, 'other.txt', 'zz-unrelated' + NL)
            r = run_cli(['--private-repos', other, root], env=env)
            self.assertEqual(r.returncode, 0)

    def test_missing_explicit_list_is_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            r = run_cli(['--private-repos', os.path.join(root, 'nope.txt'), root])
            self.assertEqual(r.returncode, 2)
            self.assertIn('cannot read the private repo list', r.stderr)


class Cli(unittest.TestCase):
    def test_output_format_exit_status_and_no_matched_values(self):
        secrets = [GH_TOKEN, EMAIL, IP, PRIVATE, ENTROPY]
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as home:
            write(root, 'docs.md', NL.join(['one', 'token ' + GH_TOKEN, 'mail ' + EMAIL, 'ip ' + IP, 'b ' + ENTROPY, '']))
            write(root, 'skills/a/SKILL.md', 'uses ' + PRIVATE + NL)
            listfile = write(home, 'list.txt', PRIVATE + NL)
            r = run_cli(['--private-repos', listfile, root], env=dict(os.environ, HOME=home))
            self.assertEqual(r.returncode, 1)
            self.assertEqual(r.stderr, '')
            self.assertEqual(
                r.stdout.splitlines(),
                [
                    'docs.md:2: github-token',
                    'docs.md:3: email',
                    'docs.md:4: private-ip',
                    'docs.md:5: high-entropy',
                    'skills/a/SKILL.md:1: private-repo',
                    'scrub: 5 findings',
                ],
            )
            out = r.stdout + r.stderr
            self.assertEqual([s for s in secrets if s in out], [])

    def test_clean_tree_and_bad_root(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as home:
            write(root, 'a.md', 'just text' + NL)
            env = dict(os.environ, HOME=home)
            r = run_cli([root], env=env)
            self.assertEqual((r.returncode, r.stdout), (0, 'scrub: 0 findings' + NL))
            r = run_cli([os.path.join(root, 'missing')], env=env)
            self.assertEqual(r.returncode, 2)
            self.assertEqual(r.stdout, '')

    def test_main_in_process_reads_no_list_when_absent(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as home:
            write(root, 'skills/a/SKILL.md', 'uses ' + PRIVATE + NL)
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {'HOME': home}), contextlib.redirect_stdout(buf):
                code = scrub.main([root])
            self.assertEqual((code, buf.getvalue()), (0, 'scrub: 0 findings' + NL))


class ImportBehavior(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        code = (
            'import importlib.util, sys' + NL
            + 'spec = importlib.util.spec_from_file_location("m", sys.argv[1])' + NL
            + 'm = importlib.util.module_from_spec(spec)' + NL
            + 'spec.loader.exec_module(m)' + NL
            + 'assert callable(m.scan_text) and callable(m.scan_tree)' + NL
        )
        with tempfile.TemporaryDirectory() as home:
            write(home, '.config/skills-catalog/private-repos.txt', PRIVATE + NL)
            r = subprocess.run(
                [sys.executable, '-c', code, SCRUB_PATH, '--private-repos', 'x'],
                capture_output=True,
                text=True,
                env=dict(os.environ, HOME=home),
            )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))


if __name__ == '__main__':
    unittest.main()
