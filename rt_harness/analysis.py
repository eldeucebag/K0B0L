"""Reasoning separation and telemetry analysis.

Ollama hands back a completion in three pieces that matter here: the ``response``
text, a separate ``thinking`` field, and the token counters. The counters are the
only honest signal that something went wrong -- Ollama will happily return a
short, clean-looking answer to a prompt it silently truncated.

Reasoning left inline in ``response`` is the dangerous case: it is handed to the
next role as if it were the artifact under test, so a target ends up grading the
attacker's planning notes. Absent a closing tag it cannot be separated at all,
which is why that case is flagged rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

#: gemma4 abliterations open with ``<|channel>thought``; DeepSeek-R1 distills
#: open with `` thinking`` and frequently omit the closing tag.
INLINE_REASONING_HEAD = re.compile(r"\s*(?:<\|channel>thought|<\s*think\s*>)\s*")
INLINE_REASONING_CLOSER = re.compile(r"<channel\|>|<\s*/\s*think\s*>")


@dataclass(frozen=True)
class CleanedOutput:
    """A completion split into answer and inline reasoning."""

    response: str
    inline_reasoning: str
    unterminated: bool


def split_inline_reasoning(response: str) -> CleanedOutput:
    """Strip a leading inline reasoning block out of ``response``.

    Returns the response unchanged with ``unterminated=True`` when the block
    never closes, because in that case there is no safe split point.
    """
    head = INLINE_REASONING_HEAD.match(response)
    if head is None:
        return CleanedOutput(response=response, inline_reasoning="", unterminated=False)

    closer = INLINE_REASONING_CLOSER.search(response)
    if closer is None:
        return CleanedOutput(response=response, inline_reasoning="", unterminated=True)

    return CleanedOutput(
        response=response[closer.end() :].strip(),
        inline_reasoning=response[head.end() : closer.start()].strip(),
        unterminated=False,
    )


@dataclass(frozen=True)
class Telemetry:
    """One call's counters, as written to ``<role>.json``."""

    model: str | None
    done_reason: str | None
    prompt_eval_count: int
    eval_count: int
    total_duration_s: float
    response_chars: int
    thinking_chars: int
    reasoning_unterminated: bool

    @classmethod
    def from_response(
        cls,
        raw: Mapping[str, Any],
        cleaned: CleanedOutput,
        separate_thinking: str,
    ) -> "Telemetry":
        return cls(
            model=raw.get("model"),
            done_reason=raw.get("done_reason"),
            prompt_eval_count=raw.get("prompt_eval_count") or 0,
            eval_count=raw.get("eval_count") or 0,
            total_duration_s=round((raw.get("total_duration") or 0) / 1e9, 2),
            response_chars=len(cleaned.response),
            # Reasoning that arrived in either channel counts; both are hidden
            # from the next role.
            thinking_chars=len(separate_thinking) + len(cleaned.inline_reasoning),
            reasoning_unterminated=cleaned.unterminated,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "done_reason": self.done_reason,
            "prompt_eval_count": self.prompt_eval_count,
            "eval_count": self.eval_count,
            "total_duration_s": self.total_duration_s,
            "response_chars": self.response_chars,
            "thinking_chars": self.thinking_chars,
            "reasoning_unterminated": self.reasoning_unterminated,
        }

    def report_line(self, role: str, num_ctx: int, num_predict: int) -> str:
        return (
            f"  [{role}] prompt={self.prompt_eval_count} tok  "
            f"out={self.eval_count} tok  "
            f"reasoning={self.thinking_chars} chars  "
            f"{self.total_duration_s}s  "
            f"budget={num_ctx} ctx / {num_predict} out"
        )


def analyse(telemetry: Telemetry, role: str, num_ctx: int, num_predict: int) -> list[str]:
    """Flag the ways a call can be cut short or polluted without Ollama saying so."""
    warnings: list[str] = []
    prompt_tokens = telemetry.prompt_eval_count
    output_tokens = telemetry.eval_count

    if num_ctx and prompt_tokens >= int(num_ctx * 0.95):
        warnings.append(
            f"prompt is {prompt_tokens} tokens against num_ctx={num_ctx}; Ollama "
            f"drops the oldest tokens to fit. Raise {role}_NUM_CTX."
        )

    # Middle-out truncation signature: the context budget is split in half, so an
    # overflowing prompt arrives as ~num_ctx/2 tokens, well under the 95% check
    # above. Measured here: 6850 tokens of the artifact arrived as 2051 at
    # ctx 4096, and a 140 KB prompt arrived as 8195 at ctx 16384 -- both exactly
    # half. A plausible prompt sitting on half the budget is not a coincidence.
    if num_ctx > 4096 and abs(prompt_tokens - num_ctx // 2) <= max(64, num_ctx // 100):
        warnings.append(
            f"prompt is {prompt_tokens} tokens against num_ctx={num_ctx} -- that is "
            f"half the budget, which is the middle-out truncation signature: the "
            f"context was halved from the head and the tail and the MIDDLE of the "
            f"prompt was deleted. Raise {role}_NUM_CTX."
        )

    if telemetry.reasoning_unterminated:
        warnings.append(
            f"{role} emitted an unterminated reasoning block (a  thinking-style "
            f"opener with no closing tag), so its output still carries that "
            f"reasoning. See {role}.inline.txt; consider {role}_THINK=true."
        )

    if num_predict and output_tokens >= num_predict:
        warnings.append(
            f"output stopped at num_predict={num_predict}; the response is cut "
            f"off mid-generation. Raise {role}_NUM_PREDICT."
        )

    if num_ctx and num_predict and prompt_tokens + num_predict > num_ctx:
        warnings.append(
            f"prompt {prompt_tokens} tok + num_predict {num_predict} exceeds "
            f"num_ctx={num_ctx} ({prompt_tokens + num_predict} total). Generation "
            f"stops at the context ceiling and the output budget is never spent. "
            f"Raise {role}_NUM_CTX or lower {role}_NUM_PREDICT."
        )

    if not telemetry.response_chars and telemetry.thinking_chars:
        spent = bool(num_predict) and output_tokens >= num_predict
        warnings.append(
            f"response is empty while reasoning produced {telemetry.thinking_chars} "
            f"chars: "
            + (
                f"the whole {num_predict}-token budget went to reasoning. "
                f"Raise {role}_NUM_PREDICT."
                if spent
                else f"the model stopped after {output_tokens} tokens of reasoning "
                f"without emitting an answer. Set {role}_THINK=false, or raise "
                f"{role}_NUM_PREDICT if it was still mid-thought."
            )
        )

    return warnings
