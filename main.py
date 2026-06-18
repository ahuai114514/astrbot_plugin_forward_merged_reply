from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from fmr_forward import ForwardResolver
from fmr_persona import PersonaResolver
from fmr_reply import shorten_reply_text


PLUGIN_ID = "astrbot_plugin_forward_merged_reply"
EXTRA_PREFIX = "forward_merged_reply"
DEFAULT_PROCESSING_MESSAGE = "让我康康"
DEFAULT_SYSTEM_GUIDANCE = (
    "下面附带的是用户引用的一份或多份 QQ 合并转发记录展开内容。"
    "请根据用户当前提问和这些记录正常作答："
    "该回应就回应，该总结就总结，该提取信息就提取信息，不要默认把任务改写成纯总结。"
)


@register(PLUGIN_ID, "Codex", "让 Bot 理解并回应被引用的 QQ 合并转发消息", "1.0.0")
class ForwardMergedReplyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config
        self.persona_resolver = PersonaResolver(context)

    @filter.regex(r"^[\s\S]*$")
    async def handle_forward_merged_reply(self, event: AstrMessageEvent):
        if not self._is_enabled():
            return

        resolver = self._forward_resolver()
        await resolver.debug_event_shapes(event)
        if not resolver.should_process_event(event):
            return

        quoted = resolver.extract_quoted_message(event)
        if quoted is None:
            return

        rendered, image_urls = await resolver.render_forward_bundle_with_fetch(event, quoted)
        if not rendered:
            yield event.plain_result("我看到你引用了消息，但暂时没能解析出可用的转发内容。")
            return
        if resolver.should_ignore_rendered_forward(rendered):
            return

        event.stop_event()
        await event.send(event.plain_result(self._processing_message()))
        self._set_extra(event, "rendered", rendered)
        self._set_extra(event, "image_urls", image_urls)
        self._set_extra(event, "active", True)
        event.should_call_llm(True)
        event.continue_event()
        yield event.request_llm(
            prompt=self._build_prompt(event),
            contexts=[],
            session_id=None,
            image_urls=image_urls,
        )

    @filter.on_llm_request(priority=100)
    async def inject_forward_context(self, event: AstrMessageEvent, request) -> None:
        rendered = self._get_extra(event, "rendered")
        if not isinstance(rendered, str) or not rendered.strip():
            return

        request.prompt = self._merge_request_prompt(getattr(request, "prompt", ""), rendered)
        self._merge_request_images(request, self._get_extra(event, "image_urls"))
        request.system_prompt = await self._build_system_prompt(event, getattr(request, "system_prompt", None))

        if self._debug_enabled():
            logger.info(
                "[%s] persona_probe system_prompt_preview=%r",
                PLUGIN_ID,
                (getattr(request, "system_prompt", "") or "")[:120],
            )

    @filter.on_llm_response(priority=100)
    async def shorten_forward_reply(self, event: AstrMessageEvent, response) -> None:
        if not self._get_extra(event, "active", False):
            return
        text = getattr(response, "completion_text", None)
        if not isinstance(text, str) or not text.strip():
            return
        response.completion_text = shorten_reply_text(text, self._reply_max_chars())

    async def _build_system_prompt(self, event: AstrMessageEvent, current_system_prompt: Any) -> str:
        guidance = self._system_guidance()
        persona_prompt = await self.persona_resolver.resolve_prompt(event)
        if persona_prompt:
            return f"{persona_prompt.rstrip()}\n\n{guidance}"
        if isinstance(current_system_prompt, str) and current_system_prompt.strip():
            return f"{current_system_prompt.rstrip()}\n\n{guidance}"
        return guidance

    def _merge_request_prompt(self, current_prompt: Any, rendered: str) -> str:
        prompt = current_prompt if isinstance(current_prompt, str) else ""
        return f"{prompt.rstrip()}\n\n被引用的合并转发记录展开内容：\n{rendered}".strip()

    def _merge_request_images(self, request: Any, image_urls: Any) -> None:
        if not isinstance(image_urls, list):
            return
        filtered = [url for url in image_urls if isinstance(url, str) and url]
        current_images = getattr(request, "image_urls", None)
        if isinstance(current_images, list):
            current_images.extend(filtered)
        elif current_images is None:
            request.image_urls = filtered

    def _build_prompt(self, event: AstrMessageEvent) -> str:
        user_text = self._normalized_user_text(event)
        if user_text:
            return f"{user_text}\n\n请结合我引用的转发记录内容回答，并先判断我真正想让你做什么。"
        return (
            "我引用了一份转发记录并 @ 了你，但没有补充额外说明。"
            "请先理解这份内容本身，优先解释、概括或指出重点；"
            "不要默认进入代拟回复模式。"
        )

    def _normalized_user_text(self, event: AstrMessageEvent) -> str:
        text = (getattr(event, "message_str", "") or "").strip()
        return text.replace("[引用消息]", "").strip() if text else ""

    def _forward_resolver(self) -> ForwardResolver:
        return ForwardResolver(
            plugin_id=PLUGIN_ID,
            max_depth=self._max_depth(),
            max_nodes=self._max_nodes(),
            max_chars=self._max_chars(),
            debug_enabled=self._debug_enabled(),
        )

    def _extra_key(self, name: str) -> str:
        return f"{EXTRA_PREFIX}.{name}"

    def _set_extra(self, event: AstrMessageEvent, name: str, value: Any) -> None:
        event.set_extra(self._extra_key(name), value)

    def _get_extra(self, event: AstrMessageEvent, name: str, default: Any = None) -> Any:
        return event.get_extra(self._extra_key(name), default)

    def _is_enabled(self) -> bool:
        return bool(self._config_get("enabled", True))

    def _debug_enabled(self) -> bool:
        return bool(self._config_get("debug_logging", False))

    def _processing_message(self) -> str:
        value = self._config_get("processing_message", DEFAULT_PROCESSING_MESSAGE)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return DEFAULT_PROCESSING_MESSAGE

    def _system_guidance(self) -> str:
        value = self._config_get("system_guidance", DEFAULT_SYSTEM_GUIDANCE)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return DEFAULT_SYSTEM_GUIDANCE

    def _max_depth(self) -> int:
        return self._safe_int("max_depth", 4, minimum=1)

    def _max_nodes(self) -> int:
        return self._safe_int("max_nodes", 80, minimum=1)

    def _max_chars(self) -> int:
        return self._safe_int("max_chars", 12000, minimum=500)

    def _reply_max_chars(self) -> int:
        return self._safe_int("reply_max_chars", 80, minimum=10)

    def _safe_int(self, key: str, default: int, minimum: int) -> int:
        raw = self._config_get(key, default)
        try:
            return max(minimum, int(raw))
        except (TypeError, ValueError):
            return default

    def _config_get(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return getattr(self.config, key, default)
