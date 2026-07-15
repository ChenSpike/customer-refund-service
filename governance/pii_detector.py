import re
from dataclasses import dataclass

# Patterns
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")


@dataclass(frozen=True)
class PIIHit:
    field: str
    pii_type: str  # "email" | "phone"
    value: str


def find_emails(text: str) -> list[str]:
    return _EMAIL_RE.findall(text)


def find_phones(text: str) -> list[str]:
    return _PHONE_RE.findall(text)


def scan_dict_for_pii(data: dict, _parent: str = "") -> list[PIIHit]:
    """Recursively scan all string values in a dict for email and phone PII."""
    hits: list[PIIHit] = []
    for key, value in data.items():
        path = f"{_parent}.{key}" if _parent else key
        if isinstance(value, str):
            for email in find_emails(value):
                hits.append(PIIHit(field=path, pii_type="email", value=email))
            for phone in find_phones(value):
                hits.append(PIIHit(field=path, pii_type="phone", value=phone))
        elif isinstance(value, dict):
            hits.extend(scan_dict_for_pii(value, _parent=path))
    return hits
