"""Reject release tags that do not identify a versioned commit on origin/main."""

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


class ReleaseError(ValueError):
    """The requested release does not satisfy the deployment policy."""


def parse_release_tag(tag: str, prefix: str = "v") -> str:
    if not prefix:
        raise ReleaseError("The release tag prefix must not be empty")
    number = r"(?:0|[1-9][0-9]*)"
    match = re.fullmatch(rf"{re.escape(prefix)}({number}\.{number}\.{number})", tag)
    if not match:
        raise ReleaseError(
            f"Release tag must have the form {prefix}X.Y.Z, without leading zeros or prerelease suffixes"
        )
    return match.group(1)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    except OSError as error:
        raise ReleaseError("Unable to run git in the release checkout") from error


def verify_release(tag: str, sha: str, *, prefix: str = "v", repo: Path | None = None) -> dict[str, str]:
    """Validate the committed manifest and a fetched origin/main; never fetch implicitly."""
    version = parse_release_tag(tag, prefix)
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha):
        raise ReleaseError("The release SHA must be a full hexadecimal commit ID")
    sha = sha.lower()
    repo = Path.cwd() if repo is None else repo

    tagged = _git(repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
    if tagged.returncode:
        raise ReleaseError(f"Release tag {tag!r} is missing or does not point to a commit")
    if tagged.stdout.strip().lower() != sha:
        raise ReleaseError("The release tag does not point to the requested commit SHA")

    main = _git(repo, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
    if main.returncode:
        raise ReleaseError("origin/main is unavailable; fetch its full history before validating the release")
    ancestor = _git(repo, "merge-base", "--is-ancestor", sha, main.stdout.strip())
    if ancestor.returncode == 1:
        raise ReleaseError("The release commit is not an ancestor of origin/main")
    if ancestor.returncode:
        raise ReleaseError("Unable to verify release ancestry; fetch the full origin/main history")

    manifest = _git(repo, "show", f"{sha}:pyproject.toml")
    if manifest.returncode:
        raise ReleaseError("The release commit has no readable pyproject.toml")
    try:
        project_version = tomllib.loads(manifest.stdout)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise ReleaseError("The release pyproject.toml must contain a valid project.version") from error
    if project_version != version:
        raise ReleaseError(
            f"Tag version {version!r} does not match committed project.version {project_version!r}"
        )
    return {"tag": tag, "version": version, "sha": sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--tag-prefix", default="v")
    args = parser.parse_args(argv)
    try:
        release = verify_release(args.tag, args.sha, prefix=args.tag_prefix)
    except ReleaseError as error:
        print(f"Release rejected: {error}", file=sys.stderr)
        return 1
    print(json.dumps(release, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
