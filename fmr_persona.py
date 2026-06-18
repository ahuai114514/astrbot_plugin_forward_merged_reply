from __future__ import annotations

from typing import Any


class PersonaResolver:
    def __init__(self, context: Any) -> None:
        self.context = context

    async def resolve_prompt(self, event: Any) -> str:
        provider = self._get_provider(event)
        provider_manager = getattr(self.context, "provider_manager", None)
        conversation = await self._find_target_conversation(event)

        personality_prompt = ""
        persona_id = getattr(conversation, "persona_id", None) if conversation else None
        if isinstance(persona_id, str) and persona_id.strip() and persona_id != "[%None]":
            personality_prompt = self._find_persona_prompt_by_name(
                getattr(provider_manager, "personas", None),
                persona_id.strip(),
            )

        if not personality_prompt and provider_manager is not None:
            default_persona = getattr(provider_manager, "selected_default_persona", None)
            if isinstance(default_persona, dict):
                default_persona_name = default_persona.get("name")
                if isinstance(default_persona_name, str) and default_persona_name.strip():
                    personality_prompt = self._find_persona_prompt_by_name(
                        getattr(provider_manager, "personas", None),
                        default_persona_name.strip(),
                    )

        if (
            not personality_prompt
            and provider is not None
            and hasattr(provider, "curr_personality")
            and isinstance(provider.curr_personality, dict)
        ):
            prompt = provider.curr_personality.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                personality_prompt = prompt.strip()

        return personality_prompt

    def _get_provider(self, event: Any) -> Any:
        get_using_provider = getattr(self.context, "get_using_provider", None)
        if not callable(get_using_provider):
            return None
        try:
            return get_using_provider(getattr(event, "unified_msg_origin", None))
        except TypeError:
            return get_using_provider()

    async def _find_target_conversation(self, event: Any) -> Any | None:
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if conversation_manager is None:
            return None

        get_conversations = getattr(conversation_manager, "get_conversations", None)
        if not callable(get_conversations):
            return None

        try:
            conversations = await get_conversations(platform_id=self._platform_id(event))
        except Exception:
            return None
        if not conversations:
            return None

        identifiers = self._conversation_identifiers(event)
        for conv in conversations:
            user_id = getattr(conv, "user_id", None)
            if not isinstance(user_id, str) or not user_id.strip():
                continue
            normalized = user_id.strip()
            if normalized in identifiers:
                return conv
            if any(normalized.endswith(f"_{identifier}") for identifier in identifiers):
                return conv
        return None

    def _platform_id(self, event: Any) -> str:
        for attr in ("platform_id", "platform_name"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        platform_meta = getattr(getattr(event, "platform_meta", None), "name", None)
        if isinstance(platform_meta, str) and platform_meta.strip():
            return platform_meta.strip()

        return "aiocqhttp"

    def _conversation_identifiers(self, event: Any) -> list[str]:
        identifiers: list[str] = []

        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                value = None
            if value is not None:
                identifiers.append(str(value).strip())

        for attr in ("session_id", "sessionid", "user_id", "sender_id", "unified_msg_origin"):
            value = getattr(event, attr, None)
            if value is not None:
                identifiers.append(str(value).strip())

        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        if isinstance(sender, dict):
            for key in ("user_id", "userId", "uin", "qq"):
                value = sender.get(key)
                if value is not None:
                    identifiers.append(str(value).strip())

        return [identifier for identifier in identifiers if identifier]

    def _find_persona_prompt_by_name(self, personas: Any, persona_name: str) -> str:
        if not isinstance(personas, list):
            return ""
        for persona in personas:
            if not isinstance(persona, dict):
                continue
            if persona.get("name") != persona_name:
                continue
            prompt = persona.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                return prompt.strip()
        return ""
