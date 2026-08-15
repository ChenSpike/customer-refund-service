def extract_text(response) -> str:
    for item in response.output:
        if item.type == "message":
            for part in item.content:
                if hasattr(part, "text"):
                    return part.text
    return ""


def usage_tokens(response) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    return (
        getattr(usage, "input_tokens", 0) or 0,
        getattr(usage, "output_tokens", 0) or 0,
    )


def is_content_filter(exc) -> bool:
    text = str(exc)
    return "content_filter" in text or "content management" in text