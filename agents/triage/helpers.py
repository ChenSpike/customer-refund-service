import re


def parse_requested_amount(value, fallback: float) -> float:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else fallback
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return fallback
        return number if number >= 0 else fallback
    return fallback


def light_clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def assistant_msg(text: str) -> dict:
    return {"role": "assistant", "content": text}


def inputs_from_state(state) -> tuple[str, str | None, bool]:
    case = state.get("case") or {}
    message = state.get("message", case.get("message"))
    user_id = state.get("user_id", case.get("user_id"))
    request_context = state.get("request_context") or {}
    buggy = bool(request_context.get("buggy_db", False))
    return message, user_id, buggy