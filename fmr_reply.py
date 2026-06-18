from __future__ import annotations

import re


def shorten_reply_text(text: str, limit: int) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text.strip()

    for i, line in enumerate(lines):
        if re.match(r"^一句话概括[：:]\s*", line):
            candidate = re.sub(r"^一句话概括[：:]\s*", "", line).strip()
            if candidate:
                return truncate_chars(candidate, limit)
            if i + 1 < len(lines):
                return truncate_chars(lines[i + 1], limit)

    cleaned: list[str] = []
    for line in lines:
        if line.endswith(("如下：", "如下:")):
            continue
        line = re.sub(r"^[\u4e00-\u9fa5A-Za-z]{1,8}[：:]\s*", "", line)
        line = re.sub(r"^(这份转发内容是在|这份转发是在|这条主要是在|这份主要是在)", "", line)
        line = re.sub(r"\s+", " ", line).strip("，。； ")
        if line:
            cleaned.append(line)

    if not cleaned:
        return truncate_chars(text.strip(), limit)

    candidate = cleaned[0]
    for part in cleaned[1:]:
        merged = f"{candidate}，{part}"
        if len(merged) > limit:
            break
        candidate = merged

    return truncate_chars(candidate, limit)


def truncate_chars(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip("，。；：: ") + "…"
