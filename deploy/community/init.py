#!/usr/bin/env python3
"""Create a new operator-local instance; never alter an existing one."""

import argparse
import os
from pathlib import Path
import re
import secrets


def initialize(root: Path, site_url: str, image: str, auth_user: str, auth_hash_file: Path, share_url: str = "") -> None:
    checkout = Path(__file__).resolve().parents[2]
    if not root.is_absolute() or root.resolve().is_relative_to(checkout):
        raise ValueError("Instance path must be absolute and outside the source checkout")
    root = root.resolve()
    # These values enter a Compose env file, so reject interpolation/control characters.
    if (any(c in str(root) + image for c in "\x00$'\"#")
            or any(c.isspace() for c in str(root) + image) or not image):
        raise ValueError("Instance path/image tag must not contain whitespace or env metacharacters")
    # A deliberately narrow DNS authority, also safe to render into Caddy/env text.
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    origin = re.fullmatch(rf"https://({label}(?:\.{label})+)(?::443)?/?", site_url, re.I | re.ASCII)
    if (not origin or len(origin[1]) > 253
            or not re.search(r"[a-z]", origin[1].split(".")[-1], re.I | re.ASCII)):
        raise ValueError("--site-url must be an HTTPS DNS origin on port 443 without credentials, path or query")
    authority = origin[1].lower()
    site_url = f"https://{authority}"
    share_authority = ""
    if share_url:
        share_origin = re.fullmatch(rf"https://({label}(?:\.{label})+)(?::443)?/?", share_url, re.I | re.ASCII)
        if (not share_origin or len(share_origin[1]) > 253 or share_origin[1].lower() == authority
                or not re.search(r"[a-z]", share_origin[1].split(".")[-1], re.I | re.ASCII)):
            raise ValueError("--share-url must be a distinct HTTPS DNS origin on port 443")
        share_authority = share_origin[1].lower()
    if auth_user == "import":
        raise ValueError("--auth-user 'import' is reserved by Caddy")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", auth_user):
        raise ValueError("--auth-user must be a 1-64 character username using letters, digits, dot, underscore or hyphen")
    try:
        with auth_hash_file.open("rb") as source:
            auth_hash = source.read(62).decode("ascii")
    except (OSError, UnicodeError):
        raise ValueError("--auth-hash-file must contain one readable bcrypt hash") from None
    if not re.fullmatch(r"\$2[aby]\$(?:0[4-9]|[12][0-9]|3[01])\$[./A-Za-z0-9]{53}\n?", auth_hash):
        raise ValueError("--auth-hash-file must contain exactly one bcrypt hash (optional final newline)")
    auth_hash = auth_hash.removesuffix("\n")
    if root.exists():
        raise ValueError("Instance path already exists; choose a new path (existing data is never changed)")
    os.umask(0o077)
    root.mkdir(mode=0o700)  # Parent must already exist: no accidental mount-path creation.
    for directory in ("config", "config/ingress", "secrets", "state", "state/reserve",
                      "state/postgres-data", "state/redis-data", "state/proxy",
                      "state/proxy/data", "state/proxy/config"):
        (root / directory).mkdir(mode=0o700)
    password = secrets.token_hex(32)
    for name, value in {
        "postgres_password": password,
        "database_url": f"postgresql+psycopg://meme_arsenal:{password}@db:5432/meme_arsenal",
        "public_session_secret": secrets.token_urlsafe(48),
        "owner_session_id": secrets.token_hex(24),
    }.items():
        (root / "secrets" / name).write_text(value + "\n")
    template = Path(__file__).with_name("app.env.example").read_text()
    (root / "config/app.env").write_text(
        template.replace("https://library.example.invalid", site_url).replace("PUBLIC_SHARE_URL=", f"PUBLIC_SHARE_URL={'https://' + share_authority if share_authority else ''}")
    )
    (root / "config/compose.env").write_text(
        f"ARSENAL_INSTANCE={root}\nARSENAL_IMAGE={image}\n"
    )
    caddyfile = Path(__file__).with_name("Caddyfile").read_text()
    rendered = caddyfile.replace("SITE_AUTHORITY", authority)
    if share_authority:
        rendered += Path(__file__).with_name("public.Caddyfile").read_text().replace("SHARE_AUTHORITY", share_authority)
    (root / "config/ingress/Caddyfile").write_text(rendered)
    (root / "config/ingress/owner.caddy").write_text(f"{auth_user} {auth_hash}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", type=Path)
    parser.add_argument("--site-url", required=True)
    parser.add_argument("--share-url", default="")
    parser.add_argument("--image", required=True, help="Unique local source-built image tag")
    parser.add_argument("--auth-user", required=True)
    parser.add_argument("--auth-hash-file", type=Path, required=True, help="Protected file containing one bcrypt hash")
    args = parser.parse_args()
    try:
        initialize(args.instance, args.site_url, args.image, args.auth_user, args.auth_hash_file, args.share_url)
    except ValueError as error:
        parser.exit(1, f"Initialization failed: {error}\n")
    except OSError:
        parser.exit(1, "Initialization failed: check parent directory existence, permissions and space; inspect any partial instance before retrying\n")
    print("Created private instance configuration and empty storage. Review config/app.env before use.")
