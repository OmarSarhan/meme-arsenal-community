#!/usr/bin/env python3
"""Targeted CI smoke check; only a unique temporary Compose project is touched."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tarfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
HOST = "fixture.example.invalid"
SHARE_HOST = "share.fixture.example.invalid"

WORKER_READY_CHECK = r'''
from pathlib import Path
import signal, sys, time
pid = sys.argv[1] if len(sys.argv) > 1 else "1"
deadline = time.monotonic() + 60
while True:
    fields = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    if int(fields["SigCgt"].strip(), 16) & (1 << (signal.SIGTERM - 1)):
        break
    if time.monotonic() >= deadline:
        raise RuntimeError("Worker has not installed its TERM handler")
    time.sleep(.1)
print("Worker TERM handler ready", flush=True)
'''

# Run inside the existing app image against Caddy and the real app/frontend.
# Disposable credentials only; no operator configuration.
API_CHECK = r'''
from base64 import b64encode
from contextlib import closing
import hashlib
import hmac
from http.client import HTTPConnection, HTTPSConnection
from http.cookies import SimpleCookie
from io import BytesIO
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urljoin

from itsdangerous import URLSafeSerializer
from PIL import Image
from sqlalchemy import func, select
from meme_arsenal.config import load_settings
from meme_arsenal.db import session_scope
from meme_arsenal.models import AnonymousSession, Classification, MediaItem, RemixJobRecord

HOST = "fixture.example.invalid"
ORIGIN = "https://" + HOST
AUTH = {"Authorization": "Basic " + b64encode(b"fixture:community-fixture-password").decode()}
SPOOF = {"X-Forwarded-For": "203.0.113.9, 203.0.113.10", "X-Forwarded-Proto": "http",
         "X-Forwarded-Host": "foreign.invalid", "Forwarded": "for=203.0.113.9;proto=http",
         "X-Real-IP": "203.0.113.9", "X-Forwarded-Prefix": "/public",
         "X-Owner-ID": "owner", "Remote-User": "fixture"}
settings = load_settings()
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
with create_engine(settings.database_url).connect() as connection:
    assert connection.execute(text("select version_num from alembic_version")).scalar_one() == ScriptDirectory.from_config(Config("/app/alembic.ini")).get_current_head()
assert settings.ai_provider == "mock"
assert not settings.telegram_api_id and not settings.telegram_api_hash
assert not settings.public_trust_proxy_headers
assert os.environ["FORWARDED_ALLOW_IPS"] == "*"
# cafile loads only the fixture CA, with normal hostname/SNI verification.
context = ssl.create_default_context(cafile="/tmp/community-ca.crt")
assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
state_path = Path("/reserve/.community-smoke.json")
signer = URLSafeSerializer(settings.public_session_secret, salt="meme-arsenal-public-session")

def request(path, method="GET", body=None, headers=None, status=200):
    with closing(HTTPSConnection(HOST, 443, timeout=30, context=context)) as client:
        client.connect()
        source, peer = client.sock.getsockname()[0], client.sock.getpeername()[0]
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        content = response.read()
        assert response.status == status, (method, path, response.status, status)
        if status in (401, 403):
            assert not response.headers.get("set-cookie")
            assert b'id="root"' not in content and b'"items"' not in content
        if status == 401:
            assert response.headers["www-authenticate"].startswith("Basic ")
        return content, response.headers, source, peer

def cookie_from(headers):
    value = SimpleCookie(headers["set-cookie"])[settings.public_session_cookie_name]
    assert value["secure"] and value["httponly"] and value["samesite"].lower() == "lax"
    assert value["path"] == "/"
    return f"{value.key}={value.value}", signer.loads(value.value)["sid"]

def snapshot():
    with session_scope(settings) as db:
        counts = tuple(db.scalar(select(func.count()).select_from(model))
                       for model in (AnonymousSession, MediaItem, Classification, RemixJobRecord))
        publications = tuple(db.execute(select(MediaItem.id, MediaItem.visibility, MediaItem.public_status)
                                       .order_by(MediaItem.id)).all())
    files = {str(p.relative_to(settings.reserve_path)) for p in settings.reserve_path.rglob("*")
             if p.is_file()}
    return counts, publications, files

# App is healthy first; allow Caddy's internal fixture certificate to finish issuing.
for attempt in range(60):
    try:
        request("/api/health", status=401)
        break
    except OSError:
        if attempt == 59:
            raise
        time.sleep(1)

if sys.argv[1] == "gate":
    request("/api/health", status=401)
    request("/api/health", headers=AUTH)
    print("Missing-credential reload retained authenticated gate")
    sys.exit(0)

request("/api/health", headers=AUTH)
page = request("/", headers=AUTH)[0]
assert b'id="root"' in page
script = re.search(r'<script[^>]+src="([^"]+)"', page.decode())
assert script, "Built dashboard script is missing"
asset = urljoin("/", script.group(1))
assert "javascript" in request(asset, headers=AUTH)[1]["content-type"]
assert json.loads(request("/arsenal-config.json", headers=AUTH)[0])["mode"] == "online"

if sys.argv[1] == "seed":
    image = BytesIO()
    Image.new("RGB", (32, 32), "green").save(image, format="PNG")
    body = (b'--community-fixture\r\n'
            b'Content-Disposition: form-data; name="file"; filename="community-fixture.png"\r\n'
            b'Content-Type: image/png\r\n\r\n' + image.getvalue()
            + b'\r\n--community-fixture--\r\n')
    multipart = {"Content-Type": "multipart/form-data; boundary=community-fixture"}
    # Cached-Basic CSRF regression: no cookie, valid upload, no app side effects.
    before = snapshot()
    for origin in ("https://foreign.invalid", "null"):
        request("/api/remix/uploads", "POST", body,
                {**AUTH, **multipart, "Origin": origin}, status=403)
    request("/api/remix/uploads", "POST", body,
            {**multipart, "Origin": "https://foreign.invalid"}, status=401)
    assert snapshot() == before, "Denied upload changed session/media/classification/files"

    # Exact request-time Redis deltas prove proxy trust without mutating owner identity.
    import redis
    counters = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    _, _, source, peer = request("/api/health", headers=AUTH)
    spoof = "203.0.113.9"
    assert len({source, peer, spoof}) == 3
    def ip_hash(value):
        return hmac.new(settings.public_session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()
    keys = [f"rate:upload:ip:{ip_hash(ip)}" for ip in (source, peer, spoof)]
    keys.append(f"rate:upload:session:{settings.owner_session_id}")
    before_counts = [int(counters.get(key) or 0) for key in keys]
    before_ttls = [counters.ttl(key) for key in keys]
    assert all(ttl == -2 or ttl > 30 for ttl in before_ttls)
    started = time.monotonic()
    content, response_headers, upload_source, upload_peer = request("/api/remix/uploads", "POST", body,
        {**AUTH, **SPOOF, **multipart, "Origin": ORIGIN})
    assert (upload_source, upload_peer) == (source, peer)
    assert time.monotonic() - started < 30
    after_counts = [int(counters.get(key) or 0) for key in keys]
    assert [a - b for a, b in zip(after_counts, before_counts)] == [1, 0, 0, 1]
    item = json.loads(content)
    assert item["origin"] == "dashboard_upload" and item["visibility"] == "private"
    cookie, sid = cookie_from(response_headers)
    assert sid == settings.owner_session_id
    owner = {**AUTH, "Cookie": cookie}
    print("Real Caddy + Uvicorn client-IP proof passed")

    # Missing Origin remains supported for CLI uploads; keep a second real source.
    second = json.loads(request("/api/remix/uploads", "POST", body, {**owner, **multipart})[0])
    job_id = "community-ready-job"
    draft = settings.remix_drafts_dir / f"{job_id}.png"
    Image.new("RGB", (96, 96), "blue").save(draft, "PNG")
    with session_scope(settings) as db:
        db.add(RemixJobRecord(id=job_id, session_id=sid, left_id=item["id"], right_id=second["id"],
            status="ready", message="Ready", safety_mode="standard",
            result_json={"result": {"image_path": draft.name, "summary": "Fixture ready remix"}}))
    media = request(item["original_path"], headers=owner)[0]
    state = {"id": item["id"], "saved_id": second["id"], "path": item["original_path"], "cookie": cookie,
             "digest": hashlib.sha256(media).hexdigest(), "job": job_id}
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o600)

    routes = [("GET", path) for path in (
        "/", "/dashboard", "/arsenal-config.json", asset, "/api/health", "/api/items",
        f"/api/remix/jobs/{job_id}", f"/api/remix/jobs/{job_id}/image",
        f"/api/remix/jobs/{job_id}/card", item["original_path"],
        "/share/invalid?source=A&playback=true", "/openapi.json", "/docs",
        "/data/telegram.session", "/unknown-private-path")]
    routes += [("POST", "/api/remix/uploads"), ("POST", f"/api/remix/uploads/{item['id']}/save"),
               ("HEAD", item["original_path"]), ("OPTIONS", "/api/items"),
               ("PATCH", f"/api/items/{item['id']}/metadata"),
               ("DELETE", f"/api/remix/items/{item['id']}")]
    before = snapshot()
    for method, path in routes:
        request(path, method, status=401)
        print("Unauthenticated 401:", method, path)
    for headers in ({"Authorization": "Basic " + b64encode(b"fixture:wrong").decode()},
                    {"Cookie": cookie}, {**SPOOF, "Cookie": cookie}):
        request(item["original_path"], headers=headers, status=401)
        request("/api/remix/uploads", "POST", body, {**headers, **multipart}, status=401)
    request(f"/api/remix/uploads/{item['id']}/save", "POST", b"",
            {**owner, "Origin": "https://foreign.invalid"}, status=403)
    assert snapshot() == before, "Denied routes mutated fixture state"

    # Cookie loss retains the configured private namespace.
    for path in (item["original_path"], f"/api/remix/jobs/{job_id}/image"):
        request(path, headers=AUTH)
    for path in ("/dashboard", "/unknown-private-path", "/openapi.json", "/api/items"):
        request(path, headers=owner)
    for path in ("/share/invalid?source=A&playback=true", "/data/telegram.session"):
        request(path, headers=owner, status=404)
    request("/api/items", "OPTIONS", headers={**owner, "Origin": ORIGIN,
        "Access-Control-Request-Method": "GET"})
    assert not request(item["original_path"], "HEAD", headers=owner)[0]
    partial, range_headers, _, _ = request(item["original_path"],
        headers={**owner, "Range": "bytes=0-7"}, status=206)
    assert partial == media[:8] and range_headers["content-range"].startswith("bytes 0-7/")
    saved = json.loads(request(f"/api/remix/uploads/{second['id']}/save", "POST", b"",
                              {**owner, "Origin": ORIGIN})[0])
    assert saved["item"]["id"] == second["id"] and saved["item"]["public_status"] == "published"
    patched = json.loads(request(f"/api/items/{second['id']}/metadata", "PATCH",
        json.dumps({"summary": "Community fixture edited"}),
        {**owner, **SPOOF, "Origin": ORIGIN, "Content-Type": "application/json"})[0])
    assert patched["summary"] == "Community fixture edited"
    for method in ("GET", "POST"):
        with closing(HTTPConnection(HOST, 80, timeout=30)) as client:
            client.request(method, "/api/health")
            response = client.getresponse()
            assert response.status == 308 and response.headers["location"] == ORIGIN + "/api/health"
            assert not response.headers.get("www-authenticate") and not response.headers.get("set-cookie")
            assert b'"status"' not in response.read()
    # Canonical SNI with unknown Host: empty unmatched-site response, never app content.
    # This is not a universal strict-SNI guarantee.
    unmatched = request("/api/health", headers={**AUTH, "Host": "unknown.invalid"})
    assert unmatched[0] == b"" and not unmatched[1].get("set-cookie")
else:
    state = json.loads(state_path.read_text())
    owner = {**AUTH, "Cookie": state["cookie"]}
    request(state["path"], headers={"Cookie": state["cookie"]}, status=401)
    assert hashlib.sha256(request(state["path"], headers=owner)[0]).hexdigest() == state["digest"]
    assert json.loads(request(f"/api/items/{state['saved_id']}", headers=owner)[0])["summary"] == "Community fixture edited"

job = f"/api/remix/jobs/{state['job']}"
assert json.loads(request(job, headers=owner)[0])["status"] == "ready"
assert Image.open(BytesIO(request(job + "/image", headers=owner)[0])).size == (96, 96)
assert Image.open(BytesIO(request(job + "/card", headers=owner)[0])).size == (1000, 1460)
print("Community verified HTTPS/frontend/upload/media/job check passed:", sys.argv[1])
'''

# Separate trusted-internal proof; Caddy's Origin guard must not mask ASGI scheme.
SCHEME_CHECK = r'''
from contextlib import closing
from http.client import HTTPConnection
import json
import socket
host = "fixture.example.invalid"
assert not socket.gethostbyname("app").startswith("127.")
for scheme, status, detail in (("https", 403, "Cross-origin write rejected"),
                               ("http", 404, "Item not found")):
    with closing(HTTPConnection("app", 8000, timeout=30)) as client:
        client.request("PATCH", "/api/items/2147483647/metadata", json.dumps({"summary": "fixture"}),
            {"Host": host, "Origin": "http://" + host, "X-Forwarded-Proto": scheme,
             "Content-Type": "application/json"})
        response = client.getresponse()
        assert response.status == status, response.status
        assert json.loads(response.read())["detail"] == detail
print("Trusted-internal installed Uvicorn ASGI scheme proof passed")
'''


PUBLIC_CHECK = r'''
from contextlib import closing
from http.client import HTTPSConnection
from base64 import b64encode
from pathlib import Path
import json, ssl, sys
from urllib.parse import urlsplit
context = ssl.create_default_context(cafile="/tmp/community-ca.crt")
state = json.loads(Path("/reserve/.community-smoke.json").read_text())
auth = {"Authorization": "Basic " + b64encode(b"fixture:community-fixture-password").decode()}
def request(host, path, method="GET", headers=None, body=None):
    with closing(HTTPSConnection(host, context=context, timeout=30)) as client:
        client.request(method, path, body=body, headers=headers or {})
        response = client.getresponse()
        return response.status, response.read(), response.headers
private, public = "fixture.example.invalid", "share.fixture.example.invalid"
endpoint = f"/api/items/{state['saved_id']}/share"
if sys.argv[1] == "disabled":
    assert request(private, endpoint, "POST", auth)[0] == 409
    status, _, headers = request(public, "/ABCDE")
    assert status == 404 and "no-store" in headers["cache-control"]
else:
    status, body, _ = request(private, endpoint, "POST", auth)
    assert status == 200, body
    path = urlsplit(json.loads(body)["url"]).path
    assert request(public, path)[0] == 200
    assert request(public, path, "HEAD")[1] == b""
    assert request(public, path + "/media", headers={"Range": "bytes=0-3"})[0] == 206
    assert request(public, path + "/card")[0] == 404
    assert request(public, path + "/events", "POST", {"Origin": "https://foreign.invalid", "Content-Type": "application/json"}, "{}")[0] == 403
    from sqlalchemy import select, func
    from meme_arsenal.config import load_settings
    from meme_arsenal.db import session_scope
    from meme_arsenal.models import AnonymousSession, MediaItem, RemixJobRecord, PublicMediaShare, PublicShareCode
    settings = load_settings()
    def counts():
        with session_scope(settings) as db:
            return tuple(db.scalar(select(func.count()).select_from(model)) for model in (AnonymousSession, MediaItem, RemixJobRecord, PublicMediaShare, PublicShareCode))
    media_status, public_media, media_headers = request(public, path + "/media")
    assert media_status == 200 and public_media
    before = counts()
    for target in ("/api/items", "/media/raw", "/assets/index.js", path + "/../api/items", path + "/%2e%2e/api/items", path + "%2fmedia", path + "%252fmedia", path + "//media", path + "/", "/abcde"):
        status, body, headers = request(public, target, headers={**auth, "Cookie": state["cookie"], "X-Forwarded-Host": private})
        if target == path + "%2fmedia" and status == 200:
            # Caddy can decode this spelling to the same public capability.
            assert body == public_media
            assert headers.get("content-type") == media_headers.get("content-type")
        else:
            assert status in (400, 403, 404, 405), (target, status)
        assert not headers.get("set-cookie") and b'"items"' not in body and b'id="root"' not in body
    assert counts() == before
    assert request(public, path, "DELETE")[0] == 404
    # Publish the existing ready job only after checking output-only denial.
    status, body, _ = request(private, f"/api/remix/jobs/{state['job']}/save", "POST", auth)
    assert status == 200, body
    remix_id = json.loads(body)["item"]["id"]
    recipe_endpoint = f"/api/items/{remix_id}/share"
    status, body, _ = request(private, recipe_endpoint + "?card=true", headers=auth)
    if status != 200:
        from meme_arsenal.media import media_item_has_original_file
        with session_scope(settings) as db:
            saved_item = db.get(MediaItem, remix_id)
            diagnostic = None if saved_item is None else {
                "public_status": saved_item.public_status, "visibility": saved_item.visibility,
                "processing_status": saved_item.processing_status,
                "processing_error": saved_item.processing_error,
                "original_exists": media_item_has_original_file(settings, saved_item),
            }
        raise AssertionError(("Saved remix sharing status", status, body, diagnostic))
    assert json.loads(body)["active"] is False
    # Local synthetic video, no provider/network; retain the existing saved poster.
    import subprocess
    video = settings.data_dir / "community-source.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=64x64:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with session_scope(settings) as db:
        source = db.get(MediaItem, state["saved_id"])
        source.original_path = str(video.relative_to(settings.reserve_path))
        source.media_type, source.mime_type = "video", "video/mp4"
        source.public_original_allowed = True
    status, body, _ = request(private, recipe_endpoint + "?card=true", "POST", auth)
    assert status == 200, body
    card = urlsplit(json.loads(body)["card_url"]).path
    assert request(public, card)[0] == 200
    assert request(public, card + "/card/image")[0] == 200
    # A remains an unsaved draft; B is the saved source at its original position.
    assert request(public, card + "/card/image?source=A")[0] == 404
    assert request(public, card + "/card/image?source=B")[0] == 200
    assert request(public, card + "/card/image?source=B&playback=true", headers={"Range": "bytes=0-3"})[0] == 206
    assert request(public, card + "/card", "HEAD")[0] == 404
    assert request(private, recipe_endpoint, "DELETE", auth)[0] == 200
    assert request(public, card)[0] == 404
    assert request(private, endpoint, "DELETE", auth)[0] == 200
    assert request(public, path)[0] == 404
print("Optional public ingress phase passed", sys.argv[1])
'''


def check_caddy_config(config):
    """Parsed configuration evidence, not observation of upstream header bytes."""
    assert config["admin"]["listen"] == "localhost:2019"
    servers = config["apps"]["http"]["servers"]
    assert len(servers) == 1
    server = next(iter(servers.values()))
    assert server["listen"] == [":443"] and len(server["routes"]) == 1
    site = server["routes"][0]
    assert site["match"] == [{"host": [HOST]}]
    leaves = []

    def visit(routes):
        for route in routes:
            for handler in route["handle"]:
                if handler["handler"] == "subroute":
                    assert not route.get("match") or route is site
                    visit(handler["routes"])
                else:
                    leaves.append((route, handler))

    visit([site])
    assert [handler["handler"] for _, handler in leaves] == [
        "authentication", "static_response", "reverse_proxy"]
    auth_route, auth = leaves[0]
    assert not auth_route.get("match")
    basic = auth["providers"]["http_basic"]
    assert basic["hash"]["algorithm"] == "bcrypt"
    assert len(basic["accounts"]) == 1 and basic["accounts"][0]["username"] == "fixture"
    origin_route, rejection = leaves[1]
    assert int(rejection["status_code"]) == 403 and len(origin_route["match"]) == 1
    matcher = origin_route["match"][0]
    assert matcher["header"] == {"Origin": ["*"]} and len(matcher["not"]) == 2
    assert {"method": ["GET", "HEAD", "OPTIONS"]} in matcher["not"]
    assert {"header": {"Origin": ["https://" + HOST]}} in matcher["not"]
    proxy_route, proxy = leaves[2]
    assert not proxy_route.get("match") and proxy["upstreams"] == [{"dial": "app:8000"}]
    headers = proxy["headers"]["request"]
    assert headers["set"] == {"Host": [HOST], "X-Forwarded-Host": [HOST],
        "X-Forwarded-Proto": ["https"], "X-Forwarded-For": ["{http.request.remote.host}"]}
    assert set(headers["delete"]) == {"Forwarded", "X-Forwarded-Prefix", "X-Real-IP", "Authorization"}
    print("Parsed Caddy host/auth/Origin/proxy/header configuration passed (hashes withheld)")


def check_topology(config, instance, *, fixture):
    services = config["services"]
    assert set(services) == {"app", "worker", "db", "redis", "caddy"}
    for name, service in services.items():
        assert not service.get("network_mode") and not service.get("container_name")
        assert service["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
        if name != "caddy" or fixture:
            assert not service.get("ports")
        for mount in service["volumes"]:
            # Compose omits false booleans from its normalized JSON output.
            assert mount["type"] == "bind" and mount["bind"].get("create_host_path", False) is False
    if not fixture:
        assert {(p["target"], str(p["published"]), p["protocol"]) for p in services["caddy"]["ports"]} == {
            (80, "80", "tcp"), (443, "443", "tcp")}
    assert set(services["caddy"]["networks"]) == {"egress"}
    assert set(services["db"]["networks"]) == set(services["redis"]["networks"]) == {"private"}
    assert {m["target"]: (m["source"], m.get("read_only", False)) for m in services["caddy"]["volumes"]} == {
        "/etc/caddy": (str(instance / "config/ingress"), True),
        "/data": (str(instance / "state/proxy/data"), False),
        "/config": (str(instance / "state/proxy/config"), False)}
    for name in ("app", "worker"):
        assert services[name]["environment"]["PUBLIC_TRUST_PROXY_HEADERS"] == "false"
    assert services["app"]["environment"]["FORWARDED_ALLOW_IPS"] == "*"
    assert "FORWARDED_ALLOW_IPS" not in services["worker"]["environment"]
    assert all(not network.get("external") for network in config["networks"].values())
    if fixture:
        assert all(network.get("internal") for network in config["networks"].values())
    print("Compose topology passed:", "offline fixture" if fixture else "base HTTPS-only ingress")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this disposable Docker smoke check as root for bind-data cleanup")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/root")}
    project = f"arsenal-smoke-{uuid.uuid4().hex[:12]}"
    image = f"{project}:test"
    browser_image = f"{project}:browser"
    temporary = Path(tempfile.mkdtemp(prefix="arsenal-community-smoke-"))
    instance = temporary / "instance"
    network_override = temporary / "internal-network.yml"
    base = ["docker", "compose", "-p", project, "--env-file", str(instance / "config/compose.env"),
            "-f", str(ROOT / "deploy/community/compose.yml")]
    compose = [*base, "-f", str(network_override)]

    def run(command, *, capture=False, input=None, check=True, timeout=None):
        return subprocess.run(command, cwd=ROOT, env=env, text=True, input=input,
                              capture_output=capture, check=check, timeout=timeout)

    def runtime_ports():
        ids = run([*compose, "ps", "-q"], capture=True).stdout.split()
        containers = json.loads(run(["docker", "inspect", *ids], capture=True).stdout)
        assert len(containers) == 5
        assert all(not c["HostConfig"]["PortBindings"] for c in containers)
        assert all(not bindings for c in containers for bindings in c["NetworkSettings"]["Ports"].values())
        print("Runtime PortBindings empty for all five fixture containers")

    def api(mode):
        run([*compose, "exec", "-T", "app", "python", "-", mode], input=API_CHECK)

    initialized = False
    try:
        # Dependency fetches only in CI; all fixture runtime networks are internal.
        caddy_image = next(line.split("image: ", 1)[1].strip()
                           for line in (ROOT / "deploy/community/compose.yml").read_text().splitlines()
                           if "image: caddy:" in line)
        run(["docker", "pull", caddy_image])
        from community_guide import hash_file as interactive_hash_file
        hash_file = temporary / "owner.hash"
        interactive_hash_file(caddy_image, hash_file, env=env, cwd=ROOT)
        # !reset is required: an ordinary empty list would retain published ports.
        network_override.write_text(f"""services:
  caddy:
    ports: !reset []
    networks:
      egress:
        aliases: [{HOST}, {SHARE_HOST}]
networks:
  egress:
    internal: true
""")
        run(["python3", str(ROOT / "deploy/community/init.py"), str(instance),
             "--site-url", "https://" + HOST, "--image", image,
             "--auth-user", "fixture", "--auth-hash-file", str(hash_file)])
        initialized = True
        run([*base, "config", "--quiet"], timeout=30)
        check_topology(json.loads(run([*base, "config", "--format", "json"], capture=True).stdout),
                       instance, fixture=False)
        profiled = json.loads(run([*base, "--profile", "telegram", "config", "--format", "json"], capture=True).stdout)
        assert set(profiled["services"]) == {"app", "worker", "db", "redis", "caddy", "sync"}
        assert not profiled["services"]["sync"].get("ports")
        check_topology(json.loads(run([*compose, "config", "--format", "json"], capture=True).stdout),
                       instance, fixture=True)
        caddyfile = instance / "config/ingress/Caddyfile"
        original = caddyfile.read_text()
        site = f"https://{HOST} {{\n"
        assert original.count(site) == 1 and "tls internal" not in original
        caddyfile.write_text(original.replace(site, site + "\ttls internal\n", 1))
        assert caddyfile.read_text().replace("\ttls internal\n", "", 1) == original
        adapted = run([*compose, "run", "--rm", "--no-deps", "caddy", "caddy", "adapt",
                       "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", "--validate"], capture=True)
        check_caddy_config(json.loads(adapted.stdout))
        run([*compose, "run", "--rm", "--no-deps", "caddy", "caddy", "validate",
             "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], timeout=60)
        run([*compose, "build", "app"])
        run([*compose, "run", "--rm", "--no-deps", "app", "--check"], timeout=60)
        run(["docker", "build", "-f", "docker/test-chromium.Dockerfile", "--target",
             "community-browser", "-t", browser_image, "."])
        print('OMA30 candidate commit:', run(['git', 'rev-parse', 'HEAD'], capture=True).stdout.strip())
        print('OMA30 retained application image:', run(['docker', 'image', 'inspect', image,
              '--format', '{{.Id}}'], capture=True).stdout.strip())
        run([*compose, "up", "-d", "--no-build", "--wait", "db", "redis"])
        run([*compose, "run", "--rm", "--no-deps", "-e", "ARSENAL_INSTALL_DASHBOARD=1", "app", "alembic", "upgrade", "head"])
        run([*compose, "run", "--rm", "--no-deps", "-e", "ARSENAL_INSTALL_DASHBOARD=0", "app", "arsenal", "owner", "initialize"])
        run([*compose, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180"])
        runtime_ports()
        assert run([*compose, "exec", "-T", "caddy", "id", "-u"], capture=True).stdout.strip() == "0"
        assert run([*compose, "exec", "-T", "caddy", "id", "-g"], capture=True).stdout.strip() == "0"
        ca = instance / "state/proxy/data/caddy/pki/authorities/local/root.crt"
        for attempt in range(60):
            if ca.exists():
                break
            time.sleep(1)
        ca_fingerprint = hashlib.sha256(ca.read_bytes()).hexdigest()
        signing = instance / "secrets/public_session_secret"
        signing_fingerprint = hashlib.sha256(signing.read_bytes()).hexdigest()
        run([*compose, "cp", str(ca), "app:/tmp/community-ca.crt"])
        run([*compose, "exec", "-T", "app", "sh", "-c",
             "python --version && python -m pip freeze && ffmpeg -version"], timeout=30)
        print("Candidate tree:", run(["git", "rev-parse", "HEAD^{tree}"], capture=True).stdout.strip())
        # Real installed UI, same storage/build/ingress; no host ports or TLS bypass.
        run(["docker", "run", "--rm", "--name", f"{project}-browser", "--network", f"{project}_egress",
             "-v", f"{ca}:/tmp/fixture-ca.crt:ro",
             "-v", f"{ROOT / 'scripts/community_ui.py'}:/tmp/community_ui.py:ro",
             "--entrypoint", "sh", browser_image, "-ec",
             'mkdir -p /root/.pki/nssdb; certutil -N --empty-password -d sql:/root/.pki/nssdb; '
             'certutil -A -d sql:/root/.pki/nssdb -n fixture -t "C,," -i /tmp/fixture-ca.crt; '
             'python /tmp/community_ui.py'], timeout=180)
        api("seed")
        # Actual entry chain, disposable one-offs, same built image and reserve.
        # No fault container publishes ports; successful child would only write a marker.
        reserve = instance / "state/reserve"
        sentinel = reserve / "bootstrap-config-sentinel"
        sentinel.write_text("preserve across initialization and recreation")
        def reserve_snapshot():
            return {str(p.relative_to(reserve)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in reserve.rglob("*") if p.is_file()}
        before_faults = reserve_snapshot()
        for setup in (
            "mv /app/static-dashboard /app/static-dashboard-disabled",
            "rm /app/static-dashboard/index.html",
            "rm -f /app/static-dashboard/arsenal-config.json; mkdir /app/static-dashboard/arsenal-config.json",
            "mkdir /tmp/bootstrap-bin; printf '#!/bin/sh\\nexit 73\\n' > /tmp/bootstrap-bin/alembic; chmod +x /tmp/bootstrap-bin/alembic; export PATH=/tmp/bootstrap-bin:$PATH",
        ):
            probe = setup + '''
online-entrypoint sh -c 'alembic upgrade head && exec sh -c "touch /tmp/served"'
result=$?
test "$result" -ne 0 && test ! -e /tmp/served
'''
            run([*compose, "run", "--rm", "--no-deps", "--entrypoint", "sh", "app", "-c", probe])
            assert reserve_snapshot() == before_faults, "Failed bootstrap changed reserve"
        print("Actual entrypoint missing-assets/index/migration failures blocked child; reserve unchanged")
        run([*compose, "exec", "-T", "worker", "python", "-"], input=SCHEME_CHECK)
        run([*compose, "exec", "-T", "redis", "redis-cli", "SET", "community-smoke", project])
        run([*compose, "up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180"])
        runtime_ports()
        assert hashlib.sha256(ca.read_bytes()).hexdigest() == ca_fingerprint
        assert hashlib.sha256(signing.read_bytes()).hexdigest() == signing_fingerprint
        run([*compose, "cp", str(ca), "app:/tmp/community-ca.crt"])
        api("verify")
        assert sentinel.read_text() == "preserve across initialization and recreation"
        assert run([*compose, "exec", "-T", "redis", "redis-cli", "GET", "community-smoke"], capture=True).stdout.strip() == project
        # Installed lifecycle proof; same image/project, no extra stack/provider.
        import runpy
        process_check = runpy.run_path(str(ROOT / 'tests/test_drain.py'))['PROCESS_CHECK']
        run([*compose, 'exec', '-T', 'worker', 'python', '-c', process_check])
        backups = temporary / 'backups'
        backups.mkdir(mode=0o700)
        (reserve / 'data/telegram.session').write_bytes(b'synthetic-session-not-a-login')
        (instance / 'secrets/future-owner-fixture').write_bytes(b'synthetic-protected-config')
        helper = ["python3", str(ROOT / 'scripts/backup_community.py'), '--instance', str(instance),
                  '--project', project, '--backup-dir', str(backups),
                  '-f', str(ROOT / 'deploy/community/compose.yml'), '-f', str(network_override),
                  '--operator-writers-stopped']
        def states():
            ids = run([*compose, 'ps', '-aq'], capture=True).stdout.split()
            return {c['Config']['Labels']['com.docker.compose.service']:
                    (c['Id'], c['State']['Running'], c['State']['StartedAt'])
                    for c in json.loads(run(['docker', 'inspect', *ids], capture=True).stdout)}
        before = states()
        files = reserve_snapshot()
        run(helper)
        after = states()
        assert set(after) == set(before) == {'app', 'worker', 'db', 'redis', 'caddy'}
        assert all(after[s][:2] == before[s][:2] for s in before)
        assert all(after[s] == before[s] for s in ('db', 'redis'))
        completed = list(backups.glob('community-*'))
        assert len(completed) == 1
        saved = completed[0]
        manifest = json.loads((saved / 'complete.json').read_text())
        assert manifest['complete'] and manifest['resumed']
        for name, component in manifest['components'].items():
            assert hashlib.sha256((saved / name).read_bytes()).hexdigest() == component['sha256']
        unpacked = temporary / 'unpacked'
        unpacked.mkdir()
        for name in ('reserve.tar.gz', 'protected.tar.gz'):
            with tarfile.open(saved / name) as archive:
                archive.extractall(unpacked, filter='data')
        for name, digest in files.items():
            assert hashlib.sha256((unpacked / 'state/reserve' / name).read_bytes()).hexdigest() == digest
        for tree in ('config', 'secrets', 'state/proxy'):
            for source in (instance / tree).rglob('*'):
                if source.is_file():
                    assert (unpacked / source.relative_to(instance)).read_bytes() == source.read_bytes()
        # Restore ONLY to a new scratch database, never over the running fixture.
        run([*compose, 'exec', '-T', 'db', 'createdb', '-U', 'meme_arsenal', 'backup_fixture'])
        with (saved / 'database.dump').open('rb') as dump:
            subprocess.run([*compose, 'exec', '-T', 'db', 'pg_restore', '-U', 'meme_arsenal',
                            '-d', 'backup_fixture'], stdin=dump, env=env, check=True)
        for table in ('media_items', 'remix_jobs', 'remix_sources', 'anonymous_sessions'):
            query = f'SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\') FROM {table} t'
            def rows(database):
                return run([*compose, 'exec', '-T', 'db', 'psql', '-U', 'meme_arsenal', '-d',
                            database, '-Atc', query], capture=True).stdout
            assert rows('backup_fixture') == rows('meme_arsenal')
        run([*compose, 'exec', '-T', 'db', 'dropdb', '-U', 'meme_arsenal', 'backup_fixture'])
        # Originally stopped worker remains stopped, including archive failure.
        worker_id = after['worker'][0]
        # Recovery starts the worker without a healthcheck. Do not send TERM
        # while PID 1 is still importing code before its handler is installed.
        run(['docker', 'exec', worker_id, 'python', '-c', WORKER_READY_CHECK])
        run(['docker', 'kill', '--signal=TERM', worker_id])
        deadline = time.monotonic() + 60
        while states()['worker'][1]:
            assert time.monotonic() < deadline, "Ready fixture worker did not finish TERM drainage"
            time.sleep(.2)
        stopped = states()
        run(helper)
        assert all(states()[s][:2] == stopped[s][:2] for s in stopped)
        failure_code = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location('backup', sys.argv.pop(1))
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module.tarfile.open
def fail(*args, **kwargs):
    if len(args) > 1 and args[1] == 'w:gz': raise OSError('fixture archive failure')
    return original(*args, **kwargs)
module.tarfile.open = fail
module.main()
'''
        known = set(backups.iterdir())
        failure = run(['python3', '-c', failure_code, *helper[1:]], capture=True, check=False)
        assert failure.returncode != 0
        assert set(backups.iterdir()) == known
        assert not (instance / '.backup-recovery.json').exists()
        assert all(states()[s][:2] == stopped[s][:2] for s in stopped)
        assert all(states()[s] == stopped[s] for s in ('db', 'redis'))
        assert run([*compose, 'exec', '-T', 'redis', 'redis-cli', 'GET', 'community-smoke'], capture=True).stdout.strip() == project
        run(['docker', 'start', worker_id])
        print('Community backup exact-ID recovery, private complete contents and scratch DB comparison passed')
        credential = instance / "config/ingress/owner.caddy"
        unavailable = credential.with_suffix(".disabled")
        credential.rename(unavailable)
        try:
            failed = run([*compose, "exec", "-T", "caddy", "caddy", "reload", "--config",
                          "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], capture=True, check=False)
            assert failed.returncode != 0
            api("gate")
        finally:
            unavailable.rename(credential)
        run([*compose, "exec", "-T", "caddy", "caddy", "reload", "--config",
             "/etc/caddy/Caddyfile", "--adapter", "caddyfile"], capture=True)
        print("Community private ingress and full-container persistence passed; fixture CA:", ca_fingerprint)
        # Optional ingress phase on the SAME image, project, database and reserve.
        run([*compose, "stop", "caddy"])
        public_site = (ROOT / "deploy/community/public.Caddyfile").read_text().replace("SHARE_AUTHORITY", SHARE_HOST)
        public_site = public_site.replace(f"https://{SHARE_HOST} {{", f"https://{SHARE_HOST} {{\n tls internal")
        caddyfile.write_text(caddyfile.read_text() + public_site)
        app_env = instance / "config/app.env"
        app_env.write_text(app_env.read_text().replace("PUBLIC_SHARE_URL=", f"PUBLIC_SHARE_URL=https://{SHARE_HOST}"))
        adapted = run([*compose, "run", "--rm", "--no-deps", "caddy", "caddy", "adapt", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", "--validate"], capture=True)
        config = json.loads(adapted.stdout)
        routes = next(iter(config["apps"]["http"]["servers"].values()))["routes"]
        assert {r["match"][0]["host"][0] for r in routes} == {HOST, SHARE_HOST}
        public = next(r for r in routes if r["match"][0]["host"] == [SHARE_HOST])
        def handlers(value):
            if isinstance(value, dict):
                if "handler" in value:
                    yield value
                for child in value.values():
                    yield from handlers(child)
            elif isinstance(value, list):
                for child in value:
                    yield from handlers(child)
        proxies = [h for h in handlers(public) if h["handler"] == "reverse_proxy"]
        assert len(proxies) == 3
        for proxy in proxies:
            headers = proxy["headers"]["request"]
            assert headers["set"]["Host"] == [SHARE_HOST]
            assert headers["set"]["X-Forwarded-Host"] == [SHARE_HOST]
            assert {"Cookie", "Authorization", "Forwarded", "X-Real-IP", "X-Forwarded-Prefix"} <= set(headers["delete"])
        run([*compose, "up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180", "app"])
        run([*compose, "up", "-d", "--no-build", "--wait", "caddy"])
        run([*compose, "cp", str(ca), "app:/tmp/community-ca.crt"])
        run([*compose, "exec", "-T", "app", "python", "-", "disabled"], input=PUBLIC_CHECK)
        app_env.write_text(app_env.read_text().replace("PUBLIC_SHARING_ENABLED=false", "PUBLIC_SHARING_ENABLED=true"))
        run([*compose, "up", "-d", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180", "app"])
        run([*compose, "cp", str(ca), "app:/tmp/community-ca.crt"])
        run([*compose, "exec", "-T", "app", "python", "-", "enabled"], input=PUBLIC_CHECK)
        # Enrich only after ordinary worker TERM drain: no provider race.
        from oma30_fixture import ENRICH, SHARES, WORKER, VERIFY, WORKER_VERIFY
        run(['docker', 'exec', worker_id, 'python', '-c', WORKER_READY_CHECK])
        run(['docker', 'kill', '--signal=TERM', worker_id])
        deadline = time.monotonic() + 60
        while states()['worker'][1]:
            if time.monotonic() >= deadline:
                raise RuntimeError('Source worker did not drain')
            time.sleep(.2)
        assert states()['worker'][0] == worker_id
        drained = json.loads(run(['docker', 'inspect', worker_id], capture=True).stdout)[0]
        assert not drained['State']['Running'] and drained['State']['ExitCode'] == 0
        run([*compose, 'exec', '-T', 'app', 'python', '-'], input=ENRICH)
        run([*compose, 'exec', '-T', 'app', 'python', '-', 'prepare'], input=SHARES)
        run([*compose, 'stop', 'caddy', 'app'])
        app_env.write_text(app_env.read_text().replace('PUBLIC_SHARING_ENABLED=true', 'PUBLIC_SHARING_ENABLED=false'))
        run([*compose, 'up', '-d', '--no-build', '--wait', 'app', 'caddy'])
        prior_sets = set(backups.glob('community-*'))
        stopped_before = states()
        run(helper)
        assert all(states()[name][:2] == stopped_before[name][:2] for name in stopped_before)
        new_sets = set(backups.glob('community-*')) - prior_sets
        assert len(new_sets) == 1
        saved = new_sets.pop()
        enriched = temporary / 'enriched'
        enriched.mkdir(mode=0o700)
        for name in ('reserve.tar.gz', 'protected.tar.gz'):
            with tarfile.open(saved / name) as archive:
                archive.extractall(enriched, filter='data')
        unpacked = enriched
        run([*compose, 'stop', 'caddy', 'app'])
        assert not states()['worker'][1]
        print('Harness drained source writers after stopped-worker enriched backup')
        # OMA29: the same complete set into one absent instance/new DB, no app.
        restored = temporary / 'restored'
        restore_project = project + '-restore'
        restore_compose = ['docker', 'compose', '-p', restore_project, '--env-file', str(restored / 'restore.env'),
                           '-f', str(ROOT / 'deploy/community/compose.yml'), '-f', str(network_override),
                           '-f', str(restored / 'restore-safe.json')]
        try:
            source_before = states()
            restore_command = ['python3', str(ROOT / 'scripts/restore_community.py'), '--backup-set', str(saved),
                               '--instance', str(restored), '--project', restore_project, '--image', image,
                               '-f', str(ROOT / 'deploy/community/compose.yml'), '-f', str(network_override),
                               '--operator-writers-stopped']
            run(restore_command)
            report = json.loads((restored / 'restore-report.json').read_text())
            assert report['phase'] == 'complete'
            db_id = report['db']
            ids = run([*restore_compose, 'ps', '-aq'], capture=True).stdout.split()
            assert ids == [db_id]
            stopped = json.loads(run(['docker', 'inspect', db_id], capture=True).stdout)[0]
            assert not stopped['State']['Running'] and stopped['State']['ExitCode'] == 0
            assert list((restored / 'state/redis-data').iterdir()) == []
            for source in unpacked.rglob('*'):
                if source.is_file():
                    assert (restored / source.relative_to(unpacked)).read_bytes() == source.read_bytes()
            assert states() == source_before
            # Only this new DB is restarted for read-only SQL equivalence and
            # the real populated-catalog refusal boundary. No second restore.
            run([*restore_compose, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', 'db'])
            durable_tables = run([*compose, 'exec', '-T', 'db', 'psql', '-U', 'meme_arsenal', '-d',
                                  'meme_arsenal', '-Atc', "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"], capture=True).stdout.split()
            durable_before = {}
            for table in durable_tables:
                query = f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM {table} t"
                original = run([*compose, 'exec', '-T', 'db', 'psql', '-U', 'meme_arsenal', '-d', 'meme_arsenal', '-Atc', query], capture=True).stdout
                recovered = run([*restore_compose, 'exec', '-T', 'db', 'psql', '-U', 'meme_arsenal', '-d', 'meme_arsenal', '-Atc', query], capture=True).stdout
                assert original == recovered, table
                durable_before[table] = json.loads(original)
            ledger = temporary / 'oma30-durable-before.json'
            ledger.write_text(json.dumps(durable_before))
            ledger.chmod(0o600)
            print('Enriched restored fixture row counts:', {t: len(v) for t, v in durable_before.items()})
            probe = '''import importlib.util, sys
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location('restore', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
r = module.Restore(SimpleNamespace(instance=sys.argv[2], backup_set=sys.argv[3], project=sys.argv[4], timeout=180))
r.record.update(db=sys.argv[5], db_image=sys.argv[6])
try:
    try:
        r.empty_catalog()
    except RuntimeError as error:
        assert str(error) == 'Target catalog is not empty'
    else:
        raise AssertionError('Populated DB accepted')
finally:
    r.stop()
'''
            run(['python3', '-c', probe, str(ROOT / 'scripts/restore_community.py'), str(restored),
                 str(saved), restore_project, db_id, report['db_image']])
            stopped = json.loads(run(['docker', 'inspect', db_id], capture=True).stdout)[0]
            assert not stopped['State']['Running'] and stopped['State']['ExitCode'] == 0
            assert states() == source_before
            assert run(restore_command, check=False, capture=True).returncode != 0
            run([*restore_compose, 'run', '--rm', '--no-deps', 'app', '--check'])
            run([*restore_compose, 'up', '-d', '--no-build', '--wait', 'db', 'redis'])
            assert run([*restore_compose, 'exec', '-T', 'redis', 'redis-cli', 'DBSIZE'], capture=True).stdout.strip() == '0'
            run([*restore_compose, 'run', '--rm', '--no-deps', 'app', 'arsenal', 'owner', 'adopt'])
            activation = temporary / 'activation.json'
            activation.write_text(json.dumps({'services': {'app': {'environment': {'ARSENAL_INSTALL_DASHBOARD': '1'}}}}))
            restore_compose += ['-f', str(activation)]
            run([*restore_compose, 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app', 'caddy'])
            target_ids = run([*restore_compose, 'ps', '-q'], capture=True).stdout.split()
            target_containers = json.loads(run(['docker', 'inspect', *target_ids], capture=True).stdout)
            assert len(target_containers) == 4
            assert all(not c['HostConfig']['PortBindings'] for c in target_containers)
            target_ca = restored / 'state/proxy/data/caddy/pki/authorities/local/root.crt'
            run([*restore_compose, 'cp', str(target_ca), 'app:/tmp/community-ca.crt'])
            restored_api = API_CHECK.replace('== "ready"', 'in ("ready", "saved")')
            run([*restore_compose, 'exec', '-T', 'app', 'python', '-', 'verify'], input=restored_api + VERIFY)
            assert run([*restore_compose, 'exec', '-T', 'redis', 'redis-cli', 'LLEN',
                        'meme_arsenal:remix_jobs'], capture=True).stdout.strip() == '0'
            run([*restore_compose, 'run', '--rm', '--no-deps', '-T', 'app', 'timeout', '90', 'python', '-'], input=WORKER)
            run([*restore_compose, 'exec', '-T', 'app', 'python', '-', 'verify'], input=restored_api + WORKER_VERIFY)
            for table, original in durable_before.items():
                query = f"SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),'[]') FROM {table} t"
                current = json.loads(run([*restore_compose, 'exec', '-T', 'db', 'psql', '-U', 'meme_arsenal',
                                          '-d', 'meme_arsenal', '-Atc', query], capture=True).stdout)
                if table == 'anonymous_sessions':
                    for row in current:
                        old = next(r for r in original if r['id'] == row['id'])
                        row['last_seen_at'] = old['last_seen_at']
                elif table == 'remix_jobs':
                    for row in current:
                        if row['id'] == 'community-restored-queued':
                            old = next(r for r in original if r['id'] == row['id'])
                            # The restored legacy job adopts its first execution identity.
                            # WORKER also checks the complete capture against its settings.
                            assert old['execution_capture_json'] is None
                            assert old['execution_first_captured_attempt'] is None
                            assert row['execution_capture_json']['origin'] == 'legacy_first_execution'
                            assert row['execution_capture_json']['provider'] == 'google'
                            assert row['execution_first_captured_attempt'] == 1
                            for key in ('status', 'message', 'result_json', 'updated_at', 'error', 'retry_after_seconds',
                                        'execution_capture_json', 'execution_first_captured_attempt'):
                                row[key] = old[key]
                elif table == 'remix_attempts':
                    added = [r for r in current if r['job_id'] == 'community-restored-queued']
                    assert len(added) == 1 and added[0]['status'] == 'succeeded'
                    current = [r for r in current if r['job_id'] != 'community-restored-queued']
                assert sorted(current, key=lambda r: json.dumps(r, sort_keys=True)) == sorted(original, key=lambda r: json.dumps(r, sort_keys=True)), table
            public_overlay = temporary / 'restored-public.json'
            public_overlay.write_text(json.dumps({'services': {'app': {'environment': {'PUBLIC_SHARING_ENABLED': 'true'}}}}))
            restore_compose += ['-f', str(public_overlay)]
            run([*restore_compose, 'up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'app'])
            run([*restore_compose, 'cp', str(target_ca), 'app:/tmp/community-ca.crt'])
            run([*restore_compose, 'exec', '-T', 'app', 'python', '-', 'retained'], input=SHARES)
            print('Empty-instance exact PG16/vector restore, bytes/SQL, dormant DB and populated refusal passed')
        finally:
            if (restored / 'restore.env').exists():
                cleanup = run([*restore_compose, 'down', '--remove-orphans'], check=False)
                if cleanup.returncode:
                    raise RuntimeError(f'Could not stop restore fixture {restore_project}; retained {temporary}')
        from oma30_upgrade import rehearse
        rehearse(run, temporary, ROOT / 'deploy/community/compose.yml', network_override, image, hash_file)
    finally:
        run(["docker", "rm", "-f", f"{project}-browser"], capture=True, check=False, timeout=30)
        if initialized:
            cleanup = run([*compose, "down", "--remove-orphans"], check=False)
            if cleanup.returncode:
                raise RuntimeError(f"Could not stop fixture project {project}; retained {temporary}")
        run(["docker", "image", "rm", image], capture=True, check=False)
        run(["docker", "image", "rm", browser_image], capture=True, check=False)
        shutil.rmtree(temporary)


if __name__ == "__main__":
    main()
