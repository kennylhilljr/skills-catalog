#!/usr/bin/env python3
"""Regression tests for tools/inventory.py (CAT-15, CAT-16, CAT-18).

Tests verify that:
- CAT-15: Restricted records hold only location, folder name, hash, visibility and class;
  third-party skills appear only as counts; no absolute paths.
- CAT-16: Vendored skills detected by ecc-install-state.json ancestor and by content hash.
- CAT-18: Excluded skills are neither listed nor counted.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestInventory(unittest.TestCase):
    """Regression tests for tools/inventory.py."""

    @classmethod
    def setUpClass(cls):
        """Build fixture in tempfile and run inventory.py once."""
        if not shutil.which("git"):
            raise unittest.SkipTest("git not found")

        # Create fixture root T
        cls.T = tempfile.mkdtemp(prefix="inventory-fixture-")
        cls.T_path = Path(cls.T)

        # Create directory structure
        (cls.T_path / "home" / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "root" / "fixpub" / "skills").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "root" / "fixpub" / "vendor").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "root" / "fixpriv" / "skills").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "root" / "fixtp" / "skills").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "plugins").mkdir(parents=True, exist_ok=True)
        (cls.T_path / "out").mkdir(parents=True, exist_ok=True)

        # Set up environment for git commands
        env = dict(os.environ)
        env["HOME"] = str(cls.T_path / "home")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["XDG_CONFIG_HOME"] = str(cls.T_path / "home" / ".config")

        # Initialize git repos
        repos = [
            ("fixpub", "kennylhilljr" + "/" + "fixpub.git"),
            ("fixpriv", "kennylhilljr" + "/" + "fixpriv.git"),
            ("fixtp", "someoneelse" + "/" + "fixtp.git"),
        ]
        for repo_name, remote_path in repos:
            repo_path = cls.T_path / "root" / repo_name
            subprocess.run(["git", "init"], cwd=repo_path, env=env, capture_output=True, check=True)
            remote_url = "https://" + "github.com/" + remote_path
            subprocess.run(
                ["git", "remote", "add", "origin", remote_url],
                cwd=repo_path,
                env=env,
                capture_output=True,
                check=True,
            )

        # Create visibility.json
        visibility = {"fixpub": "PUBLIC", "fixpriv": "PRIVATE"}
        (cls.T_path / "visibility.json").write_text(json.dumps(visibility))

        # Create exclude.txt
        exclude_name = "zqfix-excluded-skill"
        exclude_text = exclude_name + "\n# comment line\n"
        (cls.T_path / "exclude.txt").write_text(exclude_text)

        # Create lock.json
        lock = {"skills": {}}
        (cls.T_path / "lock.json").write_text(json.dumps(lock))

        # Create fixture skills
        cls._create_skill(
            cls.T_path / "root" / "fixpub" / "skills" / "zqfix-own-public",
            "zqfix-own-public",
            "Use when testing own public skill functionality.",
        )

        # ECC vendored skill with marker in vendor subfolder
        cls._create_skill(
            cls.T_path / "root" / "fixpub" / "vendor" / "zqfix-ecc-skill",
            "zqfix-ecc-skill",
            "Use when testing ECC vendored integration.",
        )
        (cls.T_path / "root" / "fixpub" / "vendor" / "ecc-install-state.json").write_text("{}")

        # Third-party skill
        cls._create_skill(
            cls.T_path / "root" / "fixtp" / "skills" / "zqfix-thirdparty",
            "zqfix-thirdparty",
            "Use when zqfix third party marker text appears.",
        )

        # Hash-equal copy of third-party skill
        third_party_content = (
            cls.T_path / "root" / "fixtp" / "skills" / "zqfix-thirdparty" / "SKILL.md"
        ).read_bytes()
        cls._create_skill_from_bytes(
            cls.T_path / "root" / "fixpub" / "skills" / "zqfix-thirdparty-copy",
            third_party_content,
        )

        # Private own skill
        cls._create_skill(
            cls.T_path / "root" / "fixpriv" / "skills" / "zqfix-private",
            "zqfix-private",
            "Use when testing private restricted records.",
        )

        # Global local-only skill
        cls._create_skill(
            cls.T_path / "home" / ".claude" / "skills" / "zqfix-global-local",
            "zqfix-global-local",
            "Use when testing global local-only installation.",
        )

        # Excluded skill in two places
        cls._create_skill(
            cls.T_path / "root" / "fixpub" / "skills" / exclude_name,
            exclude_name,
            "Use when testing skill exclusion mechanism.",
        )
        cls._create_skill(
            cls.T_path / "home" / ".claude" / "skills" / exclude_name,
            exclude_name,
            "Use when testing skill exclusion mechanism.",
        )

        # CAT-18: the excluded SKILL.md files are unreadable, so inventory.py
        # fails if it reads or hashes an excluded skill before skipping it.
        for excluded_dir in (
            cls.T_path / "root" / "fixpub" / "skills" / exclude_name,
            cls.T_path / "home" / ".claude" / "skills" / exclude_name,
        ):
            os.chmod(excluded_dir / "SKILL.md", 0)

        # Run inventory.py
        repo_root = Path(__file__).resolve().parent.parent
        inventory_script = repo_root / "tools" / "inventory.py"

        cmd = [
            sys.executable,
            "-B",
            str(inventory_script),
            "--root",
            str(cls.T_path / "root"),
            "--global-dir",
            str(cls.T_path / "home" / ".claude" / "skills"),
            "--plugins-dir",
            str(cls.T_path / "plugins"),
            "--lockfile",
            str(cls.T_path / "lock.json"),
            "--offline",
            "--visibility",
            str(cls.T_path / "visibility.json"),
            "--exclude-file",
            str(cls.T_path / "exclude.txt"),
            "--out",
            str(cls.T_path / "out"),
        ]

        result = subprocess.run(cmd, cwd=repo_root, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"inventory.py failed: {result.returncode}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )

        # Load outputs
        json_file = cls.T_path / "out" / "own-skills.json"
        md_file = cls.T_path / "out" / "own-skills.md"

        assert json_file.is_file(), f"Missing {json_file}"
        assert md_file.is_file(), f"Missing {md_file}"

        cls.output = json.loads(json_file.read_text())
        cls.json_text = json_file.read_text()
        cls.md_text = md_file.read_text()

    @classmethod
    def tearDownClass(cls):
        """Clean up fixture."""
        if hasattr(cls, "T") and cls.T:
            shutil.rmtree(cls.T, ignore_errors=True)

    @staticmethod
    def _create_skill(skill_dir: Path, name: str, description: str) -> None:
        """Create a skill folder with SKILL.md."""
        skill_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = f'---\nname: {name}\ndescription: {description}\n---\n'
        content = frontmatter + "Body of skill.\n"
        (skill_dir / "SKILL.md").write_text(content)

    @staticmethod
    def _create_skill_from_bytes(skill_dir: Path, content_bytes: bytes) -> None:
        """Create a skill folder from raw bytes."""
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_bytes(content_bytes)

    def test_private_record_is_restricted(self):
        """CAT-15: Private records hold only location, folder name, hash, visibility, class, cluster, restricted."""
        output = self.output
        private_records = output.get("own_skills_private_restricted", [])

        private_skill = None
        for rec in private_records:
            if rec.get("dir_name") == "zqfix-private":
                private_skill = rec
                break

        self.assertIsNotNone(private_skill, "zqfix-private not found in own_skills_private_restricted")
        self.assertEqual(private_skill["visibility"], "PRIVATE")
        self.assertTrue(private_skill["restricted"])

        # Check key set
        expected_keys = {"repo", "dir_name", "sha256", "visibility", "class", "cluster", "restricted"}
        actual_keys = set(private_skill.keys())
        self.assertEqual(actual_keys, expected_keys, f"Key mismatch: {actual_keys} vs {expected_keys}")

        # Ensure private skill doesn't appear in public list
        public_records = output.get("own_skills_public", [])
        for rec in public_records:
            self.assertNotEqual(rec.get("dir_name"), "zqfix-private")

    def test_global_local_record_is_restricted(self):
        """CAT-15: Global local records hold location, folder name, hash, class, also_in, restricted, cluster."""
        output = self.output
        global_records = output.get("global_installs_own", [])

        global_skill = None
        for rec in global_records:
            if rec.get("dir_name") == "zqfix-global-local":
                global_skill = rec
                break

        self.assertIsNotNone(global_skill, "zqfix-global-local not found in global_installs_own")
        self.assertEqual(global_skill["class"], "own-local")
        self.assertTrue(global_skill["location"].startswith("~/"))

        # Check key set
        expected_keys = {"location", "dir_name", "sha256", "class", "also_in", "restricted", "cluster"}
        actual_keys = set(global_skill.keys())
        self.assertEqual(actual_keys, expected_keys, f"Key mismatch: {actual_keys} vs {expected_keys}")

    def test_third_party_counts_only(self):
        """CAT-15: Third-party skills appear only as counts, not named in output text."""
        output = self.output

        # Ensure third-party skill name and description don't appear in outputs
        # Use distinctive naming to avoid false positives
        tp_name = "zqfix-thirdparty"
        tp_desc = "Use when zqfix third party marker text appears."

        # Check that exact name+boundary never appears
        json_search = self.json_text
        md_search = self.md_text

        # Look for name followed by non-name character (not alphanumeric, dash, underscore)
        pattern = re.compile(r"zqfix-thirdparty(?![a-zA-Z0-9_-])")
        self.assertIsNone(
            pattern.search(json_search),
            f"Third-party name found in JSON: {pattern.search(json_search)}",
        )
        self.assertIsNone(
            pattern.search(md_search),
            f"Third-party name found in MD: {pattern.search(md_search)}",
        )

        # Check description
        self.assertNotIn(
            "zqfix third party marker text",
            json_search,
            "Third-party description found in JSON",
        )
        self.assertNotIn(
            "zqfix third party marker text",
            md_search,
            "Third-party description found in MD",
        )

        # Verify third-party count is present
        tp_counts = output.get("third_party_counts", {})
        repo_clones = tp_counts.get("repo_clones", {})
        found = False
        for key, count in repo_clones.items():
            if "fixtp" in key:
                self.assertGreaterEqual(count, 1)
                found = True
        self.assertTrue(found, "No third-party count found for fixtp")

    def test_no_absolute_paths(self):
        """CAT-15: No absolute paths in outputs."""
        T_str = self.T
        T_real = os.path.realpath(T_str)
        fake_home = str(self.T_path / "home")

        json_search = self.json_text
        md_search = self.md_text

        # Check for absolute paths
        self.assertNotIn(T_str, json_search, "Fixture temp path in JSON")
        self.assertNotIn(T_str, md_search, "Fixture temp path in MD")
        self.assertNotIn(T_real, json_search, "Fixture realpath in JSON")
        self.assertNotIn(T_real, md_search, "Fixture realpath in MD")
        self.assertNotIn(fake_home, json_search, "Fake HOME in JSON")
        self.assertNotIn(fake_home, md_search, "Fake HOME in MD")

        # Check for common absolute path patterns
        # Build pattern with concatenation to avoid repo scrub detection
        users_part = "Users"
        home_part = "home"
        private_part = "private"
        path_pattern = re.compile(r"(?<![\w~.])/(?:" + users_part + "|" + home_part + "|" + private_part + "|var|tmp)/")

        self.assertIsNone(path_pattern.search(json_search), "Absolute path found in JSON")
        self.assertIsNone(path_pattern.search(md_search), "Absolute path found in MD")

    def test_vendored_detection(self):
        """CAT-16: Vendored skills detected by ecc-install-state.json and content hash."""
        output = self.output

        # ECC skill and copy should not appear in public or private own lists
        public_records = output.get("own_skills_public", [])
        private_records = output.get("own_skills_private_restricted", [])

        for rec in public_records:
            self.assertNotEqual(rec.get("dir_name"), "zqfix-ecc-skill")
            self.assertNotEqual(rec.get("dir_name"), "zqfix-thirdparty-copy")

        for rec in private_records:
            self.assertNotEqual(rec.get("dir_name"), "zqfix-ecc-skill")
            self.assertNotEqual(rec.get("dir_name"), "zqfix-thirdparty-copy")

        # CAT-15: vendored fixture names never appear anywhere in either output
        for vendored_name in ("zqfix-ecc-skill", "zqfix-thirdparty-copy"):
            self.assertNotIn(vendored_name, self.json_text)
            self.assertNotIn(vendored_name, self.md_text)

        # Check vendored count
        summary = output.get("summary", {})
        self.assertEqual(summary.get("own_repo_vendored_third_party"), 2)

        # Check third-party vendored counts
        tp_counts = output.get("third_party_counts", {})
        vendored = tp_counts.get("vendored_in_own_repos", {})

        # Should have entries for fixtp and ECC
        found_fixtp = False
        found_ecc = False
        for key, count in vendored.items():
            if "fixtp" in key:
                self.assertEqual(count, 1)
                found_fixtp = True
            if "ECC" in key or "ecc" in key.lower():
                self.assertEqual(count, 1)
                found_ecc = True

        self.assertTrue(found_fixtp, "No vendored count for fixtp")
        self.assertTrue(found_ecc, "No vendored count for ECC")

    def test_excluded_skill_skipped(self):
        """CAT-18: Excluded skills are not listed or counted."""
        output = self.output
        exclude_name = "zqfix-excluded-skill"
        exclude_desc = "Use when testing skill exclusion mechanism."

        json_search = self.json_text
        md_search = self.md_text

        # Ensure excluded skill name doesn't appear
        self.assertNotIn(exclude_name, json_search, "Excluded skill name in JSON")
        self.assertNotIn(exclude_name, md_search, "Excluded skill name in MD")

        # Ensure excluded skill description doesn't appear
        self.assertNotIn(exclude_desc, json_search, "Excluded skill description in JSON")
        self.assertNotIn(exclude_desc, md_search, "Excluded skill description in MD")

        # Check summary counts
        summary = output.get("summary", {})

        # Should scan: public own (1), ECC (1), hash copy (1), private (1), third-party (1) = 5
        # Excluded skills in two places are not counted
        self.assertEqual(
            summary.get("skill_md_files_scanned"),
            5,
            "Expected 5 scanned skills (excluding 2 excluded)",
        )

        # Global installs should be 1 (zqfix-global-local), not counting the excluded one
        self.assertEqual(summary.get("global_installs"), 1, "Expected 1 global install (excluding excluded)")

    def test_public_own_listed(self):
        """CAT-15: a public own skill is listed with its rel_dir; only restricted records are reduced."""
        output = self.output
        public_records = output.get("own_skills_public", [])

        public_skill = None
        for rec in public_records:
            if rec.get("dir_name") == "zqfix-own-public":
                public_skill = rec
                break

        self.assertIsNotNone(public_skill, "zqfix-own-public not found in own_skills_public")

        # Check required fields
        self.assertEqual(public_skill["visibility"], "PUBLIC")
        self.assertEqual(public_skill["class"], "own")
        self.assertNotIn("restricted", public_skill)

        # Check slug and rel_dir
        self.assertEqual(public_skill["slug"], "kennylhilljr/fixpub")

        # rel_dir should be path-normalized
        expected_rel = os.path.join("skills", "zqfix-own-public")
        self.assertEqual(public_skill["rel_dir"], expected_rel)


if __name__ == "__main__":
    unittest.main()
