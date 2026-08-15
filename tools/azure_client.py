import os
from threading import Lock

from dotenv import load_dotenv
from openai import AzureOpenAI


_client: AzureOpenAI | None = None
_client_lock = Lock()


def get_client() -> AzureOpenAI:
    """Create the shared Responses client lazily.

    Importing the workflow must remain safe for CLI help, tests, dashboard-only
    deployments, and offline simulation even when Azure credentials are absent.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        load_dotenv()
        missing = [
            name
            for name in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT")
            if not os.getenv(name)
        ]
        if missing:
            raise RuntimeError("Missing Azure settings: " + ", ".join(missing))
        _client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview"),
            timeout=float(os.getenv("AZURE_OPENAI_REQUEST_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("AZURE_OPENAI_MAX_RETRIES", "2")),
        )
        return _client


class _LazyAzureClient:
    @property
    def responses(self):
        return get_client().responses


client = _LazyAzureClient()


def deployment_for(stage: str) -> str:
    load_dotenv()
    override = os.getenv(f"AZURE_OPENAI_{stage.upper()}_DEPLOYMENT")
    deployment = override or os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if deployment:
        return deployment
    legacy_defaults = {"triage": "gpt-5.4", "response": "gpt-4o"}
    if stage in legacy_defaults:
        return legacy_defaults[stage]
    raise RuntimeError(f"Missing Azure deployment for {stage}")
