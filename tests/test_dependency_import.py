from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from repo_radar import main, run


class DependencyPreviewTests(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 11), "TOML import requires Python 3.11+")
    def test_preview_preserves_profile_and_reports_unresolved_dependencies(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "pyproject.toml"
            manifest.write_text(
                '[project]\ndependencies = ["HTTPX[http2]>=0.27", "unknown", '
                '"direct @ git+https://github.com/example/direct.git@main"]\n'
                '[project.optional-dependencies]\nlint = ["ruff; python_version < \'3.15\'"]\n'
                'ignored = ["excluded"]\n',
                encoding="utf-8",
            )
            profile = Path(directory) / "profile.json"
            original = '{"topics":["python"],"watch":["Encode/httpx"],"watch_mode":"all"}'
            profile.write_text(original, encoding="utf-8")
            matches = {"httpx": "encode/httpx", "ruff": "astral-sh/ruff", "unknown": None}
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch("dependency_import.pypi_repository", side_effect=matches.__getitem__) as lookup,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                status = main(["import-deps", str(manifest), "--profile", str(profile), "--extra", "lint"])
            self.assertEqual(status, 0)
            self.assertEqual(profile.read_text(encoding="utf-8"), original)
            self.assertEqual(json.loads(output.getvalue()), {
                "topics": ["python"],
                "watch": ["Encode/httpx", "example/direct", "astral-sh/ruff"],
                "watch_mode": "all",
            })
            self.assertIn("Unresolved unknown", errors.getvalue())
            self.assertEqual([call.args[0] for call in lookup.call_args_list], ["httpx", "unknown", "ruff"])


class ReleasesOnlyTests(unittest.TestCase):
    def test_existing_release_history_is_reused_without_commit_requests(self):
        releases = [
            {"id": number, "draft": False, "tag_name": f"v{number}", "body": "Add export support",
             "published_at": f"2026-11-{number:02d}T00:00:00Z"}
            for number in (19, 17)
        ]

        class API:
            def get(self, path, **params):
                # Any request for commits or comparisons fails this test.
                return {
                    "/repos/example/tool": {"id": 1, "full_name": "example/tool"},
                    "/repos/example/tool/releases": releases,
                }[path]

        state = {"watch": {"example/tool": {
            "id": 1, "branch": "main", "head": "a" * 40, "releases": [17],
        }}}
        args = (API(), [], ["example/tool"], state, date(2026, 11, 19), 8)
        initial, warnings = run(*args, watch_mode="releases")
        self.assertEqual(warnings, [])
        self.assertIn("Watchlist (releases only)", initial)
        self.assertIn("Release [v19]", initial)
        self.assertNotIn("Release [v17]", initial)
        self.assertNotIn("Commit lists", initial)
        repeat, warnings = run(*args, watch_mode="releases")
        self.assertEqual(warnings, [])
        self.assertIn("No new releases.", repeat)
        self.assertNotIn("Release [v19]", repeat)


if __name__ == "__main__":
    unittest.main()
