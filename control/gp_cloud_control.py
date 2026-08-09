#!/usr/bin/env python3
"""GP Cloud Preview control plane, worker, cleanup loop, and single-user UI.

The process intentionally uses Python's standard library only. Keeping the
control surface small makes the host installation easy to audit: request
validation, state writes, Vault access, Docker orchestration, and the themed
dashboard all live in this one source file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(os.environ.get("GP_CLOUD_ROOT", "/opt/gp-cloud"))
DATA = ROOT / "data"
DEPLOYMENTS = ROOT / "deployments"
WORKER = ROOT / "worker" / "gp-cloud-deploy"
CLEANER = ROOT / "worker" / "gp-cloud-clean"
PORT = int(os.environ.get("GP_CLOUD_CONTROL_PORT", "8787"))
DEFAULT_APP_PORT = int(os.environ.get("GP_CLOUD_DEFAULT_APP_PORT", "2222"))
API_TOKEN = os.environ.get("GP_CLOUD_API_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("GP_CLOUD_GITHUB_WEBHOOK_SECRET", "")
PREVIEW_DOMAIN = os.environ.get("GP_CLOUD_PREVIEW_DOMAIN", "preview.example.com")
PUBLIC_SCHEME = os.environ.get("GP_CLOUD_PUBLIC_SCHEME", "http")
PUBLIC_PORT = int(os.environ.get("GP_CLOUD_HTTP_PORT", "80"))
PUBLIC_URL_SUFFIX = "" if PUBLIC_PORT in {80, 443} else f":{PUBLIC_PORT}"
COOKIE_SECURE = os.environ.get("GP_CLOUD_COOKIE_SECURE", "false").lower() == "true"
HOST_CONFIG_KEYS = (
    "GP_CLOUD_PREVIEW_DOMAIN",
    "GP_CLOUD_HTTP_PORT",
    "GP_CLOUD_PUBLIC_SCHEME",
    "GP_CLOUD_COOKIE_SECURE",
    "GP_CLOUD_DEFAULT_APP_PORT",
    "GP_CLOUD_CONTROL_PORT",
    "GP_CLOUD_ALLOWED_REPOS",
    "GP_CLOUD_ALLOW_FORKS",
    "GP_CLOUD_CPU_LIMIT",
    "GP_CLOUD_MEMORY_LIMIT",
    "GP_CLOUD_BUILD_TIMEOUT_SECONDS",
    "GP_CLOUD_STARTUP_TIMEOUT_SECONDS",
    "GP_CLOUD_HEALTH_TIMEOUT_SECONDS",
    "GP_CLOUD_DEPLOYMENT_TTL_SECONDS",
    "GP_CLOUD_MAX_DEPLOYMENT_TTL_SECONDS",
    "GP_CLOUD_VAULT_ADDR",
    "GP_CLOUD_VAULT_MOUNT",
    "GP_CLOUD_VAULT_PATH_PREFIX",
    "GP_CLOUD_RETAIN_WORKSPACES",
)
ALLOWED_REPOS = {
    item.strip().lower()
    for item in os.environ.get("GP_CLOUD_ALLOWED_REPOS", "").split(",")
    if item.strip()
}
ALLOW_FORKS = os.environ.get("GP_CLOUD_ALLOW_FORKS", "false").lower() == "true"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_INSTALLATION_ID = os.environ.get("GITHUB_INSTALLATION_ID", "")
GITHUB_PRIVATE_KEY_FILE = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE", "")

STATE_DIR = DATA / "deployments"
QUEUE_DIR = DATA / "queue"
TOKEN_DIR = DATA / "action-tokens"
SETTINGS_FILE = DATA / "control-settings.json"
SESSION_COOKIE = "gp_cloud_session"
SESSIONS: dict[str, float] = {}
SESSION_LOCK = threading.RLock()
VAULT_ADDR = os.environ.get("GP_CLOUD_VAULT_ADDR", "").rstrip("/")
VAULT_TOKEN = os.environ.get("GP_CLOUD_VAULT_TOKEN", "")
VAULT_TOKEN_FILE = os.environ.get("GP_CLOUD_VAULT_TOKEN_FILE", "")
VAULT_MOUNT = os.environ.get("GP_CLOUD_VAULT_MOUNT", "secret")
DEPLOYMENT_TTL_SECONDS = max(0, int(os.environ.get("GP_CLOUD_DEPLOYMENT_TTL_SECONDS", "86400")))
MAX_DEPLOYMENT_TTL_SECONDS = max(
    DEPLOYMENT_TTL_SECONDS, int(os.environ.get("GP_CLOUD_MAX_DEPLOYMENT_TTL_SECONDS", "604800"))
)
ADMIN_PASSWORD = os.environ.get("GP_CLOUD_ADMIN_PASSWORD", "")
RETAIN_WORKSPACES = os.environ.get("GP_CLOUD_RETAIN_WORKSPACES", "false").lower() == "true"
VAULT_PATH_PREFIX = os.environ.get("GP_CLOUD_VAULT_PATH_PREFIX", "gp-cloud/").strip("/") + "/"
LOCK = threading.RLock()
JOBS: queue.Queue[str] = queue.Queue()
STOP = threading.Event()


def now() -> str:
    """Return an ISO-8601 UTC timestamp for persisted state and logs."""
    return datetime.now(timezone.utc).isoformat()


def load_settings() -> dict:
    """Load operator settings, falling back safely when the file is absent."""
    defaults = {
        "deployment_ttl_seconds": DEPLOYMENT_TTL_SECONDS,
        "max_deployment_ttl_seconds": MAX_DEPLOYMENT_TTL_SECONDS,
        "delete_on_pull_request_close": True,
        # Project behavior is operator configuration, never an application
        # name embedded in the control plane.
        "projects": {},
    }
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            defaults.update(value)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def save_settings(value: dict) -> dict:
    """Validate and persist dashboard settings while enforcing TTL limits."""
    allowed = {
        "deployment_ttl_seconds": max(
            0,
            min(
                int(value.get("deployment_ttl_seconds", DEPLOYMENT_TTL_SECONDS)),
                MAX_DEPLOYMENT_TTL_SECONDS,
            ),
        ),
        "max_deployment_ttl_seconds": MAX_DEPLOYMENT_TTL_SECONDS,
        "delete_on_pull_request_close": bool(value.get("delete_on_pull_request_close", True)),
        "projects": value.get("projects") if isinstance(value.get("projects"), dict) else {},
    }
    atomic_json(SETTINGS_FILE, allowed)
    return allowed


def vault_token() -> str:
    """Read the Vault token from the configured value or root-owned token file."""
    if VAULT_TOKEN:
        return VAULT_TOKEN
    if VAULT_TOKEN_FILE:
        try:
            return Path(VAULT_TOKEN_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def vault_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Make one bounded Vault API request without logging credential material."""
    if not VAULT_ADDR or not vault_token():
        raise RuntimeError("Vault is not configured")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{VAULT_ADDR}/v1/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={"X-Vault-Token": vault_token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Vault request failed with HTTP {error.code}") from error
    if not isinstance(result, dict):
        raise RuntimeError("Vault returned an invalid response")
    return result


def vault_read_env(path: str) -> dict[str, str]:
    """Read only shell-safe environment entries from the approved Vault path."""
    result = vault_request("GET", f"{VAULT_MOUNT}/data/{path}")
    data = (result.get("data") or {}).get("data") or {}
    values: dict[str, str] = {}
    for key, value in data.items():
        name = str(key)
        text = str(value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or "\n" in text or "\r" in text:
            raise ValueError(f"Vault contains an invalid environment entry: {name}")
        values[name] = text
    return values


def vault_write_env(path: str, values: dict[str, str]) -> None:
    """Write application environment values to one validated Vault path."""
    vault_request("POST", f"{VAULT_MOUNT}/data/{path}", {"data": values})


def validate_vault_path(value: object) -> str:
    """Allow application secrets only below the operator-owned Vault prefix."""
    path = str(value or "").strip().strip("/")
    if not path:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", path):
        raise ValueError("invalid Vault path")
    # A textual prefix check is insufficient when dot segments can escape the
    # operator-owned namespace after Vault or a proxy normalizes the path.
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("invalid Vault path")
    if not path.startswith(VAULT_PATH_PREFIX.rstrip("/")):
        raise ValueError(f"Vault path must be below {VAULT_PATH_PREFIX}")
    return path


def atomic_json(path: Path, value: dict) -> None:
    """Replace a JSON file atomically so readers never see partial state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_text(path: Path, value: str) -> None:
    """Replace one local configuration file without exposing partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def state_path(deployment_id: str) -> Path:
    """Map a validated deployment identifier to its state file."""
    return STATE_DIR / f"{deployment_id}.json"


def read_state(deployment_id: str) -> dict | None:
    """Read one deployment state record, treating missing or incomplete files as absent."""
    try:
        return json.loads(state_path(deployment_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_state(state: dict) -> None:
    """Stamp and atomically persist a deployment state under the process lock."""
    state["updated_at"] = now()
    with LOCK:
        atomic_json(state_path(state["id"]), state)


def update_state(deployment_id: str, **changes: object) -> dict | None:
    """Apply a partial state transition and return the resulting record."""
    with LOCK:
        state = read_state(deployment_id)
        if state is None:
            return None
        state.update(changes)
        write_state(state)
        return state


def repo_name(payload: dict) -> str:
    """Extract a normalized repository name from a GitHub event payload."""
    repo = payload.get("repository") or {}
    return str(repo.get("full_name") or "").lower()


def allowed_repo(name: str) -> bool:
    """Check the repository allowlist and fail closed when it is not configured."""
    return bool(ALLOWED_REPOS) and name.lower() in ALLOWED_REPOS


def slugify(value: str) -> str:
    """Convert user-controlled project text into a safe DNS/container slug."""
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if not value:
        raise ValueError("project slug cannot be empty")
    return value[:50]


def require_sha(value: object) -> str:
    sha = str(value or "").lower()
    # Deployments are immutable. A short SHA is ambiguous after a fetch and
    # cannot prove that the image was built from the requested commit.
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("sha must be a 40-character hexadecimal commit SHA")
    return sha


def github_repo_from_url(repo_url: str) -> str:
    """Return an owner/name pair from the two supported GitHub URL forms."""
    if repo_url.startswith("git@github.com:"):
        repo = repo_url.removeprefix("git@github.com:")
    else:
        parsed = urllib.parse.urlsplit(repo_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise ValueError("repo_url must point to github.com over HTTPS or SSH")
        if parsed.query or parsed.fragment:
            raise ValueError("repo_url must not contain a query or fragment")
        repo = parsed.path.removeprefix("/")
    repo = repo.removesuffix(".git").strip("/").lower()
    if not re.fullmatch(r"[^/]+/[^/]+", repo):
        raise ValueError("repo_url must contain a GitHub owner and repository")
    return repo


def normalize_job(body: dict) -> dict:
    """Validate a deployment request and return only safe, normalized fields.

    The browser and webhook are untrusted callers. Keeping this validation at
    the control-plane boundary prevents a caller from smuggling shell syntax,
    an arbitrary filesystem path, or an unrestricted Vault path into the
    privileged worker.
    """
    repo_url = str(body.get("repo_url") or body.get("repository_url") or "")
    repo = github_repo_from_url(repo_url)
    sha = require_sha(body.get("sha"))
    project = slugify(str(body.get("project") or body.get("repo") or "app"))
    pr_number = int(body.get("pr_number") or 0)
    if pr_number < 0:
        raise ValueError("pr_number must be non-negative")
    app_port = int(body.get("app_port") or DEFAULT_APP_PORT)
    if not 1 <= app_port <= 65535:
        raise ValueError("app_port out of range")
    health_path = str(body.get("health_path") or "/")
    if not health_path.startswith("/"):
        raise ValueError("health_path must start with /")
    vault_path = validate_vault_path(body.get("vault_path"))
    if not allowed_repo(repo):
        raise PermissionError("repository is not in GP_CLOUD_ALLOWED_REPOS")
    if not API_TOKEN:
        raise RuntimeError("GP_CLOUD_API_TOKEN is not configured")
    slug = f"{project}-pr-{pr_number}" if pr_number else f"{project}-{sha[:8]}"
    slug = slugify(slug)
    settings = load_settings()
    ttl_seconds = int(
        body.get("ttl_seconds") or settings.get("deployment_ttl_seconds") or DEPLOYMENT_TTL_SECONDS
    )
    max_ttl = int(settings.get("max_deployment_ttl_seconds") or MAX_DEPLOYMENT_TTL_SECONDS)
    if ttl_seconds < 0 or ttl_seconds > max_ttl:
        raise ValueError(f"ttl_seconds must be between 0 and {max_ttl}")
    expires_at = (
        None if ttl_seconds == 0 else (datetime.now(timezone.utc).timestamp() + ttl_seconds)
    )
    return {
        "repo_url": repo_url,
        "repo": repo,
        "sha": sha,
        "project": project,
        "pr_number": pr_number,
        "app_port": app_port,
        "health_path": health_path,
        "vault_path": vault_path,
        "slug": slug,
        "ttl_seconds": ttl_seconds,
        "expires_at": datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
        if expires_at
        else None,
    }


def enqueue(body: dict, source: str, clone_token: str = "") -> dict:
    """Normalize, persist, and queue a deployment for the worker thread."""
    job = normalize_job(body)
    deployment_id = f"dep_{int(time.time())}_{secrets.token_hex(4)}"
    state = {
        "id": deployment_id,
        **job,
        "source": source,
        "state": "QUEUED",
        "logs": str(ROOT / "logs" / job["slug"] / "worker.log"),
        "created_at": now(),
        "updated_at": now(),
    }
    if clone_token:
        TOKEN_DIR.mkdir(parents=True, exist_ok=True)
        token_path = TOKEN_DIR / deployment_id
        token_path.write_text(clone_token, encoding="utf-8")
        token_path.chmod(0o600)
        state["clone_token_file"] = str(token_path)
        state["action_token_hash"] = hashlib.sha256(clone_token.encode()).hexdigest()
    write_state(state)
    (QUEUE_DIR / deployment_id).write_text("queued\n", encoding="utf-8")
    JOBS.put(deployment_id)
    return state


def public_action_state(state: dict) -> dict:
    """Remove clone credentials and Vault references from an externally visible record."""
    result = dict(state)
    result.pop("clone_token_file", None)
    result.pop("action_token_hash", None)
    result.pop("vault_path", None)
    return result


def action_state_authorized(state: dict, token: str, repo: str) -> bool:
    """Authorize a GitHub Action against its deployment-specific token and repository."""
    expected = str(state.get("action_token_hash") or "")
    actual = hashlib.sha256(token.encode()).hexdigest() if token else ""
    return bool(
        expected and repo and state.get("repo") == repo and hmac.compare_digest(expected, actual)
    )


def append_log(state: dict, text: str) -> None:
    """Append worker output to the deployment's retained diagnostic log."""
    log = ROOT / "logs" / state["slug"] / "worker.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text)


def deployment_log_path(state: dict) -> Path:
    """Return the log path derived from the already-normalized deployment slug."""
    return ROOT / "logs" / state["slug"] / "worker.log"


def deployment_records() -> list[dict]:
    """Return one public record per slug, including legacy runtime metadata."""
    records: dict[str, dict] = {}
    for path in STATE_DIR.glob("dep_*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        public = public_action_state(state)
        slug = str(public.get("slug") or public.get("id") or path.stem)
        existing = records.get(slug)
        if existing is None or str(public.get("updated_at", "")) > str(
            existing.get("updated_at", "")
        ):
            records[slug] = public

    # The shell harness writes metadata before the control process records its
    # final state. Keep the list useful across upgrades and manual deployments.
    for metadata_path in DEPLOYMENTS.glob("*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = str(metadata.get("slug") or metadata_path.parent.name)
        if slug not in records:
            records[slug] = metadata
        elif metadata.get("state") == "RUNNING" and records[slug].get("state") != "RUNNING":
            records[slug].update({key: value for key, value in metadata.items() if key != "state"})
            records[slug]["state"] = "RUNNING"
    return sorted(records.values(), key=lambda item: str(item.get("created_at", "")), reverse=True)


def public_deployment_summary(record: dict) -> dict:
    """Safe fields for browser status pages and unauthenticated index reads."""
    return {
        key: record[key]
        for key in ("id", "slug", "state", "preview_url", "created_at", "updated_at", "expires_at")
        if key in record
    }


def host_config_summary() -> dict:
    """Expose configuration inventory without returning credential values."""
    path = ROOT / "config" / "gp-cloud.env"
    keys: list[dict[str, object]] = []
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _value = line.split("=", 1)
            name = name.strip()
            values[name] = _value
            secret = any(
                word in name for word in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY")
            )
            keys.append(
                {
                    "name": name,
                    "secret": secret,
                    "configured": bool(_value),
                    "value": None if secret else _value,
                }
            )
    except OSError:
        pass
    known = {str(item["name"]) for item in keys}
    for name in HOST_CONFIG_KEYS:
        if name in known:
            continue
        secret = any(
            word in name for word in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY")
        )
        keys.append(
            {"name": name, "secret": secret, "configured": False, "value": None if secret else ""}
        )
    return {
        "path": str(path),
        "available": path.is_file(),
        "keys": keys,
        "vault_configured": bool(VAULT_ADDR and vault_token()),
        "vault_path_prefix": VAULT_PATH_PREFIX,
        "note": "Secret values are intentionally never returned by the control API.",
    }


def save_host_config(values: object) -> dict:
    """Update only non-secret supported env settings in the live local file.

    Credentials remain deliberately outside the browser editor. This function
    also rejects shell metacharacters carried across lines; values are stored
    as data and are never evaluated by this process.
    """
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    path = ROOT / "config" / "gp-cloud.env"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    allowed = set(HOST_CONFIG_KEYS)
    secret_names = {
        name
        for name in allowed
        if any(word in name for word in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY"))
    }
    updates: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name)
        if name not in allowed or name in secret_names:
            raise PermissionError(f"{name} is local-only and cannot be edited in the UI")
        value = str(raw_value)
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} cannot contain newlines")
        updates[name] = value
    lines = existing.splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            name = line.split("=", 1)[0].strip()
            if name in updates:
                output.append(f"{name}={updates[name]}")
                seen.add(name)
                continue
        output.append(line)
    for name, value in updates.items():
        if name not in seen:
            output.append(f"{name}={value}")
    atomic_text(path, "\n".join(output).rstrip() + "\n")
    return host_config_summary()


def directory_bytes(path: Path) -> int:
    """Calculate usage below one known GP Cloud directory only."""
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def usage_metrics() -> dict:
    """Return detailed resource data scoped to GP Cloud-owned paths.

    Docker stats are queried by container name and the filesystem walk starts
    at ``ROOT``. No host-wide scan, arbitrary path supplied by a request, or
    container filesystem is exposed to the dashboard.
    """
    disk = shutil.disk_usage(ROOT)
    states: dict[str, int] = {}
    for record in deployment_records():
        status = str(record.get("state", "UNKNOWN"))
        states[status] = states.get(status, 0) + 1
    containers: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}\t{{.PIDs}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if fields and fields[0].startswith("gp-cloud-"):
                names = (
                    "name",
                    "cpu_percent",
                    "memory",
                    "memory_percent",
                    "network_io",
                    "block_io",
                    "pids",
                )
                containers.append(dict(zip(names, fields)))
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "root": str(ROOT),
        "disk": {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free},
        "gp_cloud_bytes": directory_bytes(ROOT),
        "logs_bytes": directory_bytes(ROOT / "logs"),
        "deployments_bytes": directory_bytes(DEPLOYMENTS),
        "states": states,
        "queue_depth": JOBS.qsize(),
        "containers": containers,
        "container_count": len(containers),
        "resource_limits": {
            "cpus": os.environ.get("GP_CLOUD_CPU_LIMIT", "1.0"),
            "memory": os.environ.get("GP_CLOUD_MEMORY_LIMIT", "768m"),
            "pids": 256,
        },
        "retaining_workspaces": RETAIN_WORKSPACES,
        "ttl_seconds": int(load_settings().get("deployment_ttl_seconds", DEPLOYMENT_TTL_SECONDS)),
    }


def purge_all_deployments() -> dict:
    """Stop every deployment, then delete only its GP Cloud records/artifacts."""
    stopped = 0
    purged = 0
    known_slugs: set[str] = set()
    for path in list(STATE_DIR.glob("dep_*.json")):
        state = read_state(path.stem)
        if not state:
            continue
        slug = str(state.get("slug") or "")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
            known_slugs.add(slug)
        if state.get("state") not in {"STOPPED", "FAILED"}:
            stop_deployment(state["id"])
            stopped += 1
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
            shutil.rmtree(ROOT / "logs" / slug, ignore_errors=True)
            shutil.rmtree(DEPLOYMENTS / slug, ignore_errors=True)
        token_file = Path(str(state.get("clone_token_file") or ""))
        if token_file.is_file() and token_file.is_relative_to(TOKEN_DIR):
            token_file.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        (QUEUE_DIR / path.stem).unlink(missing_ok=True)
        purged += 1
    # Metadata can exist for a deployment created by the shell harness before
    # the control service writes state JSON. Reconcile those records too so
    # Delete all cannot leave an orphaned container or Caddy route behind.
    for metadata_path in DEPLOYMENTS.glob("*/metadata.json"):
        slug = metadata_path.parent.name
        if slug not in known_slugs and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
            subprocess.run(
                [str(CLEANER), slug], check=False, timeout=120, capture_output=True, text=True
            )
            shutil.rmtree(ROOT / "logs" / slug, ignore_errors=True)
            purged += 1
    return {"stopped": stopped, "purged": purged}


def stop_all_deployments() -> int:
    """Request cleanup for every active state record and remove queue markers."""
    stopped = 0
    for record in deployment_records():
        deployment_id = str(record.get("id") or "")
        status = str(record.get("state") or "")
        if deployment_id and status in {"QUEUED", "BUILDING", "RUNNING", "STOPPING"}:
            if stop_deployment(deployment_id):
                stopped += 1
        elif not deployment_id and status in {"RUNNING", "BUILDING"}:
            slug = str(record.get("slug") or "")
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
                subprocess.run(
                    [str(CLEANER), slug], check=False, timeout=120, capture_output=True, text=True
                )
                stopped += 1
    # Removing markers prevents a restart from resurrecting jobs that the
    # operator explicitly stopped while they were still queued.
    for marker in QUEUE_DIR.iterdir():
        if marker.is_file():
            marker.unlink(missing_ok=True)
    return stopped


def project_profile(state: dict) -> dict:
    """Apply an operator-defined project profile without hardcoded project names."""
    profile = (load_settings().get("projects") or {}).get(state["project"], {})
    if not isinstance(profile, dict):
        return state
    changes = {
        key: profile[key]
        for key in (
            "app_port",
            "health_path",
            "runtime",
            "start_command",
            "build_command",
            "vault_path",
        )
        if key in profile
    }
    if "port" in profile:
        changes["app_port"] = profile["port"]
    if "kind" in profile:
        changes["profile"] = profile["kind"]
    return update_state(state["id"], **changes) or state


def materialize_profile(source_dir: Path, state: dict) -> None:
    """Create build-only files in the ephemeral workspace, never in Git."""
    profile = state.get("profile")
    if profile == "next-static":
        dockerfile = f"""FROM node:24-alpine AS build\nWORKDIR /app\nRUN corepack enable && corepack prepare pnpm@10.26.0 --activate\nCOPY package.json pnpm-lock.yaml ./\nRUN pnpm install --frozen-lockfile --ignore-scripts\nCOPY . .\nRUN pnpm build\nFROM nginx:1.27-alpine\nCOPY nginx.conf /etc/nginx/conf.d/default.conf\nCOPY --from=build /app/out /usr/share/nginx/html\nEXPOSE {DEFAULT_APP_PORT}\n"""
        (source_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (source_dir / "nginx.conf").write_text(
            f"""server {{\n  listen {DEFAULT_APP_PORT};\n  server_name _;\n  root /usr/share/nginx/html;\n  index index.html;\n  location / {{ try_files $uri $uri/ /index.html; }}\n}}\n""",
            encoding="utf-8",
        )
        return
    if profile != "vite" or (source_dir / "Dockerfile").exists():
        return
    nginx = f"""server {{\n  listen {DEFAULT_APP_PORT};\n  server_name _;\n  root /usr/share/nginx/html;\n  index index.html;\n  location / {{ try_files $uri $uri/ /index.html; }}\n}}\n"""
    dockerfile = f"""FROM node:24-alpine AS build\nWORKDIR /app\nRUN corepack enable && corepack prepare pnpm@10.28.2 --activate\nCOPY package.json pnpm-lock.yaml ./\nRUN pnpm install --frozen-lockfile --ignore-scripts\nCOPY . .\nRUN pnpm build\nFROM nginx:1.27-alpine\nCOPY nginx.conf /etc/nginx/conf.d/default.conf\nCOPY --from=build /app/dist /usr/share/nginx/html\nEXPOSE {DEFAULT_APP_PORT}\n"""
    (source_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (source_dir / "nginx.conf").write_text(nginx, encoding="utf-8")


def detect_runtime_profile(source_dir: Path, state: dict) -> dict:
    """Apply explicit project settings, then detect safe generic runtimes."""
    configured = (load_settings().get("projects") or {}).get(state["project"], {})
    if isinstance(configured, dict):
        state.update(
            {
                key: configured[key]
                for key in (
                    "app_port",
                    "health_path",
                    "runtime",
                    "start_command",
                    "build_command",
                    "vault_path",
                )
                if key in configured
            }
        )
        if "vault_path" in configured:
            state["vault_path"] = validate_vault_path(configured["vault_path"])
    if (source_dir / "Dockerfile").exists():
        state.setdefault("runtime", "dockerfile")
    elif (source_dir / "uv.lock").exists() and (source_dir / "pyproject.toml").exists():
        state.setdefault("runtime", "uv")
    elif (source_dir / "package-lock.json").exists() and (source_dir / "package.json").exists():
        state.setdefault("runtime", "npm")
    elif (source_dir / "pnpm-lock.yaml").exists() and (source_dir / "package.json").exists():
        state.setdefault("runtime", "pnpm")
    elif (source_dir / "requirements.txt").exists():
        state.setdefault("runtime", "python")
    return state


def materialize_generic_profile(source_dir: Path, state: dict) -> None:
    """Generate a conservative Dockerfile for lockfile-based applications.

    A start command is required for non-static generic apps; guessing one for
    arbitrary repositories would produce a deceptively healthy deployment.
    """
    if (source_dir / "Dockerfile").exists():
        return
    runtime = state.get("runtime")
    command = str(state.get("start_command") or "").strip()
    build = str(state.get("build_command") or "").strip()
    if runtime in {"uv", "python"} and not command:
        raise ValueError("start_command is required for generic Python deployments")
    if runtime in {"npm", "pnpm"} and not command:
        command = "npm start" if runtime == "npm" else "pnpm start"
    if runtime == "uv":
        lines = [
            "FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
            "WORKDIR /app",
            "ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1",
            "COPY pyproject.toml uv.lock ./",
            "RUN uv sync --frozen --no-dev",
            "COPY . .",
        ]
        if build:
            lines.append(f"RUN {build}")
        lines.append(f'CMD ["sh", "-c", {json.dumps(command)}]')
    elif runtime == "python":
        lines = [
            "FROM python:3.12-slim",
            "WORKDIR /app",
            "ENV PYTHONUNBUFFERED=1",
            "COPY requirements.txt ./",
            "RUN pip install --no-cache-dir -r requirements.txt",
            "COPY . .",
        ]
        if build:
            lines.append(f"RUN {build}")
        lines.append(f'CMD ["sh", "-c", {json.dumps(command)}]')
    elif runtime in {"npm", "pnpm"}:
        installer = (
            "npm ci" if runtime == "npm" else "corepack enable && pnpm install --frozen-lockfile"
        )
        lines = [
            "FROM node:24-slim",
            "WORKDIR /app",
            "COPY package.json "
            + ("package-lock.json" if runtime == "npm" else "pnpm-lock.yaml")
            + " ./",
            f"RUN {installer}",
            "COPY . .",
        ]
        if build:
            lines.append(f"RUN {build}")
        lines.append(f'CMD ["sh", "-c", {json.dumps(command)}]')
    else:
        return
    (source_dir / "Dockerfile").write_text("\n".join(lines) + "\n", encoding="utf-8")


def github_api_json(url: str) -> dict:
    """Fetch public GitHub JSON with a short timeout and no bearer credential."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gp-cloud"}
    token = GITHUB_TOKEN or installation_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("GitHub API returned a non-object response")
    return value


def github_api_json_with_token(url: str, token: str) -> dict:
    """Fetch GitHub JSON using a caller-provided token without exposing it in logs."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gp-cloud-github-action",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("GitHub API returned a non-object response")
    return value


def b64(value: bytes) -> str:
    """Encode bytes for the GitHub App JWT format."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def installation_token() -> str:
    """Exchange configured GitHub App credentials for a short-lived installation token."""
    if not (GITHUB_APP_ID and GITHUB_INSTALLATION_ID and GITHUB_PRIVATE_KEY_FILE):
        return ""
    header = b64(b'{"alg":"RS256","typ":"JWT"}')
    payload = b64(
        json.dumps(
            {"iat": int(time.time()) - 60, "exp": int(time.time()) + 540, "iss": GITHUB_APP_ID},
            separators=(",", ":"),
        ).encode()
    )
    unsigned = f"{header}.{payload}".encode()
    with tempfile.NamedTemporaryFile() as source, tempfile.NamedTemporaryFile() as signature:
        source.write(unsigned)
        source.flush()
        subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                GITHUB_PRIVATE_KEY_FILE,
                "-out",
                signature.name,
                source.name,
            ],
            check=True,
            capture_output=True,
        )
        jwt = unsigned.decode() + "." + b64(Path(signature.name).read_bytes())
    request = urllib.request.Request(
        f"https://api.github.com/app/installations/{GITHUB_INSTALLATION_ID}/access_tokens",
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "gp-cloud",
        },
        data=b"{}",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return str(json.load(response)["token"])


def run_deployment(deployment_id: str) -> None:
    """Build, start, health-check, publish, and clean one queued deployment."""
    state = read_state(deployment_id)
    if not state:
        return
    slug = state["slug"]
    workspace = DEPLOYMENTS / slug
    source_dir = workspace / "source"
    workspace.mkdir(parents=True, exist_ok=True)
    append_log(state, f"[{now()}] deployment={deployment_id} state=BUILDING\n")
    update_state(deployment_id, state="BUILDING")
    auth_home: str | None = None
    try:
        if read_state(deployment_id).get("state") == "STOPPING":  # type: ignore[union-attr]
            stop_deployment(deployment_id)
            return
        if source_dir.exists():
            shutil.rmtree(source_dir)
        clone_url = state["repo_url"]
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        token = GITHUB_TOKEN or installation_token()
        token_file = Path(str(state.get("clone_token_file") or ""))
        if not token and token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
        if token and clone_url.startswith("https://github.com/"):
            auth_home = tempfile.mkdtemp(prefix="gp-cloud-git-")
            netrc = Path(auth_home) / ".netrc"
            netrc.write_text(
                f"machine github.com login x-access-token password {token}\n", encoding="utf-8"
            )
            netrc.chmod(0o600)
            env["HOME"] = auth_home
        subprocess.run(
            ["git", "clone", "--no-checkout", clone_url, str(source_dir)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(source_dir), "fetch", "origin", state["sha"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        subprocess.run(
            ["git", "-C", str(source_dir), "checkout", "--detach", state["sha"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        state = project_profile(state)
        state = detect_runtime_profile(source_dir, state)
        materialize_profile(source_dir, state)
        materialize_generic_profile(source_dir, state)
        update_state(deployment_id, state="BUILDING", checked_out_sha=state["sha"])
        if read_state(deployment_id).get("state") == "STOPPING":  # type: ignore[union-attr]
            stop_deployment(deployment_id)
            return
        env_file: Path | None = None
        vault_path = str(state.get("vault_path") or "")
        if vault_path:
            values = vault_read_env(vault_path)
            env_file = workspace / ".runtime.env"
            env_file.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
            )
            env_file.chmod(0o600)
        command = [
            str(WORKER),
            "--source",
            str(source_dir),
            "--sha",
            state["sha"],
            "--slug",
            slug,
            "--port",
            str(state["app_port"]),
            "--health-path",
            state["health_path"],
        ]
        if env_file:
            command.extend(["--env-file", str(env_file)])
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(os.environ.get("GP_CLOUD_BUILD_TIMEOUT_SECONDS", "600")),
        )
        append_log(state, result.stdout)
        if result.returncode:
            raise RuntimeError(f"deployment harness exited with {result.returncode}")
        if read_state(deployment_id).get("state") == "STOPPING":  # type: ignore[union-attr]
            stop_deployment(deployment_id)
            return
        update_state(
            deployment_id,
            state="RUNNING",
            preview_url=f"{PUBLIC_SCHEME}://{slug}.{PREVIEW_DOMAIN}{PUBLIC_URL_SUFFIX}",
        )
        append_log(state, f"[{now()}] deployment={deployment_id} state=RUNNING\n")
    except Exception as error:
        append_log(state, f"[{now()}] deployment={deployment_id} state=FAILED error={error}\n")
        update_state(deployment_id, state="FAILED", error=str(error))
    finally:
        if auth_home:
            shutil.rmtree(auth_home, ignore_errors=True)
        # The source checkout and materialized Dockerfile are build artifacts,
        # not deployment state. Removing them bounds disk usage even after a
        # failed build; the retained worker log and JSON state are sufficient
        # for diagnosis.
        if not RETAIN_WORKSPACES:
            shutil.rmtree(source_dir, ignore_errors=True)
        (workspace / ".runtime.env").unlink(missing_ok=True)
        token_file_name = str(state.get("clone_token_file") or "")
        token_file = Path(token_file_name) if token_file_name else None
        if token_file is not None:
            token_file.unlink(missing_ok=True)


def stop_deployment(deployment_id: str) -> dict | None:
    """Stop one deployment and remove its runtime artifacts through the shell harness."""
    state = read_state(deployment_id)
    if not state:
        return None
    token_file_name = str(state.get("clone_token_file") or "")
    token_file = Path(token_file_name) if token_file_name else None
    if token_file is not None:
        token_file.unlink(missing_ok=True)
    update_state(deployment_id, state="STOPPING", stop_requested=True)
    result = subprocess.run(
        [str(CLEANER), state["slug"]],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )
    append_log(state, result.stdout)
    final = "STOPPED" if result.returncode == 0 else "FAILED"
    return update_state(
        deployment_id,
        state=final,
        error=None if result.returncode == 0 else result.stdout,
        stop_requested=False,
    )


def worker_loop() -> None:
    """Consume queued deployment identifiers serially for predictable host capacity."""
    while not STOP.is_set():
        try:
            deployment_id = JOBS.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            run_deployment(deployment_id)
        finally:
            try:
                (QUEUE_DIR / deployment_id).unlink()
            except FileNotFoundError:
                pass
            JOBS.task_done()


def cleanup_loop() -> None:
    """Enforce TTLs independently of the terminal, webhook, and UI."""
    while not STOP.wait(60):
        for path in STATE_DIR.glob("dep_*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                expires_at = state.get("expires_at")
                if not expires_at or state.get("state") not in {"QUEUED", "BUILDING", "RUNNING"}:
                    continue
                if datetime.fromisoformat(str(expires_at)) <= datetime.now(timezone.utc):
                    append_log(state, f"[{now()}] deployment={state['id']} state=EXPIRED\n")
                    stop_deployment(state["id"])
            except (OSError, ValueError, json.JSONDecodeError):
                continue


def backfill_expirations() -> None:
    """Give pre-TTL deployments the same bounded lifetime as new jobs."""
    ttl = int(load_settings().get("deployment_ttl_seconds") or DEPLOYMENT_TTL_SECONDS)
    if ttl <= 0:
        return
    for path in STATE_DIR.glob("dep_*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("state") not in {"QUEUED", "BUILDING", "RUNNING"} or state.get(
                "expires_at"
            ):
                continue
            created = datetime.fromisoformat(str(state.get("created_at")))
            state["expires_at"] = created.timestamp() + ttl
            state["expires_at"] = datetime.fromtimestamp(
                state["expires_at"], timezone.utc
            ).isoformat()
            write_state(state)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def verify_signature(handler: BaseHTTPRequestHandler, body: bytes) -> bool:
    """Verify GitHub's HMAC signature using constant-time comparison."""
    signature = handler.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(signature, expected)


def admin_password_matches(value: str) -> bool:
    """Compare the local UI password without exposing either credential."""
    expected = ADMIN_PASSWORD or API_TOKEN
    return bool(expected) and hmac.compare_digest(value, expected)


def ui_session(handler: BaseHTTPRequestHandler) -> bool:
    """Validate the short-lived in-memory dashboard session cookie."""
    raw = handler.headers.get("Cookie", "")
    token = next(
        (
            item.split("=", 1)[1]
            for item in raw.split("; ")
            if item.startswith(f"{SESSION_COOKIE}=")
        ),
        "",
    )
    with SESSION_LOCK:
        return bool(token and SESSIONS.get(token, 0) > time.time())


def ui_html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GP Cloud Preview</title><style>
:root{--bg:#fbfaf7;--surface:#fff;--ink:#111;--ink2:#2d2d2d;--muted:#6b6b6b;--line:#e8e5df;--chip:#f2efe9;--green:#176b46;--red:#a33a32;--radius:14px;--max:1080px}
[data-theme=dark]{--bg:#111;--surface:#1a1a1a;--ink:#f0f0f0;--ink2:#d4d4d4;--muted:#888;--line:#333;--chip:#222}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 Inter,system-ui,sans-serif}main{max-width:var(--max);margin:auto;padding:38px 22px 80px}h1,h2{font-family:Georgia,'Times New Roman',serif;font-weight:400;letter-spacing:-.03em}h1{font-size:clamp(42px,7vw,72px);line-height:1;margin:10px 0}h2{font-size:30px;margin:0 0 16px}.eyebrow{color:var(--muted);font-size:11px;letter-spacing:.14em;text-transform:uppercase}.top{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line);padding-bottom:20px}.theme,.button{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:999px;padding:9px 14px;cursor:pointer}.hero{padding:60px 0 42px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1.4fr 1fr;gap:18px}.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px}.card h3{margin:0 0 4px;font-size:16px}.deploy{display:flex;justify-content:space-between;gap:16px;align-items:center;border-top:1px solid var(--line);padding:16px 0}.deploy:first-child{border-top:0}.status{font-size:11px;text-transform:uppercase;letter-spacing:.08em}.RUNNING{color:var(--green)}.FAILED,.STOPPED{color:var(--red)}label{display:block;color:var(--muted);font-size:12px;margin:12px 0 5px}input,textarea,select{width:100%;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:10px;padding:10px;font:inherit}textarea{min-height:110px;resize:vertical}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:15px}.primary{background:var(--ink);color:var(--bg)}.danger{color:var(--red)}.note{font-size:12px;color:var(--muted);margin-top:10px}a{color:inherit}.hidden{display:none}@media(max-width:720px){.grid{grid-template-columns:1fr}.row{grid-template-columns:1fr}.top{align-items:flex-start}}
</style></head><body><main><div class="top"><span class="eyebrow">GP Cloud Preview · Control plane</span><div><button class="theme" id="theme">Theme</button> <button class="theme" id="logout">Log out</button></div></div>
<section class="hero"><div class="eyebrow">Single-user deployment operations</div><h1>Ship the work.<br>Keep the host calm.</h1><p class="muted">Public previews, Vault-backed configuration, and automatic cleanup for your sites and open-source projects.</p></section>
<div class="grid"><section class="card"><div style="display:flex;justify-content:space-between;gap:8px;align-items:center"><h2>Deployments</h2><div class="actions"><button class="button danger" id="stop-all">Stop all</button><button class="button danger" id="purge-all">Delete all</button></div></div><div id="deployments"><p class="muted">Loading…</p></div></section><section class="card"><h2>New preview</h2><form id="deploy-form"><label>Repository URL</label><input name="repo_url" placeholder="https://github.com/owner/project.git" required><label>Full commit SHA</label><input name="sha" minlength="40" maxlength="40" required><div class="row"><div><label>Project slug</label><input name="project" placeholder="my-site" required></div><div><label>Pull request</label><input name="pr_number" type="number" min="0" value="0"></div></div><div class="row"><div><label>Port</label><input name="app_port" type="number" value="2222"></div><div><label>TTL (seconds)</label><input name="ttl_seconds" type="number" value="86400"></div></div><label>Health path</label><input name="health_path" value="/"><label>Vault secret path (optional)</label><input name="vault_path" placeholder="gp-cloud/projects/my-site"><button class="button primary actions" type="submit">Create preview</button><p id="deploy-message" class="note"></p></form></section></div>
<section class="card" style="margin-top:18px"><h2>Usage</h2><div id="usage" class="muted">Loading metrics…</div></section>
<section class="card" style="margin-top:18px"><h2>Control settings</h2><form id="settings-form"><div class="row"><div><label>Default TTL (seconds)</label><input id="ttl" name="deployment_ttl_seconds" type="number"></div><div><label>Max TTL (seconds)</label><input id="max-ttl" name="max_deployment_ttl_seconds" type="number" readonly></div></div><label><input id="close-cleanup" name="delete_on_pull_request_close" type="checkbox" style="width:auto"> Delete previews when a pull request closes or merges</label><label>Project runtime profiles (JSON)</label><textarea id="projects" name="projects" spellcheck="false"></textarea><button class="button" type="submit">Save settings</button><p id="settings-message" class="note"></p></form><p class="note">Secrets are never stored here. Put runtime environment values in Vault and reference their path from a project or deployment.</p></section>
<section class="card" style="margin-top:18px"><h2>Host configuration</h2><div id="host-config" class="muted">Loading configuration inventory…</div><p class="note">Non-secret settings can be edited here and take effect after restarting <code>gp-cloud.service</code>. Credentials and the admin password remain local-only.</p></section>
<section class="card" style="margin-top:18px"><h2>Vault environment</h2><form id="vault-form"><label>KV v2 path</label><input name="path" placeholder="gp-cloud/projects/my-app" required><label>Environment values</label><textarea name="values" placeholder="DATABASE_URL=...&#10;OPENAI_API_KEY=..." required></textarea><button class="button" type="submit">Write encrypted values to Vault</button><p id="vault-message" class="note"></p></form></section>
</main><script>
const $=s=>document.querySelector(s); const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const r=await fetch('/ui/api/deployments');if(r.status===401){location='/ui/login';return}const d=await r.json();$('#deployments').innerHTML=d.deployments.length?d.deployments.map(x=>`<div class="deploy"><div><h3>${esc(x.slug)}</h3><div class="muted"><span class="status ${esc(x.state)}">${esc(x.state)}</span> · expires ${esc(x.expires_at||'managed')}</div></div><div>${x.preview_url?`<a class="button" target="_blank" href="${esc(x.preview_url)}">Open</a>`:''}${['STOPPED','FAILED'].includes(x.state)?'':`<button class="button danger" onclick="stop('${esc(x.id)}')">Stop</button>`}</div></div>`).join(''):'<p class="muted">No deployments yet.</p>';const u=await fetch('/ui/api/usage');if(u.ok){const x=await u.json();const gib=n=>(n/1073741824).toFixed(2)+' GiB';const containers=x.containers.map(c=>`${esc(c.name)}: ${esc(c.cpu_percent)} CPU, ${esc(c.memory)} RAM, ${esc(c.pids)} PIDs`).join('<br>')||'No running containers';$('#usage').innerHTML=`<div class="row"><div><b>${gib(x.gp_cloud_bytes)}</b><br><span class="muted">GP Cloud storage</span></div><div><b>${gib(x.logs_bytes)}</b><br><span class="muted">Logs</span></div><div><b>${gib(x.deployments_bytes)}</b><br><span class="muted">Deployment artifacts</span></div><div><b>${gib(x.disk.free_bytes)}</b><br><span class="muted">VM free space</span></div><div><b>${x.queue_depth}</b><br><span class="muted">Queued jobs</span></div></div><p class="note">States: ${esc(JSON.stringify(x.states))} · Runtime limits: ${esc(JSON.stringify(x.resource_limits))}</p><p class="note">Container usage:<br>${containers}</p>`}}
async function stop(id){await fetch('/ui/api/deployments/'+encodeURIComponent(id)+'/stop',{method:'POST'});load()}
$('#stop-all').onclick=async()=>{if(confirm('Stop every active deployment?')){await fetch('/ui/api/deployments/stop-all',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});load()}};$('#purge-all').onclick=async()=>{if(confirm('Stop and permanently delete all deployment records, logs, routes, and runtime artifacts?')){await fetch('/ui/api/deployments/stop-all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({purge:true})});load()}};
async function settings(){const r=await fetch('/ui/api/settings');if(!r.ok)return;const x=await r.json();$('#ttl').value=x.deployment_ttl_seconds;$('#max-ttl').value=x.max_deployment_ttl_seconds;$('#close-cleanup').checked=x.delete_on_pull_request_close;$('#projects').value=JSON.stringify(x.projects||{},null,2)}
async function hostConfig(){const r=await fetch('/ui/api/host-config');if(!r.ok)return;const x=await r.json();const editable=x.keys.filter(k=>!k.secret);const configured=x.keys.filter(k=>k.configured).length;const missing=x.keys.length-configured;$('#host-config').innerHTML=`<p><b>Live env file:</b> ${esc(x.path)} · <span class="status ${x.available?'RUNNING':'FAILED'}">${x.available?'available':'missing'}</span></p><p><b>Vault:</b> <span class="status ${x.vault_configured?'RUNNING':'FAILED'}">${x.vault_configured?'configured':'not configured'}</span> · allowed path prefix: <code>${esc(x.vault_path_prefix)}</code></p><p><b>Variables:</b> ${configured} configured · ${missing} missing · secret values hidden</p><form id="host-env-form"><div>${editable.map(k=>`<label>${esc(k.name)} <span class="muted">(${k.configured?'configured':'missing'})</span><input data-env-name="${esc(k.name)}" value="${esc(k.value||'')}" autocomplete="off"></label>`).join(' ')}</div><button class="button" type="submit">Save non-secret settings</button><span id="host-env-message" class="note"></span></form><p class="note">Secret variables: ${x.keys.filter(k=>k.secret).map(k=>esc(k.name)).join(', ')||'none'}. Edit those only in the local env file.</p></div>`;$('#host-env-form').onsubmit=async e=>{e.preventDefault();const values={};e.target.querySelectorAll('[data-env-name]').forEach(i=>values[i.dataset.envName]=i.value);const saved=await fetch('/ui/api/host-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});$('#host-env-message').textContent=saved.ok?'Saved locally; restart gp-cloud.service to apply.':((await saved.json()).error||'Could not save');if(saved.ok)hostConfig()}}
$('#deploy-form').onsubmit=async e=>{e.preventDefault();const x=Object.fromEntries(new FormData(e.target));for(const k of ['pr_number','app_port','ttl_seconds'])x[k]=Number(x[k]);const r=await fetch('/ui/api/deployments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});const d=await r.json();$('#deploy-message').textContent=r.ok?'Queued '+d.id:(d.error||'Deployment failed');load()};
$('#settings-form').onsubmit=async e=>{e.preventDefault();const x=Object.fromEntries(new FormData(e.target));x.deployment_ttl_seconds=Number(x.deployment_ttl_seconds);x.delete_on_pull_request_close=$('#close-cleanup').checked;try{x.projects=JSON.parse($('#projects').value||'{}')}catch(_){$('#settings-message').textContent='Project JSON is invalid';return}const r=await fetch('/ui/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});$('#settings-message').textContent=r.ok?'Saved':'Could not save'};
$('#vault-form').onsubmit=async e=>{e.preventDefault();const x=Object.fromEntries(new FormData(e.target));const r=await fetch('/ui/api/vault',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(x)});const d=await r.json();$('#vault-message').textContent=r.ok?'Written to Vault':(d.error||'Could not write to Vault')};
$('#logout').onclick=async()=>{await fetch('/ui/logout',{method:'POST'});location='/ui/login'};$('#theme').onclick=()=>{const d=document.documentElement;d.dataset.theme=d.dataset.theme==='dark'?'light':'dark';localStorage.theme=d.dataset.theme};document.documentElement.dataset.theme=localStorage.theme||'light';load();settings();hostConfig();setInterval(load,10000);
</script></body></html>"""


def login_html() -> str:
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>GP Cloud Preview · Sign in</title><style>body{margin:0;background:#fbfaf7;color:#111;font:14px Inter,system-ui,sans-serif}main{max-width:420px;margin:15vh auto;padding:28px}h1{font:400 44px Georgia,serif}input,button{width:100%;box-sizing:border-box;padding:12px;margin:8px 0;border:1px solid #e8e5df;border-radius:10px;background:#fff;font:inherit}button{background:#111;color:#fff;cursor:pointer}</style></head><body><main><small>GP CLOUD PREVIEW</small><h1>Welcome back.</h1><p>Sign in to manage deployments.</p><form method="post" action="/ui/login"><input type="password" name="password" placeholder="Control password" autofocus required><button>Sign in</button></form></main></body></html>"""


def github_event(payload: dict, event: str) -> dict | None:
    repo = repo_name(payload)
    if not allowed_repo(repo):
        raise PermissionError("repository is not allowlisted")
    installation = str((payload.get("installation") or {}).get("id") or "")
    if GITHUB_INSTALLATION_ID and installation and installation != GITHUB_INSTALLATION_ID:
        raise PermissionError("GitHub App installation is not authorized")
    if event == "issue_comment":
        issue = payload.get("issue") or {}
        if not issue.get("pull_request"):
            return None
        comment = str((payload.get("comment") or {}).get("body") or "").strip()
        if not comment.startswith("/deploy"):
            return None
        pr = issue.get("pull_request") or {}
        pr_url = str(pr.get("url") or "")
        if pr_url:
            pr = github_api_json(pr_url)
        head = pr.get("head") or {}
        head_repo = head.get("repo") or {}
        if head_repo.get("full_name", repo).lower() != repo and not ALLOW_FORKS:
            raise PermissionError("fork PR deployments are disabled")
        return enqueue(
            {
                "repo_url": head_repo.get("clone_url") or f"https://github.com/{repo}.git",
                "sha": head.get("sha"),
                "project": repo.split("/")[-1],
                "pr_number": issue.get("number"),
                "app_port": DEFAULT_APP_PORT,
                "health_path": "/",
            },
            "github_issue_comment",
        )
    if event == "pull_request" and (payload.get("action") == "closed"):
        if not load_settings().get("delete_on_pull_request_close", True):
            return None
        number = int((payload.get("pull_request") or {}).get("number") or 0)
        for path in STATE_DIR.glob("dep_*.json"):
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                state.get("repo") == repo
                and int(state.get("pr_number") or 0) == number
                and state.get("state") not in {"STOPPED", "FAILED"}
            ):
                stop_deployment(state["id"])
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "gp-cloud/1.0"

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default request logging, which could capture sensitive URLs."""
        return

    def send_json(self, status: int, value: object) -> None:
        """Send a non-cacheable API response with browser hardening headers."""
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, status: int, value: str, cookie: str = "") -> None:
        """Send dashboard HTML without allowing stale authenticated content."""
        data = value.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str) -> None:
        """Send a temporary browser redirect without a response body."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self) -> bytes:
        """Read a bounded request body to prevent unbounded memory allocation."""
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def authorized(self) -> bool:
        """Check the control-plane bearer token using constant-time comparison."""
        return bool(API_TOKEN) and hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {API_TOKEN}"
        )

    def do_GET(self) -> None:
        """Route health, metrics, dashboard, action-status, and deployment reads."""
        request_path = urllib.parse.urlsplit(self.path).path
        if request_path == "/ui/login":
            return self.send_html(200, login_html())
        if request_path == "/ui":
            return (
                self.redirect("/ui/login")
                if not ui_session(self)
                else self.send_html(200, ui_html())
            )
        if request_path.startswith("/ui/api/"):
            if not ui_session(self):
                return self.send_json(401, {"error": "ui authentication required"})
            if request_path == "/ui/api/deployments":
                return self.send_json(
                    200, {"deployments": deployment_records(), "count": len(deployment_records())}
                )
            if request_path == "/ui/api/settings":
                settings = load_settings()
                settings["vault_configured"] = bool(VAULT_ADDR and vault_token())
                return self.send_json(200, settings)
            if request_path == "/ui/api/host-config":
                return self.send_json(200, host_config_summary())
            if request_path == "/ui/api/usage":
                return self.send_json(200, usage_metrics())
            return self.send_json(404, {"error": "not found"})
        if request_path == "/":
            return self.redirect("/ui")
        if request_path == "/healthz":
            return self.send_json(200, {"ok": True, "service": "gp-cloud"})
        if request_path == "/metrics":
            usage = usage_metrics()
            counts: dict[str, int] = {}
            for path in STATE_DIR.glob("dep_*.json"):
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                    status = str(state.get("state", "UNKNOWN"))
                    counts[status] = counts.get(status, 0) + 1
                except json.JSONDecodeError:
                    continue
            lines = [
                "# HELP gp_cloud_deployments Current deployments by lifecycle state.",
                "# TYPE gp_cloud_deployments gauge",
            ]
            for status, count in sorted(counts.items()):
                lines.append(f'gp_cloud_deployments{{state="{status}"}} {count}')
            lines.extend(
                [
                    "# HELP gp_cloud_queue_depth Number of queued deployment jobs.",
                    "# TYPE gp_cloud_queue_depth gauge",
                    f"gp_cloud_queue_depth {JOBS.qsize()}",
                    "# HELP gp_cloud_storage_bytes Bytes used below the GP Cloud root.",
                    "# TYPE gp_cloud_storage_bytes gauge",
                    f"gp_cloud_storage_bytes {usage['gp_cloud_bytes']}",
                    "# HELP gp_cloud_logs_bytes Bytes used by retained logs.",
                    "# TYPE gp_cloud_logs_bytes gauge",
                    f"gp_cloud_logs_bytes {usage['logs_bytes']}",
                    "# HELP gp_cloud_deployments_bytes Bytes used by deployment artifacts.",
                    "# TYPE gp_cloud_deployments_bytes gauge",
                    f"gp_cloud_deployments_bytes {usage['deployments_bytes']}",
                    "# HELP gp_cloud_disk_free_bytes Free bytes on the filesystem containing GP Cloud.",
                    "# TYPE gp_cloud_disk_free_bytes gauge",
                    f"gp_cloud_disk_free_bytes {usage['disk']['free_bytes']}",
                    "# HELP gp_cloud_disk_total_bytes Total bytes on the filesystem containing GP Cloud.",
                    "# TYPE gp_cloud_disk_total_bytes gauge",
                    f"gp_cloud_disk_total_bytes {usage['disk']['total_bytes']}",
                    "# HELP gp_cloud_disk_used_bytes Used bytes on the filesystem containing GP Cloud.",
                    "# TYPE gp_cloud_disk_used_bytes gauge",
                    f"gp_cloud_disk_used_bytes {usage['disk']['used_bytes']}",
                    "# HELP gp_cloud_containers Running GP Cloud containers.",
                    "# TYPE gp_cloud_containers gauge",
                    f"gp_cloud_containers {len(usage['containers'])}",
                ]
            )
            data = ("\n".join(lines) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        action_match = re.fullmatch(r"/actions/gp-cloud-deploy/([A-Za-z0-9_-]+)", request_path)
        if action_match:
            token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            repo = self.headers.get("X-GitHub-Repository", "").lower().strip()
            state = read_state(action_match.group(1))
            if not state or not action_state_authorized(state, token, repo):
                return self.send_json(403, {"error": "GitHub Action is not authorized"})
            return self.send_json(200, public_action_state(state))
        if request_path == "/v1/deployments":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            requested_state = str((query.get("state") or [""])[0]).upper()
            deployments = deployment_records()
            if requested_state:
                deployments = [
                    item
                    for item in deployments
                    if str(item.get("state", "")).upper() == requested_state
                ]
            if self.authorized():
                return self.send_json(200, {"deployments": deployments, "count": len(deployments)})
            return self.send_json(
                200,
                {
                    "deployments": [public_deployment_summary(item) for item in deployments],
                    "count": len(deployments),
                },
            )
        match = re.fullmatch(r"/v1/deployments/([A-Za-z0-9_-]+)(/logs)?", request_path)
        if not match or not self.authorized():
            return self.send_json(
                404 if not match else 401, {"error": "not found" if not match else "unauthorized"}
            )
        deployment_id, logs = match.groups()
        state = read_state(deployment_id)
        if not state:
            return self.send_json(404, {"error": "deployment not found"})
        if logs:
            path = deployment_log_path(state)
            content = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            return self.send_json(200, {"id": deployment_id, "logs": content[-200_000:]})
        return self.send_json(200, public_action_state(state))

    def do_POST(self) -> None:
        """Route authenticated mutations, webhooks, and GitHub Action requests."""
        try:
            body = self.body()
            request_path = urllib.parse.urlsplit(self.path).path
            if request_path == "/ui/login":
                values = urllib.parse.parse_qs(body.decode("utf-8"))
                password = (values.get("password") or [""])[0]
                if not admin_password_matches(password):
                    return self.send_html(401, login_html())
                token = secrets.token_urlsafe(32)
                with SESSION_LOCK:
                    SESSIONS[token] = time.time() + 8 * 60 * 60
                self.send_response(303)
                self.send_header("Location", "/ui")
                secure = "; Secure" if COOKIE_SECURE else ""
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict{secure}; Max-Age=28800",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if request_path == "/ui/logout":
                raw = self.headers.get("Cookie", "")
                token = next(
                    (
                        item.split("=", 1)[1]
                        for item in raw.split("; ")
                        if item.startswith(f"{SESSION_COOKIE}=")
                    ),
                    "",
                )
                with SESSION_LOCK:
                    SESSIONS.pop(token, None)
                self.send_response(303)
                self.send_header("Location", "/ui/login")
                secure = "; Secure" if COOKIE_SECURE else ""
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}",
                )
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if request_path.startswith("/ui/api/"):
                if not ui_session(self):
                    return self.send_json(401, {"error": "ui authentication required"})
                request = json.loads(body)
                if request_path == "/ui/api/settings":
                    return self.send_json(200, save_settings(request))
                if request_path == "/ui/api/host-config":
                    return self.send_json(200, save_host_config(request.get("values")))
                if request_path == "/ui/api/vault":
                    vault_path = validate_vault_path(request.get("path"))
                    if not vault_path:
                        return self.send_json(400, {"error": "Vault path is required"})
                    values: dict[str, str] = {}
                    for line in str(request.get("values") or "").splitlines():
                        if not line.strip() or line.lstrip().startswith("#"):
                            continue
                        key, separator, value = line.partition("=")
                        if not separator or not re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*", key.strip()
                        ):
                            return self.send_json(
                                400, {"error": "environment values must use KEY=VALUE lines"}
                            )
                        value = value.strip()
                        if "\n" in value or "\r" in value:
                            return self.send_json(
                                400, {"error": "environment values cannot contain newlines"}
                            )
                        values[key.strip()] = value
                    if not values:
                        return self.send_json(
                            400, {"error": "at least one environment value is required"}
                        )
                    vault_write_env(vault_path, values)
                    return self.send_json(200, {"path": vault_path, "keys": sorted(values)})
                if request_path == "/ui/api/deployments":
                    result = enqueue(request, "control_ui")
                    return self.send_json(202, public_action_state(result))
                if request_path == "/ui/api/deployments/stop-all":
                    if request.get("purge") is True:
                        return self.send_json(200, purge_all_deployments())
                    return self.send_json(202, {"stopped": stop_all_deployments()})
                stop_match = re.fullmatch(
                    r"/ui/api/deployments/([A-Za-z0-9_-]+)/stop", request_path
                )
                if stop_match:
                    result = stop_deployment(stop_match.group(1))
                    return self.send_json(
                        404 if result is None else 202,
                        public_action_state(result or {"error": "not found"}),
                    )
                return self.send_json(404, {"error": "not found"})
            if request_path == "/webhooks/github":
                if not verify_signature(self, body):
                    return self.send_json(401, {"error": "invalid webhook signature"})
                payload = json.loads(body)
                event = self.headers.get("X-GitHub-Event", "")
                result = github_event(payload, event)
                return self.send_json(202, result or {"accepted": True})
            if request_path == "/actions/gp-cloud-deploy":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                repo = self.headers.get("X-GitHub-Repository", "").lower().strip()
                if not token or not re.fullmatch(r"[^/]+/[^/]+", repo) or not allowed_repo(repo):
                    return self.send_json(403, {"error": "GitHub Action is not authorized"})
                request = json.loads(body)
                number = int(request.get("pr_number") or 0)
                if number <= 0:
                    return self.send_json(400, {"error": "pr_number is required"})
                pr = github_api_json_with_token(
                    f"https://api.github.com/repos/{repo}/pulls/{number}", token
                )
                head = pr.get("head") or {}
                head_repo = head.get("repo") or {}
                head_name = str(head_repo.get("full_name") or repo).lower()
                if head_name != repo and not ALLOW_FORKS:
                    return self.send_json(403, {"error": "fork PR deployments are disabled"})
                result = enqueue(
                    {
                        "repo_url": head_repo.get("clone_url") or f"https://github.com/{repo}.git",
                        "sha": head.get("sha"),
                        "project": repo.split("/")[-1],
                        "pr_number": number,
                        "app_port": DEFAULT_APP_PORT,
                        "health_path": "/",
                    },
                    "github_action",
                    clone_token=token,
                )
                result["preview_url"] = (
                    f"{PUBLIC_SCHEME}://{result['slug']}.{PREVIEW_DOMAIN}{PUBLIC_URL_SUFFIX}"
                )
                return self.send_json(202, public_action_state(result))
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            if request_path == "/v1/deployments":
                result = enqueue(json.loads(body), "control_api")
                return self.send_json(202, public_action_state(result))
            match = re.fullmatch(r"/v1/deployments/([A-Za-z0-9_-]+)/stop", request_path)
            if match:
                result = stop_deployment(match.group(1))
                return self.send_json(
                    404 if result is None else 202, result or {"error": "deployment not found"}
                )
            return self.send_json(404, {"error": "not found"})
        except PermissionError as error:
            self.send_json(403, {"error": str(error)})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})


def main() -> None:
    """Initialize local state, start background workers, and serve loopback HTTP."""
    for directory in (STATE_DIR, QUEUE_DIR, DEPLOYMENTS):
        directory.mkdir(parents=True, exist_ok=True)
    backfill_expirations()
    if not API_TOKEN:
        raise SystemExit("GP_CLOUD_API_TOKEN is required")
    for marker in sorted(QUEUE_DIR.iterdir()):
        if marker.is_file():
            JOBS.put(marker.stem)
    thread = threading.Thread(target=worker_loop, name="gp-cloud-worker", daemon=True)
    thread.start()
    cleanup_thread = threading.Thread(target=cleanup_loop, name="gp-cloud-cleanup", daemon=True)
    cleanup_thread.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def shutdown(_signum: int, _frame: object) -> None:
        STOP.set()
        # shutdown() must run outside serve_forever()'s signal-handler thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()


if __name__ == "__main__":
    main()
