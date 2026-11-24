"""python -m skills_catalog_mcp --repo PATH [--ref TAG] [--exclude-file PATH] [--transport stdio|http] [--host H] [--port N]
    [--tag-trust reviewed|any] [--main-ref REF]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from skills_catalog_mcp import transport
from skills_catalog_mcp.app import build_server
from skills_catalog_mcp.snapshot import DEFAULT_EXCLUDE_FILE, DEFAULT_MAIN_REF, SnapshotError, materialize

TAG_TRUST = ('reviewed', 'any')

DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m skills_catalog_mcp',
        description='Serve the skills of one catalog tag over MCP.',
    )
    parser.add_argument('--repo', default=DEFAULT_REPO, help='catalog git repository (default: the parent of server/)')
    parser.add_argument('--ref', default=None, help='tag to serve (default: the highest v* tag)')
    parser.add_argument('--exclude-file', default=DEFAULT_EXCLUDE_FILE, help='local exclusion list (default: %(default)s)')
    parser.add_argument('--transport', default='stdio', choices=transport.TRANSPORTS,
                        help='stdio (default) or streamable HTTP on loopback')
    parser.add_argument('--host', default=transport.DEFAULT_HOST,
                        help='HTTP bind address: 127.0.0.0/8, ::1 or localhost only (default: %(default)s)')
    parser.add_argument('--port', type=int, default=transport.DEFAULT_PORT, help='HTTP port (default: %(default)s)')
    parser.add_argument('--tag-trust', default=None, choices=TAG_TRUST,
                        help='reviewed: the tag must be annotated and on --main-ref; any: any v* tag '
                             '(default: reviewed for http, any for stdio)')
    parser.add_argument('--main-ref', default=DEFAULT_MAIN_REF,
                        help='the branch a reviewed tag must be on (default: %(default)s)')
    return parser.parse_args(argv)


def require_reviewed(args):
    """True when the tag must be reviewed: --tag-trust, else on for HTTP only."""
    trust = args.tag_trust or ('reviewed' if args.transport == 'http' else 'any')
    return trust == 'reviewed'


def quiet_fastmcp_debug():
    """Keep FastMCP's logger at INFO or above.

    At DEBUG, FastMCP logs every requested URI, which would echo a name a
    client asked for, including an excluded one (INV-7).
    """
    logger = logging.getLogger('fastmcp')
    if logger.getEffectiveLevel() < logging.INFO:
        logger.setLevel(logging.INFO)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='skills-catalog-mcp: %(message)s')
    quiet_fastmcp_debug()
    if args.transport == 'http':
        # MCP-5: refuse before the snapshot is built and before any socket is opened.
        try:
            args.host = transport.check_bind_host(args.host)
        except transport.BindRefused:
            print('error: ' + transport.REFUSAL, file=sys.stderr)
            return 2
        try:
            transport.check_port(args.port)
        except ValueError as exc:
            print('error: %s' % exc, file=sys.stderr)
            return 2
    try:
        snapshot = materialize(args.repo, args.ref, exclude_file=args.exclude_file,
                               require_reviewed=require_reviewed(args), main_ref=args.main_ref)
    except SnapshotError as exc:
        print('error: %s' % exc, file=sys.stderr)
        return 2
    try:
        transport.serve(build_server(snapshot), args)
    finally:
        snapshot.cleanup()
    return 0


if __name__ == '__main__':
    sys.exit(main())
