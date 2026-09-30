"""Bounded, cancellable retries for model-generated context checkpoints."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _retry_pause(index):
    await asyncio.sleep(min(2 ** index, 8))


async def summarize_with_retries(call, *, normalize, valid, timeout_seconds=600,
                                 attempts=6, retry_delay=True):
    """Give each inference up to ten minutes; never publish an invalid summary.

    An outer request cancellation still propagates immediately. Only a valid
    answer commits a checkpoint; five retries after the first call leave the
    old ledger as-is if all six calls fail.
    """
    last_error = None
    for index in range(attempts):
        try:
            result = await asyncio.wait_for(call(), timeout=timeout_seconds)
            result = normalize(result)
            if not valid(result):
                raise ValueError("Summarizer returned no usable checkpoint")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if "context policy changed" in str(exc).lower():
                raise
            last_error = exc
            logger.warning("Context summarizer attempt %s/%s failed: %s",
                           index + 1, attempts, type(exc).__name__)
            if retry_delay and index + 1 < attempts:
                await _retry_pause(index)
    raise last_error or RuntimeError("Context summarizer did not run")


def bound_checkpoint_summary(summary: str, max_tokens: int = 1200) -> str:
    """Fit an overlong final answer without pretending omitted detail survived."""
    from src.model_context import estimate_tokens

    def weight(value):
        return estimate_tokens([{"role": "assistant", "content": value}])

    if weight(summary) <= max_tokens:
        return summary
    marker = ("\n[Checkpoint summary exceeded its token budget; middle omitted. "
              "Consult the durable transcript before relying on omitted details.]\n")
    low, high, fitted = 0, len(summary), ""
    while low <= high:
        keep = (low + high) // 2
        left = keep // 2
        right = keep - left
        candidate = summary[:left] + marker + (summary[-right:] if right else "")
        if weight(candidate) <= max_tokens:
            fitted = candidate
            low = keep + 1
        else:
            high = keep - 1
    if not fitted:
        raise ValueError("Checkpoint summary budget cannot fit omission marker")
    return fitted
