#!/usr/bin/env python3
"""A personalized digest of public GitHub repositories."""

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


TOPIC = re.compile(r"[a-z0-9][a-z0-9-]{0,49}")
REPO = re.compile(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")


class GitHubError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    def get(self, path, **params):
        url = "https://api.github.com" + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-radar",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            reset = error.headers.get("Retry-After")
            hint = f" Retry after {reset} seconds." if reset else ""
            if error.headers.get("X-RateLimit-Remaining") == "0":
                hint += " GitHub's rate limit is exhausted."
            raise GitHubError(f"GitHub returned HTTP {error.code}.{hint}", error.code) from error
        except (URLError, TimeoutError, ValueError) as error:
            raise GitHubError(f"GitHub request failed: {error}") from error


def load_profile(path):
    profile = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise ValueError("The profile must be a JSON object.")
    topics = profile.get("topics", [])
    watch = profile.get("watch", [])
    if not isinstance(topics, list) or any(
        not isinstance(topic, str) or not TOPIC.fullmatch(topic) for topic in topics
    ):
        raise ValueError("topics must be a list of lowercase GitHub topic slugs.")
    topics = list(dict.fromkeys(topics))
    if not isinstance(watch, list) or any(
        not isinstance(repo, str) or not REPO.fullmatch(repo)
        or repo.rsplit("/", 1)[1] in {".", ".."} for repo in watch
    ):
        raise ValueError("watch must be a list of GitHub repos in owner/name form.")
    watch = list({repo.lower(): repo for repo in watch}.values())
    if not topics and not watch:
        raise ValueError("Add at least one topic or watched repo.")
    return topics, watch


def markdown(value):
    """Render external content as plain text inside Markdown."""
    value = " ".join(str(value).split())
    return re.sub(r"([\\`*_{}\[\]<>#|@~!&])", r"\\\1", value)


def repo_url(name):
    return "https://github.com/" + quote(name, safe="/")


def digest_discovery(api, topics, watched, state, today, limit, warnings):
    candidates = {}
    fresh_since = today - timedelta(days=30)
    active_since = today - timedelta(days=14)
    for topic in topics:
        base = f"topic:{topic} is:public archived:false fork:false pushed:>={active_since}"
        searches = [(f"created:>={fresh_since} stars:>=5", True), ("stars:>=25", False)]
        for qualifier, emerging in searches:
            try:
                result = api.get("/search/repositories", q=f"{base} {qualifier}",
                                 sort="stars", order="desc", per_page=30)
            except GitHubError as error:
                warnings.append(f"Discovery for {topic} ({'new' if emerging else 'active'}): {error}")
                continue
            if result.get("incomplete_results"):
                warnings.append(f"GitHub returned partial search results for {topic}.")
            for repo in result["items"]:
                if repo.get("private") or repo["full_name"].lower() in watched:
                    continue
                entry = candidates.setdefault(str(repo["id"]), {
                    "repo": repo, "topics": set(), "emerging": False,
                })
                entry["topics"].add(topic)
                entry["emerging"] |= emerging

    previous = state.get("stars", {})
    snapshots = {}
    for key, sample in previous.items():
        if (today - date.fromisoformat(sample["date"])).days <= 30:
            snapshots[key] = sample

    emerging_repos = []
    established_repos = []
    for key, entry in candidates.items():
        repo = entry["repo"]
        stars = repo["stargazers_count"]
        forks = repo["forks_count"]
        sample = previous.get(key)
        baseline = sample
        if sample and sample["date"] == today.isoformat():
            baseline = sample.get("baseline")
        days = (today - date.fromisoformat(baseline["date"])).days if baseline else None
        growth = stars - baseline["count"] if days is not None and 1 <= days <= 14 else None
        snapshots[key] = {"count": stars, "date": today.isoformat()}
        if growth is not None:
            snapshots[key]["baseline"] = {"count": baseline["count"], "date": baseline["date"]}
        score = (
            2 * len(entry["topics"]) + math.log10(stars + 1)
            + 1.6 * math.log1p(forks) + 1.5 * entry["emerging"]
        )
        if growth is not None and growth > 0:
            score += 2 * math.log1p(growth / days)
        reasons = ["matches " + ", ".join(sorted(entry["topics"]))]
        if entry["emerging"]:
            reasons.append("created within 30 days")
        if growth is not None and growth > 0:
            reasons.append(f"+{growth:,} stars since {baseline['date']}")
        name = repo["full_name"]
        description = markdown((repo.get("description") or "")[:200])
        line = (
            f"- [{markdown(name)}]({repo_url(name)}) ({stars:,} stars, {forks:,} forks) — "
            f"{', '.join(reasons)}." + (f" {description}" if description else "")
        )
        group = emerging_repos if entry["emerging"] else established_repos
        group.append((-score, name.lower(), line))

    state["stars"] = snapshots
    emerging_repos.sort(reverse=True)
    established_repos.sort(reverse=True)
    lines = []
    while len(lines) < limit and (emerging_repos or established_repos):
        first, second = (
            (emerging_repos, established_repos) if len(lines) % 2 == 0
            else (established_repos, emerging_repos)
        )
        _, _, line = (first or second).pop()
        lines.append(line)
    return lines


def release_lines(release, url):
    tag = release["tag_name"]
    tag_url = url + "/releases/tag/" + quote(tag, safe="")
    label = "Prerelease" if release.get("prerelease") else "Release"
    title = release.get("name") or tag
    published = release["published_at"][:10]
    lines = [f"- {label} [{markdown(title)}]({tag_url}) · {published}"]
    notes = []
    for line in (release.get("body") or "").splitlines():
        stripped = line.strip()
        if re.match(r"^[-*+]\s+|^\d+\.\s+", stripped):
            notes.append(re.sub(r"^[-*+]\s+|^\d+\.\s+", "", stripped))
    if not notes:
        notes = [
            line.strip() for line in (release.get("body") or "").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "<", "```"))
        ]
    for note in notes[:3]:
        lines.append(f"  - {markdown(note[:200])}{'…' if len(note) > 200 else ''}")
    return lines


def commit_line(commit, url):
    sha = commit["sha"]
    title = commit["commit"]["message"].partition("\n")[0]
    return f"- [`{sha[:7]}`]({url}/commit/{sha}) {markdown(title[:180])}"


def watch_repo(api, configured_name, previous):
    path = "/repos/" + quote(configured_name, safe="/")
    repo = api.get(path)
    if repo.get("private") or repo.get("visibility") in {"private", "internal"}:
        raise GitHubError("Repo Radar only tracks public repos.")
    name = repo["full_name"]
    url = repo_url(name)
    path = "/repos/" + quote(name, safe="/")
    if previous.get("id") != repo["id"]:
        previous = {}
    branch = repo["default_branch"]
    try:
        commits = api.get(path + "/commits", sha=branch, per_page=1)
    except GitHubError as error:
        if error.status != 409:
            raise
        commits = []  # GitHub returns HTTP 409 when the repository is empty.
    head = commits[0]["sha"] if commits else None
    old_head = previous.get("head")
    releases = api.get(path + "/releases", per_page=100)
    releases = [r for r in releases if not r["draft"] and r.get("published_at")]
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    seen = set(previous.get("releases", []))
    new_releases = [r for r in releases if r["id"] not in seen] if previous else releases[:1]
    lines = [f"### [{markdown(name)}]({url})", ""]
    for release in new_releases:
        lines.extend(release_lines(release, url))

    if not previous:
        lines.append(f"- Started tracking **{markdown(branch)}**.")
        if commits:
            lines.append(commit_line(commits[0], url))
    elif head != old_head or previous.get("branch") != branch:
        if previous.get("branch") == branch and head and old_head:
            if not SHA.fullmatch(old_head) or not SHA.fullmatch(head):
                raise GitHubError("Invalid commit SHA in GitHub response or saved state.")
            comparison = f"{old_head}...{head}"
            try:
                diff = api.get(path + "/compare/" + comparison, per_page=5)
            except GitHubError as error:
                if error.status not in (404, 409):
                    raise
                diff = None  # The previous history may no longer be available.
            if diff and diff["status"] == "ahead":
                total = diff["total_commits"]
                noun = "commit" if total == 1 else "commits"
                lines.append(
                    f"- [{total:,} new {noun}]({url}/compare/{comparison}) on **{markdown(branch)}**."
                )
                lines.extend(commit_line(commit, url) for commit in diff["commits"])
                added = [file["filename"] for file in diff.get("files", []) if file["status"] == "added"]
                if added:
                    files = ", ".join(markdown(filename) for filename in added[:5])
                    suffix = f", plus {len(added) - 5} more shown by GitHub" if len(added) > 5 else ""
                    lines.append(f"- Added files: {files}{suffix}.")
            else:
                lines.append(f"- History changed on **{markdown(branch)}**.")
                lines.append(commit_line(commits[0], url))
        else:
            lines.append(f"- New baseline for **{markdown(branch)}**.")
            if commits:
                lines.append(commit_line(commits[0], url))
            else:
                lines.append("- The default branch has no commits.")
    if len(lines) == 2:
        lines.append("- No new releases or default-branch commits.")
    snapshot = {
        "id": repo["id"],
        "branch": branch,
        "head": head,
        "releases": sorted(seen | {release["id"] for release in releases}),
    }
    return lines, snapshot


def run(api, topics, watch, state, today, limit):
    warnings = []
    lines = [f"# Repo Radar · {today}", ""]
    if topics:
        lines.extend(["## Discover", ""])
        discovery = digest_discovery(
            api, topics, {repo.lower() for repo in watch}, state, today, limit, warnings,
        )
        lines.extend(discovery or ["No matching repositories found."])
        lines.append("")
    if watch:
        lines.extend(["## Watchlist", ""])
    watched = state.setdefault("watch", {})
    for name in watch:
        key = name.lower()
        try:
            updates, snapshot = watch_repo(api, name, watched.get(key, {}))
        except GitHubError as error:
            warnings.append(f"{name}: {error}")
            lines.extend([f"### {markdown(name)}", "", "- Check failed.", ""])
        else:
            watched[key] = snapshot
            lines.extend(updates + [""])
    if warnings:
        lines.extend(["## Check warnings", ""] + [f"- {markdown(w)}" for w in warnings])
    return "\n".join(lines).rstrip() + "\n", warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True, help="Path to a JSON profile")
    parser.add_argument("--state", type=Path, help="SQLite state file (default: PROFILE.state.sqlite3)")
    parser.add_argument("--limit", type=int, default=8, help="Maximum discovery results")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    state_path = args.state or args.profile.with_suffix(".state.sqlite3")
    if state_path.resolve() == args.profile.resolve():
        parser.error("--state must differ from --profile")
    try:
        topics, watch = load_profile(args.profile)
        today = datetime.now(timezone.utc).date()
        with sqlite3.connect(state_path, timeout=5) as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT)")
            row = database.execute("SELECT payload FROM state WHERE id = 1").fetchone()
            state = json.loads(row[0]) if row else {}
            report, warnings = run(GitHub(), topics, watch, state, today, args.limit)
            sys.stdout.write(report)
            sys.stdout.flush()
            database.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state),))
        return int(bool(warnings))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"repo-radar: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
