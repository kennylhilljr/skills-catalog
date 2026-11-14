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


def _job_texts(text):
    """Map each job name to its text."""
    body = text.split('\njobs:\n', 1)[1]
    parts = re.split(r'^  ([A-Za-z0-9_-]+):\s*$', body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2]))


def _steps(text):
    """Map 'job/step name' to the step's text, for steps that start with `- name:`.

    A step ends at the next step or at the end of its job.
    """
    steps = {}
    for job, body in _job_texts(text).items():
        parts = re.split(r'^      - name: ', body, flags=re.MULTILINE)
        for p in parts[1:]:
            steps['%s/%s' % (job, p.split('\n', 1)[0].strip())] = p
    return steps


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

    def test_secrets_only_where_they_are_needed(self):
        text = GATE.read_text()
        self.assertEqual(text.count('${{ secrets.'), 2)
        self.assertEqual(text.count('${{ secrets.PRIVATE_REPOS }}'), 1)
        self.assertEqual(text.count('${{ secrets.GATE_APP_PRIVATE_KEY }}'), 1)
        steps = _steps(text)
        scrub = [s for s in steps.values() if '${{ secrets.PRIVATE_REPOS }}' in s]
        self.assertEqual(len(scrub), 1)
        self.assertIn('python3 -I base/tools/scrub.py --private-repos', scrub[0])
        mint = [s for s in steps.values() if '${{ secrets.GATE_APP_PRIVATE_KEY }}' in s]
        self.assertEqual(len(mint), 1)
        self.assertIn('uses: actions/create-github-app-token@', mint[0])
        self.assertIn('permission-checks: write', mint[0])
        self.assertNotIn('head/tools', text)

    def test_app_key_only_in_the_gate_environment_job(self):
        jobs = _job_texts(GATE.read_text())
        self.assertEqual(sorted(jobs), ['checks', 'report', 'scrub'])
        for name, job in jobs.items():
            has_key = 'GATE_APP_PRIVATE_KEY }}' in job
            self.assertEqual(has_key, name == 'report', name)
        self.assertIn('\n    environment: gate\n', jobs['report'])
        self.assertNotIn('environment:', jobs['checks'] + jobs['scrub'])
        self.assertIn('needs: [checks, scrub]', jobs['report'])

    def test_mains_tools_check_the_head_files(self):
        checks = _job_texts(GATE.read_text())['checks']
        self.assertIn('python3 -I base/tools/lint.py head', checks)
        self.assertIn('python3 -I base/tools/catalog.py --root head --check', checks)
        self.assertIn('skills-ref validate', checks)
        self.assertIn('working-directory: head', checks)
        self.assertIn('base/schema/frontmatter.schema.json', checks)
        self.assertNotIn('${{ secrets.', checks)

    def test_catalog_yml_runs_only_the_unit_tests(self):
        text = CATALOG.read_text()
        self.assertIn('python3 -B -m unittest discover -s tests -t .', text)
        for tool in ('tools/lint.py', 'tools/catalog.py', 'skills-ref', 'scrub'):
            self.assertNotIn(tool, text.split('\njobs:\n', 1)[1], tool)

    def test_gate_needs_every_result(self):
        report = _job_texts(GATE.read_text())['report']
        for name in ('CATALOG_CONCLUSION', 'CHECKS_RESULT', 'SCRUB_RESULT'):
            self.assertIn('[ "${%s}" = "success" ]' % name, report)

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
        text = GATE.read_text()
        # A prefix without "/" would also match a sibling workflow file a PR
        # can add, named catalog.yml plus "@" and more characters.
        self.assertNotIn("'.github/workflows/catalog.yml@'", text)
        for job in _job_texts(text).values():
            self.assertIn("github.event.workflow_run.path == '.github/workflows/catalog.yml'", job)
            self.assertIn("startsWith(github.event.workflow_run.path, '.github/workflows/catalog.yml@refs/')", job)

    def test_private_name_pass_output_is_withheld(self):
        scrub = [s for s in _steps(GATE.read_text()).values() if '${{ secrets.PRIVATE_REPOS' in s][0]
        line = [l for l in scrub.splitlines() if '--private-repos "${list}"' in l]
        self.assertEqual(len(line), 1)
        self.assertIn('> /dev/null 2>&1', line[0])


if __name__ == '__main__':
    unittest.main()
