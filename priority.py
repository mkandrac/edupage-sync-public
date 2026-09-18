"""Preserve the source importance header removed by edupage-api 0.12.5."""


def source_priority(notification, source_items):
    source = source_items.get(str(notification.event_id), {})
    text = source.get("text")
    return {
        "source_text": text if isinstance(text, str) else None,
        "is_important": text.startswith("Dôležitá správa") if isinstance(text, str) else None,
        "importance_source": "timeline_text_prefix" if isinstance(text, str) and text.startswith("Dôležitá správa") else None,
        # A personal bookmark is not an importance flag set by the sender.
        "is_starred": bool(getattr(notification, "is_starred", False)),
    }
