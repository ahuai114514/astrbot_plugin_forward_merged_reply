from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable, Sequence
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp


QUOTE_ATTRS = (
    "quote",
    "quoted",
    "reply_message",
    "reply",
    "source_message",
    "referenced_message",
)
MESSAGE_CHAIN_ATTRS = ("message", "message_chain", "chain", "messages", "components")
FORWARD_NODE_ATTRS = (
    "nodes",
    "node_list",
    "children",
    "messages",
    "content",
    "detail",
    "forward",
    "forwards",
    "merged",
    "merged_messages",
)
NESTED_ATTRS = (
    "quote",
    "quoted",
    "reply",
    "reply_message",
    "source_message",
    "referenced_message",
    "content",
    "message",
    "message_chain",
    "chain",
    "messages",
    "children",
    "nodes",
    "forward",
    "forwards",
    "merged",
    "merged_messages",
)
PAYLOAD_NESTED_KEYS = ("messages", "msgs", "data", "records", "children", "content", "message")


class ForwardResolver:
    def __init__(self, plugin_id: str, max_depth: int, max_nodes: int, max_chars: int, debug_enabled: bool) -> None:
        self.plugin_id = plugin_id
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_chars = max_chars
        self.debug_enabled = debug_enabled

    async def debug_event_shapes(self, event: Any) -> None:
        if not self.debug_enabled:
            return

        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        chains = self.extract_message_chains(message_obj)
        quoted = self.extract_quoted_message(event)

        logger.info("[%s] debug message_str=%r", self.plugin_id, getattr(event, "message_str", ""))
        logger.info("[%s] debug chain_types=%s", self.plugin_id, [type(component).__name__ for component in chains])
        logger.info(
            "[%s] debug quoted_type=%s contains_forward=%s",
            self.plugin_id,
            type(quoted).__name__ if quoted is not None else None,
            self.contains_forward_content(quoted) if quoted is not None else False,
        )
        logger.info("[%s] debug quoted_repr=%r", self.plugin_id, quoted)
        logger.info("[%s] debug raw_message=%r", self.plugin_id, raw_message)

    def should_process_event(self, event: Any) -> bool:
        if not self.has_explicit_at_bot(event):
            return False
        quoted = self.extract_quoted_message(event)
        return quoted is not None and self.contains_forward_content(quoted)

    def extract_quoted_message(self, event: Any) -> Any | None:
        message_obj = getattr(event, "message_obj", None)
        for name in QUOTE_ATTRS:
            value = getattr(message_obj, name, None)
            if value is not None:
                return value
        if isinstance(message_obj, dict):
            for name in QUOTE_ATTRS:
                value = message_obj.get(name)
                if value is not None:
                    return value
        for component in self.extract_message_chains(message_obj):
            if isinstance(component, Comp.Reply):
                return component
        return None

    async def render_forward_bundle_with_fetch(self, event: Any, root: Any) -> tuple[str, list[str]]:
        payload_blocks: list[str] = []
        image_urls: list[str] = []
        forward_payloads = await self.fetch_forward_payloads(event, root)
        for payload in forward_payloads:
            rendered_payload, payload_images = self.render_forward_payload(payload)
            if rendered_payload:
                payload_blocks.append(rendered_payload)
            image_urls.extend(payload_images)

        if payload_blocks:
            text = "\n\n".join(block for block in payload_blocks if block.strip())
            return self.truncate_text(text), self.dedupe_strings(image_urls)

        return self.render_forward_bundle(root), []

    async def fetch_forward_payloads(self, event: Any, root: Any) -> list[Any]:
        message_ids = self.extract_forward_message_ids(event, root)
        if self.debug_enabled:
            logger.info("[%s] debug forward_message_ids=%r", self.plugin_id, message_ids)

        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            return []

        payloads: list[Any] = []
        for message_id in message_ids:
            try:
                result = await call_action("get_forward_msg", message_id=message_id)
                payloads.append(result)
                if self.debug_enabled:
                    logger.info(
                        "[%s] debug get_forward_msg success message_id=%r result=%r",
                        self.plugin_id,
                        message_id,
                        result,
                    )
            except Exception as exc:
                if self.debug_enabled:
                    logger.info(
                        "[%s] debug get_forward_msg fail message_id=%r error=%r",
                        self.plugin_id,
                        message_id,
                        exc,
                    )
        return payloads

    def extract_forward_message_ids(self, event: Any, root: Any) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()

        def push(value: Any) -> None:
            if value is None:
                return
            normalized = str(value).strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            ids.append(normalized)

        for item in self.iter_nested_values(root):
            if isinstance(item, Comp.Forward):
                push(getattr(item, "id", None))

        if isinstance(root, Comp.Reply):
            for component in getattr(root, "chain", []) or []:
                if isinstance(component, Comp.Forward):
                    push(getattr(component, "id", None))

        raw_payload = self.raw_payload_from_event(event)
        if isinstance(raw_payload, dict):
            for element in raw_payload.get("elements", []) or []:
                if not isinstance(element, dict):
                    continue
                reply_element = element.get("replyElement")
                if isinstance(reply_element, dict):
                    push(reply_element.get("sourceMsgIdInRecords"))
                    push(reply_element.get("replayMsgId"))

            for record in raw_payload.get("records", []) or []:
                if isinstance(record, dict):
                    push(record.get("msgId"))

        return ids

    def raw_payload_from_event(self, event: Any) -> Any:
        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            return raw_message.get("raw")
        return getattr(raw_message, "raw", None)

    def render_forward_payload(self, payload: Any) -> tuple[str, list[str]]:
        image_state = {"count": 0, "image_urls": []}
        blocks = self.render_forward_payload_blocks(payload, depth=0, seen=set(), image_state=image_state)
        text = "\n\n".join(block for block in blocks if block.strip())
        return (self.truncate_text(text) if text else ""), self.dedupe_strings(image_state["image_urls"])

    def render_forward_payload_blocks(
        self,
        current: Any,
        depth: int,
        seen: set[int],
        image_state: dict[str, Any],
    ) -> list[str]:
        if current is None:
            return []
        if depth > self.max_depth:
            return [self.depth_limit_notice(depth)]

        marker = id(current)
        if marker in seen:
            return []
        seen.add(marker)

        entries = self.extract_forward_payload_entries(current)
        if entries:
            return [self.render_forward_payload_entries(entries, depth, seen, image_state)]

        blocks: list[str] = []
        for child in self.iter_payload_nested_values(current):
            blocks.extend(self.render_forward_payload_blocks(child, depth + 1, seen, image_state))
        return blocks

    def extract_forward_payload_entries(self, current: Any) -> list[Any]:
        if isinstance(current, dict):
            for key in ("messages", "msgs", "data", "records"):
                value = current.get(key)
                if isinstance(value, list) and value:
                    return value
            message_value = current.get("message")
            if isinstance(message_value, list) and message_value:
                return message_value
        if isinstance(current, list) and current and any(isinstance(item, dict) for item in current):
            return current
        return []

    def render_forward_payload_entries(
        self,
        entries: Sequence[Any],
        depth: int,
        seen: set[int],
        image_state: dict[str, Any],
    ) -> str:
        lines = [f"[转发正文 depth={depth}]"]
        for index, entry in enumerate(entries):
            if index >= self.max_nodes:
                lines.append("...[节点过多，已截断]")
                break
            rendered = self.render_forward_payload_entry(entry, depth, seen, image_state)
            if rendered:
                lines.append(rendered)
        return "\n".join(lines)

    def render_forward_payload_entry(
        self,
        entry: Any,
        depth: int,
        seen: set[int],
        image_state: dict[str, Any],
    ) -> str:
        sender = self.payload_sender_name(entry)
        text_parts = self.payload_text_parts(entry, image_state)
        nested_blocks: list[str] = []
        for child in self.iter_payload_nested_values(entry):
            nested_blocks.extend(self.render_forward_payload_blocks(child, depth + 1, seen, image_state))

        body_parts: list[str] = []
        if text_parts:
            body_parts.append(" ".join(text_parts))
        if nested_blocks:
            body_parts.append("\n".join(block for block in nested_blocks if block.strip()))
        if not body_parts:
            body_parts.append("[非文本节点]")

        body = "\n".join(part for part in body_parts if part.strip())
        return f"{sender}: {body}" if sender else body

    def payload_sender_name(self, entry: Any) -> str:
        if not isinstance(entry, dict):
            return ""
        if entry.get("type") == "forward":
            nested_sender = self.payload_sender_name(entry.get("data"))
            if nested_sender:
                return nested_sender

        for key in ("nickname", "name", "user_name"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        sender = entry.get("sender")
        if isinstance(sender, dict):
            for key in ("nickname", "name", "card", "user_name", "user_id"):
                value = sender.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, int):
                    return str(value)
        return ""

    def payload_text_parts(self, entry: Any, image_state: dict[str, Any]) -> list[str]:
        if isinstance(entry, str):
            stripped = entry.strip()
            return [stripped] if stripped else []
        if not isinstance(entry, dict):
            return []

        texts: list[str] = []
        if self.looks_like_component_dict(entry):
            component_text = self.component_dict_to_text(entry, image_state)
            if component_text:
                texts.append(component_text)
            return [part for part in texts if isinstance(part, str) and part.strip()]

        for key in ("text", "content", "message"):
            value = entry.get(key)
            texts.extend(self.collect_plain_texts(value))

        for key in ("message", "content"):
            value = entry.get(key)
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, dict):
                        continue
                    typ = item.get("type")
                    data = item.get("data")
                    if typ == "text" and isinstance(data, dict):
                        text = data.get("text")
                        if isinstance(text, str) and text.strip():
                            texts.append(text.strip())
                    elif typ == "image":
                        texts.append(self.register_image_placeholder(data, image_state))
                    elif typ in ("file", "record", "video"):
                        texts.append(f"[{typ}]")
        return [part for part in texts if isinstance(part, str) and part.strip()]

    def iter_payload_nested_values(self, value: Any) -> list[Any]:
        nested: list[Any] = []
        if isinstance(value, dict):
            if self.looks_like_component_dict(value):
                component_type = value.get("type")
                data = value.get("data")
                if component_type == "forward" and isinstance(data, dict):
                    content = data.get("content")
                    if isinstance(content, list):
                        nested.append(content)
                return nested

            for key, child in value.items():
                if key in PAYLOAD_NESTED_KEYS and child is not None and not isinstance(child, (str, int, float, bool)):
                    nested.append(child)
        elif isinstance(value, list):
            for child in value:
                if not isinstance(child, (str, int, float, bool)):
                    nested.append(child)
        return nested

    def looks_like_component_dict(self, value: Any) -> bool:
        return isinstance(value, dict) and isinstance(value.get("type"), str) and isinstance(value.get("data"), dict)

    def component_dict_to_text(self, component: dict[str, Any], image_state: dict[str, Any]) -> str:
        component_type = component.get("type")
        data = component.get("data", {})
        if component_type == "text":
            text = data.get("text")
            return text.strip() if isinstance(text, str) else ""
        if component_type == "at":
            qq = data.get("qq")
            return f"[at:{qq}]" if qq else "[at]"
        if component_type == "image":
            return self.register_image_placeholder(data, image_state)
        if component_type in ("file", "record", "video", "face", "reply"):
            return f"[{component_type}]"
        if component_type == "forward":
            return ""
        return f"[{component_type}]" if component_type else ""

    def register_image_placeholder(self, data: Any, image_state: dict[str, Any]) -> str:
        image_state["count"] = int(image_state.get("count", 0)) + 1
        index = image_state["count"]
        image_url = self.extract_image_url(data)
        if image_url:
            image_state.setdefault("image_urls", []).append(image_url)
        return f"[图片{index}]"

    def extract_image_url(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in ("url", "src", "image_url"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for key in ("file", "path"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if text.startswith(("http://", "https://", "file://")):
                    return text
                path = Path(text)
                if path.is_absolute():
                    return path.as_uri()
        return None

    def contains_forward_content(self, root: Any) -> bool:
        return self.contains_forward_content_inner(root, depth=0, seen=set())

    def contains_forward_content_inner(self, current: Any, depth: int, seen: set[int]) -> bool:
        if current is None:
            return False
        if depth > self.max_depth:
            return False

        marker = id(current)
        if marker in seen:
            return False
        seen.add(marker)

        if isinstance(current, Comp.Forward):
            return True
        if self.extract_forward_nodes(current):
            return True

        for child in self.iter_nested_values(current):
            if self.contains_forward_content_inner(child, depth + 1, seen):
                return True
        return False

    def render_forward_bundle(self, root: Any) -> str:
        blocks = self.render_forward_blocks(root, depth=0, seen=set())
        limited = [block for block in blocks if block.strip()]
        if not limited:
            return ""
        return self.truncate_text("\n\n".join(limited))

    def render_forward_blocks(self, current: Any, depth: int, seen: set[int]) -> list[str]:
        if current is None:
            return []
        if depth > self.max_depth:
            return [self.depth_limit_notice(depth)]

        marker = id(current)
        if marker in seen:
            return []
        seen.add(marker)

        blocks: list[str] = []
        nodes = self.extract_forward_nodes(current)
        if nodes:
            rendered = self.render_nodes(nodes, depth, seen)
            if rendered:
                blocks.append(rendered)

        for child in self.iter_nested_values(current):
            blocks.extend(self.render_forward_blocks(child, depth + 1, seen))
        return blocks

    def render_nodes(self, nodes: Sequence[Any], depth: int, seen: set[int]) -> str:
        lines = [f"[合并转发 depth={depth}]"]
        count = 0
        for node in nodes:
            if count >= self.max_nodes:
                lines.append("...[节点过多，已截断]")
                break
            rendered = self.render_single_node(node, depth, seen)
            if rendered:
                lines.append(rendered)
                count += 1
        return "\n".join(lines)

    def render_single_node(self, node: Any, depth: int, seen: set[int]) -> str:
        sender = self.node_sender_name(node)
        text_parts = self.node_text_parts(node)
        child_blocks = self.render_nested_from_node(node, depth + 1, seen)

        body_parts: list[str] = []
        if text_parts:
            body_parts.append(" ".join(text_parts))
        if child_blocks:
            body_parts.append("\n".join(child_blocks))
        if not body_parts:
            body_parts.append("[空白节点或非文本节点]")

        body = "\n".join(body_parts)
        prefix = f"{sender}: " if sender else ""
        return prefix + body

    def render_nested_from_node(self, node: Any, depth: int, seen: set[int]) -> list[str]:
        nested: list[str] = []
        for child in self.iter_nested_values(node):
            nested.extend(self.render_forward_blocks(child, depth, seen))
        return nested

    def extract_forward_nodes(self, current: Any) -> list[Any]:
        direct_nodes = self.coerce_nodes(current)
        if direct_nodes:
            return direct_nodes

        if isinstance(current, Comp.Reply):
            nodes = self.coerce_nodes(getattr(current, "chain", None))
            if nodes:
                return nodes

        for attr in FORWARD_NODE_ATTRS:
            nodes = self.coerce_nodes(getattr(current, attr, None))
            if nodes:
                return nodes

        if isinstance(current, dict):
            for key in FORWARD_NODE_ATTRS:
                nodes = self.coerce_nodes(current.get(key))
                if nodes:
                    return nodes
        return []

    def coerce_nodes(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, Comp.Nodes):
            return list(getattr(value, "nodes", []) or [])
        if isinstance(value, Comp.Node):
            return [value]
        if isinstance(value, str):
            return []
        if isinstance(value, Sequence):
            items = list(value)
            if items and any(self.looks_like_node(item) for item in items):
                return items
        return []

    def looks_like_node(self, value: Any) -> bool:
        if isinstance(value, Comp.Node):
            return True
        for attr in ("uin", "name", "nickname", "sender", "content", "message"):
            if hasattr(value, attr):
                return True
        if isinstance(value, dict):
            return any(key in value for key in ("uin", "name", "nickname", "sender", "content", "message", "id"))
        return False

    def node_sender_name(self, node: Any) -> str:
        if isinstance(node, Comp.Forward):
            forward_id = getattr(node, "id", None)
            return f"Forward<{forward_id}>" if forward_id else "Forward"

        for attr in ("name", "nickname", "sender_name"):
            value = getattr(node, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        sender = getattr(node, "sender", None)
        if isinstance(sender, str) and sender.strip():
            return sender.strip()
        if isinstance(sender, dict):
            for key in ("name", "nickname", "card", "user_name"):
                value = sender.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        if isinstance(node, dict):
            for key in ("name", "nickname", "sender_name"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            sender_obj = node.get("sender")
            if isinstance(sender_obj, str) and sender_obj.strip():
                return sender_obj.strip()
            if isinstance(sender_obj, dict):
                for key in ("name", "nickname", "card", "user_name"):
                    value = sender_obj.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()

        uin = getattr(node, "uin", None)
        if uin is None and isinstance(node, dict):
            uin = node.get("uin")
        return str(uin) if uin else ""

    def node_text_parts(self, node: Any) -> list[str]:
        if isinstance(node, Comp.Forward):
            forward_id = getattr(node, "id", None)
            return [f"[转发 ID: {forward_id}]"] if forward_id else ["[转发消息]"]

        message_like = None
        for attr in ("content", "message", "message_chain", "chain", "messages"):
            value = getattr(node, attr, None)
            if value is not None:
                message_like = value
                break

        if message_like is None and isinstance(node, dict):
            for key in ("content", "message", "message_chain", "chain", "messages"):
                value = node.get(key)
                if value is not None:
                    message_like = value
                    break

        return self.collect_plain_texts(message_like)

    def collect_plain_texts(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, Comp.Plain):
            text = getattr(value, "text", "")
            return [text] if text else []
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        if isinstance(value, dict):
            texts: list[str] = []
            for key in ("text", "content", "message"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    texts.append(item.strip())
            return texts
        if isinstance(value, Iterable):
            texts: list[str] = []
            for item in value:
                texts.extend(self.collect_plain_texts(item))
            return texts
        text_attr = getattr(value, "text", None)
        if isinstance(text_attr, str) and text_attr.strip():
            return [text_attr.strip()]
        return []

    def extract_message_chains(self, message_obj: Any) -> list[Any]:
        if message_obj is None:
            return []
        for attr in MESSAGE_CHAIN_ATTRS:
            value = getattr(message_obj, attr, None)
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
                return list(value)
        if isinstance(message_obj, dict):
            for key in MESSAGE_CHAIN_ATTRS:
                value = message_obj.get(key)
                if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
                    return list(value)
        return []

    def iter_nested_values(self, value: Any) -> list[Any]:
        nested: list[Any] = []
        if value is None:
            return nested

        for attr in NESTED_ATTRS:
            child = getattr(value, attr, None)
            if child is not None:
                nested.append(child)

        if isinstance(value, dict):
            for key in NESTED_ATTRS:
                child = value.get(key)
                if child is not None:
                    nested.append(child)
            nested.extend(v for k, v in value.items() if k not in NESTED_ATTRS and not isinstance(v, (str, int, float, bool)))
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            nested.extend(list(value))

        return nested

    def has_explicit_at_bot(self, event: Any) -> bool:
        chains = self.extract_message_chains(getattr(event, "message_obj", None))
        return bool(chains) and any(isinstance(component, Comp.At) for component in chains)

    def depth_limit_notice(self, depth: int) -> str:
        return f"[嵌套层数超过限制，已在 depth={depth} 截断]"

    def dedupe_strings(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def truncate_text(self, text: str) -> str:
        if len(text) > self.max_chars:
            return text[: self.max_chars - 20].rstrip() + "\n...[内容已截断]"
        return text
