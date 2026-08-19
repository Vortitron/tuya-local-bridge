"""Serving correctly behind Home Assistant ingress.

Ingress puts the add-on under a generated prefix such as
``/api/hassio_ingress/<token>/`` and passes it in ``X-Ingress-Path``. An app
that ignores it emits absolute URLs like ``/login``, which escape the prefix
and land on Home Assistant's own root — a 404 that gives no hint the add-on
is at fault. That is exactly what happened on first install.
"""
from __future__ import annotations

import pytest

flask = pytest.importorskip('flask')

from tuya_local_bridge.web import create_app  # noqa: E402

PREFIX = '/api/hassio_ingress/abc123'


@pytest.fixture
def client(tmp_path):
    app = create_app(state_dir=str(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client()


def test_links_are_prefixed_when_behind_ingress(client):
    """Every link and form must stay inside the ingress path."""
    response = client.get('/', headers={'X-Ingress-Path': PREFIX})
    body = response.get_data(as_text=True)

    for attr in ('href="/', 'action="/'):
        for fragment in body.split(attr)[1:]:
            target = attr + fragment.split('"')[0]
            assert PREFIX in target, (
                f'{target!r} escapes the ingress prefix and will 404 on the '
                'Home Assistant root'
            )


def test_direct_access_is_unaffected(client):
    """Without the header the app must behave exactly as before."""
    response = client.get('/')
    body = response.get_data(as_text=True)
    assert PREFIX not in body
    assert response.status_code in (200, 302)


def test_the_prefix_is_stripped_from_the_path(client):
    """Ingress may pass the full path through; routing must still match."""
    response = client.get(PREFIX + '/', headers={'X-Ingress-Path': PREFIX})
    assert response.status_code in (200, 302), \
        'the prefixed path did not route to the index'
