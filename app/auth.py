import logging
import os
import secrets

log = logging.getLogger(__name__)

DEFAULT_SECRET_ID = "home-mcp-auth-token"


def resolve_auth_token() -> str | None:
    """Bearer token HTTP clients must send, or None to run without auth.

    Order: HOME_MCP_AUTH_TOKEN env var → Secret Manager (when running on GCP,
    detected via K_SERVICE, or when HOME_MCP_AUTH_SECRET is set explicitly).
    The Secret Manager path self-issues: if the secret doesn't exist yet a
    random token is generated and stored, so it survives restarts/revisions.
    """
    if token := os.environ.get("HOME_MCP_AUTH_TOKEN"):
        return token
    if os.environ.get("K_SERVICE") or os.environ.get("HOME_MCP_AUTH_SECRET"):
        return _token_from_secret_manager()
    return None


def _token_from_secret_manager() -> str | None:
    secret_id = os.environ.get("HOME_MCP_AUTH_SECRET", DEFAULT_SECRET_ID)
    try:
        import google.auth
        from google.api_core import exceptions as gax
        from google.cloud import secretmanager
    except ImportError:
        log.warning(
            "google-cloud-secret-manager not installed; cannot self-issue "
            "auth token — HTTP transport will be unauthenticated"
        )
        return None

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        try:
            _, project = google.auth.default()
        except google.auth.exceptions.DefaultCredentialsError:
            project = None
    if not project:
        log.warning("GCP credentials/project not found; auth token not issued")
        return None

    client = secretmanager.SecretManagerServiceClient()
    parent = client.secret_path(project, secret_id)
    latest = f"{parent}/versions/latest"

    try:
        return client.access_secret_version(name=latest).payload.data.decode().strip()
    except gax.NotFound:
        pass

    token = secrets.token_hex(32)
    try:
        client.create_secret(
            parent=f"projects/{project}",
            secret_id=secret_id,
            secret={"replication": {"automatic": {}}},
        )
    except gax.AlreadyExists:
        # Another instance created the secret first — use what it stored.
        try:
            return (
                client.access_secret_version(name=latest).payload.data.decode().strip()
            )
        except gax.NotFound:
            pass  # created but no version yet — add ours below

    client.add_secret_version(parent=parent, payload={"data": token.encode()})
    log.info(
        "issued new auth token in Secret Manager %r — retrieve with: "
        "gcloud secrets versions access latest --secret=%s",
        parent,
        secret_id,
    )
    return token
