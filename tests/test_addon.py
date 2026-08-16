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
    config = (RUN_SH.parent / "config.yaml").read_text()
    ingress_port = re.search(r"ingress_port:\s*(\d+)", config)
    assert ingress_port, "config.yaml declares no ingress_port"

    args = build_parser().parse_args(addon_argv())
    assert args.port == int(ingress_port.group(1))
