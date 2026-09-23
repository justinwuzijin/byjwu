"""The local ingest page posts a folder path. It does not accept video bytes."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from conductor.ui import make_server, page_html

FIXTURE = Path("fixtures/selects").resolve()
DURATIONS = FIXTURE / "durations.json"


@pytest.fixture
def server():
    httpd = make_server(0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _url(httpd, path: str) -> str:
    port = httpd.server_address[1]
    return f"http://127.0.0.1:{port}{path}"


def test_page_has_no_file_input():
    html = page_html()
    assert 'type="file"' not in html
    assert "FileReader" not in html
    assert "multipart" not in html
    assert "/ingest" in html


def test_post_runs_ingest_on_the_path(server, tmp_path):
    out = tmp_path / "ui-out"
    body = urlencode(
        {
            "media": str(FIXTURE),
            "brief": "A tight interview.",
            "durations": str(DURATIONS),
            "out_dir": str(out),
        }
    ).encode()
    response = urlopen(Request(_url(server, "/ingest"), data=body, method="POST"))
    page = response.read().decode()
    starter = out / "selects.fcpxml"
    shadow = out / "selects.conductor.fcpxml"
    assert response.status == 200
    assert str(starter.resolve()) in page
    assert str(shadow.resolve()) in page
    assert starter.is_file() and shadow.is_file()
    assert "Download" in page
    assert not (out / "selects.conductor.applied.fcpxml").exists()
    token = page.split("/download/", 1)[1].split('"', 1)[0]
    downloaded = urlopen(_url(server, f"/download/{token}")).read()
    assert downloaded.startswith(b"<?xml")


def test_download_of_an_unknown_token_is_404(server):
    with pytest.raises(HTTPError) as exc:
        urlopen(_url(server, "/download/nope"))
    assert exc.value.code == 404


def test_multipart_and_oversized_bodies_are_refused(server):
    huge = b"a=" + b"x" * 70000
    request = Request(
        _url(server, "/ingest"),
        data=huge,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with pytest.raises(HTTPError) as exc:
        urlopen(request)
    assert exc.value.code == 413
    upload = Request(
        _url(server, "/ingest"),
        data=b"not-a-video",
        method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=x"},
    )
    with pytest.raises(HTTPError) as upload_error:
        urlopen(upload)
    assert upload_error.value.code == 400
    assert b"does not accept file uploads" in upload_error.value.read()


def test_pick_returns_json(server):
    response = urlopen(_url(server, "/pick"))
    payload = json.loads(response.read().decode())
    assert "path" in payload or "error" in payload
