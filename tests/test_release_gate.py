import json
import subprocess
from pathlib import Path

import pytest

from scripts.release_gate import ReleaseError, main, parse_release_tag, verify_release


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def commit_version(repo: Path, version: str) -> str:
    (repo / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    git(repo, "add", "pyproject.toml")
    git(repo, "commit", "--quiet", "--message", f"Version {version}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repo(tmp_path):
    git(tmp_path, "init", "--quiet", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Release test")
    git(tmp_path, "config", "user.email", "release@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    sha = commit_version(tmp_path, "1.2.3")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", sha)
    return tmp_path, sha


@pytest.mark.parametrize("tag", ["v0.0.0", "v1.2.3", "v10.20.30"])
def test_accepts_stable_tags(tag):
    assert parse_release_tag(tag) == tag[1:]


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v1.2",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-rc.1",
        "v1.2.3+build",
        "v1.2.3\n",
        "v1.2.3;touch owned",
        "v１.2.3",
    ],
)
def test_rejects_invalid_tags(tag):
    with pytest.raises(ReleaseError, match="Release tag must"):
        parse_release_tag(tag)


def test_prefix_is_literal_and_nonempty():
    assert parse_release_tag("deploy-v1.2.3", "deploy-v") == "1.2.3"
    assert parse_release_tag("release.1.2.3", "release.") == "1.2.3"
    with pytest.raises(ReleaseError):
        parse_release_tag("releaseX1.2.3", "release.")
    with pytest.raises(ReleaseError, match="must not be empty"):
        parse_release_tag("1.2.3", "")


@pytest.mark.parametrize("annotated", [False, True])
def test_release_tag_resolves_to_commit(release_repo, annotated):
    repo, sha = release_repo
    args = ["--annotate", "--message", "Release 1.2.3"] if annotated else []
    git(repo, "-c", "tag.gpgsign=false", "tag", *args, "v1.2.3", sha)
    assert verify_release("v1.2.3", sha, repo=repo) == {"tag": "v1.2.3", "version": "1.2.3", "sha": sha}


def test_accepts_older_commit_on_main(release_repo):
    repo, sha = release_repo
    git(repo, "tag", "v1.2.3", sha)
    newer = commit_version(repo, "1.2.4.dev0")
    git(repo, "update-ref", "refs/remotes/origin/main", newer)
    assert verify_release("v1.2.3", sha, repo=repo)["version"] == "1.2.3"


def test_rejects_commit_outside_main(release_repo):
    repo, _ = release_repo
    git(repo, "checkout", "--quiet", "-b", "unreviewed")
    sha = commit_version(repo, "2.0.0")
    git(repo, "tag", "v2.0.0", sha)
    with pytest.raises(ReleaseError, match="not an ancestor"):
        verify_release("v2.0.0", sha, repo=repo)


def test_rejects_version_mismatch_even_if_working_manifest_matches(release_repo):
    repo, sha = release_repo
    git(repo, "tag", "v2.0.0", sha)
    (repo / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n', encoding="utf-8")
    with pytest.raises(ReleaseError, match="does not match committed"):
        verify_release("v2.0.0", sha, repo=repo)


def test_rejects_tag_sha_mismatch(release_repo):
    repo, sha = release_repo
    git(repo, "tag", "v1.2.3", sha)
    other = commit_version(repo, "1.2.4")
    with pytest.raises(ReleaseError, match="requested commit SHA"):
        verify_release("v1.2.3", other, repo=repo)


def test_requires_fetched_main(release_repo):
    repo, sha = release_repo
    git(repo, "tag", "v1.2.3", sha)
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(ReleaseError, match="origin/main is unavailable"):
        verify_release("v1.2.3", sha, repo=repo)


def test_rejects_missing_tag(release_repo):
    repo, sha = release_repo
    with pytest.raises(ReleaseError, match="missing or does not point"):
        verify_release("v1.2.3", sha, repo=repo)


@pytest.mark.parametrize("sha", ["HEAD", "abc123", "-bad-ref", "0" * 39, "g" * 40])
def test_requires_full_commit_id(sha):
    with pytest.raises(ReleaseError, match="full hexadecimal"):
        verify_release("v1.2.3", sha)


def test_cli_emits_machine_readable_release(release_repo, monkeypatch, capsys):
    repo, sha = release_repo
    git(repo, "tag", "deploy-v1.2.3", sha)
    monkeypatch.chdir(repo)
    assert main(["--tag", "deploy-v1.2.3", "--sha", sha, "--tag-prefix", "deploy-v"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"tag": "deploy-v1.2.3", "version": "1.2.3", "sha": sha}
    assert not output.err


def test_cli_rejects_bad_release_without_success_output(capsys):
    assert main(["--tag", "v1.2.3-rc.1", "--sha", "0" * 40]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "Release rejected:" in output.err
