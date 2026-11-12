#!/usr/bin/env python3
"""Inventory agent skills (SKILL.md) on this machine and write inventory/own-skills.{json,md}.

Facts come from the filesystem, git remotes and `gh repo view`. Judgment (keep, merge
or drop) comes from inventory/decisions.json and is merged in by cluster.

Public-content rules applied here, not left to the reader:
- Skills in private repos or local-only folders: repo or location label, skill dir
  name and content hash only.
- Third-party skills: counts per source only.
- Paths are repo-relative or written with `~/`, never absolute.
- Skills named in the owner's local exclusion list (--exclude-file, untracked and
  outside the repo) are skipped entirely: not listed, clustered, decided or counted.

Python 3.9+, standard library only.

    python3 tools/inventory.py --root ~/Documents/GitHub --out inventory
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

SKIP_DIRS = {"node_modules", ".venv", "venv", "site-packages", ".git", "__pycache__"}
HOME = str(Path.home())


def tilde(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def find_skills(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader: top-level scalar keys only."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            out[key] = val.strip("\"'") if val not in ("|", ">", ">-", "|-") else ""
        elif key and line.startswith((" ", "\t")) and out.get(key, None) is not None:
            sub = line.strip()
            if re.match(r"^[A-Za-z0-9_-]+:", sub):
                out.setdefault("_nested", []).append(f"{key}.{sub.split(':', 1)[0]}")
            elif out[key] == "" or key == "description":
                out[key] = (out[key] + " " + sub).strip()
    return out


def load_exclusions(path: str) -> set:
    """Skill folder names from the local exclusion list: one per line, # starts a comment."""
    p = Path(os.path.expanduser(path))
    if not p.is_file():
        return set()
    names = (line.split("#", 1)[0].strip() for line in p.read_text().splitlines())
    return {n.lower() for n in names if n}


def run(cmd, cwd=None) -> str:
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


class Repos:
    def __init__(self, owner: str, offline: bool, visibility_overrides: dict):
        self.owner = owner.lower()
        self.offline = offline
        self.overrides = {k.lower(): v for k, v in visibility_overrides.items()}
        self._top, self._info = {}, {}

    def toplevel(self, d: Path) -> str:
        key = str(d)
        if key not in self._top:
            self._top[key] = run(["git", "rev-parse", "--show-toplevel"], cwd=d)
        return self._top[key]

    def info(self, top: str) -> dict:
        if top in self._info:
            return self._info[top]
        url = run(["git", "remote", "get-url", "origin"], cwd=top)
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$", url)
        slug = f"{m.group(1)}/{m.group(2)}" if m else ""
        own = bool(slug) and slug.split("/")[0].lower() == self.owner
        vis = "LOCAL_ONLY" if not slug else self.visibility(slug) if own else "THIRD_PARTY"
        info = {"slug": slug, "own": own, "visibility": vis, "checkout": Path(top).name}
        self._info[top] = info
        return info

    def visibility(self, slug: str) -> str:
        name = slug.split("/")[1].lower()
        if name in self.overrides:
            return self.overrides[name]
        if self.offline:
            return "UNKNOWN"
        out = run(["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"])
        return out or "UNKNOWN"


def load_lockfiles(paths):
    """Map skill name -> source repo from vercel skills CLI lockfiles."""
    out = {}
    for p in paths:
        p = Path(os.path.expanduser(p))
        if not p.is_file():
            continue
        data = json.loads(p.read_text())
        for name, entry in (data.get("skills") or {}).items():
            out[name] = entry.get("source", "unknown")
    return out


def is_public_own(rec) -> bool:
    return rec["own"] and rec["visibility"] == "PUBLIC"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default="~/Documents/GitHub")
    ap.add_argument("--owner", default="kennylhilljr")
    ap.add_argument("--global-dir", action="append", default=None,
                    help="global skill folders (default ~/.claude/skills ~/.codex/skills ~/.agents/skills)")
    ap.add_argument("--plugins-dir", default="~/.claude/plugins")
    ap.add_argument("--lockfile", action="append", default=None)
    ap.add_argument("--out", default="inventory")
    ap.add_argument("--offline", action="store_true", help="skip gh; visibility from --visibility file only")
    ap.add_argument("--visibility", default=None, help="JSON {repo: PUBLIC|PRIVATE} overrides")
    ap.add_argument("--exclude-file", default="~/.config/skills-catalog/exclude.txt",
                    help="local, untracked list of skill folder names to skip entirely")
    args = ap.parse_args()

    root = Path(os.path.expanduser(args.root)).resolve()
    here = Path(__file__).resolve().parent.parent
    out_dir = (here / args.out) if not os.path.isabs(args.out) else Path(args.out)
    global_dirs = args.global_dir or ["~/.claude/skills", "~/.codex/skills", "~/.agents/skills"]
    lockfiles = args.lockfile or [str(root / "skills-lock.json"), "~/.agents/.skill-lock.json"]
    overrides = json.loads(Path(args.visibility).read_text()) if args.visibility else {}
    aliases = json.loads((here / "tools" / "aliases.json").read_text())
    decisions_path = here / "inventory" / "decisions.json"
    decisions = json.loads(decisions_path.read_text()) if decisions_path.is_file() else {}
    excluded = load_exclusions(args.exclude_file)

    def is_excluded(skill_dir: Path) -> bool:
        return skill_dir.name.lower() in excluded

    decisions = {k: v for k, v in decisions.items() if k.lower() not in excluded}

    repos = Repos(args.owner, args.offline, overrides)
    lock = load_lockfiles(lockfiles)

    # 1. Repo skills under root.
    records = []
    for f in sorted(find_skills(root)):
        if is_excluded(f.parent):
            continue
        top = repos.toplevel(f.parent)
        if not top:
            # Local-only folder: the owner's and private, unless a skills lockfile says it is installed.
            info = {"slug": "", "own": True, "visibility": "LOCAL_ONLY", "checkout": f.relative_to(root).parts[0]}
            top = str(root / info["checkout"])
            if f.parent.name in lock:
                info = {**info, "own": False, "slug": lock[f.parent.name], "visibility": "THIRD_PARTY"}
        else:
            info = repos.info(top)
        raw = f.read_bytes()
        text = raw.decode("utf-8", "replace")
        fm = frontmatter(text)
        rel = str(f.parent.relative_to(top))
        records.append({
            "kind": "repo", **info, "_top": top, "rel_dir": rel, "dir_name": f.parent.name,
            "name": fm.get("name", ""), "description": fm.get("description", ""),
            "fm_keys": sorted([k for k in fm if not k.startswith("_")] + fm.get("_nested", [])),
            "lines": text.count("\n"), "sha256": sha256(raw), "_text": text,
        })

    # 2. Third-party index: content hash and (name, description) of every non-own skill,
    #    plus installed plugin skills.
    tp_hash, tp_desc = {}, {}
    plugin_root = Path(os.path.expanduser(args.plugins_dir))
    plugin_files = [f for f in find_skills(plugin_root) if not is_excluded(f.parent)] if plugin_root.is_dir() else []
    for r in records:
        if not r["own"]:
            tp_hash.setdefault(r["sha256"], r["slug"])
            if r["description"]:
                tp_desc.setdefault(r["description"], r["slug"])
    for f in plugin_files:
        raw = f.read_bytes()
        fm = frontmatter(raw.decode("utf-8", "replace"))
        src = "claude-plugin:" + (f.relative_to(plugin_root).parts[1] if len(f.relative_to(plugin_root).parts) > 1 else "?")
        tp_hash.setdefault(sha256(raw), src)
        if fm.get("description"):
            tp_desc.setdefault(fm["description"], src)

    # 3. Classify skills inside own repos.
    primary_checkout = {}
    for r in records:
        if r["own"] and r["slug"]:
            repo_name = r["slug"].split("/")[1].lower()
            if r["checkout"].lower() == repo_name:
                primary_checkout[r["slug"]] = r["checkout"]
    for r in records:
        if not r["own"]:
            r["class"] = "third-party"
            continue
        parts = Path(r["rel_dir"]).parts
        top_dir = Path(r["_top"])
        anc = [top_dir.joinpath(*parts[:i]) for i in range(len(parts), 0, -1)]
        if ".claude" in parts and "worktrees" in parts or "/.claude/worktrees/" in r["_top"]:
            r["class"], r["note"] = "checkout-duplicate", "inside a .claude/worktrees checkout"
            r["checkout"] = f"{r['slug'].split('/')[-1]} (.claude/worktrees)"
        elif r["slug"] and primary_checkout.get(r["slug"], r["checkout"]) != r["checkout"]:
            r["class"], r["note"] = "checkout-duplicate", f"second checkout of {r['slug']}"
        elif any((a / "ecc-install-state.json").is_file() for a in anc):
            r["class"], r["note"] = "vendored-third-party", "ECC install (ecc-install-state.json)"
            r["vendor_source"] = "affaan-m/ECC (everything-claude-code)"
        elif r["sha256"] in tp_hash:
            r["class"], r["vendor_source"] = "vendored-third-party", tp_hash[r["sha256"]]
            r["note"] = "content hash matches a third-party skill"
        elif r["description"] and r["description"] in tp_desc:
            r["class"], r["vendor_source"] = "vendored-third-party", tp_desc[r["description"]]
            r["note"] = "description matches a third-party skill"
        else:
            r["class"] = "own"
            hints = [k for k in r["fm_keys"] if re.search(r"mcpmarket|author|license|source", k, re.I)]
            if hints:
                r["provenance_flag"] = "frontmatter suggests outside origin: " + ", ".join(hints)
    for r in records:
        if r["class"] == "vendored-third-party":
            tp_hash.setdefault(r["sha256"], r["vendor_source"])
            if r["description"]:
                tp_desc.setdefault(r["description"], r["vendor_source"])
    tp_names = {}
    for r in records:
        if r["class"] in ("third-party", "vendored-third-party"):
            tp_names.setdefault(r["dir_name"], r.get("vendor_source") or r["slug"])
    for f in plugin_files:
        tp_names.setdefault(f.parent.name, "claude-plugin:" + f.relative_to(plugin_root).parts[1])
    overrides_path = here / "inventory" / "provenance-overrides.json"
    prov = json.loads(overrides_path.read_text()) if overrides_path.is_file() else {}
    synced_src = {}
    for gd in global_dirs:
        for man in Path(os.path.expanduser(gd)).glob("synced/*/manifest.json"):
            data = json.loads(man.read_text())
            for e in (data.get("skills") or []):
                synced_src[e.get("name")] = e.get("source")

    # 4. Global installs.
    repo_by_real = {}
    for r in records:
        repo_by_real[str((root / r["checkout"] / r["rel_dir"]).resolve())] = r
    globals_ = []
    for gd in global_dirs:
        gpath = Path(os.path.expanduser(gd))
        if not gpath.is_dir():
            continue
        for entry in sorted(gpath.iterdir()):
            cands = [entry] if (entry / "SKILL.md").is_file() else []
            if entry.name in (".system", "synced") and entry.is_dir():
                cands = [p.parent for p in find_skills(entry)]
            for skill_dir in cands:
                if is_excluded(skill_dir):
                    continue
                f = skill_dir / "SKILL.md"
                real = str(skill_dir.resolve())
                raw = f.read_bytes()
                fm = frontmatter(raw.decode("utf-8", "replace"))
                fm_src = fm.get("source") or fm.get("origin") or ""
                h = sha256(raw)
                g = {"kind": "global", "location": tilde(str(gpath)), "dir_name": skill_dir.name,
                     "is_link": skill_dir.is_symlink() or entry.is_symlink(), "sha256": h,
                     "name": fm.get("name", ""), "description": fm.get("description", ""),
                     "lines": raw.count(b"\n"), "_text": raw.decode("utf-8", "replace")}
                linked = repo_by_real.get(real)
                if linked:
                    g["class"] = "own-linked" if linked["class"] == "own" else linked["class"]
                    g["linked_repo"], g["linked_rel_dir"] = linked["slug"], linked["rel_dir"]
                    g["visibility"] = linked["visibility"]
                    g["vendor_source"] = linked.get("vendor_source", linked["slug"])
                elif entry.name == ".system":
                    g["class"], g["vendor_source"] = "third-party", "openai/codex (bundled .system)"
                elif prov.get(skill_dir.name) and prov[skill_dir.name] != "own":
                    g["class"], g["vendor_source"] = "third-party", prov[skill_dir.name]
                elif (entry.name == "synced" or "/skills/synced/" in real) and synced_src.get(skill_dir.name, "").startswith("anthropic"):
                    g["class"], g["vendor_source"] = "third-party", "anthropic (claude.ai built-in, synced)"
                elif entry.name == "synced" or "/skills/synced/" in real:
                    g["class"], g["vendor_source"] = "own-local", None
                    g["note"] = f"claude.ai synced skill, source {synced_src.get(skill_dir.name, '?')}"
                elif skill_dir.name in lock:
                    g["class"], g["vendor_source"] = "third-party", lock[skill_dir.name]
                elif h in tp_hash:
                    g["class"], g["vendor_source"] = "third-party", tp_hash[h]
                elif g["description"] and g["description"] in tp_desc:
                    g["class"], g["vendor_source"] = "third-party", tp_desc[g["description"]]
                elif fm_src:
                    src = re.search(r"github\.com/([^/]+/[^/\s\"']+)", fm_src)
                    g["class"] = "third-party"
                    g["vendor_source"] = src.group(1) if src else f"frontmatter source: {fm_src[:40]}"
                elif "metadata.openclaw" in fm.get("_nested", []) or '"openclaw"' in g["_text"][:800]:
                    g["class"], g["vendor_source"] = "third-party", "openclaw/openclaw"
                elif prov.get(skill_dir.name) == "own":
                    g["class"] = "own-local"
                elif skill_dir.name in tp_names:
                    g["class"], g["vendor_source"] = "third-party-likely", f"name match: {tp_names[skill_dir.name]}"
                else:
                    g["class"] = "own-local"
                    g["note"] = "provenance unconfirmed"
                globals_.append(g)
    # Deduplicate globals that are the same skill seen through several folders.
    seen = {}
    for g in globals_:
        key = (g["dir_name"], g["sha256"])
        if key in seen:
            seen[key]["also_in"].append(g["location"])
            g["dup"] = True
        else:
            g["also_in"] = []
            seen[key] = g
    globals_ = [g for g in globals_ if not g.get("dup")]

    # 5. Clusters over own skills (repo own + own-linked are the same files; use repo records).
    def cluster_key(dir_name: str) -> str:
        k = dir_name.lower()
        return aliases.get(k, k)

    own_items = [r for r in records if r["class"] == "own"] + [g for g in globals_ if g["class"] == "own-local"]
    clusters = defaultdict(list)
    for it in own_items:
        clusters[cluster_key(it["dir_name"])].append(it)

    def label(it):
        return it.get("checkout") and f"{it['slug'] or it['checkout']}:{it['rel_dir']}" or f"{it['location']}/{it['dir_name']}"

    def publishable(it):
        return (it["kind"] == "repo" and is_public_own(it))

    cluster_out = []
    for key in sorted(clusters):
        items = clusters[key]
        variants = sorted({it["sha256"] for it in items})
        pub = [it for it in items if publishable(it)]
        diffs = []
        if len(pub) > 1:
            base = max(pub, key=lambda it: it["lines"])
            for it in pub:
                if it is base or it["sha256"] == base["sha256"]:
                    continue
                sm = difflib.SequenceMatcher(None, base["_text"].splitlines(), it["_text"].splitlines())
                added = sum(j2 - j1 for op, i1, i2, j1, j2 in sm.get_opcodes() if op in ("insert", "replace"))
                removed = sum(i2 - i1 for op, i1, i2, j1, j2 in sm.get_opcodes() if op in ("delete", "replace"))
                diffs.append({"from": label(base), "to": label(it), "similarity": round(sm.ratio(), 2),
                              "lines_added": added, "lines_removed": removed})
        restricted = len(items) - len(pub)
        d = decisions.get(key, {})
        cluster_out.append({
            "cluster": key, "members": len(items), "distinct_contents": len(variants),
            "restricted_members": restricted, "diffs": diffs,
            "decision": d.get("decision", "undecided"), "canonical_name": d.get("canonical_name", key),
            "owner": d.get("owner", ""), "reason": d.get("reason", ""), "notes_on_diff": d.get("notes_on_diff", ""),
        })

    # 6. Emit.
    def public_view(it):
        if it["kind"] == "repo":
            if is_public_own(it):
                return {k: it[k] for k in ("slug", "checkout", "rel_dir", "dir_name", "name", "description",
                                           "lines", "sha256", "visibility", "class")} | \
                       {k: it[k] for k in ("note", "vendor_source", "provenance_flag") if k in it} | \
                       {"cluster": cluster_key(it["dir_name"])}
            return {"repo": (it["slug"].split("/")[1] if it["slug"] else it["checkout"]),
                    "dir_name": it["dir_name"], "sha256": it["sha256"], "visibility": it["visibility"],
                    "class": it["class"], "cluster": cluster_key(it["dir_name"]), "restricted": True}
        if it["class"] == "own-linked" and it.get("visibility") == "PUBLIC":
            return {"location": it["location"], "dir_name": it["dir_name"], "class": it["class"],
                    "linked_repo": it["linked_repo"], "linked_rel_dir": it["linked_rel_dir"],
                    "sha256": it["sha256"], "also_in": it["also_in"]}
        return {"location": it["location"], "dir_name": it["dir_name"], "sha256": it["sha256"],
                "class": it["class"], "also_in": it["also_in"], "restricted": True,
                "cluster": cluster_key(it["dir_name"])}

    own_repo = [r for r in records if r["own"]]
    tp_counts = Counter(r["slug"] for r in records if not r["own"])
    vendored_counts = Counter(r.get("vendor_source", "?") for r in own_repo if r["class"] == "vendored-third-party")
    dup_counts = Counter(r["checkout"] for r in own_repo if r["class"] == "checkout-duplicate")
    g_tp = Counter(g.get("vendor_source") or "?" for g in globals_ if g["class"] in ("third-party", "vendored-third-party"))
    g_likely = sorted(({"dir_name": g["dir_name"], "match": g["vendor_source"], "location": g["location"]}
                       for g in globals_ if g["class"] == "third-party-likely"), key=lambda x: x["dir_name"])
    plugins_file = plugin_root / "installed_plugins.json"
    plugins = sorted((json.loads(plugins_file.read_text()).get("plugins") or {}).keys()) if plugins_file.is_file() else []
    mkt_file = plugin_root / "known_marketplaces.json"
    mkts = json.loads(mkt_file.read_text()) if mkt_file.is_file() else {}

    def mkt_label(m):
        src = (mkts.get(m) or {}).get("source") or {}
        if src.get("source") == "directory":
            return "local directory marketplace (restricted)"
        return src.get("repo") or re.sub(r"^https://github\.com/|\.git$", "", src.get("url", "")) or m
    by_mkt = Counter(mkt_label(p.split("@", 1)[1]) if "@" in p else "?" for p in plugins)

    own_public = [public_view(r) for r in own_repo if r["class"] == "own"]
    own_restricted_repo = [public_view(r) for r in own_repo if r["class"] == "own" and not is_public_own(r)]
    own_public = [x for x in own_public if not x.get("restricted")]
    g_own = [public_view(g) for g in globals_ if g["class"] in ("own-linked", "own-local")]
    canonical = [c for c in cluster_out if c["decision"] in ("keep", "merge")]

    summary = {
        "skill_md_files_scanned": len(records),
        "distinct_names_scanned": len({r["dir_name"] for r in records}),
        "third_party_repo_files": sum(tp_counts.values()),
        "own_repo_files": len(own_repo),
        "own_repo_vendored_third_party": sum(vendored_counts.values()),
        "own_repo_checkout_duplicates": sum(dup_counts.values()),
        "own_skills_public_repos": len(own_public),
        "own_skills_private_repos": len(own_restricted_repo),
        "global_installs": len(globals_),
        "global_own_linked": sum(1 for g in globals_ if g["class"] == "own-linked"),
        "global_own_local": sum(1 for g in globals_ if g["class"] == "own-local"),
        "global_third_party": sum(g_tp.values()),
        "global_third_party_likely_name_match": len(g_likely),
        "installed_claude_plugins": len(plugins),
        "clusters": len(cluster_out),
        "clusters_with_duplicates": sum(1 for c in cluster_out if c["members"] > 1),
        "proposed_canonical": len(canonical),
    }
    doc = {
        "generated_by": "tools/inventory.py", "owner": args.owner, "summary": summary,
        "own_skills_public": own_public, "own_skills_private_restricted": own_restricted_repo,
        "global_installs_own": g_own,
        "global_installs_third_party_likely": g_likely,
        "third_party_counts": {"repo_clones": dict(tp_counts.most_common()),
                               "vendored_in_own_repos": dict(vendored_counts.most_common()),
                               "checkout_duplicates_in_own_repos": dict(dup_counts.most_common()),
                               "global_installs": dict(g_tp.most_common()),
                               "claude_plugins_by_marketplace": dict(by_mkt.most_common())},
        "clusters": cluster_out,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "own-skills.json").write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    (out_dir / "own-skills.md").write_text(render_md(doc))
    print(json.dumps(summary, indent=2))
    return 0


def md_escape(s: str, n: int = 160) -> str:
    s = (s or "").replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 3] + "..."


def render_md(doc: dict) -> str:
    s = doc["summary"]
    L = ["# Own skills inventory", "",
         "Generated by `tools/inventory.py`; decisions come from `inventory/decisions.json`. Do not edit by hand: "
         "change the decisions file or the script and regenerate.", "",
         "Skills in private repos and in local-only folders show only location, skill folder name and content hash. "
         "Third-party skills show counts per source only.", "", "## Headline numbers", "",
         "| Measure | Count |", "| --- | --- |"]
    for k, v in s.items():
        L.append(f"| {k.replace('_', ' ')} | {v} |")
    L += ["", "## Proposed canonical set", "",
          "| Cluster | Decision | Canonical name | Members | Distinct contents | Owner | Reason |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    order = {"keep": 0, "merge": 1, "drop": 2, "undecided": 3}
    for c in sorted(doc["clusters"], key=lambda c: (order.get(c["decision"], 9), c["cluster"])):
        L.append(f"| {c['cluster']} | {c['decision']} | {c['canonical_name']} | {c['members']} | "
                 f"{c['distinct_contents']} | {c['owner']} | {md_escape(c['reason'], 220)} |")
    L += ["", "## Duplicate clusters and their diffs", ""]
    for c in doc["clusters"]:
        if c["members"] < 2:
            continue
        L.append(f"### {c['cluster']}")
        L.append("")
        L.append(f"{c['members']} copies, {c['distinct_contents']} distinct contents"
                 + (f", {c['restricted_members']} in private or local-only locations (diff withheld)" if c["restricted_members"] else "") + ".")
        for d in c["diffs"]:
            L.append(f"- `{d['to']}` vs `{d['from']}`: similarity {d['similarity']}, "
                     f"+{d['lines_added']} / -{d['lines_removed']} lines")
        if c["notes_on_diff"]:
            L.append(f"- What differs: {c['notes_on_diff']}")
        L.append("")
    L += ["## Own skills in public repos", "",
          "| Repo | Path | Name | Description | Lines | sha256 (12) |", "| --- | --- | --- | --- | --- | --- |"]
    for r in sorted(doc["own_skills_public"], key=lambda r: (r["slug"], r["rel_dir"])):
        flag = f" (provenance: {r['provenance_flag']})" if r.get("provenance_flag") else ""
        L.append(f"| {r['slug']} | `{r['rel_dir']}` | {md_escape(r['name'], 40)} | "
                 f"{md_escape(r['description'])}{flag} | {r['lines']} | `{r['sha256'][:12]}` |")
    L += ["", "## Own skills in private repos (restricted)", "",
          "| Repo | Skill folder | sha256 (12) |", "| --- | --- | --- |"]
    for r in sorted(doc["own_skills_private_restricted"], key=lambda r: (r["repo"], r["dir_name"])):
        L.append(f"| {r['repo']} | {r['dir_name']} | `{r['sha256'][:12]}` |")
    L += ["", "## Global installs that are the owner's", "",
          "Linked entries point into a public repo; local entries are restricted.", "",
          "| Location | Skill folder | Class | Source | sha256 (12) |", "| --- | --- | --- | --- | --- |"]
    for g in sorted(doc["global_installs_own"], key=lambda g: (g["class"], g["dir_name"])):
        src = f"{g['linked_repo']}:{g['linked_rel_dir']}" if g.get("linked_repo") else "local only"
        locs = ", ".join([g["location"]] + g.get("also_in", []))
        L.append(f"| {locs} | {g['dir_name']} | {g['class']} | {src} | `{g['sha256'][:12]}` |")
    L += ["", "## Global installs that are probably third-party (name match only; confirm)", "",
          "| Location | Skill folder | Matched |", "| --- | --- | --- |"]
    for g in doc["global_installs_third_party_likely"]:
        L.append(f"| {g['location']} | {g['dir_name']} | {g['match']} |")
    L += ["", "## Third-party counts by source", ""]
    for section, counts in doc["third_party_counts"].items():
        L += [f"### {section.replace('_', ' ')}", "", "| Source | Count |", "| --- | --- |"]
        L += [f"| {k} | {v} |" for k, v in counts.items()]
        L.append("")
    return "\n".join(L).rstrip() + "\n"


if __name__ == "__main__":
    sys.exit(main())
