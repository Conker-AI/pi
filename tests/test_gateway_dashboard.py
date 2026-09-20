from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.api import COOKIE, Config, create_app
from gateway.dashboard import API_CSP, UI_CSP, DashboardAssets

ORIGIN = "https://localhost:8050"
RUNTIME = "test-runtime-credential-" + "r" * 32
INDEX = b'<!doctype html><div id="root"></div><script src="/assets/app.js"></script>'


@pytest.fixture
def dist(tmp_path):
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_bytes(INDEX)
    (directory / "assets" / "app.js").write_bytes(b"document.title = 'Conker';")
    (directory / "assets" / "app.css").write_text("body { color: black; }", encoding="utf-8")
    (directory / "assets" / "font.woff2").write_bytes(b"font-stub")
    (directory / "portrait.png").write_bytes(b"image-stub")
    (directory / "fixture.txt").write_bytes(b"Published fixture receipt")
    return directory


def app_for(tmp_path, directory=""):
    settings = Config(ORIGIN, str(tmp_path / "auth.db"), "http://pi:8050", RUNTIME,
                      dashboard_dir=str(directory))
    return create_app(settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"status": "ok"})))


def test_optional_dashboard_does_not_change_api_only_installation(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        assert client.get("/", headers={"Accept": "text/html"}).status_code == 404
        result = client.get("/auth/session")
        assert result.status_code == 200 and result.json()["setup_required"]
        assert result.headers["content-security-policy"] == API_CSP
        assert COOKIE in result.headers["set-cookie"]


def test_public_shell_and_packaged_assets_have_local_ui_policy(tmp_path, dist):
    with TestClient(app_for(tmp_path, dist), base_url=ORIGIN) as client:
        for path in ("/", "/chat/ses_one", "/projects/project_one/", "/index.html"):
            result = client.get(path, headers={"Accept": "text/html"})
            assert result.status_code == 200 and result.content == INDEX
            assert result.headers["content-type"].startswith("text/html")
            assert result.headers["content-security-policy"] == UI_CSP
            assert result.headers["cache-control"] == "no-store"
            assert result.headers["x-content-type-options"] == "nosniff"
            assert "set-cookie" not in result.headers and RUNTIME not in result.text
        for path, media in (("/assets/app.js", "text/javascript"),
                            ("/assets/app.css", "text/css"),
                            ("/assets/font.woff2", "font/woff2"),
                            ("/portrait.png", "image/png"), ("/fixture.txt", "text/plain")):
            result = client.get(path)
            assert result.status_code == 200
            assert result.headers["content-type"].startswith(media)
        head = client.head("/", headers={"Accept": "text/html"})
        assert head.status_code == 200 and not head.content
        assert int(head.headers["content-length"]) == len(INDEX)
        assert "script-src 'self';" in UI_CSP
        assert "script-src 'self' 'unsafe-inline'" not in UI_CSP
        assert "connect-src 'self';" in UI_CSP
        assert "frame-src 'none';" in UI_CSP
        assert "https:" not in UI_CSP and "unsafe-eval" not in UI_CSP


@pytest.mark.parametrize("path", [
    "/auth", "/auth/unknown", "/api", "/api/unknown", "/health/unknown",
    "/assets/missing.js", "/assets/missing", "/missing.css", "/missing.png",
    "/.env", "/auth.db", "/assets/app.js.map", "/config.json", "/other.html",
    "/assets/%2e%2e/fixture.txt", "/%2e%2e/secret.txt", "/%252e%252e/secret.txt",
    "/assets%5c..%5csecret.txt", "/C:%5csecret.txt", "/portrait.png:stream",
    "/auth//unknown", "/%00secret.txt",
])
def test_reserved_missing_and_unsafe_paths_never_become_html(tmp_path, dist, path):
    (dist / ".env").write_text("fake secret", encoding="utf-8")
    (dist / "auth.db").write_text("fake database", encoding="utf-8")
    (dist / "config.json").write_text('{"secret":"not public"}', encoding="utf-8")
    (dist / "other.html").write_text("<script>not the shell</script>", encoding="utf-8")
    with TestClient(app_for(tmp_path, dist), base_url=ORIGIN) as client:
        result = client.get(path, headers={"Accept": "text/html"})
        assert result.status_code == 404
        assert result.headers["content-type"].startswith("application/json")
        assert result.headers["content-security-policy"] == API_CSP
        assert result.content != INDEX


def test_spa_requires_document_accept_and_get_or_head(tmp_path, dist):
    with TestClient(app_for(tmp_path, dist), base_url=ORIGIN) as client:
        for accept in ("*/*", "application/json", "text/html;q=0", "text/html;q=bad"):
            assert client.get("/chat/session", headers={"Accept": accept}).status_code == 404
        result = client.post("/chat/session", headers={"Accept": "text/html"})
        assert result.status_code == 405 and result.headers["content-security-policy"] == API_CSP


def test_static_shell_does_not_bypass_https_session_or_csrf(tmp_path, dist):
    with TestClient(app_for(tmp_path, dist), base_url=ORIGIN) as client:
        assert client.get("/api/pi/sessions", headers={"Accept": "text/html"}).status_code == 401
        start = client.get("/auth/session")
        assert start.headers["content-security-policy"] == API_CSP
        result = client.post("/auth/login", json={"password": "not configured"},
                             headers={"Accept": "text/html"})
        assert result.status_code == 403
        assert result.headers["content-security-policy"] == API_CSP
        forged = client.get("/", headers={"Host": "evil.invalid", "Accept": "text/html"})
        assert forged.status_code == 400
        insecure = client.get("http://localhost:8050/", headers={"Accept": "text/html"})
        assert insecure.status_code == 400
        health = client.get("/health", headers={"Accept": "text/html"})
        assert health.status_code == 200 and health.headers["content-security-policy"] == API_CSP


def test_directory_is_explicit_and_index_is_required(tmp_path, dist, monkeypatch):
    monkeypatch.setenv("GATEWAY_DASHBOARD_DIR", str(dist))
    assert Config.environment().dashboard_dir == str(dist)
    for directory in ("relative/dist", str(tmp_path / "absent"), str(dist / "index.html")):
        with pytest.raises(ValueError, match="dist directory"):
            DashboardAssets(directory)
    (dist / "index.html").unlink()
    with pytest.raises(ValueError, match=r"index\.html"):
        DashboardAssets(str(dist))


def symlink(target: Path, link: Path):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlinks unavailable on this host: {exc}")


@pytest.mark.parametrize("location", ["root", "index", "asset", "directory"])
def test_symlinks_cannot_publish_host_files(tmp_path, dist, location):
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.txt").write_text("private host data", encoding="utf-8")
    directory = dist
    if location == "root":
        directory = tmp_path / "dist-alias"
        symlink(dist, directory)
    elif location == "index":
        (dist / "index.html").unlink()
        symlink(outside / "secret.txt", dist / "index.html")
    elif location == "asset":
        symlink(outside / "secret.txt", dist / "assets" / "secret.txt")
    else:
        symlink(outside, dist / "extra")
    with pytest.raises(ValueError, match=r"dist directory|index\.html|symlink"):
        DashboardAssets(str(directory))


def test_publication_is_a_bounded_startup_snapshot(tmp_path, dist, monkeypatch):
    with TestClient(app_for(tmp_path, dist), base_url=ORIGIN) as client:
        (dist / "assets" / "app.js").write_bytes(b"replaced after startup")
        (dist / "new.txt").write_bytes(b"not in the publication")
        assert client.get("/assets/app.js").content == b"document.title = 'Conker';"
        assert client.get("/new.txt").status_code == 404
    monkeypatch.setattr("gateway.dashboard.MAX_FILE_BYTES", 10)
    with pytest.raises(ValueError, match="limit"):
        DashboardAssets(str(dist))
