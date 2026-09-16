import json
from typing import Any


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    result = []
    for frame in raw.replace("\r\n", "\n").split("\n\n"):
        name = "message"
        lines = []
        for line in frame.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                lines.append(line[5:].strip())
        if lines:
            result.append({"event": name, "data": json.loads("\n".join(lines))})
    return result
