"""Transfer a release over pinned-host SSH and run its installer on one Linux host."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class DeploymentError(RuntimeError):
    pass


def deploy(
    archive: Path,
    sha: str,
    version: str,
    *,
    env: dict[str, str] | None = None,
    runner=subprocess.run,
) -> dict:
    env = os.environ if env is None else env
    required = ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS")
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise DeploymentError("Configure GitHub deployment settings first: " + ", ".join(missing))
    host, user = env["DEPLOY_HOST"], env["DEPLOY_USER"]
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host) or host.startswith("-"):
        raise DeploymentError("DEPLOY_HOST must be a hostname or IP address")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", user):
        raise DeploymentError("DEPLOY_USER must be a Linux account name")
    port = env.get("DEPLOY_PORT") or "22"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise DeploymentError("DEPLOY_PORT must be between 1 and 65535")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise DeploymentError("A full commit SHA and stable release version are required")
    if archive.is_symlink() or not archive.is_file():
        raise DeploymentError("Release archive must be a regular file")
    installer = Path(__file__).resolve().parents[1] / "deploy" / "install_release.py"
    root = env.get("DEPLOY_ROOT") or "/opt/parser"
    config = env.get("DEPLOY_CONFIG") or "/var/lib/parser/config.yaml"
    python = env.get("DEPLOY_PYTHON") or "/usr/bin/python3.12"
    if any(not value.startswith("/") or "\n" in value for value in (root, config, python)):
        raise DeploymentError("Deployment paths must be absolute paths without newlines")

    def execute(args: list[str], *, capture: bool = False):
        # Child tools need no copy of the private key or unrelated runner credentials in env.
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("DEPLOY_")}
        try:
            return runner(args, check=True, text=True, capture_output=capture, env=child_env)
        except (OSError, subprocess.CalledProcessError) as error:
            raise DeploymentError("SSH deployment command failed; see the command log") from error

    with tempfile.TemporaryDirectory(prefix="parser-ssh-") as temporary:
        private = Path(temporary)
        key, hosts = private / "key", private / "known_hosts"
        key.write_text(env["DEPLOY_SSH_KEY"].rstrip() + "\n")
        hosts.write_text(env["DEPLOY_KNOWN_HOSTS"].rstrip() + "\n")
        key.chmod(0o600)
        hosts.chmod(0o600)
        options = [
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={hosts}",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=6",
        ]
        ssh = ["ssh", *options, "-p", port, "--", f"{user}@{host}"]
        remote = execute([*ssh, "mktemp -d /tmp/parser-deploy.XXXXXXXXXX"], capture=True).stdout.strip()
        if not re.fullmatch(r"/tmp/parser-deploy\.[A-Za-z0-9]+", remote):
            raise DeploymentError("SSH did not return a valid temporary deployment directory")
        try:
            scp_host = f"[{host}]" if ":" in host else host
            execute(
                [
                    "scp",
                    *options,
                    "-P",
                    port,
                    "--",
                    str(archive.resolve()),
                    str(installer),
                    f"{user}@{scp_host}:{remote}/",
                ]
            )
            command = [
                "sudo",
                "-n",
                python,
                f"{remote}/install_release.py",
                "--archive",
                f"{remote}/{archive.name}",
                "--sha",
                sha,
                "--version",
                version,
                "--root",
                root,
                "--config",
                config,
                "--python",
                python,
            ]
            execute([*ssh, shlex.join(command)])
        finally:
            # Only remove the exact temporary path returned above; never a runtime/release directory.
            try:
                execute([*ssh, shlex.join(["rm", "-rf", "--", remote])])
            except DeploymentError:
                print("Remote temporary upload could not be removed.", file=sys.stderr)
    return {"sha": sha, "version": version, "deployed": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        receipt = deploy(args.archive, args.sha, args.version)
    except DeploymentError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
