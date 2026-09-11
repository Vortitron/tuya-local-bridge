"""Guard the add-on's start-up command against the CLI it actually invokes.

`--dir` is a top-level argument, so `serve --dir /data` fails to parse. That is
invisible until the add-on starts and immediately exits, which is a slow way to
find out.
"""
import pathlib
import re
import shlex

import pytest

from tuya_local_bridge.cli import build_parser

RUN_SH = pathlib.Path(__file__).resolve().parent.parent / "addon" / "run.sh"

# Supervisor's default when config.yaml does not declare one.
SUPERVISOR_DEFAULT_INGRESS_PORT = 8099


def addon_argv():
    text = RUN_SH.read_text()
    match = re.search(r"exec tuya-local-bridge(.*?)(?:\n\n|\Z)", text, re.S)
    assert match, "could not find the exec line in run.sh"
    # Join the line continuations and drop the shell variable expansions.
    command = match.group(1).replace("\\\n", " ")
    command = re.sub(r'"\$\{[A-Z_]+\}"', "18", command)
    return shlex.split(command)


@pytest.mark.skipif(not RUN_SH.exists(), reason="add-on not present")
def test_the_addon_start_command_parses():
    args = build_parser().parse_args(addon_argv())

    assert args.command == "serve"
    assert args.dir == "/data"
    assert args.port == 8099
    assert args.host == "0.0.0.0"


@pytest.mark.skipif(not RUN_SH.exists(), reason="add-on not present")
def test_the_addon_port_matches_the_ingress_port():
    """Whatever the add-on serves on must be what ingress forwards to.

    config.yaml deliberately does not declare ingress_port: 8099 is the
    Supervisor default and restating it is an add-on lint error. So an absent
    key means 8099, not "unset".
    """
    config = (RUN_SH.parent / "config.yaml").read_text()
    declared = re.search(r"ingress_port:\s*(\d+)", config)
    ingress_port = int(declared.group(1)) if declared else SUPERVISOR_DEFAULT_INGRESS_PORT

    args = build_parser().parse_args(addon_argv())
    assert args.port == ingress_port
