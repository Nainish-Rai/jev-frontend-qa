"""Read/search loaded page text independently of the browser action space."""

import json
from pathlib import Path

_READER = Path(__file__).with_suffix(".js").read_text()


def read_page(transport, *, query: str = "", offset: int = 0) -> dict:
    if not isinstance(query, str) or len(query) > 200:
        raise ValueError("Page search requires a string of at most 200 characters")
    if type(offset) is not int or not 0 <= offset <= 250000:
        raise ValueError("Page reading offset is outside the supported range")
    arguments = json.dumps(query) + "," + str(offset)
    result = transport.evaluate_js(_READER.replace("__JEV_READ_ARGUMENTS__", arguments))
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise TypeError("Page reading is unavailable")
    return result
