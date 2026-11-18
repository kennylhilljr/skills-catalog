"""Verified install of the served skills into an empty folder (MCP-7, DESIGN 4.4).

`sync_skills()` is the catalog's own sync, built on the FastMCP 4.2 client
API (`list_skill_resources()`, `read_skill()`, `Skill.to_directory()`), since
`fastmcp.utilities.skills.sync_skills` is deprecated and removed in 4.3.

`read_skill()` checks the size and sha256 of every file against the manifest
the server announced. Every skill is read and verified before anything is
written, so one bad skill means nothing is written at all.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import shutil

from mcp.shared.exceptions import MCPError


async def sync_skills(client, target_dir) -> list[pathlib.Path]:
    """Install every skill the connected `client` lists into `target_dir`.

    `target_dir` must not exist or be an empty folder; anything else raises
    `ValueError`, so this never merges into or overwrites a folder. Returns
    the written skill folders, one per skill, in name order. A skill that
    fails to read or verify raises `ValueError` naming its URI, and nothing
    is written.
    """
    target = pathlib.Path(target_dir)
    _check_target(target)

    skills = {}
    for resource in await client.list_skill_resources():
        uri = str(resource.uri)
        try:
            skill = await client.read_skill(uri)
        except (ValueError, MCPError) as exc:
            raise ValueError('cannot verify skill ' + uri + ': ' + _reason(exc)) from exc
        name = skill.name
        if not name or name in ('.', '..') or '/' in name or '\\' in name:
            raise ValueError('cannot verify skill ' + uri + ': unsafe skill name')
        if name in skills:
            raise ValueError('cannot verify skill ' + uri + ': two skills share its name')
        skills[name] = skill

    _check_target(target)  # it may have changed while the skills were read
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / ('.' + target.name + '-sync-' + secrets.token_hex(6))
    os.mkdir(staging)
    try:
        for name in sorted(skills):
            skills[name].to_directory(staging / name)
        if target.exists():
            os.rmdir(target)  # empty, checked above; fails if something appeared
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return [target / name for name in sorted(skills)]


def _check_target(target):
    if target.is_symlink():
        raise ValueError('target folder is a symlink')
    if target.exists():
        if not target.is_dir():
            raise ValueError('target exists and is not a folder')
        if any(target.iterdir()):
            raise ValueError('target folder is not empty')


def _reason(exc):
    if isinstance(exc, MCPError):
        return 'the server refused a read'
    return str(exc)
