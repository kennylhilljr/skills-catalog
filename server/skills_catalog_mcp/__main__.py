"""python -m skills_catalog_mcp --repo PATH [--ref TAG] [--exclude-file PATH] [--transport stdio]"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from skills_catalog_mcp import transport
from skills_catalog_mcp.app import build_server
from skills_catalog_mcp.snapshot import DEFAULT_EXCLUDE_FILE, SnapshotError, materialize

DEFAULT_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m skills_catalog_mcp',
        description='Serve the skills of one catalog tag over MCP.',
    )
    parser.add_argument('--repo', default=DEFAULT_REPO, help='catalog git repository (default: the parent of server/)')
    parser.add_argument('--ref', default=None, help='tag to serve (default: the newest v* tag)')
    parser.add_argument('--exclude-file', default=DEFAULT_EXCLUDE_FILE, help='local exclusion list (default: %(default)s)')
    parser.add_argument('--transport', default='stdio', choices=transport.TRANSPORTS)
    return parser.parse_args(argv)


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
    try:
        snapshot = materialize(args.repo, args.ref, exclude_file=args.exclude_file)
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
