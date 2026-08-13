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
import ipaddress
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
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("GP_CLOUD_ROOT", "/opt/gp-cloud"))
DATA = ROOT / "data"
DEPLOYMENTS = ROOT / "deployments"
WORKER = ROOT / "worker" / "gp-cloud-deploy"
CLEANER = ROOT / "worker" / "gp-cloud-clean"
CADDY_MANAGED_MARKER = Path(
    os.environ.get("GP_CLOUD_CADDY_MANAGED_MARKER", "/etc/caddy/.gp-cloud-preview-managed")
)
PORT = int(os.environ.get("GP_CLOUD_CONTROL_PORT", "8787"))
DEFAULT_APP_PORT = int(os.environ.get("GP_CLOUD_DEFAULT_APP_PORT", "2222"))
API_TOKEN = os.environ.get("GP_CLOUD_API_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("GP_CLOUD_GITHUB_WEBHOOK_SECRET", "")
PREVIEW_DOMAIN = os.environ.get("GP_CLOUD_PREVIEW_DOMAIN", "preview.example.com")
PUBLIC_SCHEME = os.environ.get("GP_CLOUD_PUBLIC_SCHEME", "http")
PUBLIC_PORT = int(os.environ.get("GP_CLOUD_HTTP_PORT", "80"))
PUBLIC_URL_SUFFIX = "" if PUBLIC_PORT in {80, 443} else f":{PUBLIC_PORT}"
COOKIE_SECURE = os.environ.get("GP_CLOUD_COOKIE_SECURE", "false").lower() == "true"
WEBHOOK_DELIVERY_TTL_SECONDS = max(
    300, int(os.environ.get("GP_CLOUD_WEBHOOK_DELIVERY_TTL_SECONDS", "604800"))
)
MAX_QUEUE_DEPTH = max(1, int(os.environ.get("GP_CLOUD_MAX_QUEUE_DEPTH", "100")))
TRUSTED_AUTHOR_ASSOCIATIONS = {
    item.strip().upper()
    for item in os.environ.get(
        "GP_CLOUD_TRUSTED_AUTHOR_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR"
    ).split(",")
    if item.strip()
}
HOST_CONFIG_KEYS = (
    "GP_CLOUD_PREVIEW_DOMAIN",
    "GP_CLOUD_HTTP_PORT",
    "GP_CLOUD_PUBLIC_SCHEME",
    "GP_CLOUD_COOKIE_SECURE",
    "GP_CLOUD_DEFAULT_APP_PORT",
    "GP_CLOUD_CONTROL_PORT",
    "GP_CLOUD_ALLOWED_REPOS",
    "GP_CLOUD_ALLOW_FORKS",
    "GP_CLOUD_ALLOW_PR_SECRETS",
    "GP_CLOUD_ALLOW_PR_BUILD_NETWORK",
    "GP_CLOUD_CPU_LIMIT",
    "GP_CLOUD_MEMORY_LIMIT",
    "GP_CLOUD_BUILD_MEMORY_LIMIT",
    "GP_CLOUD_BUILD_CPU_QUOTA",
    "GP_CLOUD_BUILD_TIMEOUT_SECONDS",
    "GP_CLOUD_STARTUP_TIMEOUT_SECONDS",
    "GP_CLOUD_HEALTH_TIMEOUT_SECONDS",
    "GP_CLOUD_DEPLOYMENT_TTL_SECONDS",
    "GP_CLOUD_MAX_DEPLOYMENT_TTL_SECONDS",
    "GP_CLOUD_VAULT_ADDR",
    "GP_CLOUD_VAULT_MOUNT",
    "GP_CLOUD_VAULT_PATH_PREFIX",
    "GP_CLOUD_RETAIN_WORKSPACES",
    "GP_CLOUD_WEBHOOK_DELIVERY_TTL_SECONDS",
    "GP_CLOUD_MAX_QUEUE_DEPTH",
    "GP_CLOUD_TRUSTED_AUTHOR_ASSOCIATIONS",
)
ALLOWED_REPOS = {
    item.strip().lower()
    for item in os.environ.get("GP_CLOUD_ALLOWED_REPOS", "").split(",")
    if item.strip()
}
ALLOW_FORKS = os.environ.get("GP_CLOUD_ALLOW_FORKS", "false").lower() == "true"
ALLOW_PR_SECRETS = os.environ.get("GP_CLOUD_ALLOW_PR_SECRETS", "false").lower() == "true"
ALLOW_PR_BUILD_NETWORK = (
    os.environ.get("GP_CLOUD_ALLOW_PR_BUILD_NETWORK", "false").lower() == "true"
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "")
GITHUB_INSTALLATION_ID = os.environ.get("GITHUB_INSTALLATION_ID", "")
GITHUB_PRIVATE_KEY_FILE = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE", "")

STATE_DIR = DATA / "deployments"
PREVIEW_DIR = DATA / "previews"
QUEUE_DIR = DATA / "queue"
TOKEN_DIR = DATA / "action-tokens"
DELIVERY_DIR = DATA / "webhook-deliveries"
SETTINGS_FILE = DATA / "control-settings.json"
FAILURE_COUNTER_FILE = DATA / "deployment-failures.json"
SESSION_COOKIE = "gp_cloud_session"
SESSIONS: dict[str, float] = {}
SESSION_LOCK = threading.RLock()
LOGIN_FAILURES: dict[str, list[float]] = {}
ACTION_REQUESTS: dict[str, list[float]] = {}
ACTION_VALIDATION_SLOTS = threading.BoundedSemaphore(4)
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
JOBS: queue.Queue[tuple[str, str]] = queue.Queue()
STOP = threading.Event()

DEPLOYMENT_TRANSITIONS = {
    "QUEUED": {"BUILDING", "STOPPED", "FAILED"},
    "BUILDING": {"QUEUED", "RUNNING", "STOPPED", "FAILED"},
    "RUNNING": {"STOPPED", "FAILED", "SUPERSEDED"},
    "STOPPED": set(),
    "FAILED": set(),
    "SUPERSEDED": set(),
}
TERMINAL_STATES = {"STOPPED", "FAILED", "SUPERSEDED"}
ACTIVE_STATES = {"QUEUED", "BUILDING", "RUNNING"}


class DeploymentStopRequested(RuntimeError):
    """Signal that a candidate must stop without being recorded as a failure."""


class DeploymentQueueFull(RuntimeError):
    """Signal that the durable deployment queue is at its configured limit."""


def now() -> str:
    """Return an ISO-8601 UTC timestamp for persisted state and logs."""
    return datetime.now(UTC).isoformat()


def load_settings() -> dict:
    """
    Load operator settings, using defaults when the settings file is missing or invalid.
    
    Returns:
        dict: Operator settings with default values applied.
    """
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
    atomic_json(SETTINGS_FILE.parent, SETTINGS_FILE.name, allowed)
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
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
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
    """Validate and normalize a Vault path within the operator-owned namespace.
    
    Parameters:
        value (object): Vault path value to validate.
    
    Returns:
        str: The normalized Vault path, or an empty string for an empty value.
    
    Raises:
        ValueError: If the path contains invalid characters, traversal segments, or falls outside the configured Vault prefix.
    """
    path = str(value or "").strip().strip("/")
    if not path:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", path):
        raise ValueError("invalid Vault path")
    # A textual prefix check is insufficient when dot segments can escape the
    # operator-owned namespace after Vault or a proxy normalizes the path.
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("invalid Vault path")
    prefix_root = VAULT_PATH_PREFIX.strip("/")
    if path != prefix_root and not path.startswith(f"{prefix_root}/"):
        raise ValueError(f"Vault path must be below {VAULT_PATH_PREFIX}")
    return path


def safe_filename(filename: str) -> str:
    """Accept only one ordinary filename without path separators or dot segments."""
    match = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", filename)
    if match is None or match.group(0) in {".", ".."}:
        raise ValueError("invalid managed filename")
    return match.group(0)


def atomic_json(directory: Path, filename: str, value: dict) -> None:
    """Replace a JSON file atomically so readers never see partial state."""
    safe_name = safe_filename(filename)
    directory.mkdir(parents=True, exist_ok=True)  # lgtm [py/path-injection]
    destination = directory / safe_name  # lgtm [py/path-injection]
    fd, name = tempfile.mkstemp(  # lgtm [py/path-injection]
        prefix=f".{safe_name}.", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(name, destination)  # lgtm [py/path-injection]
    finally:
        if os.path.exists(name):  # lgtm [py/path-injection]
            os.unlink(name)  # lgtm [py/path-injection]


def atomic_text(directory: Path, filename: str, value: str) -> None:
    """
    Atomically replace a local text file with the specified content.
    
    Parameters:
        directory (Path): Managed destination directory.
        filename (str): Validated destination filename.
        value (str): Text to write.
        mode (int): File permission mode for the replacement file.
    """
    safe_name = safe_filename(filename)
    directory.mkdir(parents=True, exist_ok=True)  # lgtm [py/path-injection]
    destination = directory / safe_name  # lgtm [py/path-injection]
    fd, name = tempfile.mkstemp(  # lgtm [py/path-injection]
        prefix=f".{safe_name}.", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.chmod(name, 0o600)
        os.replace(name, destination)  # lgtm [py/path-injection]
    finally:
        if os.path.exists(name):  # lgtm [py/path-injection]
            os.unlink(name)  # lgtm [py/path-injection]


def atomic_route_text(directory: Path, filename: str, value: str) -> None:
    """Replace a routing file atomically with owner-only permissions."""
    atomic_text(directory, filename, value)


def state_path(deployment_id: str) -> Path:
    """Map a validated deployment identifier to its state file."""
    match = re.fullmatch(r"dep_[0-9]+_[0-9a-f]+", deployment_id)
    if match is None:
        raise ValueError("invalid deployment id")
    return STATE_DIR / f"{match.group(0)}.json"  # lgtm [py/path-injection]


def preview_identity(repo: str, pr_number: int, project: str) -> tuple[str, str]:
    """Return a stable preview identifier and hostname slug for one environment."""
    key = f"{repo}#pr:{pr_number}" if pr_number else f"{repo}#project:{project}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    preview_id = f"preview_{digest[:20]}"
    readable = slugify(f"{project}-pr-{pr_number}" if pr_number else project, 253)
    slug = f"{readable[:41].rstrip('-')}-{digest[:8]}"
    return preview_id, slug


def preview_path(preview_id: str) -> Path:
    """
    Map a preview identifier to its durable state file path.
    
    Raises:
        ValueError: If `preview_id` does not match the required format.
    
    Returns:
        Path: The path to the preview's state file.
    """
    if not re.fullmatch(r"preview_[0-9a-f]{20}", preview_id):
        raise ValueError("invalid preview id")
    return PREVIEW_DIR / f"{preview_id}.json"


def read_preview(preview_id: str) -> dict | None:
    """Read one preview record, treating incomplete files as absent."""
    try:
        return json.loads(preview_path(preview_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_preview(preview: dict) -> None:
    """Atomically persist a preview and its current-deployment pointer."""
    preview["updated_at"] = now()
    with LOCK:
        atomic_json(PREVIEW_DIR, f"{preview['id']}.json", preview)


def read_state(deployment_id: str) -> dict | None:
    """Read one deployment state record, treating missing or incomplete files as absent."""
    try:
        return json.loads(  # lgtm [py/path-injection]
            state_path(deployment_id).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def deployment_failure_count() -> int:
    """Read the durable monotonic count of deployment failure events."""
    try:
        value = json.loads(FAILURE_COUNTER_FILE.read_text(encoding="utf-8"))
        return max(0, int(value.get("count", 0))) if isinstance(value, dict) else 0
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def record_deployment_failure() -> None:
    """Persist one failure event for time-windowed monitoring."""
    atomic_json(
        FAILURE_COUNTER_FILE.parent,
        FAILURE_COUNTER_FILE.name,
        {"count": deployment_failure_count() + 1},
    )


def write_state(state: dict) -> None:
    """Stamp and atomically persist a deployment state under the process lock."""
    state["updated_at"] = now()
    with LOCK:
        atomic_json(STATE_DIR, f"{state['id']}.json", state)


def update_state(deployment_id: str, **changes: object) -> dict | None:
    """
    Update fields in a deployment record and persist the changes.
    
    Parameters:
        deployment_id (str): Identifier of the deployment record to update.
        **changes (object): Field values to apply to the record.
    
    Returns:
        dict | None: The updated deployment record, or `None` if it does not exist.
    """
    with LOCK:
        state = read_state(deployment_id)
        if state is None:
            return None
        state.update(changes)
        write_state(state)
        return state


def transition_state(deployment_id: str, target: str, **changes: object) -> dict | None:
    """
    Apply a valid deployment lifecycle transition and update the deployment record.
    
    Parameters:
        deployment_id (str): Identifier of the deployment to update
        target (str): Desired lifecycle state
        changes (object): Additional deployment fields to update
    
    Returns:
        dict | None: The updated deployment record, or `None` if the deployment does not exist
    
    Raises:
        ValueError: If the requested transition is invalid
    """
    with LOCK:
        state = read_state(deployment_id)
        if state is None:
            return None
        current = str(state.get("state") or "")
        if target != current and target not in DEPLOYMENT_TRANSITIONS.get(current, set()):
            raise ValueError(f"invalid deployment transition: {current} -> {target}")
        state.update(changes)
        state["state"] = target
        write_state(state)
        if target == "FAILED" and current != "FAILED":
            record_deployment_failure()
        return state


def queue_operation(action: str, deployment_id: str) -> None:
    """
    Queue a deployment operation durably for worker processing.
    
    Parameters:
        action (str): The operation to queue: ``deploy``, ``stop``, or ``cleanup``.
        deployment_id (str): The deployment identifier targeted by the operation.
    
    Raises:
        ValueError: If the operation or deployment identifier is invalid.
    """
    deployment_match = re.fullmatch(r"dep_[0-9]+_[0-9a-f]+", deployment_id)
    if action not in {"deploy", "stop", "cleanup"} or deployment_match is None:
        raise ValueError("invalid worker operation")
    safe_action = action
    safe_deployment_id = deployment_match.group(0)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    marker_name = safe_filename(f"{safe_deployment_id}.{safe_action}")
    marker = QUEUE_DIR / marker_name  # lgtm [py/path-injection]
    if marker.is_symlink():
        raise ValueError("invalid worker operation path")
    if not marker.exists():
        atomic_text(QUEUE_DIR, marker_name, f"{safe_action}\n")
    JOBS.put((safe_action, safe_deployment_id))


def repo_name(payload: dict) -> str:
    """Extract a normalized repository name from a GitHub event payload."""
    repo = payload.get("repository") or {}
    return str(repo.get("full_name") or "").lower()


def allowed_repo(name: str) -> bool:
    """
    Determine whether a repository is permitted by the configured allowlist.
    
    Parameters:
        name (str): Repository name to check.
    
    Returns:
        bool: `true` if the allowlist is configured and contains the repository name, `false` otherwise.
    """
    return bool(ALLOWED_REPOS) and name.lower() in ALLOWED_REPOS


def slugify(value: str, max_length: int = 50) -> str:
    """
    Convert project text into a lowercase DNS- and container-compatible slug.
    
    Parameters:
        value (str): Project text to normalize.
        max_length (int): Maximum length of the resulting slug.
    
    Returns:
        str: A normalized slug truncated to `max_length` characters.
    
    Raises:
        ValueError: If normalization produces an empty slug.
    """
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    if not value:
        raise ValueError("project slug cannot be empty")
    return value[:max_length]


def valid_dns_name(value: str) -> bool:
    """Validate a DNS name using bounded label checks without backtracking regexes."""
    if not 1 <= len(value) <= 253 or value.endswith("."):
        return False
    labels = value.split(".")
    return len(labels) >= 2 and all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(char.isalnum() or char == "-" for char in label)
        for label in labels
    ) and 2 <= len(labels[-1]) <= 63 and all(char.isalpha() for char in labels[-1])


def valid_repository_name(value: str) -> bool:
    """Validate one owner/repository entry without a regex over user input."""
    owner, separator, repository = value.partition("/")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    return bool(separator and owner and repository) and all(
        char in allowed for char in owner + repository
    )


def require_sha(value: object) -> str:
    """
    Validate and normalize a commit SHA for immutable deployments.
    
    Parameters:
        value (object): Value expected to contain a 40-character hexadecimal commit SHA
    
    Returns:
        str: The lowercase commit SHA
    
    Raises:
        ValueError: If the value is not a 40-character hexadecimal commit SHA
    """
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


def validate_health_path(value: object) -> str:
    """Accept one bounded URL path that is safe in health checks and JSON metadata."""
    path = str(value or "/")
    if not re.fullmatch(r"/[A-Za-z0-9._~/?&=%:+,@-]{0,500}", path):
        raise ValueError("health_path must be a safe URL path beginning with /")
    return path


def normalize_job(body: dict) -> dict:
    """
    Validate and normalize an untrusted deployment request.
    
    Parameters:
        body (dict): Deployment request data containing repository, commit, project, port, health-check, Vault, and TTL settings.
    
    Returns:
        dict: Normalized deployment data with validated repository and commit identifiers, preview identity, runtime settings, and expiration metadata.
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
    health_path = validate_health_path(body.get("health_path"))
    vault_path = validate_vault_path(body.get("vault_path"))
    if not allowed_repo(repo):
        raise PermissionError("repository is not in GP_CLOUD_ALLOWED_REPOS")
    if not API_TOKEN:
        raise RuntimeError("GP_CLOUD_API_TOKEN is not configured")
    preview_id, preview_slug = preview_identity(repo, pr_number, project)
    settings = load_settings()
    ttl_seconds = int(
        body.get("ttl_seconds") or settings.get("deployment_ttl_seconds") or DEPLOYMENT_TTL_SECONDS
    )
    max_ttl = int(settings.get("max_deployment_ttl_seconds") or MAX_DEPLOYMENT_TTL_SECONDS)
    if ttl_seconds < 0 or ttl_seconds > max_ttl:
        raise ValueError(f"ttl_seconds must be between 0 and {max_ttl}")
    expires_at = None if ttl_seconds == 0 else (datetime.now(UTC).timestamp() + ttl_seconds)
    return {
        "repo_url": repo_url,
        "repo": repo,
        "sha": sha,
        "project": project,
        "pr_number": pr_number,
        "app_port": app_port,
        "health_path": health_path,
        "vault_path": vault_path,
        "preview_id": preview_id,
        "preview_slug": preview_slug,
        # ``slug`` is retained as the public preview identity for API
        # compatibility. Runtime containers use a deployment-unique slug.
        "slug": preview_slug,
        "ttl_seconds": ttl_seconds,
        "expires_at": datetime.fromtimestamp(expires_at, UTC).isoformat() if expires_at else None,
    }


def enqueue(body: dict, source: str, clone_token: str = "") -> dict:
    """
    Create a queued deployment generation within a stable preview environment.
    
    Parameters:
        body (dict): Deployment configuration to normalize and enqueue.
        source (str): Origin of the deployment request.
        clone_token (str): Optional credential used to clone the repository.
    
    Returns:
        dict: The newly created deployment state.
    """
    if sum(1 for item in QUEUE_DIR.glob("*.deploy") if item.is_file()) >= MAX_QUEUE_DEPTH:
        raise DeploymentQueueFull("deployment queue is full")
    job = normalize_job(body)
    deployment_id = f"dep_{int(time.time())}_{secrets.token_hex(4)}"
    runtime_slug = slugify(f"{job['preview_slug']}-{deployment_id[-8:]}", 63)
    state = {
        "id": deployment_id,
        **job,
        "runtime_slug": runtime_slug,
        "source": source,
        "state": "QUEUED",
        "current": False,
        "stop_requested": False,
        "logs": str(ROOT / "logs" / job["preview_slug"] / deployment_id / "worker.log"),
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
    with LOCK:
        existing_preview = read_preview(job["preview_id"])
        if existing_preview and existing_preview.get("slug"):
            preserved_slug = str(existing_preview["slug"])
            state["preview_slug"] = preserved_slug
            state["slug"] = preserved_slug
            state["runtime_slug"] = slugify(f"{preserved_slug}-{deployment_id[-8:]}", 63)
            state["logs"] = str(ROOT / "logs" / preserved_slug / deployment_id / "worker.log")
        preview = existing_preview or {
            "id": job["preview_id"],
            "repo": job["repo"],
            "project": job["project"],
            "pr_number": job["pr_number"],
            "slug": job["preview_slug"],
            "current_deployment_id": None,
            "deployment_ids": [],
            "created_at": now(),
        }
        generation = (
            max(int(preview.get("generation") or 0), len(preview.get("deployment_ids") or [])) + 1
        )
        state["generation"] = generation
        preview["generation"] = generation
        preview["deployment_ids"] = [
            *[item for item in preview.get("deployment_ids", []) if item != deployment_id],
            deployment_id,
        ]
        write_state(state)
        write_preview(preview)
    queue_operation("deploy", deployment_id)
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
    log = Path(str(state.get("logs") or ROOT / "logs" / state["slug"] / "worker.log"))
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(text)


def deployment_log_path(state: dict) -> Path:
    """Return the log path derived from the already-normalized deployment slug."""
    return Path(str(state.get("logs") or ROOT / "logs" / state["slug"] / "worker.log"))


def deployment_records() -> list[dict]:
    """Return every historical deployment with its preview relationship."""
    records: dict[str, dict] = {}
    for path in STATE_DIR.glob("dep_*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        public = public_action_state(state)
        preview_id = str(public.get("preview_id") or "")
        preview = read_preview(preview_id) if preview_id else None
        public["current"] = bool(
            preview and preview.get("current_deployment_id") == public.get("id")
        )
        records[str(public.get("id") or path.stem)] = public

    # The shell harness writes metadata before the control process records its
    # final state. Keep the list useful across upgrades and manual deployments.
    for metadata_path in DEPLOYMENTS.glob("*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        deployment_id = str(metadata.get("deployment_id") or "")
        runtime_slug = metadata_path.parent.name
        already_recorded = any(
            str(record.get("runtime_slug") or record.get("slug") or "") == runtime_slug
            for record in records.values()
        )
        if already_recorded:
            continue
        if not deployment_id:
            deployment_id = f"legacy_{hashlib.sha256(runtime_slug.encode()).hexdigest()[:16]}"
        metadata.setdefault("id", deployment_id)
        metadata.setdefault("runtime_slug", runtime_slug)
        metadata.setdefault("slug", runtime_slug)
        metadata.setdefault("current", False)
        records[deployment_id] = metadata
    return sorted(records.values(), key=lambda item: str(item.get("created_at", "")), reverse=True)


def public_deployment_summary(record: dict) -> dict:
    """Safe fields for browser status pages and unauthenticated index reads."""
    return {
        key: record[key]
        for key in (
            "id",
            "preview_id",
            "slug",
            "state",
            "current",
            "preview_url",
            "superseded_at",
            "superseded_by",
            "created_at",
            "updated_at",
            "expires_at",
        )
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


def validate_host_config_value(name: str, value: str) -> str:
    """
    Validate a dashboard-editable environment value for safe systemd or shell use.
    
    Parameters:
        name (str): Environment variable name whose value is being validated.
        value (str): Proposed environment variable value.
    
    Returns:
        str: The validated value.
    
    Raises:
        ValueError: If the value contains unsupported characters or violates the setting's format or range.
    """
    if not re.fullmatch(r"[A-Za-z0-9_./,:=@%+-]{0,500}", value):
        raise ValueError(f"{name} contains unsupported characters")
    if name in {
        "GP_CLOUD_COOKIE_SECURE",
        "GP_CLOUD_ALLOW_FORKS",
        "GP_CLOUD_ALLOW_PR_SECRETS",
        "GP_CLOUD_ALLOW_PR_BUILD_NETWORK",
        "GP_CLOUD_RETAIN_WORKSPACES",
    }:
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
    elif name == "GP_CLOUD_TRUSTED_AUTHOR_ASSOCIATIONS":
        allowed_associations = {"OWNER", "MEMBER", "COLLABORATOR"}
        associations = {item.strip().upper() for item in value.split(",") if item.strip()}
        if not associations or not associations <= allowed_associations:
            raise ValueError(
                "GP_CLOUD_TRUSTED_AUTHOR_ASSOCIATIONS must list OWNER, MEMBER, or COLLABORATOR"
            )
    elif name == "GP_CLOUD_PUBLIC_SCHEME" and value not in {"http", "https"}:
        raise ValueError("GP_CLOUD_PUBLIC_SCHEME must be http or https")
    elif name in {
        "GP_CLOUD_HTTP_PORT",
        "GP_CLOUD_DEFAULT_APP_PORT",
        "GP_CLOUD_CONTROL_PORT",
    }:
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            raise ValueError(f"{name} must be a valid port")
    elif name.endswith("_SECONDS") or name in {
        "GP_CLOUD_MAX_QUEUE_DEPTH",
        "GP_CLOUD_BUILD_CPU_QUOTA",
    }:
        if not value.isdigit():
            raise ValueError(f"{name} must be a non-negative integer")
    elif name == "GP_CLOUD_PREVIEW_DOMAIN" and not valid_dns_name(value):
        raise ValueError("GP_CLOUD_PREVIEW_DOMAIN must be a DNS name")
    elif name == "GP_CLOUD_ALLOWED_REPOS" and value:
        if any(not valid_repository_name(item) for item in value.split(",")):
            raise ValueError("GP_CLOUD_ALLOWED_REPOS must contain owner/repository names")
    elif name == "GP_CLOUD_CPU_LIMIT":
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) or float(value) <= 0:
            raise ValueError("GP_CLOUD_CPU_LIMIT must be positive")
    elif name.endswith("MEMORY_LIMIT") and not re.fullmatch(r"[1-9][0-9]*(?:[kKmMgG])?", value):
        raise ValueError(f"{name} must be a positive Docker memory value")
    elif name == "GP_CLOUD_VAULT_ADDR" and value:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("GP_CLOUD_VAULT_ADDR must be an HTTP(S) URL")
    return value


def save_host_config(values: object) -> dict:
    """
    Update supported non-secret host settings in the local configuration file.
    
    Parameters:
        values (object): Mapping of supported setting names to their values.
    
    Returns:
        dict: Summary of the resulting host configuration.
    
    Raises:
        ValueError: If values is not a mapping.
        PermissionError: If a setting is unsupported or secret.
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
        updates[name] = validate_host_config_value(name, str(raw_value))
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
    atomic_text(path.parent, path.name, "\n".join(output).rstrip() + "\n")
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
    """
    Collects deployment, storage, queue, container, and configured resource-limit metrics for the dashboard.
    
    Returns:
        dict: Resource and deployment usage metrics scoped to the GP Cloud root and managed containers.
    """
    disk = shutil.disk_usage(ROOT)
    states: dict[str, int] = {}
    for record in deployment_records():
        status = str(record.get("state", "UNKNOWN"))
        states[status] = states.get(status, 0) + 1
    containers: list[dict[str, str]] = []
    try:
        result = subprocess.run(  # noqa: S603, S607
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
                containers.append(dict(zip(names, fields, strict=False)))
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
    """
    Request stops for active deployments and purge eligible terminal deployment records.
    
    Returns:
        dict: Counts of stop requests, purged records, orphaned deployment metadata,
        and pending cleanups, plus whether any work remains pending.
    """
    stopped = 0
    purged = 0
    cleanup_pending = 0
    known_slugs: set[str] = set()
    for path in list(STATE_DIR.glob("dep_*.json")):
        state = read_state(path.stem)
        if not state:
            continue
        preview_slug = str(state.get("preview_slug") or state.get("slug") or "")
        runtime_slug = str(state.get("runtime_slug") or state.get("slug") or "")
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", runtime_slug):
            known_slugs.add(runtime_slug)
        if state.get("state") not in TERMINAL_STATES:
            request_stop(state["id"])
            stopped += 1
            continue
        metadata = DEPLOYMENTS / runtime_slug / "metadata.json"
        cleanup_marker = QUEUE_DIR / f"{state['id']}.cleanup"
        if metadata.exists() or cleanup_marker.exists():
            queue_operation("cleanup", state["id"])
            cleanup_pending += 1
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", preview_slug):
            shutil.rmtree(ROOT / "logs" / preview_slug / state["id"], ignore_errors=True)
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", runtime_slug):
            shutil.rmtree(DEPLOYMENTS / runtime_slug, ignore_errors=True)
        token_file = Path(str(state.get("clone_token_file") or ""))
        if token_file.is_file() and token_file.is_relative_to(TOKEN_DIR):
            token_file.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        for action in ("deploy", "stop", "cleanup"):
            (QUEUE_DIR / f"{path.stem}.{action}").unlink(missing_ok=True)
        purged += 1
    orphaned = 0
    # Orphan metadata is reported for operator inspection. It is never cleaned
    # from an HTTP thread because the worker is the sole lifecycle owner.
    for metadata_path in DEPLOYMENTS.glob("*/metadata.json"):
        slug = metadata_path.parent.name
        if slug not in known_slugs and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", slug):
            orphaned += 1
    return {
        "stop_requested": stopped,
        "purged": purged,
        "orphaned": orphaned,
        "cleanup_pending": cleanup_pending,
        "pending": stopped > 0 or cleanup_pending > 0,
    }


def stop_all_deployments() -> int:
    """
    Request stops for all active deployments.
    
    Returns:
        int: Number of deployments for which a stop request was recorded.
    """
    stopped = 0
    for record in deployment_records():
        deployment_id = str(record.get("id") or "")
        status = str(record.get("state") or "")
        if deployment_id and status in ACTIVE_STATES:
            if request_stop(deployment_id):
                stopped += 1
    return stopped


def configured_profile(state: dict) -> dict:
    """Resolve a profile by canonical repo; legacy project keys are direct-deploy only."""
    profiles = load_settings().get("projects") or {}
    profile = profiles.get(state.get("repo"))
    if profile is None and not int(state.get("pr_number") or 0):
        profile = profiles.get(state.get("project"))
    return profile if isinstance(profile, dict) else {}


def project_profile(state: dict) -> dict:
    """Apply validated operator settings scoped to the canonical repository."""
    profile = configured_profile(state)
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
            "allow_pr_secrets",
            "allow_build_network",
        )
        if key in profile
    }
    if "port" in profile:
        changes["app_port"] = profile["port"]
    if "kind" in profile:
        changes["profile"] = profile["kind"]
    if "app_port" in changes:
        port = int(changes["app_port"])
        if not 1 <= port <= 65535:
            raise ValueError("profile app_port out of range")
        changes["app_port"] = port
    if "health_path" in changes:
        changes["health_path"] = validate_health_path(changes["health_path"])
    if "vault_path" in changes:
        changes["vault_path"] = validate_vault_path(changes["vault_path"])
    if "allow_pr_secrets" in changes and not isinstance(changes["allow_pr_secrets"], bool):
        raise ValueError("allow_pr_secrets must be a boolean")
    if "allow_build_network" in changes and not isinstance(changes["allow_build_network"], bool):
        raise ValueError("allow_build_network must be a boolean")
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
    """
    Detect and assign a supported runtime profile from project files.
    
    Parameters:
        source_dir (Path): Directory containing the project files.
        state (dict): Deployment state, including any explicitly configured runtime.
    
    Returns:
        dict: The deployment state with a detected runtime when none was configured.
    """
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


def approved_runtime_vault_path(state: dict) -> str:
    """Return the Vault path approved for runtime use by the deployment.
    
    Parameters:
        state (dict): Deployment state containing the Vault path and pull-request secret approval.
    
    Returns:
        str: The approved Vault path, or an empty string when pull-request secret access lacks explicit approval.
    """
    path = str(state.get("vault_path") or "")
    if int(state.get("pr_number") or 0) and not (
        ALLOW_PR_SECRETS and state.get("allow_pr_secrets") is True
    ):
        return ""
    return path


def approved_build_network(state: dict) -> str:
    """
    Determine the permitted Docker build network mode for a deployment.
    
    Parameters:
        state (dict): Deployment state containing network approval and pull request information.
    
    Returns:
        str: ``"default"`` when build network access is approved; ``"none"`` otherwise.
    """
    if state.get("allow_build_network") is not True:
        return "none"
    if int(state.get("pr_number") or 0) and not ALLOW_PR_BUILD_NETWORK:
        return "none"
    return "default"


def materialize_generic_profile(source_dir: Path, state: dict) -> None:
    """
    Generate a Dockerfile for supported generic Python and JavaScript applications.
    
    Parameters:
        source_dir (Path): Directory where the Dockerfile is created.
        state (dict): Deployment configuration containing the runtime, start command,
            and optional build command.
    
    Raises:
        ValueError: If a generic Python deployment does not define a start command.
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


def trusted_github_url(url: str) -> str:
    """Normalize a GitHub API URL after enforcing the fixed trusted origin."""
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        raise ValueError("GitHub API URL must use the trusted HTTPS host")
    return urllib.parse.urlunparse(
        ("https", "api.github.com", parsed.path, "", parsed.query, "")
    )


def github_api_json(url: str) -> dict:
    """Fetch public GitHub JSON with a short timeout and no bearer credential."""
    safe_url = trusted_github_url(url)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "gp-cloud"}
    token = GITHUB_TOKEN or installation_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(safe_url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("GitHub API returned a non-object response")
    return value


def github_api_json_with_token(url: str, token: str) -> dict:
    """
    Fetch a GitHub API JSON object using a caller-provided access token.
    
    Parameters:
        url (str): GitHub API URL to request
        token (str): Access token for authenticating the request
    
    Returns:
        dict: Parsed GitHub API response
    
    Raises:
        ValueError: If GitHub returns a JSON value that is not an object
    """
    request = urllib.request.Request(
        trusted_github_url(url),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "gp-cloud-github-action",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("GitHub API returned a non-object response")
    return value


def action_token_allows_repo(token: str, repo: str) -> bool:
    """
    Determine whether a GitHub token grants access to a repository.
    
    Parameters:
        token (str): GitHub installation token to validate.
        repo (str): Repository full name in `owner/name` format.
    
    Returns:
        bool: `True` if the token can access the repository, `False` otherwise.
    """
    try:
        installation = github_api_json_with_token(
            "https://api.github.com/installation/repositories?per_page=100", token
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False
    repositories = installation.get("repositories") or []
    return any(
        isinstance(item, dict) and str(item.get("full_name") or "").lower() == repo
        for item in repositories
    )


def github_api_write(method: str, url: str, payload: dict) -> dict:
    """
    Send a mutation request to the GitHub API using the configured installation credential.
    
    Parameters:
        method (str): HTTP method for the mutation.
        url (str): GitHub API endpoint.
        payload (dict): JSON request body.
    
    Returns:
        dict: Parsed JSON object returned by GitHub, or an empty dictionary when no credential is available or the response is not an object.
    """
    token = GITHUB_TOKEN or installation_token()
    if not token:
        return {}
    request = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(payload).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "gp-cloud",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def update_github_status(state: dict, status: str) -> None:
    """
    Create or update a pull request comment with the deployment status and current preview URL.
    
    Parameters:
        state (dict): Deployment state containing the pull request number, repository, and preview identity.
        status (str): Status text to publish.
    """
    pr_number = int(state.get("pr_number") or 0)
    if pr_number <= 0:
        return
    preview = read_preview(str(state.get("preview_id") or ""))
    if not preview:
        return
    current = read_state(str(preview.get("current_deployment_id") or ""))
    current_url = str((current or {}).get("preview_url") or "")
    body = f"GP Cloud Preview status: **{status}**."
    if current_url:
        body += f"\n\nCurrent preview: {current_url}"
    elif status == "failed":
        body += "\n\nThe candidate failed before a preview became current."
    try:
        comment_id = int(preview.get("status_comment_id") or 0)
        repo = str(state["repo"])
        if comment_id:
            github_api_write(
                "PATCH",
                f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}",
                {"body": body},
            )
        else:
            result = github_api_write(
                "POST",
                f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments",
                {"body": body},
            )
            if result.get("id"):
                preview["status_comment_id"] = int(result["id"])
                write_preview(preview)
    except (OSError, ValueError, urllib.error.URLError):
        # Status reporting is best-effort and must never alter lifecycle state.
        return


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
        subprocess.run(  # noqa: S603, S607
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
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return str(json.load(response)["token"])


def route_path(state: dict) -> Path:
    """Return the stable route path for a preview, never a candidate runtime."""
    return ROOT / "config" / "caddy" / "routes" / f"{state['preview_slug']}.caddy"


def render_route(state: dict, host_port: int) -> str:
    """Render one host matcher imported by the wildcard HTTPS server."""
    matcher = "preview_" + hashlib.sha256(state["preview_id"].encode()).hexdigest()[:12]
    hostname = f"{state['preview_slug']}.{PREVIEW_DOMAIN}"
    return (
        f"@{matcher} host {hostname}\n"
        f"handle @{matcher} {{\n"
        f"\treverse_proxy 127.0.0.1:{host_port}\n"
        f"}}\n"
    )


def activate_route(state: dict) -> None:
    """
    Publish the deployment's route and reload the managed proxy when active.
    
    If validation or reloading fails, restore the previous route configuration and
    propagate the error.
    
    Parameters:
        state (dict): Deployment state containing the runtime slug and route details.
    """
    metadata_path = DEPLOYMENTS / state["runtime_slug"] / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    host_port = int(metadata.get("host_port") or 0)
    if not 1 <= host_port <= 65535:
        raise RuntimeError("deployment metadata contains an invalid host port")
    path = route_path(state)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    route_directory = ROOT / "config" / "caddy" / "routes"
    route_filename = safe_filename(f"{state['preview_slug']}.caddy")
    atomic_route_text(route_directory, route_filename, render_route(state, host_port))
    try:
        active = CADDY_MANAGED_MARKER.is_file() and (
            subprocess.run(  # noqa: S603, S607
                ["systemctl", "is-active", "--quiet", "caddy"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        )
        if active:
            subprocess.run(  # noqa: S603, S607
                ["caddy", "validate", "--config", "/etc/caddy/Caddyfile"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(  # noqa: S603, S607
                ["systemctl", "reload", "caddy"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_route_text(route_directory, route_filename, previous)
        raise


def clean_runtime(state: dict, keep_route: bool) -> subprocess.CompletedProcess[str]:
    """Invoke the idempotent cleaner for deployment-owned runtime resources."""
    command = [
        str(CLEANER),
        "--runtime-slug",
        str(state["runtime_slug"]),
        "--route-slug",
        str(state["preview_slug"]),
    ]
    if keep_route:
        command.append("--keep-route")
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )


def run_worker_command(command: list[str], output_path: Path, timeout: int) -> tuple[int, str]:
    """
    Run a command in an isolated process group and return its exit status and bounded output.
    
    Parameters:
        command (list[str]): Command and arguments to execute.
        output_path (Path): File used to retain the command's combined standard output and error.
        timeout (int): Maximum execution time in seconds.
    
    Returns:
        tuple[int, str]: The process exit status and the final 200,000 characters of output.
    
    Raises:
        RuntimeError: If the command exceeds the specified timeout.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(  # noqa: S603
            command,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            raise RuntimeError(f"deployment exceeded the {timeout}-second build timeout") from None
    content = output_path.read_text(encoding="utf-8", errors="replace")
    return returncode, content[-200_000:]


def promote_deployment(deployment_id: str) -> dict:
    """
    Promote a healthy deployment to become the current preview generation.
    
    Parameters:
        deployment_id (str): Identifier of the candidate deployment to promote.
    
    Returns:
        dict: The promoted deployment state.
    
    Raises:
        RuntimeError: If the deployment or its preview cannot be found during promotion.
        DeploymentStopRequested: If stopping was requested before promotion.
    """
    with LOCK:
        state = read_state(deployment_id)
        preview = read_preview(state["preview_id"]) if state else None
        if not state or not preview:
            raise RuntimeError("preview disappeared before promotion")
        if state.get("stop_requested"):
            raise DeploymentStopRequested
        state["promotion_phase"] = "ACTIVATING_ROUTE"
        write_state(state)
        # Holding the state lock makes promotion ordered with request_stop:
        # either the stop is observed above or it is queued after commit.
        activate_route(state)
        promoted_at = now()
        state = read_state(deployment_id)
        if not state:
            raise RuntimeError("deployment disappeared during promotion")
        old_id = str(preview.get("current_deployment_id") or "")
        old = read_state(old_id) if old_id and old_id != deployment_id else None
        preview["current_deployment_id"] = deployment_id
        preview["state"] = "RUNNING"
        preview["preview_url"] = (
            f"{PUBLIC_SCHEME}://{state['preview_slug']}.{PREVIEW_DOMAIN}{PUBLIC_URL_SUFFIX}"
        )
        state.update(
            {
                "state": "RUNNING",
                "current": True,
                "promoted_at": promoted_at,
                "promotion_phase": "COMMITTED",
                "preview_url": preview["preview_url"],
                "stop_requested": False,
            }
        )
        if old and old.get("state") == "RUNNING":
            old.update(
                {
                    "state": "SUPERSEDED",
                    "current": False,
                    "preview_url": None,
                    "superseded_at": promoted_at,
                    "superseded_by": deployment_id,
                }
            )
            state["replaces"] = old_id
        write_state(state)
        write_preview(preview)
        # Write the former-current terminal state last. Every possible crash
        # prefix therefore leaves either the old pointer/current valid or the
        # new pointer/current valid for startup reconciliation.
        if old and old.get("state") == "SUPERSEDED":
            write_state(old)
    if old:
        try:
            result = clean_runtime(old, keep_route=True)
            append_log(old, result.stdout)
            if result.returncode:
                update_state(old["id"], superseded_cleanup_error=result.stdout[-4000:])
                queue_operation("cleanup", old["id"])
        except (OSError, subprocess.SubprocessError) as error:
            update_state(old["id"], superseded_cleanup_error=str(error))
            queue_operation("cleanup", old["id"])
    return read_state(deployment_id) or state


def run_deployment(deployment_id: str) -> None:
    """
    Build and deploy a queued candidate, promoting it only after it passes its health check.
    
    Parameters:
        deployment_id (str): Identifier of the deployment to process.
    """
    state = read_state(deployment_id)
    if not state or state.get("state") not in {"QUEUED", "BUILDING"}:
        return
    if state.get("stop_requested"):
        perform_stop(deployment_id)
        return
    runtime_slug = state["runtime_slug"]
    workspace = DEPLOYMENTS / runtime_slug
    source_dir = workspace / "source"
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.chmod(0o700)
    append_log(state, f"[{now()}] deployment={deployment_id} state=BUILDING\n")
    preview_before_build = read_preview(state["preview_id"])
    update_github_status(
        state,
        "building replacement"
        if preview_before_build and preview_before_build.get("current_deployment_id")
        else "building",
    )
    if state.get("state") == "QUEUED":
        transition_state(
            deployment_id,
            "BUILDING",
            started_at=now(),
            worker_lease_expires_at=datetime.fromtimestamp(
                time.time() + int(os.environ.get("GP_CLOUD_BUILD_TIMEOUT_SECONDS", "600")) + 180,
                UTC,
            ).isoformat(),
        )
    auth_home: str | None = None
    try:
        if (read_state(deployment_id) or {}).get("stop_requested"):
            perform_stop(deployment_id)
            return
        if state.get("promotion_phase") == "ACTIVATING_ROUTE":
            preview = read_preview(state["preview_id"])
            former = read_state(str((preview or {}).get("current_deployment_id") or ""))
            if former and former.get("state") == "RUNNING":
                activate_route(former)
            update_state(deployment_id, promotion_phase="ROUTE_RESTORED_AFTER_RESTART")
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
        subprocess.run(  # noqa: S603, S607
            ["git", "clone", "--no-checkout", clone_url, str(source_dir)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=120,
        )
        subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(source_dir), "fetch", "origin", state["sha"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=120,
        )
        subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(source_dir), "checkout", "--detach", state["sha"]],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=60,
        )
        state = project_profile(state)
        state = detect_runtime_profile(source_dir, state)
        materialize_profile(source_dir, state)
        materialize_generic_profile(source_dir, state)
        update_state(deployment_id, state="BUILDING", checked_out_sha=state["sha"])
        if (read_state(deployment_id) or {}).get("stop_requested"):
            perform_stop(deployment_id)
            return
        env_file: Path | None = None
        vault_path = approved_runtime_vault_path(state)
        if state.get("vault_path") and not vault_path:
            append_log(
                state,
                f"[{now()}] runtime secrets withheld: PR deployments require explicit double opt-in\n",
            )
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
            runtime_slug,
            "--port",
            str(state["app_port"]),
            "--health-path",
            state["health_path"],
            "--build-network",
            approved_build_network(state),
        ]
        if env_file:
            command.extend(["--env-file", str(env_file)])
        returncode, output = run_worker_command(
            command,
            workspace / "worker-output.log",
            int(os.environ.get("GP_CLOUD_BUILD_TIMEOUT_SECONDS", "600")),
        )
        append_log(state, output)
        if returncode:
            raise RuntimeError(f"deployment harness exited with {returncode}")
        if (read_state(deployment_id) or {}).get("stop_requested"):
            perform_stop(deployment_id)
            return
        state = promote_deployment(deployment_id)
        append_log(state, f"[{now()}] deployment={deployment_id} state=RUNNING\n")
        update_github_status(state, "running")
    except DeploymentStopRequested:
        perform_stop(deployment_id)
    except Exception as error:
        append_log(state, f"[{now()}] deployment={deployment_id} state=FAILED error={error}\n")
        latest = read_state(deployment_id)
        if latest and latest.get("state") in {"QUEUED", "BUILDING"}:
            transition_state(
                deployment_id,
                "FAILED",
                current=False,
                error=str(error),
                finished_at=now(),
                worker_lease_expires_at=None,
            )
        if latest and latest.get("state") != "RUNNING":
            cleanup = clean_runtime(latest, keep_route=True)
            append_log(latest, cleanup.stdout)
            update_github_status(latest, "failed")
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


def request_stop(deployment_id: str) -> dict | None:
    """Queue a durable request to stop a deployment.
    
    Parameters:
        deployment_id (str): Identifier of the deployment to stop.
    
    Returns:
        dict | None: The deployment state after the request, or `None` if the deployment does not exist.
    """
    state = read_state(deployment_id)
    if not state:
        return None
    if state.get("state") in TERMINAL_STATES:
        return state
    state = update_state(deployment_id, stop_requested=True, desired_state="STOPPED") or state
    queue_operation("stop", deployment_id)
    return state


def perform_stop(deployment_id: str) -> dict | None:
    """
    Stop a deployment runtime and update its lifecycle and preview state.
    
    Parameters:
        deployment_id (str): Identifier of the deployment to stop.
    
    Returns:
        dict | None: The updated deployment state, or `None` if the deployment does not exist.
    """
    state = read_state(deployment_id)
    if not state:
        return None
    if state.get("state") in TERMINAL_STATES:
        return state
    token_file_name = str(state.get("clone_token_file") or "")
    if token_file_name:
        Path(token_file_name).unlink(missing_ok=True)
    preview = read_preview(str(state.get("preview_id") or ""))
    is_current = bool(preview and preview.get("current_deployment_id") == deployment_id)
    result = clean_runtime(state, keep_route=not is_current)
    append_log(state, result.stdout)
    final = "STOPPED" if result.returncode == 0 else "FAILED"
    updated = transition_state(
        deployment_id,
        final,
        error=None if result.returncode == 0 else result.stdout,
        stop_requested=False,
        current=False,
        preview_url=None,
        finished_at=now(),
        worker_lease_expires_at=None,
    )
    if is_current and preview:
        preview["current_deployment_id"] = None
        preview["state"] = final
        write_preview(preview)
    if updated:
        update_github_status(updated, final.lower())
    return updated


def stop_deployment(deployment_id: str) -> dict | None:
    """Compatibility wrapper for callers that request an asynchronous stop."""
    return request_stop(deployment_id)


def worker_loop() -> None:
    """Own all deployment lifecycle side effects in durable request order."""
    while not STOP.is_set():
        try:
            action, deployment_id = JOBS.get(timeout=0.5)
        except queue.Empty:
            continue
        completed = False
        try:
            if action == "deploy":
                run_deployment(deployment_id)
            elif action == "stop":
                perform_stop(deployment_id)
            else:
                state = read_state(deployment_id)
                if state:
                    preview = read_preview(str(state.get("preview_id") or ""))
                    current = read_state(str((preview or {}).get("current_deployment_id") or ""))
                    if current and current.get("state") == "RUNNING":
                        activate_route(current)
                    result = clean_runtime(state, keep_route=True)
                    append_log(state, result.stdout)
            completed = True
        except Exception as error:
            state = read_state(deployment_id)
            if state:
                retry_count = int(state.get("worker_retry_count") or 0) + 1
                append_log(state, f"[{now()}] worker action={action} error={error}\n")
                update_state(
                    deployment_id,
                    worker_error=str(error),
                    worker_error_at=now(),
                    worker_retry_count=retry_count,
                )
                # Preserve request order without killing the sole worker. A
                # failed action moves behind already-queued work and receives
                # bounded in-process retries; its durable marker remains for
                # restart recovery if the fault persists.
                if retry_count <= 3 and not STOP.is_set():
                    JOBS.put((action, deployment_id))
        finally:
            if completed:
                (QUEUE_DIR / f"{deployment_id}.{action}").unlink(missing_ok=True)
                state = read_state(deployment_id)
                if state and state.get("worker_retry_count"):
                    update_state(
                        deployment_id,
                        worker_retry_count=0,
                        worker_error=None,
                        worker_error_at=None,
                    )
            JOBS.task_done()


def cleanup_loop() -> None:
    """Request stops for active deployments whose configured TTL has expired."""
    while not STOP.wait(60):
        for path in STATE_DIR.glob("dep_*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                expires_at = state.get("expires_at")
                if not expires_at or state.get("state") not in {"QUEUED", "BUILDING", "RUNNING"}:
                    continue
                if datetime.fromisoformat(str(expires_at)) <= datetime.now(UTC):
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
            state["expires_at"] = datetime.fromtimestamp(state["expires_at"], UTC).isoformat()
            write_state(state)
        except (OSError, ValueError, json.JSONDecodeError):
            continue


def reconcile_durable_state() -> None:
    """
    Rebuild durable deployment state and queue work after a process interruption.
    
    Migrates legacy deployment records, restores preview pointers and lifecycle metadata, and requeues pending deployment, stop, and cleanup operations.
    """
    def queue_reconciled(action: str, deployment_id: str) -> None:
        """Skip one malformed legacy operation without aborting startup recovery."""
        try:
            queue_operation(action, deployment_id)
        except (OSError, ValueError):
            return

    pending_cleanup: set[str] = set()
    for marker in list(QUEUE_DIR.iterdir()):
        modern = re.fullmatch(r"(dep_[0-9]+_[0-9a-f]+)\.(deploy|stop|cleanup)", marker.name)
        if modern:
            if modern.group(2) == "cleanup":
                pending_cleanup.add(modern.group(1))
            marker.unlink(missing_ok=True)
    grouped: dict[str, list[dict]] = {}
    for path in sorted(STATE_DIR.glob("dep_*.json")):
        state = read_state(path.stem)
        if not state:
            continue
        preview_id, preview_slug = preview_identity(
            str(state.get("repo") or "legacy/unknown"),
            int(state.get("pr_number") or 0),
            str(state.get("project") or state.get("slug") or "preview"),
        )
        state.setdefault("preview_id", preview_id)
        state.setdefault("preview_slug", str(state.get("slug") or preview_slug))
        state.setdefault("runtime_slug", str(state.get("slug") or preview_slug))
        state.setdefault("current", False)
        state.setdefault("stop_requested", state.get("state") == "STOPPING")
        if state.get("state") == "STOPPING":
            state["state"] = "BUILDING" if state.get("started_at") else "QUEUED"
        if state.get("state") == "BUILDING" and not state.get("stop_requested"):
            state["state"] = "QUEUED"
            state["recovery_count"] = int(state.get("recovery_count") or 0) + 1
            state["recovered_at"] = now()
        state["worker_lease_expires_at"] = None
        write_state(state)
        grouped.setdefault(state["preview_id"], []).append(state)

    for preview_id, deployments in grouped.items():
        existing = read_preview(preview_id)
        persisted_order = {
            deployment_id: index
            for index, deployment_id in enumerate((existing or {}).get("deployment_ids") or [])
        }

        def lifecycle_order(
            item: dict, order: dict[str, int] = persisted_order
        ) -> tuple[int, int, str, str]:
            """Create a sortable key for ordering deployment records by persisted order or generation metadata.
            
            Parameters:
                item (dict): Deployment record containing its identifier and ordering metadata.
                order (dict[str, int]): Persisted deployment ordering keyed by deployment identifier.
            
            Returns:
                tuple[int, int, str, str]: A sorting key containing the ordering category, sequence or generation, creation timestamp, and deployment identifier.
            """
            deployment_id = str(item.get("id") or "")
            if deployment_id in order:
                return (0, order[deployment_id], "", deployment_id)
            return (
                1,
                int(item.get("generation") or 0),
                str(item.get("created_at") or ""),
                deployment_id,
            )

        oldest_first = sorted(deployments, key=lifecycle_order)
        newest_first = list(reversed(oldest_first))
        # A candidate is written RUNNING/COMMITTED immediately before the
        # preview pointer is advanced. If the process dies in that narrow
        # window, the newest committed generation is the durable promotion
        # journal and must win over the older persisted pointer.
        current = next(
            (
                item
                for item in newest_first
                if item.get("state") == "RUNNING" and item.get("promotion_phase") == "COMMITTED"
            ),
            None,
        )
        if current is None and existing and existing.get("current_deployment_id"):
            persisted = read_state(str(existing["current_deployment_id"]))
            if persisted and persisted.get("state") == "RUNNING":
                current = persisted
        if current is None:
            current = next((item for item in newest_first if item.get("state") == "RUNNING"), None)
        preview = existing or {
            "id": preview_id,
            "repo": newest_first[0].get("repo"),
            "project": newest_first[0].get("project"),
            "pr_number": newest_first[0].get("pr_number", 0),
            "slug": newest_first[0]["preview_slug"],
            "created_at": newest_first[-1].get("created_at") or now(),
        }
        preview["deployment_ids"] = [item["id"] for item in oldest_first]
        preview["generation"] = max(
            len(oldest_first),
            max(
                (int(item.get("generation") or 0) for item in deployments),
                default=0,
            ),
        )
        preview["current_deployment_id"] = current["id"] if current else None
        preview["state"] = "RUNNING" if current else str(newest_first[0].get("state") or "STOPPED")
        write_preview(preview)
        for state in oldest_first:
            is_current = bool(current and state["id"] == current["id"])
            if state.get("current") != is_current:
                update_state(state["id"], current=is_current)
            if state.get("state") == "RUNNING" and not is_current:
                update_state(
                    state["id"],
                    state="SUPERSEDED",
                    current=False,
                    superseded_at=now(),
                    superseded_by=current["id"] if current else None,
                )
                queue_reconciled("cleanup", state["id"])
            elif state.get("stop_requested"):
                queue_reconciled("stop", state["id"])
            elif state.get("state") == "QUEUED":
                queue_reconciled("deploy", state["id"])
            elif state.get("state") in TERMINAL_STATES:
                metadata = DEPLOYMENTS / state["runtime_slug"] / "metadata.json"
                if metadata.exists():
                    queue_reconciled("cleanup", state["id"])

    # Convert pre-operation queue markers after state migration.
    for marker in list(QUEUE_DIR.iterdir()):
        if marker.is_file() and re.fullmatch(r"dep_[0-9]+_[0-9a-f]+", marker.name):
            state = read_state(marker.name)
            marker.unlink(missing_ok=True)
            if state:
                queue_reconciled("stop" if state.get("stop_requested") else "deploy", state["id"])
    for deployment_id in pending_cleanup:
        if read_state(deployment_id):
            queue_reconciled("cleanup", deployment_id)


def verify_signature(handler: BaseHTTPRequestHandler, body: bytes) -> bool:
    """
    Validate the GitHub webhook signature for a request payload.
    
    Parameters:
        body (bytes): The raw webhook request body.
    
    Returns:
        bool: `true` if the signature matches the configured webhook secret, `false` otherwise.
    """
    signature = handler.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return bool(WEBHOOK_SECRET) and hmac.compare_digest(signature, expected)


def claim_webhook_delivery(delivery_id: str, event: str, body: bytes) -> tuple[Path, Path] | None:
    """
    Claim a webhook delivery ID and payload digest for replay protection.
    
    Parameters:
        delivery_id (str): GitHub delivery identifier.
        event (str): Webhook event name.
        body (bytes): Signed webhook payload.
    
    Returns:
        tuple[Path, Path] | None: Paths for the claimed delivery ID and payload digest, or `None` if the identifier is invalid or either claim already exists.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", delivery_id):
        return None
    DELIVERY_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - WEBHOOK_DELIVERY_TTL_SECONDS
    for path in DELIVERY_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue
    delivery_path = DELIVERY_DIR / f"delivery-{delivery_id}"
    digest = hashlib.sha256(event.encode() + b"\0" + body).hexdigest()
    digest_path = DELIVERY_DIR / f"payload-{digest}"
    claimed: list[Path] = []
    try:
        for path in (delivery_path, digest_path):
            with path.open("x", encoding="utf-8") as handle:
                handle.write(now() + "\n")
            claimed.append(path)
    except FileExistsError:
        for path in claimed:
            path.unlink(missing_ok=True)
        return None
    return delivery_path, digest_path


def release_webhook_claim(claim: tuple[Path, Path]) -> None:
    """Release an in-flight claim after a transient processing failure."""
    for path in claim:
        path.unlink(missing_ok=True)


def parse_deploy_command(comment: str) -> tuple[str, str | None]:
    """
    Classify a deployment command from a comment.
    
    Parameters:
        comment (str): Comment text to inspect.
    
    Returns:
        tuple[str, str | None]: A status and optional message. The status is
        ``"deploy"`` for an exact ``/deploy`` command, ``"unsupported"`` for
        ``/deploy`` followed by arguments, and ``"ignore"`` for other comments.
    """
  
    trimmed = comment.strip(" \t")
    if trimmed == "/deploy":
        return "deploy", None
    if trimmed.startswith("/deploy") and trimmed[7] in " \t":
        return "unsupported", "unsupported command; use exactly /deploy with no arguments"
    return "ignore", None


def valid_ui_origin(handler: BaseHTTPRequestHandler) -> bool:
    """
    Validate that a request's origin matches its Host header.
    
    Parameters:
        handler (BaseHTTPRequestHandler): Request handler containing the Origin and Host headers.
    
    Returns:
        bool: True if the Origin header is absent or uses HTTP(S) with a matching host, false otherwise.
    """
    origin = handler.headers.get("Origin", "")
    if not origin:
        return True
    parsed = urllib.parse.urlsplit(origin)
    return bool(parsed.scheme in {"http", "https"} and parsed.netloc == handler.headers.get("Host"))


def login_rate_limit_address(handler: BaseHTTPRequestHandler) -> str:
    """
    Determine the client address used for login rate limiting.
    
    Parameters:
        handler (BaseHTTPRequestHandler): Request handler containing the peer address and headers.
    
    Returns:
        str: The validated forwarded client address for loopback proxy requests, or the direct peer address.
    """
    peer = str(handler.client_address[0])
    try:
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    forwarded = handler.headers.get("X-GP-Client-IP", "").strip()
    if not peer_address.is_loopback or not forwarded:
        return peer
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


def admin_password_matches(value: str) -> bool:
    """Compare the local UI password without exposing either credential."""
    expected = ADMIN_PASSWORD or API_TOKEN
    return bool(expected) and hmac.compare_digest(value, expected)


def login_allowed(address: str) -> bool:
    """Determine whether another password attempt is allowed for a source address.
    
    Parameters:
        address (str): Source address associated with the login attempts.
    
    Returns:
        bool: `true` if fewer than 10 failed attempts occurred for the address in the preceding five minutes, `false` otherwise.
    """
    cutoff = time.time() - 300
    with SESSION_LOCK:
        for source in list(LOGIN_FAILURES):
            recent = [value for value in LOGIN_FAILURES[source] if value >= cutoff]
            if recent:
                LOGIN_FAILURES[source] = recent
            else:
                LOGIN_FAILURES.pop(source, None)
        if address not in LOGIN_FAILURES and len(LOGIN_FAILURES) >= 4096:
            return False
        failures = [value for value in LOGIN_FAILURES.get(address, []) if value >= cutoff]
        LOGIN_FAILURES[address] = failures
        return len(failures) < 10


def record_login_failure(address: str) -> None:
    """Record a failed dashboard authentication attempt without logging credentials."""
    with SESSION_LOCK:
        LOGIN_FAILURES.setdefault(address, []).append(time.time())


def action_request_allowed(address: str) -> bool:
    """Bound unauthenticated Action token-validation calls per source address."""
    cutoff = time.time() - 60
    with SESSION_LOCK:
        for source in list(ACTION_REQUESTS):
            recent = [value for value in ACTION_REQUESTS[source] if value >= cutoff]
            if recent:
                ACTION_REQUESTS[source] = recent
            else:
                ACTION_REQUESTS.pop(source, None)
        if address not in ACTION_REQUESTS and len(ACTION_REQUESTS) >= 4096:
            return False
        requests = ACTION_REQUESTS.setdefault(address, [])
        if len(requests) >= 20:
            return False
        requests.append(time.time())
        return True


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
    """Generate the authenticated single-page dashboard HTML for deployment operations, configuration, Vault management, and usage monitoring."""
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
async function stop(id){await fetch('/ui/api/deployments/'+encodeURIComponent(id)+'/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});load()}
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
    """
    Handle authorized GitHub issue-comment deployment requests and pull-request closure cleanup.
    
    Parameters:
        payload (dict): GitHub event payload.
        event (str): GitHub event type.
    
    Returns:
        dict | None: Deployment details or an error response for issue-comment events; otherwise, `None`.
    """
    repo = repo_name(payload)
    if not allowed_repo(repo):
        raise PermissionError("repository is not allowlisted")
    installation = str((payload.get("installation") or {}).get("id") or "")
    if GITHUB_INSTALLATION_ID and installation != GITHUB_INSTALLATION_ID:
        raise PermissionError("GitHub App installation is not authorized")
    if event == "issue_comment":
        if payload.get("action") != "created":
            return None
        issue = payload.get("issue") or {}
        if not issue.get("pull_request"):
            return None
        association = str((payload.get("comment") or {}).get("author_association") or "").upper()
        if association not in TRUSTED_AUTHOR_ASSOCIATIONS:
            raise PermissionError("comment author is not authorized to deploy previews")
        command, error = parse_deploy_command(str((payload.get("comment") or {}).get("body") or ""))
        if command == "unsupported":
            return {"accepted": False, "deployed": False, "error": error}
        if command != "deploy":
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
        """Send a non-cacheable JSON response with browser security headers."""
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Referrer-Policy", "no-referrer")
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
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
        if length < 0 or length > 1024 * 1024:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def authorized(self) -> bool:
        """Check the control-plane bearer token using constant-time comparison."""
        return bool(API_TOKEN) and hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {API_TOKEN}"
        )

    def do_GET(self) -> None:
        """
        Route health checks, metrics, dashboard data, action status, and deployment data requests.
        
        Requires UI authentication for dashboard APIs and API authentication for detailed deployment and log requests. Public deployment listings expose summaries, while authorized requests expose deployment details.
        """
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
                    "# HELP gp_cloud_deployment_failures_total Total deployment failure events.",
                    "# TYPE gp_cloud_deployment_failures_total counter",
                    f"gp_cloud_deployment_failures_total {deployment_failure_count()}",
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
        """
        Route dashboard mutations, GitHub webhooks, Action deployment requests, and authenticated deployment API operations.
        
        Returns:
            None
        """
        try:
            body = self.body()
            request_path = urllib.parse.urlsplit(self.path).path
            if request_path.startswith("/ui") and not valid_ui_origin(self):
                return self.send_json(403, {"error": "cross-origin dashboard request rejected"})
            if request_path == "/ui/login":
                values = urllib.parse.parse_qs(body.decode("utf-8"))
                password = (values.get("password") or [""])[0]
                address = login_rate_limit_address(self)
                if not login_allowed(address):
                    return self.send_html(429, login_html())
                if not admin_password_matches(password):
                    record_login_failure(address)
                    return self.send_html(401, login_html())
                with SESSION_LOCK:
                    LOGIN_FAILURES.pop(address, None)
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
                if (
                    self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                    != "application/json"
                ):
                    return self.send_json(415, {"error": "application/json is required"})
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
                if (
                    self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
                    != "application/json"
                ):
                    return self.send_json(415, {"error": "application/json is required"})
                if not verify_signature(self, body):
                    return self.send_json(401, {"error": "invalid webhook signature"})
                event = self.headers.get("X-GitHub-Event", "")
                if event not in {"issue_comment", "pull_request"}:
                    return self.send_json(400, {"error": "unsupported webhook event"})
                delivery = self.headers.get("X-GitHub-Delivery", "")
                if not delivery:
                    return self.send_json(400, {"error": "X-GitHub-Delivery is required"})
                claim = claim_webhook_delivery(delivery, event, body)
                if not claim:
                    return self.send_json(409, {"error": "webhook delivery already processed"})
                try:
                    payload = json.loads(body)
                    result = github_event(payload, event)
                except Exception:
                    release_webhook_claim(claim)
                    raise
                return self.send_json(202, result or {"accepted": True})
            if request_path == "/actions/gp-cloud-deploy":
                token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                repo = self.headers.get("X-GitHub-Repository", "").lower().strip()
                repo_match = re.fullmatch(
                    r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo
                )
                if not token or repo_match is None or not allowed_repo(repo):
                    return self.send_json(403, {"error": "GitHub Action is not authorized"})
                address = login_rate_limit_address(self)
                if not action_request_allowed(address):
                    return self.send_json(429, {"error": "too many Action requests"})
                if not ACTION_VALIDATION_SLOTS.acquire(blocking=False):
                    return self.send_json(503, {"error": "Action validation is busy"})
                try:
                    token_allowed = action_token_allows_repo(token, repo)
                finally:
                    ACTION_VALIDATION_SLOTS.release()
                if not token_allowed:
                    return self.send_json(403, {"error": "repository-scoped token is required"})
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
                    404 if result is None else 202,
                    public_action_state(result) if result else {"error": "deployment not found"},
                )
            return self.send_json(404, {"error": "not found"})
        except DeploymentQueueFull as error:
            self.send_json(503, {"error": str(error)})
        except PermissionError as error:
            self.send_json(403, {"error": str(error)})
        except (ValueError, KeyError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
        except Exception:
            self.send_json(500, {"error": "internal server error"})


def main() -> None:
    """Initialize local state, start background workers, and serve loopback HTTP."""
    for directory in (STATE_DIR, PREVIEW_DIR, QUEUE_DIR, TOKEN_DIR, DELIVERY_DIR, DEPLOYMENTS):
        directory.mkdir(parents=True, exist_ok=True)
    backfill_expirations()
    reconcile_durable_state()
    if not API_TOKEN:
        raise SystemExit("GP_CLOUD_API_TOKEN is required")
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
