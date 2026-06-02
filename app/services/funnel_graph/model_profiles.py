from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.services.funnel_graph.state import FunnelGraphState


ModelProfileName = Literal["max_only", "plus_max_router", "plus_only", "fast_max", "flash_plus_max"]


@dataclass(slots=True, frozen=True)
class ModelProfile:
    name: str
    semantic_model: str
    reply_model: str
    complex_reply_model: str
    use_fast_path: bool
    route_complex_to_max: bool


COMPLEX_MARKERS = (
    "?",
    "почему",
    "зачем",
    "сколько",
    "зарплат",
    "зп",
    "деньг",
    "оплат",
    "график",
    "безопас",
    "опас",
    "скам",
    "обман",
    "вебкам",
    "webcam",
    "onlyfans",
    "онлифанс",
    "гол",
    "раздев",
    "эрот",
    "интим",
    "нюд",
    "оформ",
    "договор",
    "документ",
)


SIMPLE_ACKS = {
    "да",
    "давай",
    "ок",
    "окей",
    "ага",
    "поняла",
    "понял",
    "ясно",
    "хорошо",
    "интересно",
    "расскажи",
}


def active_model_profile(settings: Settings) -> ModelProfile:
    name = normalize_profile_name(settings.model_test_profile)
    max_model = settings.qwen_max_model
    plus_model = settings.qwen_plus_model
    flash_model = settings.qwen_flash_model
    if name == "max_only":
        return ModelProfile(name, max_model, max_model, max_model, use_fast_path=False, route_complex_to_max=False)
    if name == "plus_only":
        return ModelProfile(name, plus_model, plus_model, plus_model, use_fast_path=True, route_complex_to_max=False)
    if name == "fast_max":
        return ModelProfile(name, max_model, max_model, max_model, use_fast_path=True, route_complex_to_max=False)
    if name == "flash_plus_max":
        return ModelProfile(name, flash_model, plus_model, max_model, use_fast_path=True, route_complex_to_max=True)
    return ModelProfile(
        "plus_max_router",
        plus_model,
        plus_model,
        max_model,
        use_fast_path=True,
        route_complex_to_max=True,
    )


def normalize_profile_name(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    allowed = {"max_only", "plus_max_router", "plus_only", "fast_max", "flash_plus_max"}
    return normalized if normalized in allowed else "plus_max_router"


def component_model_for_profile(settings: Settings, component: str) -> str | None:
    profile = active_model_profile(settings)
    if component == "semantic_analyzer":
        return settings.brain_semantic_model or profile.semantic_model
    if component == "semantic_analyzer_complex":
        return settings.brain_complex_semantic_model or profile.complex_reply_model
    if component == "reply_orchestrator":
        return settings.brain_reply_model or profile.reply_model
    if component == "reply_orchestrator_complex":
        return settings.brain_complex_reply_model or profile.complex_reply_model
    return None


def component_provider_for_profile(settings: Settings, component: str) -> str | None:
    if component == "semantic_analyzer":
        return settings.brain_semantic_provider
    if component == "semantic_analyzer_complex":
        return settings.brain_complex_semantic_provider
    if component == "reply_orchestrator":
        return settings.brain_reply_provider
    if component == "reply_orchestrator_complex":
        return settings.brain_complex_reply_provider
    return None


def is_complex_turn(state: FunnelGraphState) -> bool:
    semantic = dict(state.get("semantic_result") or {})
    if semantic.get("has_unresolved_interrupt"):
        return True
    message_type = str(semantic.get("message_type") or "")
    if message_type in {"interrupt_question", "objection", "mixed", "soft_refusal", "hard_refusal", "do_not_contact"}:
        return True
    text = normalize_plain(str(state.get("incoming_message") or ""))
    if not text:
        return False
    if sum(1 for marker in ("?", "почему", "зачем", "как", "сколько", "что ") if marker in text) >= 2:
        return True
    return any(marker in text for marker in COMPLEX_MARKERS)


def is_simple_turn_text(text: str) -> bool:
    normalized = normalize_plain(text)
    if not normalized:
        return False
    if normalized in SIMPLE_ACKS:
        return True
    if normalized.isdigit() and 14 <= int(normalized) <= 80:
        return True
    return len(normalized.split()) <= 3 and not any(marker in normalized for marker in COMPLEX_MARKERS)


def normalize_plain(text: str) -> str:
    return " ".join(text.strip().lower().replace("ё", "е").split())
