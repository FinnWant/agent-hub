"""The container settings from the security checklist, verified without Docker."""

from __future__ import annotations

import dataclasses

from app import config
from app.db import Sandbox
from app.docker_backend import WORKDIR, container_spec, exec_argv, exec_env
from app.templates import TEMPLATES
from tests.conftest import GIB


def spec(**settings_overrides):
    if settings_overrides:
        config.settings = dataclasses.replace(config.settings, **settings_overrides)
    sb = Sandbox(id="abc123def456", name="t", template="python", volume="ah-abc123def456",
                 memory=2 * GIB, cpus=1.5)
    return container_spec(sb, TEMPLATES["python"])


def test_security_options(settings):
    s = spec()
    assert s["user"] == "agent"
    assert s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]
    assert s["privileged"] is False
    assert s["restart_policy"] == {"Name": "no"}
    assert "cap_add" not in s and "devices" not in s and "pid_mode" not in s


def test_only_own_named_volume_no_host_paths_no_socket(settings):
    s = spec()
    assert s["volumes"] == {"ah-abc123def456": {"bind": WORKDIR, "mode": "rw"}}
    assert not any(str(k).startswith("/") for k in s["volumes"])
    assert "docker.sock" not in repr(s)
    assert "mounts" not in s and "binds" not in s


def test_resource_limits(settings):
    s = spec()
    assert s["mem_limit"] == s["memswap_limit"] == 2 * GIB
    assert s["nano_cpus"] == 1_500_000_000
    assert s["pids_limit"] == 512
    assert s["oom_score_adj"] > 0


def test_network_and_labels(settings):
    s = spec()
    assert s["network"] == "kiln-sb-abc123def456"  # its own network: sandboxes can't reach each other
    assert s["labels"] == {"agent-hub.managed": "true", "agent-hub.sandbox-id": "abc123def456"}
    assert "ports" not in s  # nothing published in phase 1


def test_io_limits_and_runtime_are_config(settings):
    s = spec()
    assert "device_read_bps" not in s and "runtime" not in s
    s = spec(io_device="/dev/dm-0", io_read_bps=100, io_write_bps=50, container_runtime="runsc")
    assert s["device_read_bps"] == [{"Path": "/dev/dm-0", "Rate": 100}]
    assert s["device_write_bps"] == [{"Path": "/dev/dm-0", "Rate": 50}]
    assert s["runtime"] == "runsc"


def test_exec_is_wrapped_in_timeout():
    assert exec_argv("echo hi", 30) == ["/usr/bin/timeout", "-k", "5", "30", "bash", "-lc", "echo hi"]
    assert exec_argv(["python3", "-V"], 5) == ["/usr/bin/timeout", "-k", "5", "5", "python3", "-V"]


def test_kilns_own_commands_ignore_the_agents_path():
    # The agent controls /home/agent/.local/bin (first in the image's PATH), so Kiln's own
    # commands (lists) only look in system directories; your typed commands (strings) don't change.
    assert exec_env(["git", "diff"], {"A": "1"}) == {"PATH": "/usr/local/bin:/usr/bin:/bin", "A": "1"}
    assert exec_env("ls", None) is None
