"""Build a reproducible deployment bundle from a wheel and locked requirements."""

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


class BundleError(ValueError):
    """The release inputs cannot be packaged safely."""


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise BundleError(f"Release input must be a regular file, not a symlink: {path.name}")
    try:
        return path.read_bytes()
    except OSError as error:
        raise BundleError(f"Cannot read release input: {path.name}") from error


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _verify_wheel(wheel_data: bytes, name: str, version: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_data)) as archive:
            metadata_entries = [
                entry
                for entry in archive.infolist()
                if re.fullmatch(r"[^/]+\.dist-info/METADATA", entry.filename)
            ]
            if len(metadata_entries) != 1:
                raise BundleError("Wheel must contain exactly one distribution METADATA file")
            metadata = BytesParser().parsebytes(archive.read(metadata_entries[0]))
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        raise BundleError("Cannot read wheel metadata") from error
    if len(metadata.get_all("Name", [])) != 1 or len(metadata.get_all("Version", [])) != 1:
        raise BundleError("Wheel metadata must declare exactly one Name and Version")
    if _canonical_name(metadata["Name"]) != _canonical_name(name):
        raise BundleError("Wheel distribution name does not match project.name")
    if metadata["Version"] != version:
        raise BundleError("Wheel version does not match project.version")


def build_release(wheel: Path, sha: str, output: Path, *, repo: Path | None = None) -> dict:
    """Package only manifest.json, requirements.txt, and the verified wheel."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise BundleError("Release SHA must be a full 40-character hexadecimal commit ID")
    sha = sha.lower()
    repo = Path.cwd() if repo is None else repo
    try:
        project = tomllib.loads(_read_file(repo / "pyproject.toml").decode("utf-8"))["project"]
        name, version = project["name"], project["version"]
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, KeyError, TypeError) as error:
        raise BundleError("pyproject.toml must declare project.name and project.version") from error
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise BundleError("project.name and project.version must be nonempty strings")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.whl", wheel.name):
        raise BundleError("Wheel filename must be a plain .whl basename")

    wheel_data = _read_file(wheel)
    _verify_wheel(wheel_data, name, version)
    files = {"requirements.txt": _read_file(repo / "requirements.txt"), wheel.name: wheel_data}
    manifest = {
        "schema_version": 1,
        "sha": sha,
        "version": version,
        "files": {filename: hashlib.sha256(data).hexdigest() for filename, data in sorted(files.items())},
    }
    files["manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    created = False
    try:
        # Exclusive creation also rejects an existing symlink or an input path as output.
        with output.open("xb") as raw:
            created = True
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for filename, data in sorted(files.items()):
                        info = tarfile.TarInfo(filename)
                        info.size = len(data)
                        info.mode = 0o644
                        info.mtime = 0
                        archive.addfile(info, io.BytesIO(data))
    except (OSError, ValueError, tarfile.TarError) as error:
        if created:
            output.unlink(missing_ok=True)
        raise BundleError(
            "Cannot create release bundle; choose a new path in an existing directory"
        ) from error
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = build_release(args.wheel, args.sha, args.output)
    except BundleError as error:
        print(f"Release bundle rejected: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "sha": manifest["sha"], "version": manifest["version"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
