"""Context assembly: system prompt, user role description, current page summary,
last N rounds of history, earliest-round truncation when over the token budget."""
from __future__ import annotations

from app.orchestration.tokens import estimate_tokens


def build_history_window(
    history: list[dict], max_rounds: int, token_budget: int, fixed_overhead_tokens: int
) -> list[dict]:
    """history: chronological list of {"role": "user"|"assistant", "content": str}.
    A round = one user message + the assistant reply that follows it.

    1. Keep at most the last `max_rounds` rounds.
    2. If the kept history exceeds `token_budget - fixed_overhead_tokens`,
       drop whole earliest rounds until it fits (never half-truncate a round).
    """
    # Split into rounds: each round starts with a user message.
    rounds: list[list[dict]] = []
    for msg in history:
        if msg.get("role") == "user" or not rounds:
            rounds.append([msg])
        else:
            rounds[-1].append(msg)
    rounds = rounds[-max_rounds:] if max_rounds > 0 else []
    available = max(0, token_budget - fixed_overhead_tokens)

    def round_size(r: list[dict]) -> int:
        return sum(estimate_tokens(m.get("content", "")) for m in r)

    while rounds and sum(round_size(r) for r in rounds) > available:
        rounds.pop(0)
    return [msg for r in rounds for msg in r]


def assemble_context(
    system_prompt: str,
    role_description: str,
    page_summary: str,
    history: list[dict],
    user_message: str,
    token_budget: int,
    max_rounds: int,
) -> tuple[list[dict], dict]:
    """Returns (messages_for_gateway, stats). stats records what was kept/dropped."""
    fixed_parts = [system_prompt, role_description, page_summary, user_message]
    overhead = sum(estimate_tokens(p) for p in fixed_parts)
    kept_history = build_history_window(history, max_rounds, token_budget, overhead)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"User role: {role_description}"},
        {"role": "system", "content": f"Current page: {page_summary}"},
    ]
    messages.extend(kept_history)
    messages.append({"role": "user", "content": user_message})
    stats = {
        "history_rounds_kept": len(kept_history) // 2,
        "history_messages_kept": len(kept_history),
        "history_truncated": len(history) > len(kept_history),
        "context_tokens_estimate": sum(estimate_tokens(m["content"]) for m in messages),
    }
    return messages, stats
