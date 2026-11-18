"""Transports for the server. stdio only; M4 part 2 adds HTTP."""

TRANSPORTS = ('stdio',)


def serve(mcp, args):
    """Run mcp on the transport args.transport names, until the client disconnects."""
    if args.transport != 'stdio':
        raise ValueError('unsupported transport: ' + str(args.transport))
    mcp.run(transport='stdio', show_banner=False)
