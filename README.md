# Grok2API Home Assistant App

Home Assistant OS add-on repository packaging [chenyme/grok2api](https://github.com/chenyme/grok2api) v3.1.8
as an OpenAI-compatible API gateway backed by **Grok Web Chat** sessions, plus an internal
[FlareSolverr](https://github.com/Flaresolverr/Flaresolverr) v3.3.25 add-on used to solve
Cloudflare challenges for those sessions.

This repository contains **only the packaging layer** (manifests, Dockerfiles, entrypoint/bootstrap
scripts, translations, icon/logo). The server binary, Web UI bundle and provider integration code are
taken unmodified from the upstream Docker images pinned in `build.yaml`.

## Components

| Component | Path | Role |
| --- | --- | --- |
| `grok2api` | `grok2api/` | Home Assistant OS add-on wrapping upstream `ghcr.io/chenyme/grok2api:v3.1.8-*` |
| `flaresolverr` | `flaresolverr/` | Internal Home Assistant OS add-on wrapping upstream `ghcr.io/flaresolverr/flaresolverr:v3.3.25` |
| Tests | `tests/integration/` | Integration harness that runs the real Go binary (no mocks) |
| Branding | `tools/make_branding.py` | Generates `grok2api/icon.png` and `grok2api/logo.png` |

## What Grok2API does

Grok2API is an API gateway that accepts OpenAI-style requests (`/v1/models`,
`/v1/chat/completions`, streaming SSE) and forwards them to `https://grok.com` using a stored
**Grok Web session** (SSO cookie + `cf_clearance` + browser user agent). It exposes an admin API
and a Web UI for managing accounts, client API keys, models and runtime settings.

Authentication model: **Grok Web sessions/cookies**, NOT Grok/xAI OAuth. This is distinct from
OAuth-based proxies such as CLIProxyAPI.

## FlareSolverr role

Grok Web sits behind Cloudflare bot protection. FlareSolverr is an optional, internal-only
Cloudflare challenge proxy used by Grok2API (when `providerWeb.clearanceMode = flaresolverr`) to
obtain a fresh `cf_clearance` cookie + browser user agent. It does **not** bypass every Cloudflare
challenge and is not a general-purpose Cloudflare bypass; it is a local helper for the specific
Grok Web clearance flow. See `flaresolverr/README.md`.

## Deployment (Home Assistant OS)

1. Copy this repository folder into the HAOS `addons/` directory (via Samba/SSH add-on), or add
   it as a Custom Repository in the Add-on Store:
   `https://github.com/quangtienhn/grok2api-haos-app`
2. Add-on Store -> **Grok2API** -> **Install** (and **FlareSolverr** if you want Cloudflare
   clearance automation) -> **Start**.
3. Grok2API Configuration tab: set a strong `admin_password` (8+ chars). Keep
   `bootstrap_client_key: true`.
4. Open the Web UI (port 8000), import your Grok Web SSO cookie under **Accounts**, then
   **API keys -> reveal** to obtain the client key.
5. Point any OpenAI-compatible client at `http://<home-assistant-ip>:8000/v1`.

Detailed guide: `grok2api/DOCS.md`. Vietnamese install guide: see the archived reports.

## Security notes

- No Home Assistant Ingress: the SPA uses absolute asset/API paths, so the add-on is exposed
  directly on LAN port 8000 via `webui` + `watchdog`.
- Admin password is the source of truth (options win); never generated silently.
- JWT secret and credential encryption key are generated once into
  `/data/config/secrets.env` (mode 0600). Back them up together with the database.
- Credentials in SQLite are encrypted **at field level** (AES-256-GCM); the `.db` file is
  **not** full-file encrypted. Do not share the database file.
- No secret is ever logged.
- Audit ledger runs in `enforce` mode: if it cannot write, the request is rejected.

## Repository layout

```
Grok2API-HAOS-App/
  README.md
  LICENSE
  .gitignore
  repository.yaml
  grok2api/        # HAOS add-on (manifest, Dockerfile, rootfs, translations, icon/logo)
  flaresolverr/    # HAOS add-on (FlareSolverr wrapper)
  tests/           # integration harness (real binary, no mocks)
  tools/           # make_branding.py
```

## Status / known limitations

- Not verified on Docker / Supervisor / HAOS (dev machine has no Docker/WSL2).
- Not verified against real grok.com (no production Grok credentials used in testing).
- Not benchmarked on Intel N150 (numbers are from Windows amd64).
- Statsig signer defaults to a third-party public signer (`https://grok.wodf.de/sign`); can be
  switched to `manual` or a private signer in the Web UI.

## Development and tests

Prerequisites for running the integration harness (`tests/integration/run_integration.py`):

- Python 3.10+
- A real Grok2API binary placed at `.build/grok2api-windows-amd64.exe` (or passed via
  `--binary`). Linux/macOS binaries are not exercised by this harness.
- `jq` on `PATH` (or in `.tools/`).
- Git Bash on Windows (the harness shells out to `bash` for the HAOS scripts).

The harness runs the **real** Go binary and the **real** bash scripts; it does not use mocks.
It never contacts grok.com and uses a fake marker token for the account import test.

```bash
python tests/integration/run_integration.py
```

Expected: `37 tests, 37 PASS, 0 FAIL, 0 NOT VERIFIED` (on a developer machine with Docker/HAOS
not available, Docker-dependent items are reported separately).

## License

MIT. See `LICENSE` (includes upstream credit to Chenyme). Upstream `chenyme/grok2api` v3.1.8
remains under its own MIT license and copyright.
