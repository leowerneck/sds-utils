"""Dashboard of issues, PRs, and Actions across IMAP SDC repos (NiceGUI + PyGithub).

Read-only, except for the workflow dispatches in WORKFLOWS (dry runs only, for now).
Auth: $GITHUB_TOKEN, falling back to `gh auth token`.
"""

import asyncio
import functools
import html
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import islice

import requests
from github import Auth, Github, GithubException
from nicegui import run, ui

ORG = "IMAP-Science-Operations-Center"
REPOS = {  # repo name -> upstream (base) branch
    "imap-data-access": "main",
    "sds-data-manager": "dev",
    "imap_processing": "dev",
    "imap_L3_processing": "main",
}
LIMIT = 100
STATE_FILTERS = {  # UI choice -> (issue states, PR states)
    "Open": (["OPEN"], ["OPEN"]),
    "Closed": (["CLOSED"], ["CLOSED"]),
    "Merged": ([], ["MERGED"]),
    "All": (["OPEN", "CLOSED"], ["OPEN", "CLOSED", "MERGED"]),
}
TYPE_ORDER = ["Bug", "Task", "Feature"]  # initial table order within each repo; other/no type last
TYPE_COLORS = {  # GitHub's issue type colors (text on a light background)
    "RED": "#d1242f", "BLUE": "#0969da", "YELLOW": "#9a6700", "GREEN": "#1a7f37",
    "ORANGE": "#bc4c00", "PURPLE": "#8250df", "PINK": "#bf3989", "GRAY": "#59636e",
}
CI_ICONS = {"SUCCESS": "✅", "FAILURE": "❌", "ERROR": "❌", "PENDING": "⏳", "EXPECTED": "⏳"}


# ---------------------------------------------------------------------- github
def connect() -> Github:
    """Client for $GITHUB_TOKEN, else `gh auth token`. Re-run by Refresh to pick up a new token."""
    token = os.environ.get("GITHUB_TOKEN") or subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    return Github(auth=Auth.Token(token), per_page=100, lazy=True)  # lazy: no request until data is needed


GH = connect()


def org_repo(name: str):
    return GH.get_repo(f"{ORG}/{name}")


def graphql(query: str, **variables: str) -> dict:
    if "mutation" in query:  # queries only, never mutations
        raise PermissionError("Refusing GraphQL mutation")
    return GH.requester.graphql_query(query, variables)[1]["data"]["repository"]


def cached(ttl: float | None = None):
    """Memoize by arguments for `ttl` seconds (forever if None). `fn.cache.clear()` resets."""
    def decorator(fn):
        cache = {}

        @functools.wraps(fn)
        def wrapper(*args):
            if args in cache and (ttl is None or time.time() - cache[args][0] < ttl):
                return cache[args][1]
            cache[args] = (time.time(), fn(*args))
            return cache[args][1]
        wrapper.cache = cache
        return wrapper
    return decorator


def _states(states: list[str]) -> str:
    return "[" + ", ".join(states) + "]"


ISSUE_FIELDS = """
  number title url state updatedAt
  issueType { name color }
  author { login }
  assignees(first: 5) { nodes { login } }
  labels(first: 10) { nodes { name } }
  milestone { title }
  linkedBranches(first: 5) { nodes { ref { name } } }
  closedByPullRequestsReferences(first: 5, includeClosedPrs: true) { nodes { number } }
"""
PR_FIELDS = """
  number title url state isDraft updatedAt headRefName baseRefName reviewDecision
  author { login }
  assignees(first: 5) { nodes { login } }
  labels(first: 10) { nodes { name } }
  milestone { title }
  closingIssuesReferences(first: 5) { nodes { url } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
"""
PROJECT_FIELDS = """
  number
  projectItems(first: 5) { nodes {
    project { title }
    fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
  } }
"""


def _query(fields_issue: str, fields_pr: str, issue_states, pr_states) -> str:
    order = "orderBy: {field: UPDATED_AT, direction: DESC}"
    issues = (
        f"issues(first: {LIMIT}, states: {_states(issue_states)}, {order}) "
        f"{{ nodes {{ {fields_issue} }} }}"
        if issue_states
        else ""
    )
    return f"""
    query($owner: String!, $name: String!, $base: String!) {{
      repository(owner: $owner, name: $name) {{
        openIssues: issues(states: OPEN) {{ totalCount }}
        openPRs: pullRequests(states: OPEN, baseRefName: $base) {{ totalCount }}
        {issues}
        pullRequests(first: {LIMIT}, states: {_states(pr_states)}, baseRefName: $base, {order})
          {{ nodes {{ {fields_pr} }} }}
      }}
    }}"""


@cached(ttl=300)
def fetch_repo(repo: str, base: str, state: str) -> dict:
    issue_states, pr_states = STATE_FILTERS[state]
    variables = {"owner": ORG, "name": repo, "base": base}
    data = graphql(_query(ISSUE_FIELDS, PR_FIELDS, issue_states, pr_states), **variables)

    # Projects need the `read:project` scope; a missing scope fails the whole
    # query, so fetch them separately and degrade gracefully.
    projects, projects_error = {}, None
    try:
        pdata = graphql(_query(PROJECT_FIELDS, PROJECT_FIELDS, issue_states, pr_states), **variables)
        for kind in ("issues", "pullRequests"):
            for node in (pdata.get(kind) or {}).get("nodes", []):
                projects[(kind, node["number"])] = "\n".join(
                    f"{i['project']['title']}: {(i['fieldValueByName'] or {}).get('name', '-')}"
                    for i in node["projectItems"]["nodes"]
                )
    except GithubException as exc:
        projects_error = str(exc)

    runs = list(org_repo(repo).get_workflow_runs()[:20])
    return {"data": data, "projects": projects, "projects_error": projects_error, "runs": runs}


# ----------------------------------------------------------------- flattening
# Multi-value cells hold one value per line (the table CSS renders the newlines)
def _names(conn: dict, key: str = "name") -> str:
    return "\n".join(n[key] for n in conn["nodes"])


def _nums(conn: dict) -> str:
    return "\n".join(f"#{n['number']}" for n in conn["nodes"])


def type_rank(row: dict) -> int:
    """Sort key putting issues in TYPE_ORDER; rows without a type (PRs, runs) all tie."""
    return TYPE_ORDER.index(row["type"]) if row.get("type") in TYPE_ORDER else len(TYPE_ORDER)


def _time(value: str | datetime) -> str:
    return (value if isinstance(value, datetime) else datetime.fromisoformat(value)).strftime("%Y-%m-%d %H:%M")


def _ci(pr: dict) -> str:
    """Icon for the combined CI state of a PR's latest commit."""
    commit = pr["commits"]["nodes"][0]["commit"] if pr["commits"]["nodes"] else {}
    ci = (commit.get("statusCheckRollup") or {}).get("state", "")
    return CI_ICONS.get(ci, ci)


def _pr_status(pr: dict) -> str:
    """Closed / Merged, or for open PRs: Draft, else the review state (Open if no review is required)."""
    if pr["state"] != "OPEN":
        return pr["state"].title()
    if pr["isDraft"]:
        return "Draft"
    return (pr["reviewDecision"] or "Open").replace("_", " ").title()  # e.g. REVIEW_REQUIRED -> Review Required


def _common(repo: str, node: dict, projects: dict, kind: str, projects_ok: bool) -> dict:
    issue_type = node.get("issueType") or {}
    return {
        "repository": repo,
        "link": node["url"],
        **({"type": issue_type.get("name", ""), "_type_color": TYPE_COLORS.get(issue_type.get("color"))}
           if kind == "issues" else {}),  # PRs have no type
        **({"closes": [i["url"] for i in node["closingIssuesReferences"]["nodes"]], "CI": _ci(node)}
           if kind == "pullRequests" else {}),
        "title": node["title"],
        "status": node["state"],
        "author": (node["author"] or {}).get("login", "ghost"),
        "assignees": _names(node["assignees"], "login"),
        "labels": _names(node["labels"]),
        "milestone": (node["milestone"] or {}).get("title", ""),
        "projects": projects.get((kind, node["number"]), "") if projects_ok else "n/a",
        "updated": _time(node["updatedAt"]),
    }


def issue_rows(repo: str, result: dict) -> list[dict]:
    ok = result["projects_error"] is None
    rows = []
    for n in (result["data"].get("issues") or {}).get("nodes", []):
        row = _common(repo, n, result["projects"], "issues", ok)
        row["linked PRs"] = _nums(n["closedByPullRequestsReferences"])
        row["linked branches"] = "\n".join(b["ref"]["name"] for b in n["linkedBranches"]["nodes"] if b["ref"])
        rows.append(row)
    return rows


def pr_rows(repo: str, result: dict) -> list[dict]:
    ok = result["projects_error"] is None
    rows = []
    for n in result["data"]["pullRequests"]["nodes"]:
        row = _common(repo, n, result["projects"], "pullRequests", ok)
        row.update({
            "status": _pr_status(n),
            "branches": f"{n['baseRefName']} ← {n['headRefName']}",
        })
        rows.append(row)
    return rows


def run_rows(repo: str, result: dict) -> list[dict]:
    return [
        {
            "repository": repo,
            "link": r.html_url,
            "workflow": r.name,
            "title": r.display_title,
            "branch": r.head_branch,
            "event": r.event,
            "result": r.conclusion or r.status,
            "created": _time(r.created_at),
        }
        for r in result["runs"]
    ]


# ---------------------------------------------------------- deployment status
SDM = "sds-data-manager"
L3_REPO = "imap_L3_processing"
# Keep in sync with VALID_INSTRUMENTS / VALID_L3_INSTRUMENTS in sds-data-manager's
# promote_containers_dev_to_prod.yml.
INSTRUMENTS = ["codice", "glows", "hit", "hi", "ialirt", "idex", "lo", "mag", "spacecraft", "swapi", "swe", "ultra"]
L3_INSTRUMENTS = ["codice", "glows", "hit", "hi", "lo", "swapi", "swe", "ultra"]
PRETTY_NAMES = {
    "codice": "CoDICE", "glows": "GLOWS", "hi": "Hi", "hit": "HIT", "ialirt": "I-ALiRT", "idex": "IDEX",
    "lo": "Lo", "mag": "MAG", "spacecraft": "Spacecraft", "swapi": "SWAPI", "swe": "SWE", "ultra": "Ultra",
}
INSTRUMENT_GROUPS = [  # status table column order; groups are separated by a heavier border
    ["lo", "hi", "ultra"],  # ENA
    ["codice", "glows", "hit", "swapi", "swe"]  # in-situ with L3
    + ["idex", "mag"],  # in-situ without L3
    ["ialirt", "spacecraft"],  # space weather
]
GROUP_STARTS = {group[0] for group in INSTRUMENT_GROUPS[1:]}
STATUS_COLUMNS = [inst for group in INSTRUMENT_GROUPS for inst in group]
DOT_COLORS = {"success": "#2da44e", "running": "#d4a72c", "failure": "#cf222e"}
PROMOTE_JOBS = ["promote_images", "verify_promotion"]


def list_runs(repo: str, workflow: str) -> list:
    return list(org_repo(repo).get_workflow(workflow).get_runs()[:100])


def outcome(item) -> str | None:
    """success / running / failure, or None for cancelled/skipped (not an attempt). Works for runs and jobs."""
    if item.status != "completed":
        return "running"
    return {"success": "success", "failure": "failure", "timed_out": "failure",
            "startup_failure": "failure"}.get(item.conclusion)


_completed_jobs: dict[int, list] = {}  # completed runs are immutable, so cache forever


def run_jobs(run) -> list:
    if run.id in _completed_jobs:
        return _completed_jobs[run.id]
    jobs = list(run.jobs())
    if run.status == "completed":
        _completed_jobs[run.id] = jobs
    return jobs


def instrument_history(runs, title_re, universe, job_prefixes) -> dict[str, list]:
    """{instrument: [(outcome, created_at, url), ...]}, newest first, one entry per attempt.

    The instrument (or "all") is parsed from the run title. Multi-instrument runs
    that didn't fully succeed are resolved per instrument from their matrix jobs.
    """
    history = defaultdict(list)
    for run in runs:
        if not (m := re.search(title_re, run.display_title)):
            continue
        instruments = universe if m[1] == "all" else [m[1]] if m[1] in universe else []
        run_outcome = outcome(run)
        if len(instruments) == 1 or run_outcome == "success":
            results = dict.fromkeys(instruments, run_outcome)
        else:
            jobs = run_jobs(run)
            results = {}
            for inst in instruments:
                mine = [outcome(j) for j in jobs if j.name in {f"{p} ({inst})" for p in job_prefixes}]
                results[inst] = next((o for o in ("failure", "running") if o in mine),
                                     "success" if mine and all(o == "success" for o in mine) else None)
        for inst, o in results.items():
            if o:
                history[inst].append((o, run.created_at, run.html_url))
    return dict(history)


def last_success(events: list, before: datetime | None = None) -> datetime | None:
    return next((t for o, t, _ in events if o == "success" and (before is None or t <= before)), None)


def pending_commits(repo: str, branch: str, root: str, specs: dict) -> dict:
    """Count commits on `branch` since each spec's time, under root/<instrument> and root."""
    if not specs:
        return {}
    fields = []
    for alias, (since, inst) in specs.items():
        fields.append(f'{alias}: history(first: 10, since: "{since.isoformat()}", path: "{root}/{inst}") '
                      "{ totalCount nodes { messageHeadline } }")
        fields.append(f'{alias}_all: history(first: 0, since: "{since.isoformat()}", path: "{root}") {{ totalCount }}')
    query = ("query($owner: String!, $name: String!, $branch: String!) {"
             " repository(owner: $owner, name: $name) { ref(qualifiedName: $branch) {"
             " target { ... on Commit { " + " ".join(fields) + " } } } } }")
    target = graphql(query, owner=ORG, name=repo, branch=branch)["ref"]["target"]
    return {
        alias: {
            "count": target[alias]["totalCount"],
            "titles": [n["messageHeadline"] for n in target[alias]["nodes"]],
            "other": target[f"{alias}_all"]["totalCount"] - target[alias]["totalCount"],
        }
        for alias in specs
    }


def infra_status() -> dict:
    """Latest sds-data-manager Deploy run per environment + commits not yet deployed."""
    sdm = org_repo(SDM)
    runs = list_runs(SDM, "deploy.yml")
    # Deploy records the exact deployed commit in refs/last-deploy/<env>.
    markers = {r.ref.rsplit("/", 1)[1]: r.object.sha for r in sdm.get_git_matching_refs("last-deploy")}
    status = {}
    for env in ("dev", "prod"):
        events = [r for r in runs if outcome(r) and _deploy_env(r) == env]
        ok = next((r for r in events if outcome(r) == "success"), None)
        sha = markers.get(env)
        diff = sdm.compare(sha, "dev") if sha else None
        status[env] = {
            "latest": events[0] if events else None,
            "last_ok": ok.updated_at if ok else None,
            "sha": sha,
            "pending": diff.ahead_by if diff else None,
            "titles": [c.commit.message.split("\n")[0] for c in diff.commits][::-1][:10] if diff else [],
        }
    return status


def _deploy_env(run) -> str | None:
    if m := re.search(r"Deploy to (dev|prod)", run.display_title):
        return m[1]
    return "dev" if run.event == "push" else None  # runs from before run-name existed


@cached(ttl=120)
def fetch_status() -> dict:
    with ThreadPoolExecutor() as pool:
        infra = pool.submit(infra_status)
        dev_runs = pool.submit(list_runs, SDM, "deploy_dev_containers.yml")
        promote_runs = pool.submit(list_runs, SDM, "promote_containers_dev_to_prod.yml")
        l3_runs = pool.submit(list_runs, L3_REPO, "deploy_container.yml")
    hist = {
        "dev": instrument_history(dev_runs.result(), r"Instruments: (\w+)", INSTRUMENTS, ["build_and_push"]),
        "prod": instrument_history(promote_runs.result(), r"Promote (\w+) L0-L2 Containers", INSTRUMENTS, PROMOTE_JOBS),
        "dev_l3": instrument_history(l3_runs.result(), r"Docker Image for (\w+)$", L3_INSTRUMENTS, []),
        "prod_l3": instrument_history(promote_runs.result(), r"Promote (\w+) L3 Containers", L3_INSTRUMENTS, PROMOTE_JOBS),
    }
    # The code in an image is approximated by the branch state when its last successful
    # build started. A prod image is the dev image that was current when it was promoted.
    baselines = {kind: {} for kind in hist}
    for dev_kind, prod_kind in (("dev", "prod"), ("dev_l3", "prod_l3")):
        for inst, events in hist[dev_kind].items():
            baselines[dev_kind][inst] = last_success(events)
        for inst, events in hist[prod_kind].items():
            if promoted := last_success(events):
                baselines[prod_kind][inst] = last_success(hist[dev_kind].get(inst, []), before=promoted)

    def specs(kinds):
        return {f"{k}_{i}": (t, i) for k in kinds for i, t in baselines[k].items() if t}

    pending = {
        **pending_commits("imap_processing", "dev", "imap_processing", specs(["dev", "prod"])),
        **pending_commits(L3_REPO, "main", "imap_l3_processing", specs(["dev_l3", "prod_l3"])),
    }
    return {"infra": infra.result(), "hist": hist, "baselines": baselines, "pending": pending}


def ago(timestamp: datetime) -> str:
    seconds = (datetime.now(UTC) - timestamp).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


def dot(state: str | None) -> str:
    return f'<span class="dot" style="background:{DOT_COLORS.get(state, "#8c959f")}"></span>'


def badges(pending: dict | None) -> str:
    if not pending:
        return ""
    out = f'<span class="badge">▲{pending["count"]}</span>' if pending["count"] else ""
    return out + (f'<span class="other">+{pending["other"]}</span>' if pending["other"] else "")


def tip(lines: list[str]) -> str:
    return html.escape("\n".join(lines), quote=True)


def infra_pill(env: str, s: dict) -> str:
    latest = s["latest"]
    state = outcome(latest) if latest else None
    if state == "running":
        text = f"deploying ({ago(latest.created_at)})"
    elif state == "failure":
        text = f"failed {ago(latest.updated_at)} ago" + (f" · last OK {ago(s['last_ok'])} ago" if s["last_ok"] else "")
    else:
        text = f"deployed {ago(s['last_ok'])} ago" if s["last_ok"] else "no deploys found"
    pending = s["pending"]
    where = "on dev, not yet deployed" if env == "dev" else "on dev, not in prod"
    lines = [f"Deployed commit: {s['sha'] or '?'}", f"{pending} commit(s) {where}", *[f"• {t}" for t in s["titles"]]]
    badge = f'<span class="badge">▲{pending}</span>' if pending else ""
    url = latest.html_url if latest else "#"
    return (f'<a class="pill" href="{url}" target="_blank" title="{tip(lines)}">'
            f'{dot(state)}<b>{env.upper()}</b> {text}{" · " + s["sha"][:7] if s["sha"] else ""}{badge}</a>')


def oval(state: str | None, label: str, url: str | None = None) -> str:
    text_color = "#1f2328" if state == "running" else "#fff"  # dark text on yellow
    style = f'background:{DOT_COLORS.get(state, "#8c959f")};color:{text_color}'
    attrs = f' href="{url}" target="_blank"' if url else ""
    return f'<a class="oval" style="{style}"{attrs}>{label}</a>'


def level_status(status: dict, kind: str, inst: str) -> str:
    """One level (L0–L2 or L3) of one instrument: colored oval over a detail line."""
    label = "L3" if kind.endswith("_l3") else "L0–L2"
    events = status["hist"][kind].get(inst)
    if not events:
        return (f'<div class="lvl" title="No runs among the last 100">{oval(None, label)}'
                '<div class="detail">?</div></div>')
    state, created, url = events[0]
    lines = [f"{label} latest attempt: {state}, {created}"]
    if state != "success" and (ok := last_success(events)):
        lines.append(f"Last success: {ok}")
    base = status["baselines"][kind].get(inst)
    pending = status["pending"].get(f"{kind}_{inst}")
    if not base:
        lines.append("Can't tell which commit this image contains")
    elif pending:
        lines.append(f"Image contains code as of ~{base}")
        if pending["count"]:
            lines += [f"▲ {pending['count']} commit(s) to {inst}/ not in this image:", *[f"  • {t}" for t in pending["titles"]]]
        if pending["other"]:
            lines.append(f"+ {pending['other']} other package commit(s) (shared code)")
    detail = ago(created) + ("" if base else " ?")
    return (f'<div class="lvl" title="{tip(lines)}">{oval(state, label, url)}'
            f'<div class="detail">{detail}{badges(pending)}</div></div>')


def container_cell(status: dict, env: str, inst: str) -> str:
    kinds = [env, f"{env}_l3"] if inst in L3_INSTRUMENTS else [env]
    css = ' class="group-start"' if inst in GROUP_STARTS else ""
    return f'<td{css}><div class="levels">' + "".join(level_status(status, k, inst) for k in kinds) + "</div></td>"


STATUS_CSS = """
.sbar { font-size: 0.85rem; margin-bottom: 0.75rem; width: 100%; }
.sbar .pills { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem; align-items: center; }
.sbar .pill { border: 1px solid rgba(128,128,128,.35); border-radius: 999px; padding: 0.15rem 0.7rem; }
.sbar a { color: inherit; text-decoration: none; }
.sbar .dot { display: inline-block; width: 0.7em; height: 0.7em; border-radius: 50%; margin-right: 0.35em; }
.sbar .scroll { overflow-x: auto; }
.sbar table { border-collapse: collapse; width: 100%; }
.sbar th, .sbar td { border: 1px solid rgba(128,128,128,.25); padding: 0.25rem 0.2rem; white-space: nowrap; text-align: center; }
.sbar th.row { text-align: left; font-weight: 600; }
.sbar .group-start { border-left: 3px solid rgba(128,128,128,.6); }
.sbar .levels { display: flex; justify-content: center; gap: 0.2rem; }
.sbar .lvl { display: flex; flex-direction: column; align-items: center; gap: 0.15rem; }
.sbar .oval { border-radius: 999px; padding: 0 0.4rem; font-size: 0.7rem; font-weight: 600; }
.sbar .detail { font-size: 0.7rem; opacity: 0.85; }
.sbar .badge { margin-left: 0.2em; padding: 0 0.4em; border-radius: 0.6em; background: #0969da; color: #fff; font-size: 0.75em; font-weight: 600; }
.sbar .other { margin-left: 0.25em; opacity: 0.55; font-size: 0.75em; }
.sbar .legend { opacity: 0.65; font-size: 0.75rem; margin-top: 0.25rem; }
"""


def render_status(status: dict) -> str:
    pills = "".join(infra_pill(env, status["infra"][env]) for env in ("dev", "prod"))
    header = "<tr><th></th>" + "".join(
        f'<th{" class=group-start" if inst in GROUP_STARTS else ""}>{PRETTY_NAMES[inst]}</th>'
        for inst in STATUS_COLUMNS
    ) + "</tr>"
    body = "".join(
        f'<tr><th class="row">{env.upper()}</th>'
        + "".join(container_cell(status, env, inst) for inst in STATUS_COLUMNS) + "</tr>"
        for env in ("dev", "prod")
    )
    return (
        f'<div class="sbar"><div class="pills"><span>Infrastructure ({SDM}):</span>{pills}</div>'
        f'<div class="scroll"><table>{header}{body}</table></div>'
        '<div class="legend">Oval: latest attempt (green ok · yellow running · red failed · grey unknown) · '
        "below it: time since it ran · "
        "<b>▲n</b> commits to that instrument's code not yet in the image · +n other package commits "
        "(shared code) · hover for details, click for the run. L0–L2 assumes dev images are built "
        "from imap_processing@dev.</div></div>"
    )


# ------------------------------------------------------------------ workflows
# The dashboard's only write calls. Dry runs dispatch the copies in .github/workflows
# of this repo, which take the same inputs as the real workflows but only print.
DRY_RUN_REPO = "leowerneck/sds-utils"
WORKFLOWS = {  # key -> (dry-run workflow in DRY_RUN_REPO, real repo, real workflow)
    "dev": ("dry-run-build_and_deploy_l0-l2_dev_containers.yml", SDM, "deploy_dev_containers.yml"),
    "dev_l3": ("dry-run-build_and_deploy_l3_dev_containers.yml", L3_REPO, "deploy_container.yml"),
    "promote": ("dry-run-promote_containers_dev_to_prod.yml", SDM, "promote_containers_dev_to_prod.yml"),
    "infra": ("dry-run-deploy_infrastructure.yml", SDM, "deploy.yml"),
}
PIPELINE = {  # (action, level) -> (workflow key, instrument choices, inputs for an instrument)
    ("Deploy to Dev", "L0–L2"): ("dev", ["all", *INSTRUMENTS], lambda i: {"instruments": i}),
    ("Deploy to Dev", "L3"): ("dev_l3", ["all", *L3_INSTRUMENTS], lambda i: {"instrument_name": i}),
    ("Promote to Prod", "L0–L2"): ("promote", ["all", *INSTRUMENTS], lambda i: {"instruments": i, "l3_data": "false"}),
    ("Promote to Prod", "L3"): ("promote", ["all", *L3_INSTRUMENTS], lambda i: {"instruments": i, "l3_data": "true"}),
}
STEP_ICONS = {"success": "✅", "failure": "❌", "running": "⏳", None: "⚪"}


def target(key: str, dry_run: bool) -> tuple[str, str, str]:
    """(repo, workflow file, ref) that `key` dispatches."""
    dry_workflow, repo, workflow = WORKFLOWS[key]
    if not dry_run:  # real dispatches are not enabled yet
        raise NotImplementedError("Only dry runs are supported for now")
    return (DRY_RUN_REPO, dry_workflow, "main") if dry_run else (f"{ORG}/{repo}", workflow, REPOS[repo])


def dispatch(key: str, inputs: dict, dry_run: bool):
    repo, workflow, ref = target(key, dry_run)
    return GH.get_repo(repo).get_workflow(workflow).create_dispatch(
        ref, inputs, throw=True, return_run_details=True)


def recent_runs(keys: list[str], dry_run: bool) -> list:
    runs = []
    for key in set(keys):
        repo, workflow, _ = target(key, dry_run)
        runs += islice(GH.get_repo(repo).get_workflow(workflow).get_runs(), 10)  # [:10] fails on short lists
    return sorted(runs, key=lambda r: r.created_at, reverse=True)[:10]


def run_status(run) -> tuple:
    """Refresh `run` in place; return it, its jobs, and (once completed) each job's log."""
    run.update()
    jobs = list(run.jobs())
    logs = {}
    if run.status == "completed":
        for job in jobs:
            text = requests.get(job.logs_url(), timeout=30).text
            text = re.sub(r"^\S+Z |\x1b\[[0-9;]*m", "", text, flags=re.M)  # drop timestamps and colors
            logs[job.name] = re.sub(r"##\[group\].*?##\[endgroup\]\n", "", text, flags=re.S)  # and setup noise
    return run, jobs, logs


# ------------------------------------------------------------------------- UI
THEMES = {"Light": False, "Auto": None, "Dark": True}  # Auto follows the OS
FONT = '<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">'
APP_CSS = """
body { font-family: "Source Sans 3", sans-serif !important; }
.q-drawer { background: #f0f2f6; }
.body--dark .q-drawer { background: #262730; }
.warn { padding: 0.75rem 1rem; border-radius: 0.5rem; background: rgba(255, 189, 69, .2); }
.q-btn { font-weight: 600; }
.q-table th { font-weight: 700; }
.q-table--no-wrap .q-table td { white-space: pre; }  /* never wrap, but show newlines: one value per line */
"""
# Flatter, denser Quasar widgets without the default ALL CAPS
ui.button.default_props("no-caps unelevated text-color=black")  # black reads better on orange
ui.toggle.default_props("no-caps unelevated toggle-text-color=black")
ui.input.default_props("outlined dense")
ui.select.default_props("outlined dense")
ui.table.default_props("flat bordered dense separator=cell")

LINK_CELL = ('<q-td :props="props"><a :href="props.value" target="_blank" class="text-primary">'
             '#{{ props.value.split("/").pop() }}</a></q-td>')
CLOSES_CELL = ('<q-td :props="props"><div v-for="url in props.value"><a :href="url" target="_blank" '
               'class="text-primary">#{{ url.split("/").pop() }}</a></div></q-td>')  # one link per line
TYPE_CELL = '<q-td :props="props" :style="{color: props.row._type_color}" class="text-weight-bold">{{ props.value }}</q-td>'
COLUMN_STYLES = {  # column -> inline CSS for its cells (the table otherwise never wraps)
    "title": "min-width: 16rem; max-width: 32rem; white-space: normal",
}


def show(rows: list[dict]) -> None:
    if not rows:
        ui.label("Nothing to show.")
        return
    ui.label(f"{len(rows)} rows (at most {LIMIT} per repo)").classes("text-sm opacity-60")
    columns = [{"name": k, "label": "No." if k == "link" else k[0].upper() + k[1:], "field": k, "sortable": True,
                "align": "left", "style": COLUMN_STYLES.get(k, "")}
               for k in rows[0] if not k.startswith("_")]  # _keys are for slots, not columns
    table = ui.table(rows=rows, columns=columns, row_key="link", pagination=100).classes("w-full")
    table.add_slot("body-cell-link", LINK_CELL)
    table.add_slot("body-cell-type", TYPE_CELL)
    table.add_slot("body-cell-closes", CLOSES_CELL)


def metric(label: str, value: int) -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-sm opacity-70")
        ui.label(str(value)).classes("text-4xl")


LIST_PAGES = {  # path -> (title, row function, state choices, filter on the text boxes)
    "/issues": ("Issues", issue_rows, ["Open", "Closed", "All"], True),
    "/pulls": ("Pull Requests", pr_rows, list(STATE_FILTERS), True),
    "/actions": ("Actions", run_rows, [], False),
}
WORKFLOW_PAGES = {"/pipeline": "Pipeline Containers", "/infrastructure": "Infrastructure Containers"}
SIDEBAR = {  # section -> {path: button label}
    "Development": {path: page[0] for path, page in LIST_PAGES.items()},
    "Deployment Workflows": WORKFLOW_PAGES,
}
TEXT_FILTERS = {  # text box label -> the row text it searches
    "Label contains": lambda row: row["labels"],
    "Author / assignee contains": lambda row: row["author"] + " " + row["assignees"],
    "Title contains": lambda row: row["title"],
}


def refresh() -> None:
    """Pick up a new token, drop cached data, and reload the page."""
    global GH
    GH = connect()
    fetch_repo.cache.clear()
    fetch_status.cache.clear()
    ui.navigate.reload()


def page_setup() -> None:
    """Styling and sidebar shared by every page."""
    ui.add_head_html(FONT)
    ui.add_css(STATUS_CSS + APP_CSS)
    ui.colors(primary="#ff8f00", dark="#0e1117", dark_page="#0e1117")
    ui.query(".nicegui-content").classes("px-12 py-8")
    dark = ui.dark_mode(THEMES["Auto"])
    with ui.left_drawer().classes("gap-3 p-4"):
        ui.button("Home", icon="home", on_click=lambda: ui.navigate.to("/")).classes("w-full")
        ui.button("Refresh", icon="refresh", on_click=refresh).classes("w-full")
        for section, pages in SIDEBAR.items():
            ui.label(section).classes("text-xl font-bold mt-4")
            for path, label in pages.items():
                ui.button(label, on_click=lambda p=path: ui.navigate.to(p)).classes("w-full")
        ui.label("Theme").classes("font-semibold mt-4")
        ui.toggle(list(THEMES), value="Auto", on_change=lambda e: dark.set_value(THEMES[e.value]))


@ui.page("/")
async def index():
    page_setup()

    async def load_status():
        status_box.clear()
        with status_box:
            ui.spinner()
        try:
            content = render_status(await run.io_bound(fetch_status))
        except Exception as exc:  # keep the rest of the dashboard usable
            content = f"<p class='text-negative'>Couldn't load deployment status: {html.escape(str(exc))}</p>"
        status_box.clear()
        with status_box:
            ui.html(content, sanitize=False).classes("w-full")

    async def load_summary():
        fetches = (run.io_bound(fetch_repo, repo, base, "Open") for repo, base in REPOS.items())
        results = dict(zip(REPOS, await asyncio.gather(*fetches)))
        summary.clear()
        with summary:
            for repo, res in results.items():
                with ui.card().props("flat bordered").classes("flex-1 min-w-64"):
                    ui.label(repo).classes("text-xl font-semibold")
                    with ui.grid(columns=2).classes("w-full gap-4"):
                        metric("Open issues", res["data"]["openIssues"]["totalCount"])
                        metric(f"Open PRs → {REPOS[repo]}", res["data"]["openPRs"]["totalCount"])
                        metric("Drafts (shown)", sum(p["isDraft"] for p in res["data"]["pullRequests"]["nodes"]))
                        metric("Failed runs (last 20)", sum(r.conclusion == "failure" for r in res["runs"]))

    ui.label("IMAP SDC GitHub Dashboard").classes("text-4xl font-bold")
    ui.label("Read-only, except for the workflow pages in the sidebar. "
             "Data from GitHub, cached for 5 minutes (deployment status: 2 minutes).").classes("text-sm opacity-60")
    status_box = ui.column().classes("w-full")
    summary = ui.row().classes("w-full")

    await ui.context.client.connected()
    await asyncio.gather(load_status(), load_summary())


async def list_page(path: str) -> None:
    """Issues, pull requests, or Actions runs across the selected repos, as one table."""
    title, row_fn, states, filtered = LIST_PAGES[path]
    page_setup()
    latest = None  # the newest load; older ones that finish later are dropped

    def matches(row: dict) -> bool:
        return all(box.value.lower() in text(row).lower() for box, text in zip(boxes, TEXT_FILTERS.values()))

    async def load():
        nonlocal latest
        latest = this = object()
        loading.props("showing")
        names = [r for r in REPOS if r in repo_tree.props["ticked"]]
        repo_box.value = "All" if len(names) == len(REPOS) else ", ".join(names) or "None"
        fetches = (run.io_bound(fetch_repo, r, REPOS[r], state.value if states else "Open") for r in names)
        results = dict(zip(names, await asyncio.gather(*fetches)))
        if this is not latest:
            return
        table_box.clear()
        with table_box:
            if filtered and any(res["projects_error"] for res in results.values()):
                ui.label("Projects are unavailable (token likely missing the `read:project` scope). "
                         "Grant read-only access with `gh auth refresh -s read:project` "
                         "(or put a token with that scope in $GITHUB_TOKEN and restart), then click Refresh."
                         ).classes("warn w-full")
            show([row for r, res in results.items() for row in sorted(row_fn(r, res), key=type_rank) if matches(row)])
        loading.props(remove="showing")

    ui.label(title).classes("text-4xl font-bold")
    ui.label("Data from GitHub, cached for 5 minutes.").classes("text-sm opacity-60")
    with ui.row().classes("w-full items-center gap-4"):
        repo_box = ui.input("Repositories").props("readonly").classes("min-w-64")  # opens the checkbox tree
        with repo_box.add_slot("append"):
            ui.icon("arrow_drop_down")
        with repo_box, ui.menu().props("fit"):
            repo_tree = ui.tree([{"id": "All", "children": [{"id": r} for r in REPOS]}], label_key="id",
                                tick_strategy="leaf", on_tick=load).expand().tick()
        if states:
            state = ui.toggle(states, value=states[0], on_change=load)
        boxes = [ui.input(label, value="", on_change=load).props("debounce=300").classes("min-w-48")
                 for label in TEXT_FILTERS] if filtered else []
    with ui.element("div").classes("w-full relative min-h-48"):  # the overlay dims the table while loading
        table_box = ui.column().classes("w-full")
        with ui.element("q-inner-loading") as loading:
            ui.spinner(size="3em")

    await ui.context.client.connected()
    await load()


@ui.page("/issues")
async def issues_page():
    await list_page("/issues")


@ui.page("/pulls")
async def pulls_page():
    await list_page("/pulls")


@ui.page("/actions")
async def actions_page():
    await list_page("/actions")


async def workflow_page(path: str, keys: list[str], form) -> None:
    """Layout shared by the workflow pages: inputs, Run Action, live run monitor, recent runs.

    `form(on_change)` adds the input widgets and returns a function giving
    (workflow key, inputs) once every input is chosen, else None.
    """
    page_setup()

    async def start():
        key, inputs = choice()
        try:
            new_run = await run.io_bound(dispatch, key, inputs, dry_run.value)
        except GithubException as exc:
            ui.notify(f"Dispatch failed: {exc}", type="negative", multi_line=True)
            return
        ui.notify("Dispatched", type="positive")
        await asyncio.gather(watch(new_run), load_recent())

    async def watch(wf_run):
        nonlocal watched
        watched = wf_run
        timer.activate()
        await poll()

    async def poll():
        wf_run, jobs, logs = await run.io_bound(run_status, watched)
        monitor.refresh(wf_run, jobs, logs)
        if wf_run.status == "completed":
            timer.deactivate()
            await load_recent()

    @ui.refreshable
    def monitor(wf_run=None, jobs=(), logs=None):
        if wf_run is None:
            return
        with ui.card().props("flat bordered").classes("w-full"):
            with ui.row().classes("w-full items-center"):
                ui.label(f"{STEP_ICONS[outcome(wf_run)]} {wf_run.display_title}").classes("text-lg font-semibold")
                ui.label(wf_run.conclusion or wf_run.status).classes("opacity-60")
                ui.space()
                ui.button("View on GitHub", icon="open_in_new",
                          on_click=lambda: ui.navigate.to(wf_run.html_url, new_tab=True))
            for job in jobs:
                ui.label(f"{STEP_ICONS[outcome(job)]} {job.name}").classes("font-semibold")
                for step in job.steps:
                    ui.label(f"{STEP_ICONS[outcome(step)]} {step.name}").classes("ml-6 text-sm")
                if logs and job.name in logs:
                    with ui.expansion("Log", value=True).classes("w-full"):
                        ui.code(logs[job.name], language="text").classes("w-full")

    async def load_recent():
        runs = await run.io_bound(recent_runs, keys, dry_run.value)
        recent_box.clear()
        with recent_box, ui.list().props("dense bordered separator").classes("w-full"):
            for r in runs:
                ui.item(f"{STEP_ICONS[outcome(r)]} {r.display_title} · {_time(r.created_at)}",
                        on_click=lambda r=r: watch(r))

    ui.label(WORKFLOW_PAGES[path].replace("\n", " ")).classes("text-4xl font-bold")
    ui.label(f"Dry runs dispatch the [Dry Run] copies of these workflows in {DRY_RUN_REPO}; "
             "they only print what a real run would do.").classes("text-sm opacity-60")
    with ui.row().classes("items-center gap-4"):
        choice = form(lambda: run_button.set_enabled(choice() is not None))
    with ui.row().classes("items-center gap-4"):
        dry_run = ui.checkbox("dry-run", value=True).props("disable")  # real runs not supported yet
        run_button = ui.button("Run Action", icon="play_arrow", on_click=start)
        run_button.disable()
    watched = None
    timer = ui.timer(3, poll, active=False)
    monitor()
    ui.label("Recent runs (click to monitor)").classes("text-xl font-semibold mt-4")
    recent_box = ui.column().classes("w-full")

    await ui.context.client.connected()
    await load_recent()


@ui.page("/pipeline")
async def pipeline_page():
    def form(on_change):
        def update():
            _, choices, _ = PIPELINE.get((action.value, level.value), (None, [], None))
            instrument.set_options({i: PRETTY_NAMES.get(i, "All") for i in choices},
                                   value=instrument.value if instrument.value in choices else None)
            on_change()

        action = ui.select(list(dict.fromkeys(a for a, _ in PIPELINE)), label="Action", on_change=update)
        level = ui.select(["L0–L2", "L3"], label="Data Level", on_change=update)
        instrument = ui.select({}, label="Instrument", on_change=on_change)
        for box in (action, level, instrument):
            box.classes("min-w-48")

        def choice():
            if instrument.value is None:
                return None
            key, _, inputs = PIPELINE[(action.value, level.value)]
            return key, inputs(instrument.value)
        return choice

    await workflow_page("/pipeline", ["dev", "dev_l3", "promote"], form)


@ui.page("/infrastructure")
async def infrastructure_page():
    def form(on_change):
        env = ui.select({"dev": "Development", "prod": "Production"}, label="Environment",
                        on_change=on_change).classes("min-w-48")
        skips = [ui.checkbox(f"Skip {stack}", value=True).bind_visibility_from(env, "value", value="prod")
                 for stack in ("IalirtStack", "IalirtSsrStack")]

        def choice():
            if env.value is None:
                return None
            skip_ialirt, skip_ialirt_ssr = (str(box.value).lower() for box in skips)
            return "infra", {"environment": env.value, "skip_ialirt": skip_ialirt, "skip_ialirt_ssr": skip_ialirt_ssr}
        return choice

    await workflow_page("/infrastructure", ["infra"], form)


def main():
    """Entrypoint for the IMAP GitHub Dashboard."""
    # localhost only: the app acts with your GitHub token. `--reload` restarts on source edits.
    ui.run(title="IMAP GitHub Dashboard", host="127.0.0.1", port=8765,  # 8080 is often taken
           reload="--reload" in sys.argv, uvicorn_reload_dirs=os.path.dirname(__file__))


if __name__ in {"__main__", "__mp_main__"}:
    main()
