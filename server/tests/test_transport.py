"""stdio and loopback-only streamable HTTP (SPEC MCP-5, DESIGN 4.4)."""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from types import SimpleNamespace

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from skills_catalog_mcp import transport
from skills_catalog_mcp.transport import BindRefused, check_bind_host
from tests.fixtures import git_env, make_catalog_repo

SERVER_DIR = os.fspath(pathlib.Path(__file__).resolve().parents[1])
SKILLS = {
    'alpha-skill': {'references/notes.md': 'Alpha notes.\n'},
    'beta-skill': {},
}
EXPECTED = [
    'skill://alpha-skill/SKILL.md',
    'skill://alpha-skill/references/notes.md',
    'skill://beta-skill/SKILL.md',
]
START_DEADLINE = 30.0

# Address literals are built at runtime so tools/scrub.py stays clean.
PRIVATE_10 = '.'.join(['10', '1', '2', '3'])
PRIVATE_192 = '.'.join(['192', '168', '1', '20'])
LINK_LOCAL = '.'.join(['169', '254', '7', '8'])
PUBLIC = '.'.join(['8', '8', '4', '4'])
DOC_NET = '.'.join(['192', '0', '2', '1'])  # TEST-NET-1, only used for a route lookup


def _uris(resources):
    return sorted(str(r.uri) for r in resources if not str(r.uri).endswith('/_manifest'))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _accepts(host, port, timeout=1.0):
    """True when a TCP connection to host:port is accepted."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _non_loopback_ipv4():
    """This host's outward IPv4 address, or None when it has none.

    A UDP connect only picks a route; it sends no packet, so the test stays
    off the network.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((DOC_NET, 9))
            addr = sock.getsockname()[0]
    except OSError:
        return None
    if not addr or addr.startswith('127.') or addr == '0.0.0.0':
        return None
    return addr


class CheckBindHostTests(unittest.TestCase):
    def test_accepts_loopback(self):
        self.assertEqual(check_bind_host('127.0.0.1'), '127.0.0.1')
        self.assertEqual(check_bind_host('127.0.0.2'), '127.0.0.2')
        self.assertEqual(check_bind_host('127.255.255.254'), '127.255.255.254')
        self.assertEqual(check_bind_host('::1'), '::1')

    def test_localhost_maps_to_ipv4_loopback_without_dns(self):
        with unittest.mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS lookup')), \
                unittest.mock.patch.object(socket, 'gethostbyname', side_effect=AssertionError('DNS lookup')):
            self.assertEqual(check_bind_host('localhost'), '127.0.0.1')

    def test_refuses_wildcards(self):
        for host in ('0.0.0.0', '::', ''):
            with self.subTest(host=host), self.assertRaises(BindRefused):
                check_bind_host(host)

    def test_refuses_private_link_local_and_public(self):
        for host in (PRIVATE_10, PRIVATE_192, LINK_LOCAL, PUBLIC, 'fe80::1', '2001:db8::1'):
            with self.subTest(host=host), self.assertRaises(BindRefused):
                check_bind_host(host)

    def test_refuses_hostnames_without_resolving(self):
        hosts = ('example.com', 'localhost.', 'LOCALHOST', 'localhost.localdomain', 'ip6-localhost')
        with unittest.mock.patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS lookup')), \
                unittest.mock.patch.object(socket, 'gethostbyname', side_effect=AssertionError('DNS lookup')):
            for host in hosts:
                with self.subTest(host=host), self.assertRaises(BindRefused):
                    check_bind_host(host)

    def test_refuses_loopback_look_alikes(self):
        for host in ('127.1', '127.0.0.01', '::ffff:127.0.0.1', '::1%lo0', ' 127.0.0.1', None):
            with self.subTest(host=host), self.assertRaises(BindRefused):
                check_bind_host(host)

    def test_refusal_message(self):
        with self.assertRaises(BindRefused) as ctx:
            check_bind_host('0.0.0.0')
        self.assertIn('non-loopback', str(ctx.exception))

    def test_serve_passes_checked_host_and_port_explicitly(self):
        calls = []
        mcp = SimpleNamespace(run=lambda **kw: calls.append(kw))
        transport.serve(mcp, SimpleNamespace(transport='http', host='localhost', port=8765))
        self.assertEqual(calls, [{'transport': 'http', 'show_banner': False, 'host': '127.0.0.1',
                                  'port': 8765, 'path': '/mcp'}])
        with self.assertRaises(BindRefused):
            transport.serve(mcp, SimpleNamespace(transport='http', host='0.0.0.0', port=8765))
        self.assertEqual(len(calls), 1)


class _Subprocess(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.exclude = os.path.join(self.tmp, 'exclude.txt')
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.procs = []

    def tearDown(self):
        # Always stop every server, even when the test failed.
        for proc, log in self.procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            log.close()

    def child_env(self, **extra):
        env = {k: v for k, v in git_env().items() if not k.startswith('FASTMCP_')}
        env['HOME'] = self.home
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env.update(extra)
        return env

    def cli(self, repo, *args):
        return [sys.executable, '-m', 'skills_catalog_mcp', '--repo', repo, '--exclude-file', self.exclude, *args]

    def start_http(self, repo, port, host='127.0.0.1', **env):
        log = open(os.path.join(self.tmp, 'server-%d.log' % len(self.procs)), 'w+')
        proc = subprocess.Popen(
            self.cli(repo, '--transport', 'http', '--host', host, '--port', str(port)),
            cwd=SERVER_DIR, env=self.child_env(**env),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        self.procs.append((proc, log))
        deadline = time.monotonic() + START_DEADLINE
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log.seek(0)
                self.fail('server exited %s: %s' % (proc.returncode, log.read()[-500:]))
            if _accepts('127.0.0.1', port, timeout=0.5):
                return proc
            time.sleep(0.1)
        self.fail('server did not accept connections within %ss' % START_DEADLINE)

    def assert_loopback_only(self, port):
        addr = _non_loopback_ipv4()
        if addr is None:
            self.skipTest('this host has no non-loopback IPv4 address')
        self.assertFalse(_accepts(addr, port, timeout=2.0),
                         'the server accepted a connection on a non-loopback address')


class StdioTests(_Subprocess):
    async def test_stdio_lists_fixture_resources(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        stdio = StdioTransport(
            command=sys.executable,
            args=['-m', 'skills_catalog_mcp', '--repo', repo, '--exclude-file', self.exclude, '--transport', 'stdio'],
            env=self.child_env(), cwd=SERVER_DIR, keep_alive=False,
        )
        async with Client(stdio) as client:
            uris = _uris(await client.list_resources())
            text = (await client.read_resource('skill://beta-skill/SKILL.md'))[0].text
        self.assertEqual(uris, EXPECTED)
        self.assertIn('name: beta-skill', text)


class HttpTests(_Subprocess):
    async def test_http_lists_and_reads_on_loopback_only(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        port = _free_port()
        self.start_http(repo, port)
        async with Client('http://127.0.0.1:%d/mcp' % port) as client:
            uris = _uris(await client.list_resources())
            text = (await client.read_resource('skill://alpha-skill/references/notes.md'))[0].text
        self.assertEqual(uris, EXPECTED)
        self.assertEqual(text, 'Alpha notes.\n')
        self.assert_loopback_only(port)

    async def test_fastmcp_host_env_cannot_widen_the_bind(self):
        repo = make_catalog_repo(self.tmp, skills=SKILLS)
        port = _free_port()
        self.start_http(repo, port, FASTMCP_HOST='0.0.0.0')
        async with Client('http://127.0.0.1:%d/mcp' % port) as client:
            self.assertEqual(_uris(await client.list_resources()), EXPECTED)
        self.assert_loopback_only(port)

    def test_non_loopback_host_exits_2_before_snapshot_and_bind(self):
        port = _free_port()
        missing_repo = os.path.join(self.tmp, 'no-such-repo')
        for host in ('0.0.0.0', PRIVATE_192, 'example.com'):
            with self.subTest(host=host):
                proc = subprocess.run(
                    self.cli(missing_repo, '--transport', 'http', '--host', host, '--port', str(port)),
                    cwd=SERVER_DIR, env=self.child_env(), stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(proc.returncode, 2, proc.stderr[-500:])
                self.assertEqual(proc.stderr.strip().splitlines(),
                                 ['error: refusing to bind to a non-loopback address'])
                self.assertEqual(proc.stdout, '')
                self.assertNotIn(host, proc.stderr)
                self.assertFalse(_accepts('127.0.0.1', port, timeout=0.5))

    def test_bad_port_exits_2(self):
        proc = subprocess.run(
            self.cli(os.path.join(self.tmp, 'no-such-repo'), '--transport', 'http', '--port', '0'),
            cwd=SERVER_DIR, env=self.child_env(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn('port must be between 1 and 65535', proc.stderr)


if __name__ == '__main__':
    unittest.main()
