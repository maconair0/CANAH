"""
The context window and the KV cache: how much of the model's window a conversation fills, when it is
compacted, and how much of each prompt the model did not have to read again.

Context window
  LangChain models carry a profile; `max_input_tokens` in it is the window. deepagents' summarization reads
  it: at `compact_at` of the window (default 0.85) the older part of the conversation is summarised, the
  last `compact_keep` (default 0.10) is kept word for word, and what was summarised is saved under
  /conversation_history/ for the agent to read back. A model without a profile (ChatOllama has none) falls
  back to a fixed 170,000 tokens, which a 32,768-token Ollama window never reaches: Ollama then silently
  drops the start of the prompt, system prompt first. `give_profile` sets the window from the model itself
  (num_ctx, less the room kept for the answer) or from `context_window` in the settings.
  `compact_conversation` (deepagents' SummarizationToolMiddleware) lets the agent, or the operator by asking
  for it, compact before that, once the conversation is at half of the threshold.

KV cache
  A model keeps the keys and values it computed for a prompt; a next prompt that starts the same way (the
  same system prompt, tools and history, with something added at the end) reuses them, and only the new
  tokens are read. CANAH keeps its prompts append-only for that reason: the system prompt and tool list do
  not change within a session, and checks are added at the end, never inserted.
    hosted models   LangChain reports it exactly: usage_metadata.input_token_details.cache_read (and
                    cache_creation). deepagents adds Anthropic's (and Bedrock's, Fireworks') prompt-caching
                    middleware itself, which marks the cache breakpoints.
    Ollama          reuses the longest common prefix with the last prompt in the same slot, but reports only
                    the prompt's length and how long reading it took. The share read from cache is estimated
                    from that time against the model's cold reading speed, measured in this process on
                    prompts that were read whole (first default: 3,000 tokens/s), and marked as an estimate.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

# Ollama prompt reading speed, tokens per second, when nothing is cached: the fastest seen on a long prompt
# read in full. Starts from a typical figure for a 30B model on one workstation GPU.
_COLD = {"rate": 3000.0, "measured": False}
_lock = threading.Lock()


def window_of(model: Any, override: int = 0) -> Optional[int]:
    """The model's input window in tokens: the setting, the model's profile, or Ollama's num_ctx less num_predict."""
    if override:
        return int(override)
    prof = getattr(model, "profile", None)
    if isinstance(prof, dict) and isinstance(prof.get("max_input_tokens"), int):
        return prof["max_input_tokens"]
    num_ctx = getattr(model, "num_ctx", None)
    if num_ctx:
        return int(num_ctx) - int(getattr(model, "num_predict", None) or 0)
    return None


def give_profile(model: Any, override: int = 0) -> Optional[int]:
    """Put the window in the model's profile, so compaction is measured against the real window."""
    w = window_of(model, override)
    if w is None:
        return None
    prof = getattr(model, "profile", None)
    if not (isinstance(prof, dict) and prof.get("max_input_tokens") == w):
        try:
            model.profile = {**(prof if isinstance(prof, dict) else {}), "max_input_tokens": w}
        except Exception:  # noqa: BLE001 - a model that refuses a profile keeps deepagents' fixed fallback
            return None
    return w


@contextmanager
def compaction(at: float, keep: float) -> Iterator[None]:
    """deepagents fixes its thresholds at 0.85 and 0.10 of the window; build the agent with these instead."""
    import deepagents.middleware.summarization as s
    original = s.compute_summarization_defaults

    def ours(model):
        d = original(model)
        if d["trigger"][0] == "fraction":
            d = {**d, "trigger": ("fraction", at), "keep": ("fraction", keep),
                 "truncate_args_settings": {"trigger": ("fraction", at), "keep": ("fraction", keep)}}
        return d
    with _lock:
        s.compute_summarization_defaults = ours
        try:
            yield
        finally:
            s.compute_summarization_defaults = original


_LAST: Dict[str, Tuple[int, int]] = {}      # conversation -> (prompt tokens, answer tokens) of its last model call


def _conversation() -> str:
    """Which conversation a model call belongs to: its thread, and for a subagent the run of that subagent."""
    try:
        from langgraph.config import get_config
        c = (get_config() or {}).get("configurable") or {}
    except Exception:  # noqa: BLE001
        return ""
    ns = str(c.get("checkpoint_ns") or "")
    return f"{c.get('thread_id', '')}|{ns.split('|')[0] if '|' in ns else ''}"


def cache_of(message: Any, conversation: str = "") -> Dict[str, Any]:
    """What one model call read from the KV cache: {"prompt", "cached", "exact"} (tokens)."""
    usage = getattr(message, "usage_metadata", None) or {}
    meta = getattr(message, "response_metadata", None) or {}
    prompt = int(usage.get("input_tokens") or meta.get("prompt_eval_count") or 0)
    details = usage.get("input_token_details") or {}
    if "cache_read" in details:
        # LangChain's usage for a hosted model: input_tokens counts every input token, cache hits included.
        return {"prompt": prompt, "cached": int(details.get("cache_read") or 0), "exact": True,
                "written": int(details.get("cache_creation") or 0)}
    secs = (meta.get("prompt_eval_duration") or 0) / 1e9
    count = int(meta.get("prompt_eval_count") or 0)
    out_tokens = int(usage.get("output_tokens") or meta.get("eval_count") or 0)
    if not count or not secs:
        return {"prompt": prompt, "cached": None, "exact": False}
    # Ollama: a conversation's next prompt starts with its last prompt and answer, which is what its cache can
    # reuse (the prefix). The time it took to read says whether it did: the new part at the reading speed
    # learned here (only from calls whose new part is known), or, if it took far longer, the cache was lost.
    with _lock:
        last = _LAST.get(conversation) if conversation else None
        rate = _COLD["rate"]
        if last and last[0] + last[1] <= count:
            prefix = last[0] + last[1]
            new = count - prefix
            if new >= 300 and secs >= 0.05:
                sample = new / secs
                _COLD["rate"] = sample if not _COLD["measured"] else 0.8 * _COLD["rate"] + 0.2 * sample
                _COLD["measured"] = True
            hit = prefix if secs <= new / rate * 2.5 + 0.25 else max(0, int(count - secs * rate))
        else:
            hit = max(0, int(count - secs * rate))
        if conversation:
            _LAST[conversation] = (count, out_tokens)
            if len(_LAST) > 5000:
                _LAST.pop(next(iter(_LAST)))
    return {"prompt": count, "cached": int(min(hit, count)), "exact": False}


def usage(run: Dict[str, Any], window: Optional[int]) -> Dict[str, Any]:
    """The reply's figures: how full the window is after this turn, and the KV cache over its model calls."""
    out: Dict[str, Any] = {}
    used = run.get("context_tokens")
    if used is not None:
        out["context"] = {"used": used, "window": window,
                          "percent": round(100 * used / window, 1) if window else None}
    p, c = run.get("cache_prompt", 0), run.get("cache_hit", 0)
    if p:
        out["kv_cache"] = {"prompt_tokens": p, "cached_tokens": c, "percent": round(100 * c / p, 1),
                           "exact": bool(run.get("cache_exact"))}
    return out


def record(run: Dict[str, Any], message: Any, main: bool) -> None:
    """Add one model call to this request's counters (safety.RunTracker calls this)."""
    k = cache_of(message, _conversation())
    if k["cached"] is not None:
        run["cache_prompt"] = run.get("cache_prompt", 0) + k["prompt"]
        run["cache_hit"] = run.get("cache_hit", 0) + k["cached"]
        run["cache_exact"] = k["exact"]
    if main:
        out = int((getattr(message, "usage_metadata", None) or {}).get("output_tokens") or 0)
        # What the next call of this conversation starts from: this prompt and this answer.
        run["context_tokens"] = k["prompt"] + out
