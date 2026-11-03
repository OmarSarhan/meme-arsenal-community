# Developer guide

Use synthetic data for source changes. After installing the tools below, change
one behavior, run its focused tests, and review the diff. For a CLI change, start
with `src/meme_arsenal/cli.py` and `python -m pytest tests/test_cli.py`.

The [community guide](../deploy/community/README.md#upload-first-setup) is the
full owner-app route. A contributor checkout installs editable tools; the
SQLite/static demo below demonstrates local fixture browsing. Installing tools
alone does not initialize the full owner app.

[Start here: README and documentation index](../README.md#read-next).
For everyday usage, follow the [command guide](../README.md#everyday-commands).

## First local setup

The reserve is the app's data directory, separate from source code. Local mode
uses SQLite and exported catalog files; hosted mode uses PostgreSQL and a live
API. Read [Local vs Live Dashboard](../README.md#local-vs-live-dashboard) before
choosing a server or dashboard build command.

The initial development target is Linux with Python 3.12 and Node 22.12 or newer
within Node 22 (the locked toolchain's minimum). CI selects Python 3.12 and Node
major 22; it does not test every Node minor. Package metadata permits Python 3.11+,
but that is not a claim that every Python/OS combination is release-tested.
Native Windows is not currently supported because media processing imports
`fcntl`. Install ffmpeg for media processing. Chromium is needed only for
browser checks. In a fresh checkout:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
npm --prefix frontend/dashboard ci
```

This installs the `arsenal` command and dashboard dependencies; it does not
initialize an archive. Requirements and scripts are in
[pyproject.toml](../pyproject.toml) and the
[dashboard manifest](../frontend/dashboard/package.json). Root npm scripts
are devcontainer helpers; this checkout has no devcontainer configuration.

For real local use, follow [Quick Start](../README.md#quick-start) and
[Configuration](../README.md#configuration). Choose a new writable `RESERVE_PATH`
and ensure `DATABASE_URL`, if set, targets that local database. A fixture import
avoids Telegram only; it can still call the configured AI provider and write to
the configured database. Use `AI_PROVIDER=mock` and isolated local data for fixture
work. Do not reuse hosted settings. Settings can also come from `config.yaml`,
`env`, `.env`, environment variables, and secret-file settings; see
[the loader](../src/meme_arsenal/config.py).

## No-account local demo

Use a disposable, fresh source checkout with the Linux/Python/Node/npm/ffmpeg
prerequisites above. Start from its root with no `env`, `.env`, `config.yaml` or
`config.yml`, installed environments, dashboard build or reserve carried over.
The clean shell below excludes inherited provider, Telegram, owner, proxy and
database settings. Installation needs registry access. No account setup is needed.

```bash
env -i HOME="$(mktemp -d)" PATH="$PATH" LANG=C.UTF-8 bash --noprofile --norc
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
npm --prefix frontend/dashboard ci
export RESERVE_PATH="$PWD/meme-arsenal-reserve"
export DATABASE_URL="sqlite:///$RESERVE_PATH/data/arsenal.sqlite"
export AI_PROVIDER=mock
arsenal init
arsenal sync --fixture-dir tests/fixtures --limit 10
arsenal serve-dashboard --host 127.0.0.1 --port 8123
```

Open <http://127.0.0.1:8123/dashboard/dist/>. Expect two fixture cards; search
`deploy` to find one, clear it to see both, and open a card for caption/source
details. Sync processes the tracked images and builds the React dashboard itself.
Stop the server with Ctrl-C. Keep or delete only this disposable checkout's
reserve, `.venv`, `frontend/dashboard/node_modules` and temporary HOME after use.
Optional CLI search (`arsenal search "code review win"`) exports a search bundle;
it is separate from the gallery's lexical search.

This demonstrates local import, deterministic mock classification/embeddings and
static browsing, not AI quality, Telegram login, generation or community features.
Mock remix health is disabled. Online editing/sharing and owner authentication
require the separate API-backed deployment described below.

CI's [fresh-demo acceptance script](../scripts/verify_fresh_mock_demo.sh) installs
`.[dev,browser]` in a new venv and runs `npm ci` in a disposable candidate checkout.
It then reuses those same installed paths in a new container with external
networking disabled, running the real CLI and Chromium smoke without a prebuild.
Cached tooling/downloads are allowed; preinstalled project environments are not
fresh-install evidence. Its artifact records commands, versions, dependencies and
runtime results. This proves unavailable external runtime networking, not zero
connection attempts; the interactive commands above do not impose that isolation.

## Common edit and check loop

Use temporary data and mocked provider calls. Never point contributor commands
at a running library. Record the checks you ran and any skips or limitations.

## Where to make changes

Choose the row matching the behavior, then start with its tests.

For worked contributor seams, see [internal extension examples](internal-extension-examples.md).

| Change | Source entry points | Related checks |
| --- | --- | --- |
| CLI flags and workflow order | [cli.py](../src/meme_arsenal/cli.py), [pipeline.py](../src/meme_arsenal/pipeline.py) | [test_cli.py](../tests/test_cli.py), [test_pipeline.py](../tests/test_pipeline.py) |
| Import and previews | [telegram_importer.py](../src/meme_arsenal/telegram_importer.py), [media.py](../src/meme_arsenal/media.py) | [test_import_media.py](../tests/test_import_media.py) |
| Classification and search | [ai.py](../src/meme_arsenal/ai.py), [embeddings.py](../src/meme_arsenal/embeddings.py), [search.py](../src/meme_arsenal/search.py) | [test_ai_search_catalog.py](../tests/test_ai_search_catalog.py) |
| Gallery and API client | [App.tsx](../frontend/dashboard/src/App.tsx), [api.ts](../frontend/dashboard/src/api.ts), [catalog.ts](../frontend/dashboard/src/catalog.ts) | [dashboard workflow tests](../tests/test_dashboard_workflow.py), [frontend build](#checks-without-live-services) |
| Hosted routes and jobs | [online.py](../src/meme_arsenal/online.py), [online_worker.py](../src/meme_arsenal/online_worker.py) | [test_online_api.py](../tests/test_online_api.py), [test_online_worker.py](../tests/test_online_worker.py) |
| Schema and configuration | [models.py](../src/meme_arsenal/models.py), [config.py](../src/meme_arsenal/config.py), [migrations](../migrations/versions) | [test_config_db.py](../tests/test_config_db.py), [test_migration.py](../tests/test_migration.py) |

For deployment changes, begin with the [community core recipe](../deploy/community/README.md)
and [Compose layout](online-architecture.md#docker-compose-layout). The latter also
documents legacy external-proxy examples. Deployment and database migrations
are not local documentation checks.

## Checks without live services

After installing development dependencies, run these from the repository root:

```bash
ruff check src tests
python -m pytest tests/test_config_db.py tests/test_pipeline.py tests/test_cli.py
npm --prefix frontend/dashboard run build
```

Ruff should exit successfully without lint findings. Pytest should report passing
tests; the [shared fixtures](../tests/conftest.py) create temporary SQLite data
and use mock AI. The frontend check runs TypeScript and Vite and writes a build
under `frontend/dashboard/dist`; it does not start a server. These are focused
checks, not proof of production behavior. Select related tests from the source
map when changing another area.

For the broader non-browser suite, CI uses `pytest -m "not chromium"`; see the
[CI workflow](../.github/workflows/ci.yml). PostgreSQL concurrency tests require
`TEST_POSTGRES_URL` and create/drop test schemas and a vector extension, so only
point that variable at a disposable test database. Without it, those tests skip.

See [Tests](../README.md#tests) for browser checks. The
[Chromium helper](../scripts/run_chromium_tests.sh) installs npm dependencies,
builds the dashboard, and runs the workflow and recipe suites in both local and
Docker modes; it is not a read-only check. CI uses that same helper, with backend,
frontend-build, and browser checks reported as independent jobs.
`arsenal test-backend` runs all of `tests`, including browser-marked tests;
use explicit pytest selections when you need a narrower check.

## Limits and troubleshooting

- Missing CLI or stale local dashboard: follow [Troubleshooting](../README.md#troubleshooting)
  and [local dashboard recovery](../README.md#local-vs-live-dashboard).
- Mock AI is deterministic and offline, but does not produce useful archive
  metadata. Real classification and remix calls need provider credentials and quota;
  see [AI Classification](../README.md#ai-classification) and [Remix](../README.md#remix).
- Local static mode does not expose all online metadata-editing, public-sharing,
  and saved-remix deletion controls. Use the API-backed deployment for those
  features. A non-editable Python package alone does not bundle the full React
  dashboard and migration workflow; use the documented checkout/container paths.
- Local SQLite tests do not establish PostgreSQL concurrency or Redis behavior.
  Browser emulation does not establish physical-device playback; see
  [playback diagnostics](online-architecture.md#playback-and-usage-diagnostics).
- For a handoff, record changed files, exact checks run, skips, and what remains
  unverified. Keep provider calls and deployment separate from offline validation.
