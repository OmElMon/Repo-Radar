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


def profile_settings(profile, *, allow_empty=False):
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
    mode = profile.get("watch_mode", "all")
    if mode not in ("all", "releases"):
        raise ValueError('watch_mode must be "all" or "releases".')
    if not allow_empty and not topics and not watch:
        raise ValueError("Add at least one topic or watched repo.")
    return topics, watch, mode


def markdown(value):
    """Render external content as plain text inside Markdown."""
    value = " ".join(str(value).split())
    return re.sub(r"([\\`*_{}\[\]<>#|@~!&])", r"\\\1", value)


def repo_url(name):
    return "https://github.com/" + quote(name, safe="/")


def plain_text(value):
    """Keep readable text from release-note Markdown before escaping it."""
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\\([\\`*_{}\[\]<>#|~!])", r"\1", value)
    value = re.sub(r"\`+([^\`]+)\`+", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return markdown(value)


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
        url = repo_url(name)
        description = markdown(repo.get("description") or "The repository has no description.")
        license_name = (repo.get("license") or {}).get("spdx_id") or "Unspecified"
        if license_name == "NOASSERTION":
            license_name = "Review the repository license"
        details = [
            f"### [{markdown(name)}]({url})", "",
            description, "",
            f"- **Why it surfaced** — {', '.join(reasons)}.",
            f"- **Community** — {stars:,} stars · {forks:,} forks.",
            f"- **Language and license** — {markdown(repo.get('language') or 'Unspecified')} · "
            f"{markdown(license_name)}.",
            f"- **Activity** — created {(repo.get('created_at') or 'Unknown')[:10]} · "
            f"last push {(repo.get('pushed_at') or 'Unknown')[:10]}.",
        ]
        if growth is None:
            details.append("- **Momentum** — collecting a baseline for a later UTC-day comparison.")
        elif growth <= 0:
            details.append(f"- **Momentum** — {growth:+,} stars since {baseline['date']}.")
        details.extend([
            f"- **Explore** — [README]({url}#readme) · [Releases]({url}/releases) · "
            f"[Contribution opportunities]({url}/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22).",
            "",
        ])
        group = emerging_repos if entry["emerging"] else established_repos
        group.append((-score, name.lower(), details))

    state["stars"] = snapshots
    emerging_repos.sort(reverse=True)
    established_repos.sort(reverse=True)
    lines = []
    while len(lines) < limit and (emerging_repos or established_repos):
        first, second = (
            (emerging_repos, established_repos) if len(lines) % 2 == 0
            else (established_repos, emerging_repos)
        )
        _, _, details = (first or second).pop()
        lines.append("\n".join(details))
    return lines


def release_lines(release, url):
    tag = release["tag_name"]
    tag_url = url + "/releases/tag/" + quote(tag, safe="")
    label = "Prerelease" if release.get("prerelease") else "Release"
    title = release.get("name") or tag
    published = release["published_at"][:10]
    lines = [f"- {label} [{markdown(title)}]({tag_url}) · {published}"]
    notes = []
    section = ""
    in_code = False
    for line in (release.get("body") or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code:
            continue
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip()
        elif re.match(r"^[-*+]\s+|^\d+\.\s+", stripped):
            notes.append([section, re.sub(r"^[-*+]\s+|^\d+\.\s+", "", stripped)])
        elif stripped and line[:1].isspace() and notes:
            notes[-1][1] += " " + stripped
        elif stripped:
            notes.append([section, stripped])
    for section, note in notes[:5]:
        prefix = f"**{plain_text(section)}** — " if section else ""
        lines.append(f"  - {prefix}{plain_text(note[:600])}{'…' if len(note) > 600 else ''}")
    if not notes:
        lines.append("  - Release notes were not provided.")
    return lines


def commit_line(commit, url):
    sha = commit["sha"]
    title = commit["commit"]["message"].partition("\n")[0]
    return f"- [`{sha[:7]}`]({url}/commit/{sha}) {markdown(title[:180])}"


def watch_repo(api, configured_name, previous, *, releases_only=False):
    path = "/repos/" + quote(configured_name, safe="/")
    repo = api.get(path)
    if repo.get("private") or repo.get("visibility") in {"private", "internal"}:
        raise GitHubError("Repo Radar only tracks public repos.")
    name = repo["full_name"]
    url = repo_url(name)
    path = "/repos/" + quote(name, safe="/")
    if previous.get("id") != repo["id"]:
        previous = {}
    releases = api.get(path + "/releases", per_page=100)
    releases = [r for r in releases if not r["draft"] and r.get("published_at")]
    releases.sort(key=lambda r: r["published_at"], reverse=True)
    seen = set(previous.get("releases", []))
    new_releases = [r for r in releases if r["id"] not in seen] if previous else releases[:1]
    lines = [f"### [{markdown(name)}]({url})", ""]
    for release in new_releases:
        lines.extend(release_lines(release, url))

    snapshot = {
        "id": repo["id"],
        "releases": sorted(seen | {release["id"] for release in releases}),
    }
    if releases_only:
        if not new_releases:
            lines.append("- No published releases yet." if not previous else "- No new releases.")
    else:
        commit_lines, head = watch_commits(api, path, url, repo["default_branch"], previous)
        lines.extend(commit_lines)
        snapshot.update(branch=repo["default_branch"], head=head)
        if len(lines) == 2:
            lines.append("- No new releases or default-branch commits.")
    return lines, snapshot


def watch_commits(api, path, url, branch, previous):
    lines = []
    try:
        commits = api.get(path + "/commits", sha=branch, per_page=1)
    except GitHubError as error:
        if error.status != 409:
            raise
        commits = []  # GitHub returns HTTP 409 when the repository is empty.
    head = commits[0]["sha"] if commits else None
    old_head = previous.get("head")
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
                changed_files = diff.get("files", [])
                if changed_files:
                    counts = {}
                    for file in changed_files:
                        counts[file["status"]] = counts.get(file["status"], 0) + 1
                    breakdown = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
                    lines.append(f"- File changes returned by GitHub — {breakdown}.")
                added = [file["filename"] for file in changed_files if file["status"] == "added"]
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
    return lines, head


def run(api, topics, watch, state, today, limit, *, watch_mode="all"):
    releases_only = watch_mode == "releases"
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
        heading = "## Watchlist (releases only)" if releases_only else "## Watchlist"
        lines.extend([heading, ""])
    watched = state.setdefault("watch", {})
    for name in watch:
        key = name.lower()
        try:
            updates, snapshot = watch_repo(api, name, watched.get(key, {}), releases_only=releases_only)
        except GitHubError as error:
            warnings.append(f"{name}: {error}")
            lines.extend([f"### {markdown(name)}", "", "- Check failed.", ""])
        else:
            watched[key] = snapshot
            lines.extend(updates + [""])
    if warnings:
        lines.extend(["## Check warnings", ""] + [f"- {markdown(w)}" for w in warnings])
    lines.extend([
        "", "## About this check", "",
        f"Checked on {today} in UTC with {len(topics)} discovery topics and {len(watch)} watched repositories.",
        "",
        "Discovery samples up to 30 results per query from repositories pushed within 14 days. "
        "Emerging projects were created within 30 days and have at least 5 stars. "
        "The established-project search starts at 25 stars. Ranking uses topic matches, stars, "
        "forks, recent creation, and observed star growth, then alternates between both groups.",
        "",
        "Descriptions and release-note excerpts come from project maintainers. "
        "Topic matches describe relevance to the configured profile. "
        "Stars and forks measure community activity. Evaluate the README, license, and issues "
        "when deciding whether a project fits your work.",
        "",
    ])
    if watch:
        lines.extend(["", "Watchlist checks inspect up to 100 releases."])
        if not releases_only:
            lines[-1] += (
                " Commit lists show up to five entries in chronological order from the comparison. "
                "GitHub returns at most 300 changed files. Follow the comparison link for further detail."
            )
    return "\n".join(lines).rstrip() + "\n", warnings


def preview_dependencies(argv):
    from dependency_import import import_watchlist

    parser = argparse.ArgumentParser(
        prog="repo_radar.py import-deps",
        description="Print a proposed profile for review. The current profile is read-only.",
    )
    parser.add_argument("pyproject", type=Path, help="Path to pyproject.toml")
    parser.add_argument("--profile", type=Path, help="Existing profile to merge into the preview")
    parser.add_argument("--extra", action="append", default=[], help="Include a named project extra (repeatable)")
    args = parser.parse_args(argv)
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8")) if args.profile else {}
        _, existing, _ = profile_settings(profile, allow_empty=True)
        repos, resolved, skipped = import_watchlist(args.pyproject, args.extra)
        for package, repo in resolved:
            print(f"{package} → {repo}", file=sys.stderr)
        for package, reason in skipped:
            print(f"Unresolved {package}: {reason}", file=sys.stderr)
        if not repos:
            raise ValueError("No GitHub repositories were resolved.")
        merged = {name.lower(): name for name in existing}
        for name in repos:
            merged.setdefault(name.lower(), name)
        profile["watch"] = list(merged.values())
        if not args.profile:
            profile["watch_mode"] = "releases"
        profile_settings(profile)
        print(json.dumps(profile, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print(f"repo-radar: {error}", file=sys.stderr)
        return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["import-deps"]:
        return preview_dependencies(argv[1:])
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="Preview a dependency watchlist with import-deps --help.",
    )
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
        topics, watch, mode = profile_settings(json.loads(args.profile.read_text(encoding="utf-8")))
        today = datetime.now(timezone.utc).date()
        with sqlite3.connect(state_path, timeout=5) as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT)")
            row = database.execute("SELECT payload FROM state WHERE id = 1").fetchone()
            state = json.loads(row[0]) if row else {}
            report, warnings = run(GitHub(), topics, watch, state, today, args.limit, watch_mode=mode)
            sys.stdout.write(report)
            sys.stdout.flush()
            database.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state),))
        return int(bool(warnings))
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"repo-radar: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
