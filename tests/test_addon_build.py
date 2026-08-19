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


def test_the_package_is_pinned_not_tracking_a_branch():
    """Docker caches the install layer on the text of the RUN line.

    With "@main" the line never changes, so a rebuild reuses the cached
    layer and reinstalls the *old* package: the add-on version goes up, the
    code inside does not, and a fixed bug comes back looking unfixed. That
    happened between 0.1.1 and 0.1.2 — the image was rebuilt and still had
    none of the fix in it.
    """
    dockerfile = (ADDON / 'Dockerfile').read_text()
    ref = [l for l in dockerfile.splitlines() if l.startswith('ARG BRIDGE_REF')]
    assert ref, 'BRIDGE_REF is not declared'
    value = ref[0].split('=', 1)[1].strip() if '=' in ref[0] else ''
    assert value not in ('main', 'master', ''), (
        f'BRIDGE_REF={value!r} tracks a branch, so rebuilds will reuse a '
        'cached layer and ship stale code'
    )


def test_a_version_bump_alone_invalidates_the_layer():
    """Belt and braces if BRIDGE_REF is ever forgotten on a release."""
    dockerfile = (ADDON / 'Dockerfile').read_text()
    install = [l for l in dockerfile.splitlines() if 'pip3 install' in l]
    assert install, 'no install step found'
    block = dockerfile.split('RUN')[-2] if 'RUN' in dockerfile else dockerfile
    assert 'BUILD_VERSION' in dockerfile
