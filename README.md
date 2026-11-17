# Repo Radar

Repo Radar finds public GitHub repositories that match your interests and tracks releases and code changes in the repositories you follow. It produces a Markdown digest with links to each repository, release, commit, and comparison.

## Quick start

Use Python 3.9 or newer with an internet connection. The project uses the Python standard library and runs directly from this folder.

```sh
cp profile.example.json profile.json
python3 repo_radar.py --profile profile.json
```

Edit `profile.json` to fit your work. Use GitHub topic slugs such as `python`, `accessibility`, or `self-hosted`, and specify repositories as `owner/name`. Either list can be empty. Save a digest with shell redirection.

```sh
python3 repo_radar.py --profile profile.json --limit 5 > digest.md
```

The optional `GITHUB_TOKEN` environment variable authenticates requests when you need a higher rate limit. Keep credentials in the environment. The default SQLite database sits beside the profile as `profile.state.sqlite3`. Keep the same database between checks, or supply its path with `--state`. Use a separate database per profile.

## What the digest shows

- **Discover.** GitHub Search finds recently pushed public repositories for each topic. Repo Radar ranks candidates by matching topics, stars, forks, and observed star growth. It alternates between emerging and established projects while both have results. The first run records a baseline. Positive growth can appear after a later UTC-day check. The ranking is specific to your profile and the sampled search results.
- **Watchlist.** The first check shows the latest published release from the fetched results and the current default-branch commit. Later checks show newly seen published releases, including labeled prereleases, and compare the current branch head to the saved head. Each release includes up to three excerpts from its notes. Commit updates include a full comparison link, up to five commit titles, and up to five added file paths when GitHub includes them. A branch switch or rewritten history establishes a new commit baseline.
- **Check warnings.** An API error or partial search is recorded in the digest and the command exits with status 1. Successful checks are saved and failed watchlist checks keep their previous baseline so they can be retried.

Discovery samples up to 30 results per query and uses two search requests per topic. Each watchlist check inspects the first 100 results from GitHub's Releases endpoint. Ordinary Git tags are outside that endpoint. Compare responses can include up to 300 changed files. [Repository search](https://docs.github.com/en/rest/search/search#search-repositories) · [Releases](https://docs.github.com/en/rest/releases/releases#list-releases) · [Commit comparisons](https://docs.github.com/en/rest/commits/commits#compare-two-commits)

To run daily, add a cron entry with paths suited to your machine. This example saves one digest per day at 9 a.m. in the scheduler's local timezone. Create the digests directory before using the entry.

```cron
0 9 * * * /path/to/python3 /path/to/repo-radar/repo_radar.py --profile /path/to/profile.json > /path/to/digests/$(date +\%F).md
```

## License

MIT. See [LICENSE](LICENSE).
