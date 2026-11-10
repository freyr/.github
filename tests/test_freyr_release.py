import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import freyr_release as fr


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


class Repo:
    """A temporary git repository; the tests chdir into it."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.old = os.getcwd()
        os.chdir(self.path)
        sh("git", "init", "-q", "-b", "main", cwd=self.path)
        sh("git", "config", "user.email", "t@example.com", cwd=self.path)
        sh("git", "config", "user.name", "t", cwd=self.path)
        sh("git", "config", "tag.gpgSign", "false", cwd=self.path)
        sh("git", "config", "commit.gpgSign", "false", cwd=self.path)

    def write(self, rel, text):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, message):
        sh("git", "add", "-A", cwd=self.path)
        sh("git", "commit", "-q", "--allow-empty", "-m", message, cwd=self.path)
        return self.head()

    def head(self):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, cwd=self.path, capture_output=True, text=True
        ).stdout.strip()

    def tag(self, name):
        sh("git", "tag", "-a", name, "-m", name, cwd=self.path)

    def close(self):
        os.chdir(self.old)
        self.tmp.cleanup()


PLUGIN_CONFIG = {
    "packages": [
        {
            "name": name,
            "path": f"plugins/{name}",
            "tag": name + "--v{version}",
            "legacy_tags": [name + "-v{version}"],
            "version_files": [{"file": f"plugins/{name}/.claude-plugin/plugin.json", "format": "json"}],
            "ignore": [f"plugins/{name}/tests/**", f"plugins/{name}/README.md"],
            "min_bump": "patch",
        }
        for name in ("sdlc", "ha")
    ]
}


class TitleTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(fr.title_level("feat: add x"), "minor")
        self.assertEqual(fr.title_level("fix(sdlc): y"), "patch")
        self.assertEqual(fr.title_level("perf: z"), "patch")
        self.assertEqual(fr.title_level("feat!: drop x"), "major")
        self.assertEqual(fr.title_level("fix: y", "text\n\nBREAKING CHANGE: gone"), "major")
        self.assertEqual(fr.title_level("docs: readme"), "none")
        self.assertEqual(fr.title_level("Add a thing"), "none")

    def test_validate(self):
        self.assertIsNone(fr.validate_title("feat(sdlc): add a stage"))
        self.assertIsNone(fr.validate_title("fix: handle API errors"))
        self.assertIsNotNone(fr.validate_title("Add a stage"))
        self.assertIsNotNone(fr.validate_title("feature: add"))
        self.assertIsNotNone(fr.validate_title("feat: Add a stage"))
        self.assertIsNone(fr.validate_title("feat: API keys in env"))

    def test_bump(self):
        self.assertEqual(fr.bump("1.2.3", "major"), "2.0.0")
        self.assertEqual(fr.bump("1.2.3", "minor"), "1.3.0")
        self.assertEqual(fr.bump("1.2.3", "patch"), "1.2.4")
        self.assertEqual(fr.bump("0.7.1", "major"), "0.8.0")


class VersionFileTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def test_pyproject_with_array_before_version(self):
        self.repo.write(
            "pyproject.toml",
            '[project]\nname = "x"\nkeywords = ["a"]\nversion = "1.0.0"\n\n[tool.x]\nversion = "9.9.9"\n',
        )
        vf = fr.VersionFile("pyproject.toml", "pyproject")
        self.assertEqual(vf.read(), "1.0.0")
        vf.write("1.1.0")
        text = Path("pyproject.toml").read_text()
        self.assertIn('version = "1.1.0"', text)
        self.assertIn('version = "9.9.9"', text)

    def test_cargo_and_lock(self):
        self.repo.write(
            "Cargo.toml",
            '[package]\nname = "pc-agent"\nversion = "0.3.1"\n\n[dependencies]\nserde = { version = "1" }\n',
        )
        self.repo.write(
            "Cargo.lock",
            '[[package]]\nname = "aaa"\nversion = "0.3.1"\n\n[[package]]\nname = "pc-agent"\nversion = "0.3.1"\n',
        )
        touched = fr.VersionFile("Cargo.toml", "cargo").write("0.4.0")
        self.assertEqual(sorted(touched), ["Cargo.lock", "Cargo.toml"])
        lock = Path("Cargo.lock").read_text()
        self.assertIn('name = "aaa"\nversion = "0.3.1"', lock)
        self.assertIn('name = "pc-agent"\nversion = "0.4.0"', lock)

    def test_json_nested_key_keeps_indent(self):
        self.repo.write("server.json", '{\n    "version": "2.1.1",\n    "packages": [{"version": "2.1.1"}]\n}\n')
        fr.VersionFile("server.json", "json", "packages.0.version").write("2.2.0")
        data = json.loads(Path("server.json").read_text())
        self.assertEqual(data["packages"][0]["version"], "2.2.0")
        self.assertEqual(data["version"], "2.1.1")
        self.assertIn('\n    "version"', Path("server.json").read_text())


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.write(".github/release.json", json.dumps(PLUGIN_CONFIG))
        for name, version in (("sdlc", "0.7.1"), ("ha", "0.3.1")):
            self.repo.write(
                f"plugins/{name}/.claude-plugin/plugin.json",
                json.dumps({"name": name, "version": version}, indent=2) + "\n",
            )
            self.repo.write(f"plugins/{name}/skills/a/SKILL.md", "a\n")
        self.base = self.repo.commit("chore: init")
        self.repo.tag("sdlc-v0.7.1")  # legacy tag form
        self.repo.tag("ha--v0.3.1")
        self.packages = fr.load_config(".github/release.json")

    def tearDown(self):
        self.repo.close()

    def plan(self, title, body=""):
        return {p["name"]: p for p in fr.plan(self.packages, self.base, "HEAD", title, body)}

    def test_feat_bumps_only_changed_plugin(self):
        self.repo.write("plugins/sdlc/skills/a/SKILL.md", "b\n")
        self.repo.commit("wip")
        p = self.plan("feat(sdlc): new stage")
        self.assertEqual((p["sdlc"]["level"], p["sdlc"]["to"], p["sdlc"]["tag"]), ("minor", "0.8.0", "sdlc--v0.8.0"))
        self.assertEqual((p["ha"]["level"], p["ha"]["to"]), ("none", "0.3.1"))

    def test_docs_on_shipped_file_gives_min_patch(self):
        self.repo.write("plugins/ha/skills/a/SKILL.md", "b\n")
        self.repo.commit("wip")
        self.assertEqual(self.plan("docs: reword the skill")["ha"]["to"], "0.3.2")

    def test_ignored_files_give_no_release(self):
        self.repo.write("plugins/ha/tests/test_x.py", "x\n")
        self.repo.write("plugins/ha/README.md", "x\n")
        self.repo.commit("wip")
        self.assertEqual(self.plan("feat: tests")["ha"]["level"], "none")

    def test_apply_then_check_is_idempotent(self):
        self.repo.write("plugins/sdlc/skills/a/SKILL.md", "b\n")
        self.repo.commit("wip")
        planned = fr.plan(self.packages, self.base, "HEAD", "fix: x", "")
        self.assertTrue(fr.check(self.packages, planned))  # version not set yet
        self.assertEqual(fr.apply(self.packages, planned), ["plugins/sdlc/.claude-plugin/plugin.json"])
        self.repo.commit("chore: set version sdlc 0.7.2")
        planned = fr.plan(self.packages, self.base, "HEAD", "fix: x", "")
        self.assertEqual(planned[0]["to"], "0.7.2")  # the version file change is not a package change
        self.assertEqual(fr.check(self.packages, planned), [])
        self.assertEqual(fr.apply(self.packages, planned), [])

    def test_title_change_lowers_version_again(self):
        self.repo.write("plugins/sdlc/skills/a/SKILL.md", "b\n")
        self.repo.commit("wip")
        fr.apply(self.packages, fr.plan(self.packages, self.base, "HEAD", "feat!: x", ""))
        self.repo.commit("chore: set version")
        planned = fr.plan(self.packages, self.base, "HEAD", "fix: x", "")
        fr.apply(self.packages, planned)
        self.assertEqual(json.loads(Path("plugins/sdlc/.claude-plugin/plugin.json").read_text())["version"], "0.7.2")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def test_version_file_higher_than_tag_releases(self):
        self.repo.write(".github/release.json", json.dumps(PLUGIN_CONFIG))
        for name in ("sdlc", "ha"):
            self.repo.write(f"plugins/{name}/.claude-plugin/plugin.json", json.dumps({"version": "0.1.0"}))
        self.repo.commit("chore: init")
        self.repo.tag("sdlc--v0.1.0")
        self.repo.tag("ha--v0.1.0")
        before = self.repo.head()
        self.repo.write("plugins/sdlc/.claude-plugin/plugin.json", json.dumps({"version": "0.2.0"}))
        self.repo.write("plugins/sdlc/x.md", "x")
        after = self.repo.commit("feat(sdlc): add x (#7)")
        out = fr.do_release(fr.load_config(".github/release.json"), before, after, dry_run=True, single_title=False)
        self.assertEqual([(x["tag"], x["previous_tag"]) for x in out], [("sdlc--v0.2.0", "sdlc--v0.1.0")])
        self.assertIn("feat(sdlc): add x (#7)", out[0]["notes"])
        self.assertEqual(out[0]["title"], "sdlc v0.2.0")

    def test_tag_only_package(self):
        cfg = {"packages": [{"name": "site", "path": ".", "tag": "v{version}", "initial": "1.0.0"}]}
        self.repo.write(".github/release.json", json.dumps(cfg))
        first = self.repo.commit("chore: init")
        packages = fr.load_config(".github/release.json")
        self.assertEqual(fr.release_candidates(packages, fr.ZERO_SHA, first), [])
        self.repo.write("content/a.md", "a")
        second = self.repo.commit("feat: a page (#1)")
        self.assertEqual(fr.release_candidates(packages, first, second)[0]["tag"], "v1.0.0")
        self.repo.tag("v1.0.0")
        self.assertEqual(fr.release_candidates(packages, first, second), [])  # a repeated run
        self.repo.write("content/a.md", "b")
        third = self.repo.commit("fix: typo (#2)")
        self.assertEqual(fr.release_candidates(packages, second, third)[0]["tag"], "v1.0.1")
        self.repo.write("README.md", "b")
        fourth = self.repo.commit("docs: readme (#3)")
        self.assertEqual(fr.release_candidates(packages, third, fourth), [])

    def test_disagreeing_version_files_fail(self):
        cfg = {
            "packages": [
                {
                    "name": "server",
                    "path": ".",
                    "tag": "server-v{version}",
                    "version_files": [
                        {"file": "pyproject.toml", "format": "pyproject"},
                        {"file": "server.json", "format": "json"},
                    ],
                }
            ]
        }
        self.repo.write(".github/release.json", json.dumps(cfg))
        self.repo.write("pyproject.toml", '[project]\nversion = "1.0.0"\n')
        self.repo.write("server.json", '{"version": "1.0.1"}')
        head = self.repo.commit("chore: init")
        with self.assertRaises(fr.ReleaseError):
            fr.release_candidates(fr.load_config(".github/release.json"), fr.ZERO_SHA, head)


class ConfigTests(unittest.TestCase):
    def test_unknown_key_fails(self):
        with self.assertRaises(fr.ReleaseError):
            fr.Package.from_dict({"name": "x", "tag": "v{version}", "tagg": 1})

    def test_tag_needs_placeholder(self):
        with self.assertRaises(fr.ReleaseError):
            fr.Package.from_dict({"name": "x", "tag": "v1"})


if __name__ == "__main__":
    unittest.main()
