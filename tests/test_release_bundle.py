import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import BundleError, build_release, main

SHA = "abcdef0123456789" * 2 + "abcdef01"


def make_wheel(path: Path, *, name="remont-lead-parser", version="1.2.3") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "remont_lead_parser-1.2.3.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n",
        )
        archive.writestr("lead_parser/__init__.py", "")
    return path


@pytest.fixture
def release_inputs(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "remont-lead-parser"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text("example==1.0 --hash=sha256:012345\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DO_NOT_PACKAGE=secret", encoding="utf-8")
    wheel = make_wheel(tmp_path / "remont_lead_parser-1.2.3-py3-none-any.whl")
    return tmp_path, wheel, tmp_path / "release.tar.gz"


def test_bundle_contains_only_manifest_wheel_and_hashed_requirements(release_inputs):
    repo, wheel, output = release_inputs
    manifest = build_release(wheel, SHA, output, repo=repo)
    with tarfile.open(output, "r:gz") as archive:
        assert set(archive.getnames()) == {"manifest.json", "requirements.txt", wheel.name}
        assert json.load(archive.extractfile("manifest.json")) == manifest
        assert manifest["schema_version"] == 1
        assert manifest["sha"] == SHA
        assert manifest["version"] == "1.2.3"
        for filename, digest in manifest["files"].items():
            data = archive.extractfile(filename).read()
            assert data == (repo / filename).read_bytes()
            assert hashlib.sha256(data).hexdigest() == digest
        for member in archive.getmembers():
            assert member.isfile()
            assert member.mtime == 0
            assert member.uid == member.gid == 0
            assert member.mode == 0o644


def test_output_is_reproducible_despite_name_and_input_mtime(release_inputs):
    repo, wheel, first = release_inputs
    build_release(wheel, SHA, first, repo=repo)
    os.utime(wheel, (1234567890, 1234567890))
    os.utime(repo / "requirements.txt", (1234567890, 1234567890))
    second = repo / "different-name.tar.gz"
    build_release(wheel, SHA, second, repo=repo)
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("sha", ["HEAD", "0123", "a" * 39, "a" * 41, "a" * 64, "g" * 40])
def test_rejects_invalid_sha_before_creating_output(release_inputs, sha):
    repo, wheel, output = release_inputs
    with pytest.raises(BundleError, match="40-character hexadecimal"):
        build_release(wheel, sha, output, repo=repo)
    assert not output.exists()


@pytest.mark.parametrize(
    ("metadata", "message"),
    [({"version": "2.0.0"}, "version does not match"), ({"name": "another-project"}, "name does not match")],
)
def test_rejects_wheel_metadata_mismatch(release_inputs, metadata, message):
    repo, wheel, output = release_inputs
    make_wheel(wheel, **metadata)
    with pytest.raises(BundleError, match=message):
        build_release(wheel, SHA, output, repo=repo)
    assert not output.exists()


def test_accepts_normalized_distribution_name(release_inputs):
    repo, wheel, output = release_inputs
    make_wheel(wheel, name="Remont_Lead.Parser")
    assert build_release(wheel, SHA.upper(), output, repo=repo)["sha"] == SHA


@pytest.mark.parametrize("input_name", ["requirements.txt", "wheel", "pyproject.toml"])
def test_rejects_symlink_inputs(release_inputs, input_name):
    repo, wheel, output = release_inputs
    path = wheel if input_name == "wheel" else repo / input_name
    target = path.with_suffix(path.suffix + ".real")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(BundleError, match="not a symlink"):
        build_release(wheel, SHA, output, repo=repo)
    assert not output.exists()


@pytest.mark.parametrize("symlink", [False, True])
def test_never_overwrites_existing_output(release_inputs, symlink):
    repo, wheel, output = release_inputs
    existing = repo / "existing.txt" if symlink else output
    existing.write_bytes(b"keep existing bytes")
    if symlink:
        output.symlink_to(existing)
    with pytest.raises(BundleError, match="choose a new path"):
        build_release(wheel, SHA, output, repo=repo)
    assert existing.read_bytes() == b"keep existing bytes"
    if symlink:
        assert output.is_symlink()


def test_rejects_wheel_without_distribution_metadata(release_inputs):
    repo, wheel, output = release_inputs
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("README.md", "not a distribution")
    with pytest.raises(BundleError, match="exactly one distribution"):
        build_release(wheel, SHA, output, repo=repo)


def test_cli_builds_bundle(release_inputs, monkeypatch, capsys):
    repo, wheel, output = release_inputs
    monkeypatch.chdir(repo)
    assert main(["--wheel", str(wheel), "--sha", SHA, "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"output": str(output), "sha": SHA, "version": "1.2.3"}
    assert output.is_file()
