import copy
from datetime import date
import unittest

from repo_radar import digest_discovery, release_lines


class DigestTests(unittest.TestCase):
    def test_discovery_keeps_context_and_same_day_growth(self):
        repository = {
            "id": 12, "full_name": "example/tool", "stargazers_count": 54,
            "forks_count": 7, "description": "Useful capabilities " * 25,
            "language": "Python", "license": {"spdx_id": "MIT"},
            "created_at": "2026-11-01T00:00:00Z", "pushed_at": "2026-11-17T00:00:00Z",
        }

        class Search:
            def get(self, path, **params):
                return {"items": [repository]}

        state = {"stars": {"12": {"count": 40, "date": "2026-11-16"}}}
        def discover():
            return "\n".join(digest_discovery(
                Search(), ["python"], set(), state, date(2026, 11, 17), 5, [],
            ))

        report = discover()
        self.assertIn(repository["description"].strip(), report)
        self.assertIn("Python · MIT", report)
        self.assertIn("+14 stars since 2026-11-16", report)
        self.assertIn("last push 2026-11-17", report)
        self.assertIn("Contribution opportunities", report)
        self.assertEqual(report, discover())

        repository["license"] = None
        repository["language"] = None
        state.clear()
        report = discover()
        self.assertIn("Unspecified · Unspecified", report)
        self.assertIn("collecting a baseline", report)

    def test_release_notes_keep_multiline_features_and_section_context(self):
        release = {
            "tag_name": "v1.0", "published_at": "2026-11-17T00:00:00Z",
            "body": (
                "## Features\n"
                "- [Export support](https://example.com/change) for large files\n"
                "  including streamed output and configurable limits.\n"
                "## Fixes\n"
                "- Preserve `settings` on restart.\n"
                "```sh\nrm example\n```\n"
            ),
        }
        original = copy.deepcopy(release)
        report = "\n".join(release_lines(release, "https://github.com/example/tool"))
        self.assertIn("**Features** — Export support for large files including streamed output", report)
        self.assertIn("**Fixes** — Preserve settings on restart.", report)
        self.assertNotIn("rm example", report)
        self.assertNotIn("https://example.com/change", report)
        self.assertEqual(original, release)

    def test_empty_release_notes_are_explicit(self):
        release = {"tag_name": "v1", "published_at": "2026-11-17", "body": None}
        report = "\n".join(release_lines(release, "https://github.com/example/tool"))
        self.assertIn("Release notes were not provided.", report)


if __name__ == "__main__":
    unittest.main()
