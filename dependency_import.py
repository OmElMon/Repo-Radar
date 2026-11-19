"""Find GitHub watchlist candidates for static Python project dependencies."""

import json
import re
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


REQUIREMENT = re.compile(
    r"([a-zA-Z0-9](?:[a-zA-Z0-9_.-]*[a-zA-Z0-9])?)(?:\s*\[[^\]]+\])?\s*(.*)"
)
REPOSITORY = re.compile(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+")
NON_REPO_OWNERS = {"apps", "collections", "features", "marketplace", "orgs", "settings", "topics", "users"}


def github_repository(value):
    """Extract a repo from an HTTPS GitHub URL without visiting that URL."""
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value.strip().removeprefix("git+"))
    except ValueError:
        return None
    if url.scheme != "https" or url.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    parts = url.path.strip("/").split("/")
    if len(parts) < 2 or parts[0].lower() in NON_REPO_OWNERS:
        return None
    if len(parts) > 2 and parts[2] not in {"tree", "blob", "issues", "releases", "wiki"}:
        return None
    owner, repo = parts[:2]
    repo = repo.split("@", 1)[0].removesuffix(".git")
    name = f"{owner}/{repo}"
    if REPOSITORY.fullmatch(name) and repo not in {".", ".."}:
        return name
    return None


def pypi_repository(name):
    with urlopen(f"https://pypi.org/pypi/{name}/json", timeout=10) as response:
        info = json.load(response)["info"]
    links = info.get("project_urls") or {}
    source_links = {
        value for label, value in links.items()
        if re.search(r"\b(source|code|repo|repository|github)\b", label.lower())
        and isinstance(value, str)
    }
    preferred = {repo.lower(): repo for repo in map(github_repository, sorted(source_links)) if repo}
    if len(preferred) == 1:
        return next(iter(preferred.values()))
    if preferred:
        raise ValueError("PyPI lists multiple GitHub source repositories")
    homepages = {label.lower(): value for label, value in links.items()}
    return github_repository(homepages.get("homepage")) or github_repository(info.get("home_page"))


def import_watchlist(path, extras):
    try:
        import tomllib
    except ModuleNotFoundError:
        raise ValueError("Importing pyproject.toml requires Python 3.11 or newer.") from None

    with path.open("rb") as project_file:
        project = tomllib.load(project_file).get("project")
    if not isinstance(project, dict):
        raise ValueError("Expected a [project] table with static dependencies.")

    requirements = project.get("dependencies", [])
    if not isinstance(requirements, list):
        raise ValueError("project.dependencies must be a list.")
    requirements = requirements.copy()
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("project.optional-dependencies must be a table.")
    for extra in dict.fromkeys(extras):
        if extra not in optional:
            raise ValueError(f"Unknown project extra: {extra}")
        if not isinstance(optional[extra], list):
            raise ValueError(f"Dependencies for extra {extra} must be a list.")
        requirements.extend(optional[extra])
    if not requirements:
        raise ValueError("No static dependencies found in the selected dependency groups.")

    repos = {}
    resolved = []
    skipped = []
    lookups = {}
    for index, spec in enumerate(requirements, 1):
        match = REQUIREMENT.fullmatch(spec.strip()) if isinstance(spec, str) else None
        if not match:
            raise ValueError(f"Invalid dependency entry #{index}.")
        package, suffix = match.groups()
        if suffix and suffix[0] not in "@;<>=!~(":
            raise ValueError(f"Invalid dependency entry for {package}.")
        if suffix.startswith("@"):
            direct_url = suffix[1:].split(maxsplit=1)
            repo = github_repository(direct_url[0]) if direct_url else None
            reason = "direct dependency has no supported GitHub URL"
        else:
            normalized = re.sub(r"[-_.]+", "-", package).lower()
            if normalized not in lookups:
                try:
                    lookups[normalized] = (pypi_repository(normalized), "no unambiguous GitHub source link on PyPI")
                except (URLError, OSError, KeyError, ValueError) as error:
                    lookups[normalized] = (None, str(error) or "PyPI lookup failed")
            repo, reason = lookups[normalized]
        if repo:
            repos.setdefault(repo.lower(), repo)
            resolved.append((package, repo))
        else:
            skipped.append((package, reason))
    return list(repos.values()), list(dict.fromkeys(resolved)), list(dict.fromkeys(skipped))
