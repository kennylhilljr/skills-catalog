"""Invariants of the CI workflows that keep the scrub secret away from PR code.

See DESIGN 4.1. Text checks only (standard library, no YAML parser).
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'
GATE = WORKFLOWS / 'gate.yml'
CATALOG = WORKFLOWS / 'catalog.yml'

_USES = re.compile(r'^\s*(?:-\s+)?uses:\s*(\S+)', re.MULTILINE)
_PINNED = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$')
_JOB = re.compile(r'^  ([A-Za-z0-9_-]+):\s*$', re.MULTILINE)
_PR_EVENTS = re.compile(r'^\s*(pull_request|pull_request_target|pull_request_review|pull_request_review_comment|issue_comment)\s*:', re.MULTILINE)


def _workflow_files():
    return sorted(p for p in WORKFLOWS.iterdir() if p.suffix in ('.yml', '.yaml'))


def _jobs(text):
    body = text.split('\njobs:\n', 1)[1]
    return _JOB.findall(body)


def _steps(text):
    """Map each step name to its text, for steps that start with `- name:`."""
    parts = re.split(r'^      - name: ', text, flags=re.MULTILINE)
    return {p.split('\n', 1)[0].strip(): p for p in parts[1:]}


class WorkflowTests(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        for path in _workflow_files():
            for ref in _USES.findall(path.read_text()):
                self.assertRegex(ref, _PINNED, '%s: %s' % (path.name, ref))

    def test_only_gate_yml_reads_secrets(self):
        for path in _workflow_files():
            if path != GATE:
                self.assertNotIn('${{ secrets.', path.read_text(), path.name)

    def test_secret_workflow_has_no_pr_trigger(self):
        text = GATE.read_text()
        on_block = text.split('\non:\n', 1)[1].split('\n\n', 1)[0]
        self.assertEqual(_PR_EVENTS.findall(on_block), [])
        self.assertIn('workflow_run:', on_block)
        self.assertIn('workflows: [catalog]', on_block)

    def test_secret_only_in_the_scrub_step_which_runs_mains_scrub(self):
        text = GATE.read_text()
        self.assertEqual(text.count('${{ secrets.'), 1)
        steps = _steps(text)
        scrub = [s for name, s in steps.items() if '${{ secrets.' in s]
        self.assertEqual(len(scrub), 1)
        self.assertIn('python3 -I base/tools/scrub.py --private-repos', scrub[0])
        self.assertNotIn('head/tools', text)

    def test_no_event_values_in_run_scripts(self):
        text = GATE.read_text()
        for name, step in _steps(text).items():
            run = step.split('        run: |', 1)
            if len(run) == 2:
                self.assertNotIn('${{', run[1], name)

    def test_only_gate_yml_posts_gate(self):
        self.assertNotIn('gate', _jobs(GATE.read_text()))
        self.assertIn('-f name=gate', GATE.read_text())
        for path in _workflow_files():
            self.assertNotIn('gate', _jobs(path.read_text()), path.name)

    def test_gate_listens_to_catalog_yml(self):
        self.assertIn('name: catalog\n', CATALOG.read_text())
        self.assertIn("'.github/workflows/catalog.yml'", GATE.read_text())
        self.assertIn("'.github/workflows/catalog.yml@'", GATE.read_text())

    def test_private_name_pass_output_is_withheld(self):
        scrub = [s for s in _steps(GATE.read_text()).values() if '${{ secrets.' in s][0]
        line = [l for l in scrub.splitlines() if '--private-repos "${list}"' in l]
        self.assertEqual(len(line), 1)
        self.assertIn('> /dev/null 2>&1', line[0])


if __name__ == '__main__':
    unittest.main()
