"""Transports for the server: stdio, and streamable HTTP on loopback only (SPEC MCP-5).

DESIGN 4.4: stdio for local clients, and streamable HTTP on loopback for
OpenClaw. No remote exposure. A non-loopback bind is refused here, not just
off by default, and the checked address is passed to FastMCP explicitly so a
FASTMCP_HOST setting in the environment cannot widen it.
"""

from __future__ import annotations

import ipaddress

TRANSPORTS = ('stdio', 'http')
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8765
HTTP_PATH = '/mcp'
REFUSAL = 'refusing to bind to a non-loopback address'

_LOOPBACK_V4 = ipaddress.ip_network('127.0.0.0/8')
_LOOPBACK_V6 = ipaddress.IPv6Address('::1')


class BindRefused(ValueError):
    """The host is not a loopback address this server may bind to."""

    def __init__(self):
        super().__init__(REFUSAL)


def check_bind_host(host) -> str:
    """Return the address to bind for host, or raise BindRefused.

    Accepted: an IPv4 literal in 127.0.0.0/8, the IPv6 literal ::1, and the
    exact string 'localhost' (mapped to 127.0.0.1 without any DNS lookup).
    Everything else is refused, including 0.0.0.0, ::, private and link-local
    literals, public addresses and any other hostname. No name is resolved, so
    a name that resolves to loopback is refused too.
    """
    if not isinstance(host, str):
        raise BindRefused()
    if host == 'localhost':
        return DEFAULT_HOST
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused() from None
    if isinstance(addr, ipaddress.IPv4Address):
        if addr in _LOOPBACK_V4:
            return str(addr)
        raise BindRefused()
    # An IPv6 literal: only ::1 itself. A scope id (::1%lo0) or an
    # IPv4-mapped form (::ffff:127.0.0.1) is not equal to ::1 and is refused.
    if addr == _LOOPBACK_V6 and addr.scope_id is None:
        return str(addr)
    raise BindRefused()


def check_port(port) -> int:
    """Return port if it is a TCP port number a server can bind (1 to 65535)."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    return port


def serve(mcp, args):
    """Run mcp on the transport args.transport names, until the client disconnects or the process stops."""
    if args.transport == 'stdio':
        mcp.run(transport='stdio', show_banner=False)
        return
    if args.transport == 'http':
        host = check_bind_host(args.host)
        port = check_port(args.port)
        mcp.run(transport='http', show_banner=False, host=host, port=port, path=HTTP_PATH)
        return
    raise ValueError('unsupported transport: ' + str(args.transport))
