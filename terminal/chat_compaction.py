"""Prepare bounded AI chat context without dropping source messages on failure."""

from __future__ import annotations

from typing import Any, Callable

import ai_chat


def prepare_chat_context(
    database: Any,
    session_id: int,
    snapshot: str,
    limit: int,
    *,
    keep_last: int = 20,
    summarizer: Callable[..., str] | None = None,
) -> tuple[list[dict[str, str]], str, str, int, int]:
    summarizer = summarizer or ai_chat.summarize_for_compaction
    memory = database.get_session_memory(session_id)
    summary = database.get_session_summary(session_id)
    history = _history(database, session_id)
    estimate = ai_chat.estimate_prompt_tokens(
        history,
        snapshot,
        session_summary=summary,
        session_memory=memory,
    )
    if estimate <= limit:
        return history, memory, summary, estimate, 0

    candidates, expected_summary = database.compaction_candidates(session_id, keep_last=keep_last)
    if not candidates:
        raise ai_chat.ChatError("AI context exceeds CHAT_CONTEXT_LIMIT and has no older messages to compact")

    usage: dict[str, Any] = {}
    new_summary = summarizer(candidates, expected_summary, usage_sink=usage.update)
    database.add_billed_tokens(session_id, int(usage.get("total_tokens") or 0))
    if not database.commit_compaction(
        session_id,
        [int(message["id"]) for message in candidates],
        expected_summary,
        new_summary,
    ):
        raise ai_chat.ChatError("AI context changed during compaction; retry the request")

    history = _history(database, session_id)
    summary = new_summary
    estimate = ai_chat.estimate_prompt_tokens(
        history,
        snapshot,
        session_summary=summary,
        session_memory=memory,
    )
    if estimate > limit:
        raise ai_chat.ChatError("AI context still exceeds CHAT_CONTEXT_LIMIT after compaction")
    return history, memory, summary, estimate, len(candidates)


def _history(database: Any, session_id: int) -> list[dict[str, str]]:
    return [
        {"role": message["role"], "content": message["content"]}
        for message in database.get_messages(session_id, limit=400)
    ]
