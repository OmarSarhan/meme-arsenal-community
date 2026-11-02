"""Install only the image's generated dashboard before migrations or serving."""

import json
from pathlib import Path
import shutil
import sys
import tempfile

from .config import load_settings


ONLINE_CONFIG = {"mode": "online", "apiBase": "/api", "remixApiBase": "/api/remix"}


def validate_assets(directory: Path) -> None:
    index = directory / "index.html"
    if not index.is_file() or not index.read_bytes().strip():
        raise ValueError("prebuilt dashboard requires a readable, nonempty index.html")


def install_dashboard(source: Path) -> None:
    # Validate before creating even the generated parent directory.
    validate_assets(source)
    target = load_settings().dashboard_dist_dir.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".dashboard-install-", dir=target.parent))
    previous = staging / "previous"
    prepared = staging / "dist"
    try:
        shutil.copytree(source, prepared)
        config = prepared / "arsenal-config.json"
        # Never follow a packaged config symlink when writing runtime configuration.
        if config.is_symlink():
            config.unlink()
        config.write_text(json.dumps(ONLINE_CONFIG) + "\n")
        validate_assets(prepared)
        if json.loads(config.read_text()) != ONLINE_CONFIG:
            raise ValueError("invalid staged online dashboard configuration")
        if target.exists():
            target.rename(previous)
        try:
            prepared.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    finally:
        # If restoration itself fails, retain the prior dashboard for recovery.
        if previous.exists() and not target.exists():
            print(f"Dashboard recovery required: prior dist retained at {previous}", file=sys.stderr)
        else:
            shutil.rmtree(staging)


if __name__ == "__main__":
    try:
        install_dashboard(Path("/app/static-dashboard"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"Dashboard initialization failed; check prebuilt image assets and dashboard storage ({type(error).__name__})") from error
