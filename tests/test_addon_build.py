"""The add-on's build configuration.

Supervisor passes BUILD_FROM into the Dockerfile from build.yaml. Without
that file the ARG is empty and the build fails immediately with "base name
(${BUILD_FROM}) should not be blank" — which Home Assistant surfaces to the
user as "An unknown error occurred while trying to build the image", with
the real reason only in the Supervisor log.
"""
from __future__ import annotations

import pathlib

import pytest

yaml = pytest.importorskip('yaml')

ADDON = pathlib.Path(__file__).resolve().parents[1] / 'addon'


def _load(name: str) -> dict:
    return yaml.safe_load((ADDON / name).read_text())


def test_build_config_exists():
    assert (ADDON / 'build.yaml').exists(), \
        'without build.yaml the add-on cannot be built by Supervisor at all'


def test_every_declared_architecture_can_be_built():
    """A missing entry fails only on the machines it covers.

    Which means it passes every test on the developer's own hardware and
    breaks for somebody with a Raspberry Pi.
    """
    declared = set(_load('config.yaml')['arch'])
    buildable = set(_load('build.yaml')['build_from'])
    missing = declared - buildable
    assert not missing, f'config.yaml offers {sorted(missing)} with no base image'


def test_base_image_provides_what_the_scripts_use():
    """run.sh uses bashio and the Dockerfile uses apk, so the base must be
    a Home Assistant Alpine image rather than a bare python one."""
    bases = _load('build.yaml')['build_from'].values()
    for base in bases:
        assert 'home-assistant' in base, f'{base} will not provide bashio'
        assert 'base' in base

    run_sh = (ADDON / 'run.sh').read_text()
    if 'bashio' in run_sh:
        assert all('-base' in b for b in bases)
