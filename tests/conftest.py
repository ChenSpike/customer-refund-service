import os

# tools/azure_client.py builds the AzureOpenAI client at import time from these
# env vars (no network I/O at construction). Set harmless dummies so the triage
# node imports offline; tests inject a FakeAzureClient and mock the order lookup.
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://dummy.local/")
