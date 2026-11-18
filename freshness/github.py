"""The freshness bot's only GitHub writer (SPEC INV-5, FRS-13; DESIGN 5, 7.5).

`GitHub(repo, token, transport=None)` carries out part 1's Actions
(freshness/finding.py), lists the bot's own issues and PRs, and writes the
state file. Every request goes through one wrapper, so INV-5 (no automated
push to a default branch; every change arrives as a PR) holds by
construction:

- Write requests are allowed only on an explicit list of endpoints (git
  blobs, trees, commits and refs, pulls, issues, comments and labels).
  Merges (`PUT .../pulls/{n}/merge`, `POST .../merges`) and every write to
  the contents API are refused before anything is sent.
- Every ref write passes `_check_ref`, which allows only
  `refs/heads/freshness/<12 hex>` and `refs/heads/freshness-state`, and
  refuses the repository's default branch (read once via `GET /repos/{repo}`),
  `main`, tags and anything else.
- The class has no merge method.

`transport(method, path, body) -> (status, json)` is injected; the default
uses `urllib.request` against https://api.github.com. The token is never
logged, repeated in `repr` or put in an exception message.

Ops messages (`send_ops`) are not sent here: part 7 sends them. `apply`
returns them with `{issue_url}` (alerts.ISSUE_URL_PLACEHOLDER) replaced by
the URL of the issue it opened in the same call for that fingerprint, and
keeps the action's fingerprint, so part 7 can call
`alerts.record_send(state, text, ok, fingerprint)` after each send. A
`send_ops` whose issue was not opened comes back with `ok: False`; part 7
then calls `record_send(..., ok=False, fingerprint)` without sending, which
marks the alert as due again. The Actions half plans with
`plan_alerts(..., announce=False)`, so its actions hold no `send_ops`.

Python 3.9+, standard library only. Importing has no side effects.
"""

import base64
import datetime
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from . import finding as fmod

API = 'https://api.github.com'
USER_AGENT = 'skills-catalog-freshness'
BASE_BRANCH = 'main'
STATE_REF = 'refs/heads/freshness-state'
STATE_PATH = 'freshness/state.json'
CLOSED_WINDOW = datetime.timedelta(days=14)
PER_PAGE = 100
MAX_PAGES = 50
ISSUE_URL_PLACEHOLDER = '{issue_url}'

_PR_REF = re.compile(r'^refs/heads/freshness/[0-9a-f]{12}$')

# Write endpoints the bot may call, relative to /repos/{repo}. Everything
# else that is not a GET is refused.
_WRITES = (
    ('POST', re.compile(r'^/git/blobs$')),
    ('POST', re.compile(r'^/git/trees$')),
    ('POST', re.compile(r'^/git/commits$')),
    ('POST', re.compile(r'^/git/refs$')),
    ('PATCH', re.compile(r'^/git/refs/heads/.+$')),
    ('POST', re.compile(r'^/pulls$')),
    ('POST', re.compile(r'^/issues$')),
    ('PATCH', re.compile(r'^/issues/\d+$')),
    ('POST', re.compile(r'^/issues/\d+/comments$')),
    ('POST', re.compile(r'^/issues/\d+/labels$')),
)


class GitHubError(Exception):
    """A GitHub request failed. The message never holds the token."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class RefusedWrite(GitHubError):
    """A write that INV-5 forbids. Nothing was sent."""


def _urllib_transport(token):
    def transport(method, path, body):
        data = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request(API + path, data=data, method=method)
        request.add_header('Authorization', 'Bearer ' + token)
        request.add_header('Accept', 'application/vnd.github+json')
        request.add_header('X-GitHub-Api-Version', '2022-11-28')
        request.add_header('User-Agent', USER_AGENT)
        if data is not None:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as err:
            status, raw = err.code, err.read()
        except (urllib.error.URLError, OSError) as err:
            raise GitHubError('network error: ' + err.__class__.__name__) from None
        try:
            parsed = json.loads(raw.decode('utf-8')) if raw else None
        except ValueError:
            parsed = None
        return status, parsed
    return transport


def _utc(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    return now.astimezone(datetime.timezone.utc)


def _parse_time(text):
    if not text:
        return None
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(datetime.timezone.utc)


class GitHub:
    """Writer for one repository. No merge method, by design (INV-5)."""

    def __init__(self, repo, token, transport=None, bot_login=None):
        """`bot_login`: the App's bot user (`<app-slug>[bot]`). Only items it
        opened count in `list_bot_items`; when None, only items opened by a bot
        account or by the owner, a member or a collaborator count, so an
        outside user cannot suppress a finding with a copied marker.
        """
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo or ''):
            raise ValueError('repo must be owner/name')
        self.repo = repo
        self._token = token or ''
        self._transport = transport if transport is not None else _urllib_transport(self._token)
        self._default_branch = None
        self.bot_login = bot_login

    def __repr__(self):
        return 'GitHub(%r)' % self.repo

    # -- requests ------------------------------------------------------

    def _clean(self, text):
        text = str(text)
        if self._token:
            text = text.replace(self._token, '[token]')
        return text

    def _guard(self, method, rel, body):
        """Refuse every write INV-5 does not allow, before it is sent."""
        if method == 'GET':
            return
        bare = rel.split('?', 1)[0]
        if re.search(r'/merges?(/|$)', bare):
            raise RefusedWrite('refused: the freshness bot never merges (INV-5)')
        if bare.startswith('/contents/') or bare == '/contents':
            raise RefusedWrite('refused: no writes through the contents API (INV-5)')
        if not any(method == m and pattern.match(bare) for m, pattern in _WRITES):
            raise RefusedWrite('refused: %s %s is not a freshness write' % (method, bare))
        if bare == '/git/refs':
            self._check_ref((body or {}).get('ref'))
        elif bare.startswith('/git/refs/'):
            self._check_ref('refs/' + urllib.parse.unquote(bare[len('/git/refs/'):]))

    def _request(self, method, rel, body=None, ok=(200, 201)):
        """`rel` is relative to /repos/{repo}; '' is the repository itself."""
        method = method.upper()
        self._guard(method, rel, body)
        try:
            status, data = self._transport(method, '/repos/' + self.repo + rel, body)
        except RefusedWrite:
            raise
        except GitHubError as err:
            raise GitHubError(self._clean(err), getattr(err, 'status', None)) from None
        except Exception as err:  # a transport bug must not leak the token either
            raise GitHubError(self._clean('transport error: %s: %s' % (err.__class__.__name__, err))) from None
        if status not in ok:
            message = data.get('message') if isinstance(data, dict) else None
            raise GitHubError(self._clean('%s %s failed: %s %s' % (
                method, rel.split('?', 1)[0], status, message or '')).rstrip(), status)
        return status, data

    def default_branch(self):
        if self._default_branch is None:
            _, data = self._request('GET', '')
            name = (data or {}).get('default_branch')
            if not isinstance(name, str) or not name:
                raise GitHubError('could not read the default branch')
            self._default_branch = name
        return self._default_branch

    def _check_ref(self, ref):
        """Allow only freshness PR branches and the state branch (INV-5)."""
        if not isinstance(ref, str):
            raise RefusedWrite('refused: missing ref')
        if ref == 'refs/heads/' + BASE_BRANCH:
            raise RefusedWrite('refused: write to the default branch (INV-5)')
        if not (ref == STATE_REF or _PR_REF.match(ref)):
            raise RefusedWrite('refused: %s is not a freshness branch (INV-5)' % ref)
        if ref == 'refs/heads/' + self.default_branch():
            raise RefusedWrite('refused: write to the default branch (INV-5)')
        return ref

    # -- reading -------------------------------------------------------

    def _pages(self, query):
        for page in range(1, MAX_PAGES + 1):
            _, data = self._request('GET', '/issues?' + query + '&per_page=%d&page=%d' % (PER_PAGE, page))
            data = data or []
            for item in data:
                yield item
            if len(data) < PER_PAGE:
                return

    def _trusted(self, item):
        return (item.get('user') or {}).get('login') == self.bot_login

    def list_bot_items(self, now=None):
        """Open, and closed in the last 14 days, issues and PRs carrying a fingerprint marker."""
        if not self.bot_login:
            raise ValueError('list_bot_items needs bot_login, the freshness App login')
        now = _utc(now)
        since = now - CLOSED_WINDOW
        out, seen = [], set()
        queries = ('state=open',
                   'state=closed&since=' + since.strftime('%Y-%m-%dT%H:%M:%SZ'))
        for query in queries:
            for item in self._pages(query):
                number = item.get('number')
                fp = fmod.parse_marker(item.get('body') or '')
                if fp is None or number in seen or not self._trusted(item):
                    continue
                state = item.get('state')
                closed_at = item.get('closed_at')
                if state == 'closed':
                    when = _parse_time(closed_at)
                    if when is None or when < since:
                        continue
                seen.add(number)
                out.append({'number': number, 'fingerprint': fp, 'is_pr': 'pull_request' in item,
                            'state': state, 'closed_at': closed_at, 'body': item.get('body') or ''})
        return out

    def read_state(self):
        """`(text, commit sha)` of the state file, or `('', None)` before the first write."""
        status, data = self._request('GET', '/git/ref/heads/freshness-state', ok=(200, 404))
        if status == 404:
            return '', None
        sha = data['object']['sha']
        _, commit = self._request('GET', '/git/commits/' + sha)
        _, tree = self._request('GET', '/git/trees/' + commit['tree']['sha'] + '?recursive=1')
        for entry in tree.get('tree') or ():
            if entry.get('path') == STATE_PATH and entry.get('type') == 'blob':
                _, blob = self._request('GET', '/git/blobs/' + entry['sha'])
                return _blob_text(blob), sha
        return '', sha

    # -- building commits ----------------------------------------------

    def _main_commit(self):
        _, data = self._request('GET', '/git/ref/heads/' + BASE_BRANCH)
        sha = data['object']['sha']
        _, commit = self._request('GET', '/git/commits/' + sha)
        return sha, commit['tree']['sha']

    def _commit_files(self, files, message, parent, base_tree):
        modes = {}
        if base_tree:
            _, tree = self._request('GET', '/git/trees/' + base_tree + '?recursive=1')
            modes = {e['path']: e.get('mode') for e in (tree or {}).get('tree') or () if e.get('type') == 'blob'}
        entries = []
        for path in sorted(files):
            if not isinstance(path, str) or path.startswith('/') or '..' in path.split('/'):
                raise RefusedWrite('refused: bad file path')
            _, blob = self._request('POST', '/git/blobs', {'content': files[path], 'encoding': 'utf-8'})
            entries.append({'path': path, 'mode': modes.get(path) or '100644', 'type': 'blob', 'sha': blob['sha']})
        tree_body = {'tree': entries}
        if base_tree:
            tree_body['base_tree'] = base_tree
        _, tree = self._request('POST', '/git/trees', tree_body)
        _, commit = self._request('POST', '/git/commits', {
            'message': message, 'tree': tree['sha'], 'parents': [parent] if parent else []})
        return commit['sha']

    def _set_ref(self, ref, sha, force):
        """Create `ref`, or move it when it exists (force only for PR branches)."""
        self._check_ref(ref)
        status, _ = self._request('POST', '/git/refs', {'ref': ref, 'sha': sha}, ok=(201, 422))
        if status == 201:
            return
        self._request('PATCH', '/git/' + ref, {'sha': sha, 'force': bool(force)})

    # -- actions -------------------------------------------------------

    def _open_pr(self, action):
        ref = 'refs/heads/' + action['branch']
        self._check_ref(ref)
        parent, base_tree = self._main_commit()
        sha = self._commit_files(action['files'], _message(action['title']), parent, base_tree)
        self._set_ref(ref, sha, force=True)
        _, pr = self._request('POST', '/pulls', {'title': action['title'], 'body': action['body'],
                                                 'head': action['branch'], 'base': BASE_BRANCH})
        if action.get('labels'):
            # The pulls API takes no labels, so they are a second call. Retry
            # it once: the next run deduplicates on the marker and would not
            # label the PR again.
            try:
                self._request('POST', '/issues/%d/labels' % pr['number'], {'labels': action['labels']})
            except RefusedWrite:
                raise
            except GitHubError:
                try:
                    self._request('POST', '/issues/%d/labels' % pr['number'], {'labels': action['labels']})
                except RefusedWrite:
                    raise
                except GitHubError as err:
                    # Close the unlabeled PR, so the next run opens it again
                    # (same branch, force-moved) with its labels.
                    self._request('PATCH', '/issues/%d' % pr['number'], {'state': 'closed'})
                    raise GitHubError('labels failed, PR #%d closed for a retry next run: %s'
                                      % (pr['number'], self._clean(err)), err.status) from None
        return pr

    def _update_pr(self, action):
        ref = 'refs/heads/' + action['branch']
        self._check_ref(ref)
        _, pr = self._request('GET', '/pulls/%d' % action['number'])
        head = (pr or {}).get('head') or {}
        if head.get('ref') != action['branch'] or ((head.get('repo') or {}).get('full_name') != self.repo):
            raise RefusedWrite('refused: update_pr may only move the PR\'s own freshness branch')
        parent, base_tree = self._main_commit()
        sha = self._commit_files(action['files'], _message(action.get('title') or pr.get('title') or 'freshness update'),
                                 parent, base_tree)
        self._request('PATCH', '/git/' + ref, {'sha': sha, 'force': True})
        return pr

    def apply(self, actions):
        """Carry out Actions in order; one result dict per action.

        Result: `{"op", "fingerprint", "ok", "number"?, "url"?, "text"?,
        "error"?}`. API errors are recorded and the next action runs; a
        RefusedWrite (INV-5) is raised, since it means a planner bug.
        """
        results, opened = [], {}
        for action in actions or ():
            result = {'op': action.get('op') if isinstance(action, dict) else None,
                      'fingerprint': action.get('fingerprint') if isinstance(action, dict) else None,
                      'ok': False}
            results.append(result)
            errors = fmod.validate_action(action)
            if errors:
                result['error'] = '; '.join(errors)
                continue
            op, fp = action['op'], action['fingerprint']
            try:
                if op == 'open_issue':
                    _, issue = self._request('POST', '/issues', {
                        'title': action['title'], 'body': action['body'], 'labels': action['labels']})
                    result.update(number=issue['number'], url=issue.get('html_url'))
                    opened[fp] = issue.get('html_url') or 'https://github.com/%s/issues/%d' % (
                        self.repo, issue['number'])
                elif op == 'comment':
                    self._request('POST', '/issues/%d/comments' % action['number'], {'body': action['body']})
                    result['number'] = action['number']
                elif op in ('reopen', 'close'):
                    self._request('PATCH', '/issues/%d' % action['number'],
                                  {'state': 'open' if op == 'reopen' else 'closed'})
                    result['number'] = action['number']
                elif op == 'open_pr':
                    pr = self._open_pr(action)
                    result.update(number=pr['number'], url=pr.get('html_url'))
                elif op == 'update_pr':
                    self._update_pr(action)
                    result['number'] = action['number']
                elif op == 'send_ops':
                    text = action['text']
                    result['text'] = text
                    if ISSUE_URL_PLACEHOLDER in text:
                        if fp not in opened:
                            result['error'] = 'no issue was opened for this fingerprint in this run'
                            continue
                        result['text'] = text.replace(ISSUE_URL_PLACEHOLDER, opened[fp])
                result['ok'] = True
            except RefusedWrite:
                raise
            except GitHubError as err:
                result['error'] = self._clean(err)
            except (KeyError, TypeError) as err:
                result['error'] = 'unexpected response: ' + err.__class__.__name__
        return results

    def write_state(self, text, message='freshness: state (FRS-24, FRS-25, FRS-27)'):
        """Commit the state file to `freshness-state` (orphan on first use).

        Compare-and-swap: the new commit's parent is the sha just read, and
        the ref moves only as a fast-forward, so a concurrent writer makes
        the update fail; it is then retried once on the new sha.
        """
        for attempt in (1, 2):
            status, data = self._request('GET', '/git/ref/heads/freshness-state', ok=(200, 404))
            if status == 404:
                sha = self._commit_files({STATE_PATH: text}, message, None, None)
                self._check_ref(STATE_REF)
                status, _ = self._request('POST', '/git/refs', {'ref': STATE_REF, 'sha': sha}, ok=(201, 422))
                if status == 201:
                    return sha
            else:
                parent = data['object']['sha']
                _, commit = self._request('GET', '/git/commits/' + parent)
                sha = self._commit_files({STATE_PATH: text}, message, parent, commit['tree']['sha'])
                status, _ = self._request('PATCH', '/git/' + STATE_REF, {'sha': sha, 'force': False},
                                          ok=(200, 409, 422))
                if status == 200:
                    return sha
        raise GitHubError('state write conflicted twice', status)


def _message(title):
    """Commit message of a freshness PR; cites the requirement (AGENTS.md)."""
    return title if 'FRS-' in title else title + ' (FRS-12)'


def _blob_text(blob):
    content = (blob or {}).get('content') or ''
    if (blob or {}).get('encoding') == 'base64':
        return base64.b64decode(content).decode('utf-8')
    return content
