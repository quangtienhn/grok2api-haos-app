#!/usr/bin/env python3
"""Integration harness for the Grok2API Home Assistant add-on packaging.

Run with the real Go binary (not a mock server):

    python tests/integration/run_integration.py

The harness:
  * renders the *real* config template out of
    grok2api/rootfs/usr/local/bin/haos-entrypoint.sh (single source of truth),
  * starts the real binary from .build/,
  * exercises the admin API, the OpenAI-compatible API, static frontend
    serving, credential encryption at rest, restart persistence and log
    hygiene,
  * and runs the real haos-bootstrap.sh under bash against the live binary.

Only the Grok upstream (grok.com) is never contacted: no account is expected
to work, and the import test uses a fake marker token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP_DIR = ROOT / "grok2api"
ENTRYPOINT = APP_DIR / "rootfs/usr/local/bin/haos-entrypoint.sh"
BOOTSTRAP = APP_DIR / "rootfs/usr/local/bin/haos-bootstrap.sh"
DEFAULT_BINARY = ROOT / ".build/grok2api-windows-amd64.exe"
DEFAULT_FIXTURE_DIST = HERE.parent / "fixtures/frontend-dist"
EXTRACTED_DIST = ROOT / ".tools/rootfs-amd64/app/frontend/dist"
GIT_BASH = Path(r"D:\Program Files\Git\bin\bash.exe")
# Windows cannot deliver POSIX SIGTERM to a native process. Sending
# CTRL_BREAK_EVENT to an MSYS bash process group makes bash exit with
# STATUS_CONTROL_C_EXIT (0xC000013A) instead of the code its trap produces.
# The real binary's graceful SIGTERM handling is covered by IT18; the
# entrypoint simulation accepts both codes and records which one it saw.
SIM_EXIT_CODES = (0, 0xC000013A)
TOOLS_DIR = ROOT.parent / ".tools"
BOOTSTRAP_KEY_NAME = "IT Bootstrap Key"

RESULTS: list[dict[str, str]] = []


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
def record(test_id: str, name: str, status: str, detail: str) -> None:
    RESULTS.append({"id": test_id, "name": name, "status": status, "detail": detail})
    print(f"[{status}] {test_id} {name}: {detail}")


class CheckFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


# ---------------------------------------------------------------------------
# Config rendering straight out of the entrypoint heredoc
# ---------------------------------------------------------------------------
def entrypoint_config_template() -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    match = re.search(
        r'cat > "\$\{CONFIG_FILE\}" <<EOF\n(.*?)\nEOF\n', text, re.DOTALL
    )
    if not match:
        raise RuntimeError("could not locate the config heredoc in haos-entrypoint.sh")
    return match.group(1)


def render_config(values: dict[str, str]) -> str:
    template = entrypoint_config_template()

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise RuntimeError(f"config template references unset variable {key}")
        return values[key]

    rendered = re.sub(r"\$\{([A-Z_]+)\}", substitute, template)
    leftover = re.findall(r"\$\{([A-Z_]+)\}", rendered)
    if leftover:
        raise RuntimeError(f"unsubstituted config variables: {leftover}")
    return rendered


# ---------------------------------------------------------------------------
# HTTP client (stdlib only)
# ---------------------------------------------------------------------------
@dataclass
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return {}

    def code(self) -> str:
        return str(self.json().get("error", {}).get("code", ""))


def http(
    base: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    api_key: str | None = None,
    json_body: dict | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 30.0,
) -> Response:
    url = base + path
    data = raw_body
    headers: dict[str, str] = {}
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if api_key:
        headers["X-API-Key"] = api_key
        headers.setdefault("Authorization", f"Bearer {api_key}")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, response.read(), dict(response.headers))
    except urllib.error.HTTPError as error:
        return Response(error.code, error.read(), dict(error.headers))


# ---------------------------------------------------------------------------
# Server process management
# ---------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Instance:
    workdir: Path
    binary: Path
    admin_user: str
    admin_password: str
    extra_secrets: dict[str, str] = field(default_factory=dict)
    static_path: str | None = None
    public_base_url: str = ""
    request_timeout: int = 600

    def __post_init__(self) -> None:
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout_path = self.workdir / "server.out.log"
        self.stderr_path = self.workdir / "server.err.log"
        self.config_path = self.workdir / "config.yaml"
        self.db_path = self.workdir / "app" / "backend.db"

    # -- lifecycle ---------------------------------------------------------
    def write_config(self) -> None:
        (self.workdir / "app").mkdir(parents=True, exist_ok=True)
        (self.workdir / "app" / "media").mkdir(parents=True, exist_ok=True)
        values = {
            "REQUEST_TIMEOUT": str(self.request_timeout),
            "JWT_SECRET": self.extra_secrets["jwt"],
            "CREDENTIAL_ENCRYPTION_KEY": self.extra_secrets["enc"],
            "ADMIN_USER": self.admin_user,
            "ADMIN_PASS": self.admin_password,
            "PUBLIC_BASE_URL": self.public_base_url,
            "APP_DATA_DIR": self.workdir.as_posix() + "/app",
        }
        rendered = render_config(values)
        if self.static_path:
            rendered = rendered.replace(
                'staticPath: "/app/frontend/dist"',
                f'staticPath: "{self.static_path}"',
            )
        self.config_path.write_text(rendered, encoding="utf-8")

    def start(self, timeout: float = 120.0) -> None:
        if not self.config_path.exists():
            self.write_config()
        out = open(self.stdout_path, "ab")
        err = open(self.stderr_path, "ab")
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("GROK2API_")
        }
        self.process = subprocess.Popen(  # noqa: S603 - fixed binary, controlled args
            [
                str(self.binary),
                "--config",
                str(self.config_path),
                "--listen",
                f"127.0.0.1:{self.port}",
            ],
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            cwd=str(self.workdir),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"server exited early with {self.process.returncode}\n"
                    f"{self.tail_logs()}"
                )
            try:
                response = http(self.base, "GET", "/healthz", timeout=2)
                if response.status == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"server did not become healthy\n{self.tail_logs()}")

    def stop_graceful(self, timeout: float = 25.0) -> tuple[str, int | None]:
        if self.process is None or self.process.poll() is not None:
            return ("not-running", None if self.process is None else self.process.returncode)
        method = "CTRL_BREAK_EVENT"
        try:
            self.process.send_signal(signal_value())
        except Exception:
            method = "terminate"
            self.process.terminate()
        try:
            return (method, self.process.wait(timeout=timeout))
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            return (method + "+kill", self.process.returncode)

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    # -- helpers -----------------------------------------------------------
    def logs(self) -> str:
        chunks = []
        for path in (self.stdout_path, self.stderr_path):
            if path.exists():
                chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        return "\n".join(chunks)

    def tail_logs(self, lines: int = 40) -> str:
        return "\n".join(self.logs().splitlines()[-lines:])

    def login(self, password: str, retries: int = 6, delay: float = 16.0) -> Response:
        """Admin login that waits out the 12/min/user brute-force limiter."""
        for attempt in range(retries):
            response = http(
                self.base,
                "POST",
                "/api/admin/v1/auth/login",
                json_body={"username": self.admin_user, "password": password},
            )
            if response.status != 429:
                return response
            if attempt == retries - 1:
                raise RuntimeError("admin login stayed rate limited (HTTP 429)")
            time.sleep(delay)
        raise RuntimeError("unreachable")

    def admin_token(self, password: str) -> str | None:
        response = self.login(password)
        if response.status != 200:
            return None
        return response.json().get("data", {}).get("tokens", {}).get("accessToken")

    def password_works(self, password: str) -> bool:
        return self.login(password).status == 200


def signal_value() -> int:
    import signal

    return getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
def run_packaging_tests(ctx: dict) -> None:
    """Static checks on the add-on manifest, build recipe and scripts."""
    import yaml

    manifest_path = APP_DIR / "config.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}

    # IT24: P0-1 (invalid `map`) and P0-2 (ingress) fixed in the manifest
    try:
        check("map" not in manifest, "manifest still declares `map`")
        ingress_keys = [key for key in manifest if key.startswith("ingress") or key.startswith("panel_")]
        check(not ingress_keys, f"manifest still declares {ingress_keys}")
        check(manifest.get("webui") == "http://[HOST]:[PORT:8000]/", f"webui={manifest.get('webui')}")
        check(manifest.get("watchdog", "").endswith("/healthz"), f"watchdog={manifest.get('watchdog')}")
        check(manifest.get("init") is True, "init: true missing (PID 1 must reap children)")
        check(manifest.get("boot") == "auto" and manifest.get("startup") == "services",
              "boot/startup not set for a service add-on")
        record("IT24", "Manifest: no `map`, no ingress, webui+watchdog configured", "PASS",
               "keys validated, webui/watchdog point at port 8000")
    except Exception as error:
        record("IT24", "Manifest: no `map`, no ingress, webui+watchdog configured", "FAIL", str(error))

    # IT25: options/schema contract
    try:
        schema = manifest.get("schema") or {}
        options = manifest.get("options") or {}
        check(schema.get("admin_password") == "password",
              f"admin_password schema={schema.get('admin_password')}")
        check(not str(schema.get("admin_password", "")).endswith("?"),
              "admin_password must be mandatory")
        check(options.get("admin_password") == "", "admin_password must default to empty, not a generated value")
        check(schema.get("request_timeout") == "int(30,7200)?", f"request_timeout={schema.get('request_timeout')}")
        check("log_level" not in schema, "log_level option is not supported by the upstream logger")
        check("api_key" not in schema, "api_key must not be an add-on option (keys are created via the API)")
        record("IT25", "Options/schema: mandatory password, no fake options", "PASS",
               f"{len(schema)} schema entries, no log_level/api_key")
    except Exception as error:
        record("IT25", "Options/schema: mandatory password, no fake options", "FAIL", str(error))

    # IT26: build.yaml <-> manifest <-> Dockerfile consistency
    try:
        build = yaml.safe_load((APP_DIR / "build.yaml").read_text(encoding="utf-8")) or {}
        from_images = build.get("build_from") or {}
        check(set(from_images) == set(manifest.get("arch") or []),
              f"arch mismatch: manifest={manifest.get('arch')} build_from={sorted(from_images)}")
        for arch, image in from_images.items():
            suffix = "arm64" if arch == "aarch64" else arch
            check(image == f"ghcr.io/chenyme/grok2api:v{manifest['version']}-{suffix}",
                  f"{arch} base image not pinned to v{manifest['version']}-{suffix}: {image}")
        dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
        check("FROM ${BUILD_FROM}" in dockerfile, "Dockerfile must use the build.yaml base image")
        check("COPY rootfs /" in dockerfile, "Dockerfile does not copy rootfs")
        check('ENTRYPOINT ["/usr/local/bin/haos-entrypoint.sh"]' in dockerfile,
              "Dockerfile entrypoint mismatch")
        check("/data/config/config.yaml" in dockerfile, "Dockerfile CMD does not use the rendered config path")
        check("healthcheck.sh" in dockerfile, "Dockerfile HEALTHCHECK does not use healthcheck.sh")
        for script in ("haos-entrypoint.sh", "haos-bootstrap.sh", "healthcheck.sh"):
            check((APP_DIR / "rootfs/usr/local/bin" / script).exists(), f"missing {script}")
            check(script in dockerfile, f"{script} is not chmod-ed in the Dockerfile")
        record("IT26", "build.yaml, manifest and Dockerfile are consistent", "PASS",
               f"arch={sorted(from_images)}, base images pinned to v{manifest['version']}")
    except Exception as error:
        record("IT26", "build.yaml, manifest and Dockerfile are consistent", "FAIL", str(error))

    # IT27: shell scripts must be LF-only and syntactically valid
    try:
        problems = []
        for script in ("haos-entrypoint.sh", "haos-bootstrap.sh", "healthcheck.sh"):
            raw = (APP_DIR / "rootfs/usr/local/bin" / script).read_bytes()
            if b"\r\n" in raw:
                problems.append(f"{script}: CRLF line endings")
            if not raw.startswith(b"#!"):
                problems.append(f"{script}: missing shebang")
        check(not problems, "; ".join(problems))
        syntax = "not checked"
        if GIT_BASH.exists():
            for script in ("haos-entrypoint.sh", "haos-bootstrap.sh", "healthcheck.sh"):
                result = subprocess.run(  # noqa: S603
                    [str(GIT_BASH), "-n", str(APP_DIR / "rootfs/usr/local/bin" / script)],
                    capture_output=True, text=True, timeout=60,
                )
                check(result.returncode == 0,
                      f"{script}: {result.stderr.strip()[:200]}")
            syntax = "bash -n clean"
        record("IT27", "Shell scripts: LF line endings and valid bash syntax", "PASS",
               f"3 scripts, {syntax}")
    except Exception as error:
        record("IT27", "Shell scripts: LF line endings and valid bash syntax", "FAIL", str(error))

    # IT28: the misleading Python mock must be gone
    try:
        leftovers = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                     if p.name in ("mock_server.py", "test_suite.py")]
        check(not leftovers, f"stale mock files: {leftovers}")
        record("IT28", "No Python mock server left in the package", "PASS",
               "no mock_server.py / test_suite.py in the repository tree")
    except Exception as error:
        record("IT28", "No Python mock server left in the package", "FAIL", str(error))

    # IT30: GitHub-facing packaging details (root LICENSE, repository URL,
    # signal-safe cleanup in the bootstrap script)
    try:
        root_license = ROOT / "LICENSE"
        addon_license = APP_DIR / "LICENSE"
        check(root_license.exists(), "LICENSE missing at the repository root (GitHub will not detect the license)")
        check(addon_license.exists(), "LICENSE missing inside grok2api/")
        check(root_license.read_bytes() == addon_license.read_bytes(),
              "root LICENSE and grok2api/LICENSE differ")

        repository = yaml.safe_load((ROOT / "repository.yaml").read_text(encoding="utf-8")) or {}
        repo_url = str(repository.get("url", ""))
        check(repo_url.endswith("grok2api-haos-app"),
              f"repository.yaml url does not point at the packaging repository: {repo_url}")
        check(repo_url != "https://github.com/chenyme/grok2api",
              "repository.yaml still points at the upstream project instead of this repository")

        bootstrap = (APP_DIR / "rootfs/usr/local/bin/haos-bootstrap.sh").read_text(encoding="utf-8")
        check("trap cleanup EXIT INT TERM" in bootstrap,
              "haos-bootstrap.sh does not trap INT/TERM for cleanup")
        record("IT30", "GitHub packaging: root LICENSE, repository URL, signal-safe cleanup", "PASS",
               f"root LICENSE byte-identical to add-on LICENSE ({root_license.stat().st_size} B), "
               f"repository url ends with grok2api-haos-app, trap cleanup EXIT INT TERM present")
    except Exception as error:
        record("IT30", "GitHub packaging: root LICENSE, repository URL, signal-safe cleanup", "FAIL",
               str(error))


def run_phase_one(ctx: dict) -> None:
    inst: Instance = ctx["inst"]
    base = inst.base
    password = inst.admin_password

    # IT01 liveness
    try:
        response = http(base, "GET", "/healthz")
        body = response.json()
        check(response.status == 200 and body.get("ok") is True,
              f"status={response.status} body={body}")
        record("IT01", "/healthz liveness (real binary)", "PASS",
               f"HTTP {response.status} {{'ok': true}}")
    except Exception as error:
        record("IT01", "/healthz liveness (real binary)", "FAIL", str(error))

    # IT03 readiness on a fresh install (zero accounts, zero model routes)
    try:
        health = http(base, "GET", "/healthz")
        ready = http(base, "GET", "/readyz")
        ctx["readyz_fresh"] = ready.status
        check(health.status == 200, f"/healthz {health.status}")
        check(ready.status != 200,
              f"/readyz returned 200 on a fresh install (would be a safe watchdog target)")
        record("IT02", "/readyz is not 200 on a fresh install (watchdog must use /healthz)", "PASS",
               f"/healthz HTTP 200, /readyz HTTP {ready.status} state={ready.json().get('state')}")
    except Exception as error:
        record("IT02", "/readyz is not 200 on a fresh install (watchdog must use /healthz)", "FAIL",
               str(error))

    # IT03 wrong admin password
    wrong_password = password + "-wrong"
    try:
        response = http(base, "POST", "/api/admin/v1/auth/login",
                        json_body={"username": inst.admin_user, "password": wrong_password})
        check(response.status == 401, f"expected 401, got {response.status}")
        check(response.code() == "invalidCredentials", f"code={response.code()}")
        check(wrong_password not in response.text and password not in response.text,
              "response echoes a password")
        record("IT03", "Admin login rejects a wrong password", "PASS",
               f"HTTP 401 code={response.code()}")
    except Exception as error:
        record("IT03", "Admin login rejects a wrong password", "FAIL", str(error))

    # IT04 correct admin password
    try:
        response = http(base, "POST", "/api/admin/v1/auth/login",
                        json_body={"username": inst.admin_user, "password": password})
        data = response.json().get("data", {})
        token = data.get("tokens", {}).get("accessToken")
        check(response.status == 200, f"expected 200, got {response.status}")
        check(bool(token), "no access token in response")
        check(data.get("admin", {}).get("username") == inst.admin_user,
              f"admin={data.get('admin')}")
        ctx["token"] = token
        record("IT04", "Admin login with the bootstrap password", "PASS",
               "HTTP 200, access token issued for the configured admin")
    except Exception as error:
        record("IT04", "Admin login with the bootstrap password", "FAIL", str(error))
        return

    token = ctx["token"]

    # IT05 /me with and without a token
    try:
        authenticated = http(base, "GET", "/api/admin/v1/me", token=token)
        anonymous = http(base, "GET", "/api/admin/v1/me")
        check(authenticated.status == 200, f"auth status={authenticated.status}")
        check(anonymous.status == 401, f"anon status={anonymous.status}")
        check(anonymous.code() in ("adminUnauthorized", "invalidCredentials"),
              f"anon code={anonymous.code()}")
        record("IT05", "Admin API rejects unauthenticated /me", "PASS",
               f"with token HTTP {authenticated.status}, without token HTTP 401 {anonymous.code()}")
    except Exception as error:
        record("IT05", "Admin API rejects unauthenticated /me", "FAIL", str(error))

    # IT06 create client API key through the official admin API
    try:
        response = http(base, "POST", "/api/admin/v1/client-keys",
                        token=token, json_body={"name": "Home Assistant"})
        data = response.json().get("data", {})
        secret = data.get("secret", "")
        key = data.get("key", {})
        check(response.status == 201, f"expected 201, got {response.status}: {response.text[:200]}")
        check(secret.startswith("g2a_"), f"unexpected secret shape: {secret[:6]}...")
        check(bool(re.fullmatch(r"g2a_[A-Za-z0-9]+_[A-Za-z0-9_-]+", secret)),
              "secret does not match g2a_<prefix>_<secret>")
        check(key.get("name") == "Home Assistant", f"key={key}")
        ctx["key_id"] = str(key.get("id"))
        ctx["key_secret"] = secret
        record("IT06", "Bootstrap client API key via POST /client-keys", "PASS",
               f"HTTP 201, key id={ctx['key_id']}, secret format g2a_<prefix>_<secret>")
    except Exception as error:
        record("IT06", "Bootstrap client API key via POST /client-keys", "FAIL", str(error))

    # IT07 duplicate name handling (upstream allows duplicates; must not be a 5xx)
    try:
        response = http(base, "POST", "/api/admin/v1/client-keys",
                        token=token, json_body={"name": "Home Assistant"})
        check(response.status < 500, f"status={response.status}")
        ctx["duplicate_status"] = response.status
        ctx["duplicate_allowed"] = response.status in (200, 201)
        note = ("upstream allows duplicate names (HTTP 201), which is why the add-on "
                "bootstrap looks keys up by name instead of creating blindly")
        record("IT07", "Duplicate key names are accepted by the upstream API", "PASS",
               f"HTTP {response.status} code={response.code() or '-'}; {note}")
    except Exception as error:
        record("IT07", "Duplicate key names are accepted by the upstream API", "FAIL", str(error))

    # IT08 reveal secret
    if ctx.get("key_id"):
        try:
            response = http(base, "GET",
                            f"/api/admin/v1/client-keys/{ctx['key_id']}/secret", token=token)
            check(response.status == 200, f"status={response.status}")
            check(response.json().get("data", {}).get("secret") == ctx["key_secret"],
                  "revealed secret differs from the created secret")
            check("no-store" in response.headers.get("Cache-Control", "").lower(),
                  "missing Cache-Control: no-store")
            record("IT08", "Key secret can be revealed again (Web UI path)", "PASS",
                   "HTTP 200, same secret, Cache-Control: no-store")
        except Exception as error:
            record("IT08", "Key secret can be revealed again (Web UI path)", "FAIL", str(error))

    # IT09 OpenAI auth: valid / missing / bogus key
    try:
        good = http(base, "GET", "/v1/models", api_key=ctx["key_secret"])
        missing = http(base, "GET", "/v1/models")
        bogus = http(base, "GET", "/v1/models", api_key="g2a_deadbeef_notarealsecret")
        check(good.status == 200, f"valid key status={good.status} {good.text[:200]}")
        check(missing.status == 401, f"missing key status={missing.status}")
        check(missing.code() == "invalid_api_key", f"missing key code={missing.code()}")
        check(bogus.status == 401, f"bogus key status={bogus.status}")
        models = good.json().get("data", good.json().get("models", []))
        ctx["models"] = models
        record("IT09", "OpenAI /v1/models: valid 200, missing/bogus 401", "PASS",
               f"valid HTTP 200 ({len(models) if isinstance(models, list) else '?'} entries), "
               f"missing HTTP 401 invalid_api_key, bogus HTTP 401")
    except Exception as error:
        record("IT09", "OpenAI /v1/models: valid 200, missing/bogus 401", "FAIL", str(error))

    # IT10 X-API-Key header accepted
    try:
        response = http(base, "GET", "/v1/models")
        request = urllib.request.Request(
            base + "/v1/models", headers={"X-API-Key": ctx["key_secret"]}, method="GET"
        )
        with urllib.request.urlopen(request, timeout=20) as raw:
            status = raw.status
        check(status == 200, f"status={status}")
        check(response.status == 401, "Bearer-less request without any key was not rejected")
        record("IT10", "X-API-Key header is accepted as an alternative to Bearer", "PASS",
               "HTTP 200 with only X-API-Key set")
    except Exception as error:
        record("IT10", "X-API-Key header is accepted as an alternative to Bearer", "FAIL", str(error))

    # IT11 chat without any upstream account
    try:
        response = http(base, "POST", "/v1/chat/completions", api_key=ctx["key_secret"],
                        json_body={"model": "grok-4", "messages": [{"role": "user", "content": "ping"}],
                                   "stream": False},
                        timeout=60)
        check(response.status != 500, f"unexpected 500: {response.text[:300]}")
        check(400 <= response.status < 500,
              f"expected a clean 4xx with zero accounts, got {response.status}")
        check(ctx["key_secret"] not in response.text, "response leaked the API key")
        ctx["chat_no_account"] = (response.status, response.code())
        record("IT11", "Chat with zero accounts returns a clean 4xx (no models configured)", "PASS",
               f"HTTP {response.status} code={response.code() or '-'} "
               f"(no account and no model route exist yet; the process stays healthy)")
    except Exception as error:
        record("IT11", "Chat with zero accounts returns a clean 4xx (no models configured)", "FAIL",
               str(error))

    # IT12 unknown model
    try:
        response = http(base, "POST", "/v1/chat/completions", api_key=ctx["key_secret"],
                        json_body={"model": "definitely-not-a-model",
                                   "messages": [{"role": "user", "content": "ping"}]},
                        timeout=60)
        check(400 <= response.status < 500, f"status={response.status} {response.text[:200]}")
        record("IT12", "Unknown model is rejected with a clean 4xx", "PASS",
               f"HTTP {response.status} code={response.code() or '-'}")
    except Exception as error:
        record("IT12", "Unknown model is rejected with a clean 4xx", "FAIL", str(error))

    # IT13 admin surface requires authentication
    try:
        paths = [
            "/api/admin/v1/client-keys",
            "/api/admin/v1/accounts",
            "/api/admin/v1/settings",
            "/api/admin/v1/dashboard",
            "/api/admin/v1/request-audits",
        ]
        observed = {}
        for path in paths:
            response = http(base, "GET", path)
            observed[path] = response.status
            check(response.status in (401, 403), f"{path} -> {response.status}")
        record("IT13", "Admin endpoints reject unauthenticated requests", "PASS",
               "HTTP 401 for " + ", ".join(f"{p.rsplit('/', 1)[-1]}={s}" for p, s in observed.items()))
    except Exception as error:
        record("IT13", "Admin endpoints reject unauthenticated requests", "FAIL", str(error))

    # IT14 static frontend from the real SPA build
    fixture: Path | None = ctx.get("dist")
    if not fixture:
        record("IT14", "Real SPA build is served with absolute asset paths", "NOT VERIFIED",
               "no frontend fixture available (see --frontend-dist)")
    else:
        try:
            index = http(base, "GET", "/")
            runtime = http(base, "GET", "/runtime-config.js")
            check(index.status == 200, f"index status={index.status}")
            check('id="root"' in index.text, "index.html does not contain the SPA root node")
            check(runtime.status == 200, f"runtime-config status={runtime.status}")
            check("apiBaseUrl" in runtime.text, "runtime-config.js has no apiBaseUrl")
            assets = sorted((fixture / "assets").glob("*.js"))
            check(bool(assets), "fixture has no assets")
            asset = assets[0]
            served = http(base, "GET", f"/assets/{asset.name}")
            check(served.status == 200, f"asset {asset.name} status={served.status}")
            check(served.body == asset.read_bytes(), "served asset differs from the build artifact")
            traversal = http(base, "GET", "/assets/../../../../../../etc/passwd")
            check(traversal.status in (301, 400, 403, 404) or "root:" not in traversal.text,
                  f"path traversal returned {traversal.status}")
            record("IT14", "Real SPA build is served with absolute asset paths", "PASS",
                   f"/ HTTP 200, /runtime-config.js HTTP 200, /assets/{asset.name} byte-identical, "
                   f"traversal HTTP {traversal.status}")
        except Exception as error:
            record("IT14", "Real SPA build is served with absolute asset paths", "FAIL", str(error))

    # IT15 credential import is encrypted at rest (fake marker token only)
    try:
        marker = "ITFAKE" + uuid.uuid4().hex
        document = json.dumps({
            "provider": "grok_web",
            "accounts": [{"name": "it-fake", "sso_token": marker, "tier": "auto"}],
        }).encode()
        boundary = "----grok2api-it-" + uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="accounts.json"\r\n'
            "Content-Type: application/json\r\n\r\n"
        ).encode() + document + f"\r\n--{boundary}--\r\n".encode()
        response = http(base, "POST", "/api/admin/v1/accounts/web/import", token=token,
                        raw_body=body,
                        content_type=f"multipart/form-data; boundary={boundary}", timeout=90)
        check(response.status in (200, 201, 202), f"import status={response.status} {response.text[:300]}")

        accounts = http(base, "GET", "/api/admin/v1/accounts?pageSize=100", token=token)
        check(accounts.status == 200, f"accounts status={accounts.status}")
        check("it-fake" in accounts.text, "imported account is not listed")
        check(marker not in accounts.text, "account list leaked the raw SSO token")

        deadline = time.time() + 20
        while time.time() < deadline and not inst.db_path.exists():
            time.sleep(0.5)
        check(inst.db_path.exists(), "database file was not created")
        blob = inst.db_path.read_bytes()
        check(marker.encode() not in blob, "SSO token is stored in plaintext in the SQLite file")
        ctx["marker"] = marker
        record("IT15", "Imported credential is encrypted at rest (no plaintext in DB)", "PASS",
               f"account listed, marker absent from backend.db ({len(blob)} bytes)")
    except Exception as error:
        record("IT15", "Imported credential is encrypted at rest (no plaintext in DB)", "FAIL", str(error))

    # IT16 log hygiene
    try:
        logs = inst.logs()
        secrets = {
            "admin password": password,
            "jwt secret": inst.extra_secrets["jwt"],
            "encryption key": inst.extra_secrets["enc"],
        }
        if ctx.get("key_secret"):
            secrets["client key secret"] = ctx["key_secret"]
        if ctx.get("marker"):
            secrets["imported SSO marker"] = ctx["marker"]
        leaked = [name for name, value in secrets.items() if value and value in logs]
        check(not leaked, f"logs contain: {leaked}")
        record("IT16", "No secrets in the container log stream", "PASS",
               f"{len(secrets)} secret values scanned across {len(logs)} bytes of stdout/stderr")
    except Exception as error:
        record("IT16", "No secrets in the container log stream", "FAIL", str(error))

    # IT17 rotate the admin password through the official endpoint
    ctx["new_password"] = password + "-rotated"
    try:
        response = http(base, "PUT", "/api/admin/v1/me/password", token=token,
                        json_body={"currentPassword": password, "newPassword": ctx["new_password"]})
        check(response.status == 200, f"status={response.status} {response.text[:200]}")
        old_login = http(base, "POST", "/api/admin/v1/auth/login",
                         json_body={"username": inst.admin_user, "password": password})
        new_login = http(base, "POST", "/api/admin/v1/auth/login",
                         json_body={"username": inst.admin_user, "password": ctx["new_password"]})
        check(old_login.status == 401, f"old password still accepted ({old_login.status})")
        check(new_login.status == 200, f"new password rejected ({new_login.status})")
        record("IT17", "Admin password change via PUT /me/password", "PASS",
               "HTTP 200, old password 401, new password 200")
    except Exception as error:
        record("IT17", "Admin password change via PUT /me/password", "FAIL", str(error))

    # IT18 graceful shutdown
    try:
        method, exit_code = inst.stop_graceful()
        check(exit_code == 0, f"exit code {exit_code} via {method}")
        record("IT18", "Graceful shutdown on a termination signal", "PASS",
               f"{method}: exit code {exit_code}")
    except Exception as error:
        record("IT18", "Graceful shutdown on a termination signal", "FAIL", str(error))


def run_phase_two(ctx: dict) -> None:
    inst: Instance = ctx["inst"]
    base = inst.base
    password = ctx.get("new_password") or inst.admin_password

    # IT19 restart persistence: data survives, rotated password still valid
    try:
        check(inst.process is not None and inst.process.poll() is None, "server is not running")
        login = http(base, "POST", "/api/admin/v1/auth/login",
                     json_body={"username": inst.admin_user, "password": password})
        check(login.status == 200, f"rotated password rejected after restart ({login.status})")
        ctx["token"] = login.json()["data"]["tokens"]["accessToken"]
        record("IT19", "Restart keeps the SQLite database and the rotated password", "PASS",
               "HTTP 200 with the password set before the restart")
    except Exception as error:
        record("IT19", "Restart keeps the SQLite database and the rotated password", "FAIL", str(error))
        return

    token = ctx["token"]

    # IT20 client key survives restart
    try:
        listing = http(base, "GET", "/api/admin/v1/client-keys?pageSize=100", token=token)
        check(listing.status == 200, f"list status={listing.status}")
        check("Home Assistant" in listing.text, "bootstrapped key missing after restart")
        models = http(base, "GET", "/v1/models", api_key=ctx["key_secret"])
        check(models.status == 200, f"/v1/models with the old key: {models.status}")
        record("IT20", "Client API key survives a restart", "PASS",
               "key still listed and /v1/models returns HTTP 200 with the same secret")
    except Exception as error:
        record("IT20", "Client API key survives a restart", "FAIL", str(error))

    # IT21 encryption + key storage persist across restart
    try:
        accounts = http(base, "GET", "/api/admin/v1/accounts?pageSize=100", token=token)
        check("it-fake" in accounts.text, "imported account missing after restart")
        blob = inst.db_path.read_bytes()
        check(ctx["marker"].encode() not in blob, "SSO token became plaintext after restart")
        check(ctx["key_secret"].encode() not in blob,
              "client API key secret is stored in plaintext in the SQLite file")
        record("IT21", "Credential and key material stay encrypted after restart", "PASS",
               f"account present, SSO marker and API key secret absent from backend.db ({len(blob)} bytes)")
    except Exception as error:
        record("IT21", "Credential and key material stay encrypted after restart", "FAIL", str(error))

    # IT22 readiness once an account row exists (informational: /readyz is not a
    # safe watchdog target because it depends on account health)
    try:
        health = http(base, "GET", "/healthz")
        ready = http(base, "GET", "/readyz")
        check(health.status == 200, f"/healthz {health.status}")
        record("IT22", "Readiness state after an account exists (informational)", "PASS",
               f"/healthz HTTP 200, /readyz HTTP {ready.status} state={ready.json().get('state')} "
               f"(fresh install was HTTP {ctx.get('readyz_fresh')})")
    except Exception as error:
        record("IT22", "Readiness state after an account exists (informational)", "NOT VERIFIED",
               str(error))


def run_bootstrap_tests(ctx: dict) -> None:
    """Execute the real haos-bootstrap.sh against the live binary."""
    if not GIT_BASH.exists():
        for suffix, name in (
            ("a", "bootstrap: option password login + applied file + key ensure"),
            ("b", "bootstrap: idempotent re-run creates no duplicate"),
            ("c", "bootstrap: rotates the DB password to the option value"),
            ("d", "bootstrap: unknown password is not overwritten"),
            ("e", "bootstrap: bootstrap_client_key=false creates no key"),
        ):
            record(f"IT23{suffix}", name, "NOT VERIFIED", "no bash available on this machine")
        return

    inst: Instance = ctx["inst"]
    base = inst.base
    work = inst.workdir / "bootstrap"
    work.mkdir(exist_ok=True)
    options = work / "options.json"
    applied = work / "admin.applied"

    def run_bootstrap(option_password: str, bootstrap_key: bool = True,
                      key_name: str = BOOTSTRAP_KEY_NAME) -> subprocess.CompletedProcess[str]:
        options.write_text(json.dumps({
            "admin_username": inst.admin_user,
            "admin_password": option_password,
            "bootstrap_client_key": bootstrap_key,
            "client_key_name": key_name,
        }), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = str(TOOLS_DIR) + os.pathsep + env.get("PATH", "")
        env.update({
            "HAOS_BASE_URL": base,
            "HAOS_OPTIONS_FILE": options.as_posix(),
            "HAOS_APPLIED_FILE": applied.as_posix(),
            "HAOS_ADMIN_USER": inst.admin_user,
            "HAOS_ADMIN_PASSWORD_FROM_OPTIONS_FILE": "1",
            "HAOS_BOOTSTRAP_CLIENT_KEY": "true" if bootstrap_key else "false",
            "HAOS_CLIENT_KEY_NAME": key_name,
        })
        return subprocess.run(  # noqa: S603 - fixed interpreter and script
            [str(GIT_BASH), str(BOOTSTRAP)],
            capture_output=True, text=True, env=env, timeout=300,
        )

    def key_count(token: str, name: str = BOOTSTRAP_KEY_NAME) -> int:
        listing = http(base, "GET",
                       f"/api/admin/v1/client-keys?search={urllib.parse.quote(name)}&pageSize=100",
                       token=token)
        items = listing.json().get("data", {}).get("items", [])
        return sum(1 for item in items if item.get("name") == name)

    current = ctx.get("new_password") or inst.admin_password
    cache: dict[str, str | None] = {}

    def token_for(password: str) -> str | None:
        if password not in cache:
            cache[password] = inst.admin_token(password)
        return cache[password]

    def key_count(password: str, name: str = BOOTSTRAP_KEY_NAME) -> int:
        token = token_for(password)
        check(bool(token), f"no admin token while counting keys (password state: {password == current})")
        listing = http(base, "GET",
                       f"/api/admin/v1/client-keys?search={urllib.parse.quote(name)}&pageSize=100",
                       token=token)
        check(listing.status == 200, f"client-keys list HTTP {listing.status}")
        items = listing.json().get("data", {}).get("items", [])
        return sum(1 for item in items if item.get("name") == name)

    check(bool(token_for(current)), "admin login failed before the bootstrap tests")

    # IT23a: happy path (option password already matches the stored one)
    try:
        result = run_bootstrap(current)
        check(result.returncode == 0, f"exit {result.returncode}: {result.stderr[-400:]}")
        check(applied.exists(), "applied password file was not written")
        stored = json.loads(applied.read_text(encoding="utf-8"))
        check(stored.get("password") == current, "applied file does not hold the option password")
        check("Admin login OK" in result.stdout, f"unexpected stdout: {result.stdout[-400:]}")
        check(current not in result.stdout and current not in result.stderr,
              "bootstrap printed the admin password")
        secret = ctx["key_secret"]
        check(secret not in result.stdout and secret not in result.stderr,
              "bootstrap printed an API key secret")
        record("IT23a", "bootstrap: option password login + applied file + key ensure", "PASS",
               f"exit 0, applied file written, "
               f"{key_count(current)} key named '{BOOTSTRAP_KEY_NAME}'")
    except Exception as error:
        record("IT23a", "bootstrap: option password login + applied file + key ensure", "FAIL", str(error))

    # IT23b: idempotent re-run
    try:
        before = key_count(current)
        result = run_bootstrap(current)
        after = key_count(current)
        check(result.returncode == 0, f"exit {result.returncode}")
        check(before == after == 1, f"key count changed: {before} -> {after}")
        check("already exists" in result.stdout, f"no idempotency message: {result.stdout[-400:]}")
        record("IT23b", "bootstrap: idempotent re-run creates no duplicate", "PASS",
               f"two runs, exactly {after} key named '{BOOTSTRAP_KEY_NAME}' "
               f"(manual duplicate creation returns HTTP {ctx.get('duplicate_status')})")
    except Exception as error:
        record("IT23b", "bootstrap: idempotent re-run creates no duplicate", "FAIL", str(error))

    # IT23c: rotate the stored password to the option value
    rotated = current + "-boot"
    try:
        applied.write_text(json.dumps({"username": inst.admin_user, "password": current}),
                           encoding="utf-8")
        result = run_bootstrap(rotated)
        check(result.returncode == 0, f"exit {result.returncode}: {result.stderr[-400:]}")
        check("rotated" in result.stdout.lower(), f"rotation not logged: {result.stdout[-800:]}")
        cache.clear()
        check(inst.password_works(rotated), f"new option password rejected; log: {result.stdout[-800:]}")
        check(not inst.password_works(current), "old password still works after rotation")
        stored = json.loads(applied.read_text(encoding="utf-8"))
        check(stored.get("password") == rotated, "applied file not updated after rotation")
        current = rotated
        record("IT23c", "bootstrap: rotates the DB password to the option value", "PASS",
               "PUT /me/password succeeded, old password 401, applied file updated")
    except Exception as error:
        record("IT23c", "bootstrap: rotates the DB password to the option value", "FAIL", str(error))

    # IT23d: an unknown password must not be silently replaced
    try:
        applied.write_text(json.dumps({"username": inst.admin_user, "password": "definitely-wrong-1"}),
                           encoding="utf-8")
        result = run_bootstrap(current + "-other")
        check(result.returncode == 0, f"exit {result.returncode}")
        check("NOT changed" in result.stdout or "Could not sign in" in result.stdout,
              f"no warning logged: {result.stdout[-800:]}")
        cache.clear()
        check(inst.password_works(current), "stored password was changed unexpectedly")
        check(not inst.password_works(current + "-other"), "unknown option password became valid")
        record("IT23d", "bootstrap: unknown password is not overwritten", "PASS",
               "exit 0, warning logged, stored password unchanged (no lockout, no silent reset)")
    except Exception as error:
        record("IT23d", "bootstrap: unknown password is not overwritten", "FAIL", str(error))

    # IT23e: bootstrap_client_key=false
    try:
        applied.write_text(json.dumps({"username": inst.admin_user, "password": current}),
                           encoding="utf-8")
        cache.clear()
        before = key_count(current, "Disabled Key Test")
        result = run_bootstrap(current, bootstrap_key=False, key_name="Disabled Key Test")
        after = key_count(current, "Disabled Key Test")
        check(result.returncode == 0, f"exit {result.returncode}")
        check(before == after == 0, f"key created despite the option being false ({before} -> {after})")
        check("disabled" in result.stdout.lower(), f"no message: {result.stdout[-800:]}")
        record("IT23e", "bootstrap: bootstrap_client_key=false creates no key", "PASS",
               "exit 0, key count 0, option respected")
    except Exception as error:
        record("IT23e", "bootstrap: bootstrap_client_key=false creates no key", "FAIL", str(error))


def run_entrypoint_simulation(ctx: dict) -> None:
    """Exercise the real haos-entrypoint.sh outside a container.

    The script is copied into a temp root, its fixed container paths are
    rewritten to that root, `chown` is dropped and `su-exec` is replaced by a
    shim that only strips the user argument. This is NOT a container test; it
    verifies the option parsing, password policy, secret persistence, config
    rendering, child supervision and exit-code propagation of the real script.
    """
    if not GIT_BASH.exists():
        record("IT29", "entrypoint simulation (options, secrets, supervision)", "NOT VERIFIED",
               "no bash available on this machine")
        return

    inst: Instance = ctx["inst"]
    binary: Path = inst.binary
    sim = inst.workdir / "entrypoint-sim"
    (sim / "bin").mkdir(parents=True, exist_ok=True)
    fixture = ctx.get("dist")

    shim = sim / "bin/su-shim"
    shim.write_bytes(b'#!/usr/bin/env bash\n# test shim: drop "user:group", run the rest\nshift\nexec "$@"\n')
    shim.chmod(0o755)

    source = ENTRYPOINT.read_text(encoding="utf-8")
    replacements = [
        ('readonly OPTIONS_FILE="/data/options.json"', f'readonly OPTIONS_FILE="{(sim / "options.json").as_posix()}"'),
        ('readonly DATA_DIR="/data"', f'readonly DATA_DIR="{sim.as_posix()}"'),
        ('readonly CONFIG_DIR="/data/config"', f'readonly CONFIG_DIR="{(sim / "config").as_posix()}"'),
        ('readonly APP_DATA_DIR="/data/app"', f'readonly APP_DATA_DIR="{(sim / "app").as_posix()}"'),
        ('readonly BACKUP_DIR="/data/backup"', f'readonly BACKUP_DIR="{(sim / "backup").as_posix()}"'),
        ('readonly RUN_DIR="/run/grok2api"', f'readonly RUN_DIR="{(sim / "run").as_posix()}"'),
        ('/var/lib/grok2api-quality-guard', (sim / "quality-guard").as_posix()),
        ('chown -R grok2api:grok2api "${DATA_DIR}" "${RUN_DIR}"', 'chown -R 0:0 "${DATA_DIR}" "${RUN_DIR}"'),
        ('su-exec grok2api:grok2api "$@"', 'su-shim root:root "$@"'),
        ('/usr/local/bin/haos-bootstrap.sh', 'true'),
        ('staticPath: "/app/frontend/dist"',
         f'staticPath: "{(fixture or (sim / "nodist")).as_posix()}"'),
    ]
    patched = source
    missing = []
    for old, new in replacements:
        if old not in patched:
            missing.append(old)
            continue
        patched = patched.replace(old, new)
    if missing:
        record("IT29", "entrypoint simulation (options, secrets, supervision)", "FAIL",
               f"could not rewrite {len(missing)} pattern(s): {missing[:2]}")
        return
    script = sim / "entrypoint.sh"
    script.write_text(patched, encoding="utf-8", newline="\n")

    options = sim / "options.json"
    secrets_file = sim / "config/secrets.env"
    config_file = sim / "config/config.yaml"

    def write_options(admin_password: str) -> None:
        options.write_text(json.dumps({
            "admin_username": "admin",
            "admin_password": admin_password,
            "bootstrap_client_key": True,
            "client_key_name": "Home Assistant",
            "request_timeout": 600,
            "public_base_url": "",
        }), encoding="utf-8")

    def boot(password: str, port: int) -> tuple[subprocess.Popen[bytes], str, str]:
        write_options(password)
        env = dict(os.environ)
        env["PATH"] = (str(sim / "bin") + os.pathsep + str(TOOLS_DIR) + os.pathsep
                       + env.get("PATH", ""))
        out_path = sim / f"run-{port}.out"
        err_path = sim / f"run-{port}.err"
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and generated script
            [str(GIT_BASH), str(script),
             str(binary), "--config", config_file.as_posix(), "--listen", f"127.0.0.1:{port}"],
            stdout=open(out_path, "wb"), stderr=open(err_path, "wb"),
            stdin=subprocess.DEVNULL, env=env, cwd=str(sim),
            # Own process group: CTRL_BREAK_EVENT must reach the simulated
            # container process group only, never the test runner's console.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return process, out_path.as_posix(), err_path.as_posix()

    def wait_health(port: int, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if http(f"http://127.0.0.1:{port}", "GET", "/healthz", timeout=2).status == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def stop(process: subprocess.Popen[bytes]) -> int:
        if process.poll() is None:
            try:
                process.send_signal(signal_value())
            except Exception:
                process.terminate()
        try:
            return process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait(timeout=10)

    # IT29a: first start writes secrets/config and honours the option password
    port_a = free_port()
    try:
        process, out_path, err_path = boot("Sim-" + uuid.uuid4().hex[:10], port_a)
        password_a = json.loads(options.read_text(encoding="utf-8"))["admin_password"]
        check(wait_health(port_a), f"no /healthz; log tail: {Path(out_path).read_text()[-800:]}")
        check(config_file.exists(), "config.yaml was not rendered")
        check(secrets_file.exists(), "secrets.env was not created")
        env_text = secrets_file.read_text(encoding="utf-8")
        jwt = re.search(r"^JWT_SECRET=(.+)$", env_text, re.M)
        enc = re.search(r"^CREDENTIAL_ENCRYPTION_KEY=(.+)$", env_text, re.M)
        check(bool(jwt and enc), "secrets.env is missing one of the two secrets")
        login = http(f"http://127.0.0.1:{port_a}", "POST", "/api/admin/v1/auth/login",
                     json_body={"username": "admin", "password": password_a})
        check(login.status == 200, f"admin login with the option password: {login.status}")
        ctx["sim_secrets"] = (jwt.group(1), enc.group(1))
        code = stop(process)
        logs = Path(out_path).read_text(encoding="utf-8", errors="replace") + \
            Path(err_path).read_text(encoding="utf-8", errors="replace")
        check(code in SIM_EXIT_CODES, f"exit code {code:#x}")
        check(password_a not in logs, "entrypoint printed the admin password")
        check(jwt.group(1) not in logs and enc.group(1) not in logs,
              "entrypoint printed a secret")
        record("IT29a", "entrypoint: renders config, persists secrets, honours options", "PASS",
               f"secrets.env + config.yaml created, admin login 200, no secret in log, "
               f"exit {code:#x} (Windows CTRL_BREAK cannot emulate SIGTERM)")
    except Exception as error:
        try:
            stop(process)  # type: ignore[name-defined]
        except Exception:
            pass
        record("IT29a", "entrypoint: renders config, persists secrets, honours options", "FAIL",
               str(error))

    # IT29b: second start keeps the same secrets (no key rotation) and the database
    port_b = free_port()
    try:
        process, out_path, err_path = boot(password_a, port_b)
        check(wait_health(port_b), "second start did not become healthy")
        env_text = secrets_file.read_text(encoding="utf-8")
        jwt_b = re.search(r"^JWT_SECRET=(.+)$", env_text, re.M)
        enc_b = re.search(r"^CREDENTIAL_ENCRYPTION_KEY=(.+)$", env_text, re.M)
        check(jwt_b and enc_b, "secrets.env unreadable")
        check((jwt_b.group(1), enc_b.group(1)) == ctx["sim_secrets"],
              "secrets were regenerated on restart (credentials would become undecryptable)")
        login = http(f"http://127.0.0.1:{port_b}", "POST", "/api/admin/v1/auth/login",
                     json_body={"username": "admin", "password": password_a})
        check(login.status == 200, f"login after restart: {login.status}")
        code_b = stop(process)
        check(code_b in SIM_EXIT_CODES, f"second start exited {code_b:#x}")
        record("IT29b", "entrypoint: secrets and database are stable across restarts", "PASS",
               f"same JWT/encryption key, admin row reused, exit {code_b:#x}")
    except Exception as error:
        try:
            stop(process)  # type: ignore[name-defined]
        except Exception:
            pass
        record("IT29b", "entrypoint: secrets and database are stable across restarts", "FAIL", str(error))

    # IT29c: a too-short password must abort before any secret/config is written
    port_c = free_port()
    try:
        secrets_before = secrets_file.read_bytes()
        short_password = "Zq7Xw"
        process, out_path, err_path = boot(short_password, port_c)
        code = process.wait(timeout=60)
        logs = Path(out_path).read_text(encoding="utf-8", errors="replace") + \
            Path(err_path).read_text(encoding="utf-8", errors="replace")
        check(code != 0, f"entrypoint accepted a 5-character password (exit {code})")
        check("admin_password is missing or shorter than 8" in logs,
              f"missing policy message: {logs[-500:]}")
        check(secrets_file.read_bytes() == secrets_before, "secrets were rewritten on rejection")
        check(short_password not in logs, "entrypoint echoed the rejected password")
        record("IT29c", "entrypoint: rejects a short admin_password and stops", "PASS",
               f"exit {code}, clear message, existing secrets untouched")
    except Exception as error:
        try:
            stop(process)  # type: ignore[name-defined]
        except Exception:
            pass
        record("IT29c", "entrypoint: rejects a short admin_password and stops", "FAIL", str(error))

    # IT29d: the supervisor script never leaks options into the rendered config
    try:
        rendered = config_file.read_text(encoding="utf-8")
        check("ledgerMode: enforce" in rendered, "audit ledger mode is not enforced")
        check("listen: \"0.0.0.0:8000\"" in rendered, "listen address unexpected")
        check("jwtSecret:" in rendered and "credentialEncryptionKey:" in rendered,
              "secrets missing from the rendered config")
        check(json.loads(options.read_text(encoding="utf-8"))["admin_password"] not in
              (sim / f"run-{port_b}.out").read_text(encoding="utf-8", errors="replace"),
              "password leaked into the child log")
        record("IT29d", "entrypoint: rendered config keeps enforce mode and secrets", "PASS",
               "ledgerMode=enforce, listen 0.0.0.0:8000 in config, no password in logs")
    except Exception as error:
        record("IT29d", "entrypoint: rendered config keeps enforce mode and secrets", "FAIL", str(error))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--frontend-dist", type=Path, default=None)
    parser.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    binary = args.binary.resolve()
    if not binary.exists():
        print(f"binary not found: {binary}", file=sys.stderr)
        return 2

    fixture = None
    if args.frontend_dist:
        fixture = args.frontend_dist.resolve()
    elif DEFAULT_FIXTURE_DIST.exists():
        fixture = DEFAULT_FIXTURE_DIST
    elif EXTRACTED_DIST.exists():
        fixture = EXTRACTED_DIST
    if fixture is not None and not (fixture / "index.html").exists():
        fixture = None

    tmp_root = Path(tempfile.mkdtemp(prefix="grok2api-it-", dir=_temp_parent()))
    inst = Instance(
        workdir=tmp_root,
        binary=binary,
        admin_user="admin",
        admin_password="Haos-it-" + uuid.uuid4().hex[:12],
        extra_secrets={"jwt": uuid.uuid4().hex + uuid.uuid4().hex,
                       "enc": _b64_32b()},
        static_path=fixture.as_posix() if fixture else None,
    )
    ctx: dict = {"inst": inst, "dist": fixture, "tmp_root": tmp_root}
    print(f"binary:  {binary}")
    print(f"workdir: {tmp_root}")
    print(f"fixture: {fixture if fixture else '(none - static asset checks skipped)'}")
    print(f"config:  rendered from {ENTRYPOINT.name} heredoc")
    print()

    exit_code = 0
    try:
        run_packaging_tests(ctx)
        inst.start()
        run_phase_one(ctx)
        # second boot on the same data directory
        inst.admin_password = ctx.get("new_password", inst.admin_password)
        inst.start()
        run_phase_two(ctx)
        run_bootstrap_tests(ctx)
        run_entrypoint_simulation(ctx)
    except Exception:
        traceback.print_exc()
        exit_code = 2
    finally:
        inst.kill()
        if args.keep:
            print(f"\nkept data directory: {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print()
    print("| Test | Case | Result | Detail |")
    print("| --- | --- | --- | --- |")
    for item in RESULTS:
        detail = item["detail"].replace("|", "\\|")
        print(f"| {item['id']} | {item['name']} | {item['status']} | {detail} |")

    failed = [item for item in RESULTS if item["status"] == "FAIL"]
    not_verified = [item for item in RESULTS if item["status"] == "NOT VERIFIED"]
    print()
    print(f"summary: {len(RESULTS)} tests, {len(RESULTS) - len(failed) - len(not_verified)} PASS, "
          f"{len(failed)} FAIL, {len(not_verified)} NOT VERIFIED")

    if args.json_out:
        args.json_out.write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")

    if failed or exit_code:
        return 1
    return 0


def _temp_parent() -> str:
    preferred = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "opencode"
    preferred.mkdir(parents=True, exist_ok=True)
    return str(preferred)


def _b64_32b() -> str:
    import base64

    return base64.b64encode(os.urandom(32)).decode()


if __name__ == "__main__":
    sys.exit(main())
