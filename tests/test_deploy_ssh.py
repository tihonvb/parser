import json
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.deploy_ssh import DeploymentError, deploy

SHA = "0123456789abcdef" * 2 + "01234567"
REMOTE = "/tmp/parser-deploy.a1B2c3D4e5"


@pytest.fixture
def deployment(tmp_path):
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"release")
    settings = {
        "DEPLOY_HOST": "parser.example.invalid",
        "DEPLOY_USER": "parser-deploy",
        "DEPLOY_SSH_KEY": "private key fixture",
        "DEPLOY_KNOWN_HOSTS": "parser.example.invalid ssh-ed25519 host-key-fixture",
    }
    return archive, settings


class FakeRunner:
    def __init__(self, *, remote=REMOTE, fail_install=False):
        self.remote = remote
        self.fail_install = fail_install
        self.calls = []
        self.credential_paths = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        assert kwargs["check"] is True
        assert kwargs["text"] is True
        assert not any(key.startswith("DEPLOY_") for key in kwargs["env"])
        key = Path(args[args.index("-i") + 1])
        host_option = next(arg for arg in args if arg.startswith("UserKnownHostsFile="))
        hosts = Path(host_option.split("=", 1)[1])
        self.credential_paths.extend([key, hosts])
        for path in (key, hosts):
            assert path.is_file()
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
        assert key.read_text() == "private key fixture\n"
        assert hosts.read_text() == "parser.example.invalid ssh-ed25519 host-key-fixture\n"
        for required_option in ("StrictHostKeyChecking=yes", "BatchMode=yes", "IdentitiesOnly=yes"):
            assert required_option in args
        if args[-1].startswith("mktemp "):
            assert kwargs["capture_output"] is True
            return subprocess.CompletedProcess(args, 0, stdout=self.remote + "\n")
        if self.fail_install and args[-1].startswith("sudo "):
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, stdout="")


@pytest.mark.parametrize("missing", ["DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"])
def test_missing_configuration_fails_before_any_ssh(deployment, missing):
    archive, settings = deployment
    settings[missing] = " \n"
    runner = FakeRunner()
    with pytest.raises(DeploymentError, match=missing):
        deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert runner.calls == []


def test_deploy_pins_host_and_removes_private_credentials(deployment, monkeypatch):
    archive, settings = deployment
    monkeypatch.setenv("DEPLOY_SSH_KEY", "must not reach subprocess environment")
    runner = FakeRunner()
    result = deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert result == {"sha": SHA, "version": "1.2.3", "deployed": True}
    assert [args[0] for args, _ in runner.calls] == ["ssh", "scp", "ssh", "ssh"]
    assert shlex.split(runner.calls[-1][0][-1]) == ["rm", "-rf", "--", REMOTE]
    assert all(not path.exists() for path in runner.credential_paths)
    assert all(not path.parent.exists() for path in runner.credential_paths)


def test_shell_characters_and_spaces_remain_single_remote_arguments(deployment):
    archive, settings = deployment
    unusual_archive = archive.with_name("release file; $(touch nope).tar.gz")
    archive.rename(unusual_archive)
    settings.update(
        {
            "DEPLOY_ROOT": "/opt/parser space; touch /tmp/nope",
            "DEPLOY_CONFIG": "/etc/parser/$(touch nope)'config.yaml",
            "DEPLOY_PYTHON": "/opt/python tools/python3.12",
            "DEPLOY_PORT": "2202",
        }
    )
    runner = FakeRunner()
    deploy(unusual_archive, SHA, "1.2.3", env=settings, runner=runner)
    scp_args = runner.calls[1][0]
    assert scp_args[scp_args.index("-P") + 1] == "2202"
    assert str(unusual_archive.resolve()) in scp_args
    assert scp_args[-1] == f"parser-deploy@parser.example.invalid:{REMOTE}/"
    install_args = runner.calls[2][0]
    assert install_args[install_args.index("-p") + 1] == "2202"
    assert shlex.split(install_args[-1]) == [
        "sudo",
        "-n",
        settings["DEPLOY_PYTHON"],
        f"{REMOTE}/install_release.py",
        "--archive",
        f"{REMOTE}/{unusual_archive.name}",
        "--sha",
        SHA,
        "--version",
        "1.2.3",
        "--root",
        settings["DEPLOY_ROOT"],
        "--config",
        settings["DEPLOY_CONFIG"],
        "--python",
        settings["DEPLOY_PYTHON"],
    ]


def test_failed_installer_cleans_upload_and_credentials_without_success(deployment):
    archive, settings = deployment
    runner = FakeRunner(fail_install=True)
    with pytest.raises(DeploymentError, match="SSH deployment command failed"):
        deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert len(runner.calls) == 4
    assert shlex.split(runner.calls[-1][0][-1]) == ["rm", "-rf", "--", REMOTE]
    assert all(not path.exists() for path in runner.credential_paths)


@pytest.mark.parametrize(
    "remote",
    [
        "/",
        "/opt/parser",
        "/tmp/parser-deploy.good/../../etc",
        "/tmp/parser-deploy.good; rm -rf /",
        REMOTE + "\n/",
    ],
)
def test_malicious_remote_directory_is_never_used_or_removed(deployment, remote):
    archive, settings = deployment
    runner = FakeRunner(remote=remote)
    with pytest.raises(DeploymentError, match="valid temporary deployment directory"):
        deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert len(runner.calls) == 1
    assert all(not path.exists() for path in runner.credential_paths)


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("DEPLOY_HOST", "-oProxyCommand=touch /tmp/nope"),
        ("DEPLOY_HOST", "host; command"),
        ("DEPLOY_HOST", "host\nother"),
        ("DEPLOY_USER", "user@other-host"),
        ("DEPLOY_USER", "-root"),
        ("DEPLOY_USER", "root;command"),
        ("DEPLOY_PORT", "0"),
        ("DEPLOY_PORT", "65536"),
        ("DEPLOY_PORT", "22;command"),
        ("DEPLOY_ROOT", "relative/root"),
        ("DEPLOY_CONFIG", "/etc/config\nsecond-command"),
    ],
)
def test_invalid_ssh_configuration_is_rejected_before_connecting(deployment, setting, value):
    archive, settings = deployment
    settings[setting] = value
    runner = FakeRunner()
    with pytest.raises(DeploymentError):
        deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert runner.calls == []


def test_ipv6_host_is_bracketed_for_scp(deployment):
    archive, settings = deployment
    settings["DEPLOY_HOST"] = "2001:db8::1"
    runner = FakeRunner()
    deploy(archive, SHA, "1.2.3", env=settings, runner=runner)
    assert runner.calls[1][0][-1] == f"parser-deploy@[2001:db8::1]:{REMOTE}/"


def test_deployment_workflow_requires_tag_gate_and_checks_before_secrets():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"
    # YAML 1.1 treats "on" as a boolean; BaseLoader preserves GitHub Actions' literal keys.
    workflow = yaml.load(workflow_path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"push"}
    assert workflow["on"]["push"]["tags"]
    jobs = workflow["jobs"]
    assert jobs["checks"]["needs"] == "release"
    assert {"release", "checks"} <= set(jobs["package"]["needs"])
    assert {"release", "package"} <= set(jobs["deploy"]["needs"])
    assert jobs["deploy"]["environment"] == "production"
    for name, job in jobs.items():
        if name != "deploy":
            assert "secrets." not in json.dumps(job)
        for step in job.get("steps", []):
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["ref"] == "${{ github.sha }}"
                assert step["with"]["persist-credentials"] == "false"
