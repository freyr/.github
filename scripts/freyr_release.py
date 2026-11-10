#!/usr/bin/env python3
"""Version and release tool of the freyr organization standard.

The PR title (Conventional Commits) decides the version change. The version
files change inside the feature PR (``plan`` + ``apply``). On ``main`` the
``release`` command only creates tags and GitHub releases; it never commits.

Configuration: ``.github/release.json`` in the calling repository::

    {
      "packages": [
        {
          "name": "sdlc",
          "path": "plugins/sdlc",
          "tag": "sdlc--v{version}",
          "legacy_tags": ["sdlc-v{version}"],
          "version_files": [
            {"file": "plugins/sdlc/.claude-plugin/plugin.json", "format": "json", "key": "version"}
          ],
          "ignore": ["plugins/sdlc/tests/**", "plugins/sdlc/README.md"],
          "min_bump": "patch",
          "initial": "0.1.0"
        }
      ]
    }

Formats of a version file: ``json`` (``key`` is a dot path, list indexes are
numbers), ``pyproject`` (``[project].version``), ``cargo`` (``[package].version``
of ``Cargo.toml`` and the entry of the crate in ``Cargo.lock`` next to it).

Only the standard library is used, so the tool runs on a bare runner.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = {"none": 0, "patch": 1, "minor": 2, "major": 3}
LEVEL_NAMES = {v: k for k, v in LEVELS.items()}
TYPE_LEVEL = {"feat": "minor", "fix": "patch", "perf": "patch", "revert": "patch"}
ALLOWED_TYPES = ("feat", "fix", "perf", "revert", "docs", "test", "ci", "build", "refactor", "chore", "style")
TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()\s]+)\))?(?P<bang>!)?: (?P<subject>\S.*)$")
BREAKING_RE = re.compile(r"^BREAKING[ -]CHANGE: ", re.MULTILINE)
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ZERO_SHA = "0" * 40


class ReleaseError(Exception):
    pass


# ---------------------------------------------------------------- semver


def parse_version(text: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(text.strip())
    if not m:
        raise ReleaseError(f"not a semantic version X.Y.Z: {text!r}")
    return int(m[1]), int(m[2]), int(m[3])


def format_version(v: tuple[int, int, int]) -> str:
    return f"{v[0]}.{v[1]}.{v[2]}"


def bump(version: str, level: str) -> str:
    major, minor, patch = parse_version(version)
    if level == "major" and major == 0:
        level = "minor"  # semver: before 1.0.0 a breaking change is a minor change
    if level == "major":
        return format_version((major + 1, 0, 0))
    if level == "minor":
        return format_version((major, minor + 1, 0))
    if level == "patch":
        return format_version((major, minor, patch + 1))
    return version


# ---------------------------------------------------------------- titles


def title_level(title: str, body: str = "") -> str:
    """Return the version change of a Conventional Commits title and body."""
    m = TITLE_RE.match(title.strip())
    if not m:
        return "none"
    if m["bang"] or BREAKING_RE.search(body or ""):
        return "major"
    return TYPE_LEVEL.get(m["type"], "none")


def validate_title(title: str) -> str | None:
    """Return an error text, or None when the title is valid."""
    m = TITLE_RE.match(title.strip())
    if not m:
        return (
            "The PR title must follow Conventional Commits: '<type>(<scope>)!: <subject>'. "
            f"Types: {', '.join(ALLOWED_TYPES)}."
        )
    if m["type"] not in ALLOWED_TYPES:
        return f"Unknown type '{m['type']}'. Types: {', '.join(ALLOWED_TYPES)}."
    if m["subject"][0].isupper() and not m["subject"].split()[0].isupper():
        return "Start the subject with a lowercase letter (an acronym is allowed)."
    return None


# ---------------------------------------------------------------- git


def git(*args: str, cwd: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], check=False, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git_show(rev: str, path: str) -> str | None:
    proc = subprocess.run(["git", "show", f"{rev}:{path}"], check=False, capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def changed_files(base: str, head: str) -> list[str]:
    out = git("diff", "--name-only", f"{base}...{head}")
    return [line for line in out.splitlines() if line]


def commit_files(sha: str) -> list[str]:
    out = git("diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
    return [line for line in out.splitlines() if line]


def commit_message(sha: str) -> tuple[str, str]:
    msg = git("log", "-1", "--format=%B", sha)
    subject, _, body = msg.partition("\n")
    return subject.strip(), body.strip()


# ---------------------------------------------------------------- version files


def _get_path(data, dotted: str):
    for part in dotted.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def _set_path(data, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        data = data[int(part)] if isinstance(data, list) else data[part]
    last = parts[-1]
    if isinstance(data, list):
        data[int(last)] = value
    else:
        data[last] = value


def _section_version_re(section: str) -> re.Pattern[str]:
    # The `version = "..."` line inside one TOML table, before the next table.
    return re.compile(
        r"(^\[" + re.escape(section) + r"\][ \t]*\n(?:(?!^\[).)*?^version\s*=\s*\")([^\"]+)(\")",
        re.MULTILINE | re.DOTALL,
    )


def _json_indent(text: str) -> int:
    m = re.search(r"\n( +)\"", text)
    return len(m[1]) if m else 2


@dataclass
class VersionFile:
    file: str
    format: str
    key: str = "version"

    def read_text(self, text: str) -> str:
        if self.format == "json":
            return str(_get_path(json.loads(text), self.key))
        if self.format in ("pyproject", "cargo"):
            section = "project" if self.format == "pyproject" else "package"
            m = _section_version_re(section).search(text)
            if not m:
                raise ReleaseError(f"{self.file}: no version in [{section}]")
            return m[2]
        raise ReleaseError(f"{self.file}: unknown format {self.format!r}")

    def read(self, rev: str | None = None) -> str | None:
        text = git_show(rev, self.file) if rev else _read_file(self.file)
        return None if text is None else self.read_text(text)

    def write(self, version: str) -> list[str]:
        """Write the version. Return the paths that changed."""
        text = _read_file(self.file)
        if text is None:
            raise ReleaseError(f"{self.file}: the file does not exist")
        if self.format == "json":
            data = json.loads(text)
            _set_path(data, self.key, version)
            new = json.dumps(data, indent=_json_indent(text), ensure_ascii=False) + "\n"
        else:
            section = "project" if self.format == "pyproject" else "package"
            new, n = _section_version_re(section).subn(lambda m: m[1] + version + m[3], text, count=1)
            if n != 1:
                raise ReleaseError(f"{self.file}: no version in [{section}]")
        touched = []
        if new != text:
            Path(self.file).write_text(new)
            touched.append(self.file)
        if self.format == "cargo":
            touched += _write_cargo_lock(Path(self.file), new, version)
        return touched


def _write_cargo_lock(cargo_toml: Path, toml_text: str, version: str) -> list[str]:
    lock = cargo_toml.with_name("Cargo.lock")
    if not lock.exists():
        return []
    m = re.search(r"^\[package\][ \t]*\n(?:(?!^\[).)*?^name\s*=\s*\"([^\"]+)\"", toml_text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    text = lock.read_text()
    pattern = re.compile(r"(\[\[package\]\]\nname = \"" + re.escape(m[1]) + r"\"\nversion = \")([^\"]+)(\")")
    new = pattern.sub(lambda x: x[1] + version + x[3], text, count=1)
    if new == text:
        return []
    lock.write_text(new)
    return [str(lock)]


def _read_file(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------- packages


@dataclass
class Package:
    name: str
    path: str
    tag: str
    legacy_tags: list[str] = field(default_factory=list)
    version_files: list[VersionFile] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    min_bump: str = "none"
    initial: str = "0.1.0"

    @classmethod
    def from_dict(cls, d: dict) -> Package:
        unknown = set(d) - {"name", "path", "tag", "legacy_tags", "version_files", "ignore", "min_bump", "initial"}
        if unknown:
            raise ReleaseError(f"package {d.get('name')}: unknown keys {sorted(unknown)}")
        pkg = cls(
            name=d["name"],
            path=d.get("path", ".").rstrip("/") or ".",
            tag=d.get("tag", "v{version}"),
            legacy_tags=list(d.get("legacy_tags", [])),
            version_files=[VersionFile(**vf) for vf in d.get("version_files", [])],
            ignore=list(d.get("ignore", [])),
            min_bump=d.get("min_bump", "none"),
            initial=d.get("initial", "0.1.0"),
        )
        if "{version}" not in pkg.tag:
            raise ReleaseError(f"package {pkg.name}: tag must contain {{version}}")
        if pkg.min_bump not in LEVELS:
            raise ReleaseError(f"package {pkg.name}: min_bump must be one of {list(LEVELS)}")
        parse_version(pkg.initial)
        return pkg

    def owns(self, path: str) -> bool:
        if self.path != "." and not (path == self.path or path.startswith(self.path + "/")):
            return False
        if path in {vf.file for vf in self.version_files}:
            return False
        if any(fnmatch.fnmatch(path, pattern) for pattern in self.ignore):
            return False
        return path != "Cargo.lock" or not any(vf.format == "cargo" for vf in self.version_files)

    def tag_for(self, version: str) -> str:
        return self.tag.format(version=version)

    def _tag_res(self) -> list[re.Pattern[str]]:
        out = []
        for pattern in [self.tag, *self.legacy_tags]:
            prefix, _, suffix = pattern.partition("{version}")
            out.append(re.compile("^" + re.escape(prefix) + r"(\d+\.\d+\.\d+)" + re.escape(suffix) + "$"))
        return out

    def tagged_versions(self, merged: str | None = None) -> dict[str, str]:
        """Map each released version of this package to its tag."""
        args = ["tag", "--list"] + (["--merged", merged] if merged else [])
        found: dict[str, str] = {}
        for tag in git(*args).split():
            for rx in self._tag_res():
                m = rx.match(tag)
                if m:
                    found.setdefault(m[1], tag)
        return found

    def tags_at(self, rev: str) -> list[str]:
        """The tags of this package that point at ``rev``."""
        return [t for t in git("tag", "--points-at", rev).split() if any(rx.match(t) for rx in self._tag_res())]

    def last_tag(self, merged: str | None = None) -> tuple[str, str] | None:
        tagged = self.tagged_versions(merged)
        if not tagged:
            return None
        best = max(tagged, key=parse_version)
        return best, tagged[best]

    def file_version(self, rev: str | None = None) -> str | None:
        """The version in the version files; all files must agree."""
        values = {vf.file: vf.read(rev) for vf in self.version_files}
        present = {k: v for k, v in values.items() if v is not None}
        if not present:
            return None
        if len(set(present.values())) > 1:
            raise ReleaseError(f"package {self.name}: the version files disagree: {present}")
        return next(iter(present.values()))


def load_config(path: str) -> list[Package]:
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError as e:
        raise ReleaseError(f"no configuration file {path}") from e
    packages = [Package.from_dict(d) for d in data.get("packages", [])]
    if not packages:
        raise ReleaseError(f"{path}: no packages")
    names = [p.name for p in packages]
    if len(names) != len(set(names)):
        raise ReleaseError(f"{path}: duplicate package names")
    return packages


# ---------------------------------------------------------------- plan


def package_level(pkg: Package, files: list[str], level: str) -> str:
    if not any(pkg.owns(f) for f in files):
        return "none"
    return LEVEL_NAMES[max(LEVELS[level], LEVELS[pkg.min_bump])]


def plan(packages: list[Package], base: str, head: str, title: str, body: str) -> list[dict]:
    files = changed_files(base, head)
    level = title_level(title, body)
    result = []
    for pkg in packages:
        pkg_level = package_level(pkg, files, level)
        candidates = []
        base_file = pkg.file_version(base) if pkg.version_files else None
        if base_file:
            candidates.append(base_file)
        last = pkg.last_tag(merged=base)
        if last:
            candidates.append(last[0])
        current = max(candidates, key=parse_version) if candidates else None
        if pkg_level == "none":
            target = current if current else (pkg.initial if pkg.version_files else None)
        elif current is None:
            target = pkg.initial
        else:
            target = bump(current, pkg_level)
        result.append(
            {
                "name": pkg.name,
                "level": pkg_level,
                "from": current,
                "to": target,
                "tag": pkg.tag_for(target) if (target and pkg_level != "none") else None,
                "release": pkg_level != "none",
            }
        )
    return result


def apply(packages: list[Package], planned: list[dict]) -> list[str]:
    by_name = {p.name: p for p in packages}
    touched: list[str] = []
    for item in planned:
        pkg = by_name[item["name"]]
        if item["to"] is None:
            continue
        for vf in pkg.version_files:
            touched += vf.write(item["to"])
    return touched


def check(packages: list[Package], planned: list[dict]) -> list[str]:
    by_name = {p.name: p for p in packages}
    errors = []
    for item in planned:
        pkg = by_name[item["name"]]
        for vf in pkg.version_files:
            actual = vf.read()
            if item["to"] is not None and actual != item["to"]:
                errors.append(f"{vf.file}: version {actual}, expected {item['to']} ({item['level']} change)")
    return errors


# ---------------------------------------------------------------- release


def release_notes(pkg: Package, previous_tag: str | None, head: str) -> str:
    rng = f"{previous_tag}..{head}" if previous_tag else head
    args = ["log", "--format=%H%x1f%s%x1f%b%x1e", rng]
    if pkg.path != ".":
        args += ["--", pkg.path]
    groups: dict[str, list[str]] = {"Breaking changes": [], "Features": [], "Fixes": [], "Other changes": []}
    for raw in git(*args).split("\x1e"):
        record = raw.strip()
        if not record:
            continue
        sha, subject, body = [*record.split("\x1f"), "", ""][:3]
        if re.match(r"^chore: set version ", subject):
            continue
        level = title_level(subject, body)
        line = f"- {subject} ({sha[:7]})"
        if level == "major":
            groups["Breaking changes"].append(line)
        elif subject.startswith("feat"):
            groups["Features"].append(line)
        elif level == "patch":
            groups["Fixes"].append(line)
        else:
            groups["Other changes"].append(line)
    parts = [f"## {name}\n\n" + "\n".join(lines) for name, lines in groups.items() if lines]
    return "\n\n".join(parts) + "\n" if parts else "No changes.\n"


def release_candidates(packages: list[Package], before: str, after: str) -> list[dict]:
    """Decide which packages release at ``after`` (the new head of main)."""
    if not before or before == ZERO_SHA or git("cat-file", "-t", before, check=False).strip() != "commit":
        commits = [after]
    else:
        commits = [c for c in git("rev-list", "--reverse", f"{before}..{after}").split() if c]
    result = []
    for pkg in packages:
        last = pkg.last_tag(merged=after)
        if pkg.version_files:
            version = pkg.file_version(after)
            if version is None:
                continue
            if last is None or parse_version(version) > parse_version(last[0]):
                result.append(
                    {
                        "name": pkg.name,
                        "version": version,
                        "tag": pkg.tag_for(version),
                        "previous_tag": last[1] if last else None,
                    }
                )
            continue
        if pkg.tags_at(after):
            continue  # a repeated run on a released commit
        level = "none"
        for sha in commits:
            subject, body = commit_message(sha)
            lv = package_level(pkg, commit_files(sha), title_level(subject, body))
            level = LEVEL_NAMES[max(LEVELS[level], LEVELS[lv])]
        if level == "none":
            continue
        version = bump(last[0], level) if last else pkg.initial
        result.append(
            {
                "name": pkg.name,
                "version": version,
                "tag": pkg.tag_for(version),
                "previous_tag": last[1] if last else None,
            }
        )
    return result


def do_release(packages: list[Package], before: str, after: str, dry_run: bool, single_title: bool) -> list[dict]:
    by_name = {p.name: p for p in packages}
    out = []
    for candidate in release_candidates(packages, before, after):
        pkg = by_name[candidate["name"]]
        notes = release_notes(pkg, candidate["previous_tag"], after)
        title = f"v{candidate['version']}" if single_title else f"{pkg.name} v{candidate['version']}"
        item = {**candidate, "title": title, "notes": notes}
        out.append(item)
        if dry_run:
            continue
        if git("tag", "--list", item["tag"]).strip():
            raise ReleaseError(f"the tag {item['tag']} already exists")
        git("tag", "-a", item["tag"], "-m", item["tag"], after)
        git("push", "origin", f"refs/tags/{item['tag']}")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(notes)
            notes_file = fh.name
        proc = subprocess.run(
            ["gh", "release", "create", item["tag"], "--title", title, "--notes-file", notes_file, "--verify-tag"],
            check=False,
            capture_output=True,
            text=True,
        )
        os.unlink(notes_file)
        if proc.returncode != 0:
            raise ReleaseError(f"gh release create {item['tag']} failed: {proc.stderr.strip()}")
    return out


# ---------------------------------------------------------------- CLI


def _output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{name}<<__FREYR_EOF__\n{value}\n__FREYR_EOF__\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def _plan_table(planned: list[dict]) -> str:
    rows = ["| Package | Change | From | To | Tag |", "|---|---|---|---|---|"]
    for p in planned:
        rows.append(f"| {p['name']} | {p['level']} | {p['from'] or '-'} | {p['to'] or '-'} | {p['tag'] or '-'} |")
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=".github/release.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("title", help="validate a PR title")
    t.add_argument("--title", default=os.environ.get("PR_TITLE", ""))

    for name in ("plan", "apply", "check"):
        p = sub.add_parser(name)
        p.add_argument("--base", required=True)
        p.add_argument("--head", default="HEAD")
        p.add_argument("--title", default=os.environ.get("PR_TITLE", ""))
        p.add_argument("--body", default=os.environ.get("PR_BODY", ""))

    r = sub.add_parser("release")
    r.add_argument("--before", default=os.environ.get("BEFORE_SHA", ""))
    r.add_argument("--after", default="HEAD")
    r.add_argument("--dry-run", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "title":
            err = validate_title(args.title)
            if err:
                print(f"::error::{err}")
                return 1
            print(f"OK: {args.title} ({title_level(args.title)} change)")
            return 0

        packages = load_config(args.config)

        if args.cmd in ("plan", "apply", "check"):
            planned = plan(packages, args.base, args.head, args.title, args.body)
            _output("plan", json.dumps(planned))
            _summary("### Release plan\n\n" + _plan_table(planned))
            print(json.dumps(planned, indent=2))
            if args.cmd == "apply":
                touched = apply(packages, planned)
                _output("changed", "true" if touched else "false")
                _output("files", "\n".join(touched))
                print("changed: " + (", ".join(touched) if touched else "nothing"))
            if args.cmd == "check":
                errors = check(packages, planned)
                for e in errors:
                    print(f"::error::{e}")
                return 1 if errors else 0
            return 0

        if args.cmd == "release":
            single = len(packages) == 1
            released = do_release(packages, args.before, args.after, args.dry_run, single)
            public = [{k: v for k, v in x.items() if k != "notes"} for x in released]
            _output("releases", json.dumps(public))
            _output("released", "true" if released else "false")
            if released:
                _summary("### Releases\n\n" + "\n".join(f"- `{x['tag']}`" for x in released))
            else:
                _summary("No release.")
            print(json.dumps(released, indent=2))
            return 0
    except ReleaseError as e:
        print(f"::error::{e}")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
