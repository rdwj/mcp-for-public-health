"""Domain-neutral data agent with structured reasoning protocol.

This agent extends BaseAgent with two post-processing layers:

1. A tool-call insistor that retries when the model produces an answer
   without calling any tools. The system prompt requires grounding in
   retrieved data, so a tool-free answer is always wrong.

2. A reasoning rubric injector that, after the tool-calling loop
   produces an answer, makes a second structured-output call to
   produce the 6-step reasoning rubric. The model's single-shot
   response often skips structured output; the second call is
   deterministic and prefix-cache-cheap.

The confidence card is built programmatically from the tool trace --
no LLM call needed. This makes confidence calibration reproducible.

NOTE: Model-specific workarounds (streaming parser fixes, chat template
quirks) would go in setup(). See the comments in that method.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from fipsagents.baseagent import BaseAgent, StepResult
from fipsagents.baseagent.events import (
    ContentDelta,
    StreamComplete,
    StreamEvent,
    ToolResultEvent,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured reasoning rubric
# ---------------------------------------------------------------------------

class ReasoningRubric(BaseModel):
    """The 6-consideration reasoning rubric.

    Each field is a brief sentence or two. ``cross_dataset`` is never
    "N/A" per the protocol rules; the others may be "N/A" when the
    consideration doesn't apply to the user's question.
    """

    cross_dataset: str = Field(
        ...,
        description=(
            "Which dataset(s) you used and why, including which alternatives "
            "were considered and rejected. NEVER 'N/A'."
        ),
    )
    methodology: str = Field(
        ...,
        description=(
            "What methodology you retrieved and how it informed your answer, "
            "or 'N/A' if not applicable."
        ),
    )
    scope: str = Field(
        ...,
        description=(
            "Whether the question is within the data's scope, "
            "or 'N/A' if not applicable."
        ),
    )
    causal_inference: str = Field(
        ...,
        description=(
            "Whether causal claims are appropriate given the data, "
            "or 'N/A' if the question doesn't imply causation."
        ),
    )
    geographic: str = Field(
        ...,
        description=(
            "Geographic resolution analysis (level, why, alternatives), "
            "or 'N/A' if the question isn't geographic."
        ),
    )
    terminology: str = Field(
        ...,
        description=(
            "Any term mapping needed between the user's wording and the "
            "indicator name, or 'N/A' if no mapping was needed."
        ),
    )


_RUBRIC_REQUEST = (
    "Now produce ONLY the structured reasoning rubric for the answer you just "
    "gave above, as JSON matching the provided schema. Each value should be a "
    "brief sentence or two -- not a paragraph. Use 'N/A' for considerations "
    "that don't apply, EXCEPT cross_dataset which is never 'N/A' (every data "
    "query involves a dataset choice, even when obvious)."
)


def _format_rubric_block(rubric: ReasoningRubric) -> str:
    """Render the rubric as the ``<reasoning>...</reasoning>`` block.

    The chat UI can parse these tags into labeled cards.
    """
    return (
        "<reasoning>\n"
        f"cross_dataset: {rubric.cross_dataset}\n"
        f"methodology: {rubric.methodology}\n"
        f"scope: {rubric.scope}\n"
        f"causal_inference: {rubric.causal_inference}\n"
        f"geographic: {rubric.geographic}\n"
        f"terminology: {rubric.terminology}\n"
        "</reasoning>"
    )


# ---------------------------------------------------------------------------
# Programmatic confidence card
# ---------------------------------------------------------------------------

def _build_confidence_card(
    used_tools: bool,
    tool_results: list[str],
    rubric: ReasoningRubric | None = None,
) -> str:
    """Compute a Data Confidence + Data Freshness card from the tool trace.

    Deterministic -- no LLM call. The confidence level is based on what
    actually happened during the turn, not the model's self-assessment.
    If a rubric is provided, scope assessment can downgrade confidence
    when the question is out of scope for the available data.
    """
    import json as _json
    from datetime import datetime, timezone

    level = "LOW"
    basis = "No tools were called; the answer may not be grounded in data."
    source_name = ""
    source_url = ""
    data_year = ""
    dataset_updated = ""

    if used_tools and tool_results:
        has_data = False
        has_methodology = False
        # Data kinds declared by the tools (see templates/mcp-server/README.md).
        # Survey estimates need methodology to be fully trusted; administrative
        # and registry records are authoritative as records of what happened.
        data_kinds: set[str] = set()

        for result_text in tool_results:
            try:
                result = _json.loads(result_text)
            except (ValueError, TypeError):
                continue

            kind = str(result.get("data_kind") or "").strip().lower()
            if kind:
                data_kinds.add(kind)

            results_list = (
                result.get("results")
                or result.get("availability")
                or result.get("records")
            )
            if results_list:
                has_data = True
            elif kind in ("administrative", "registry") and not result.get("error"):
                # A single-record response (one award, one case) has no list.
                has_data = True

            if result.get("methodology") or result.get("methodology_structured"):
                has_methodology = True

            freshness = result.get("data_freshness", {})
            if freshness:
                source_name = freshness.get("dataset_name", source_name)
                source_url = freshness.get("dataset_url", source_url)
                dataset_updated = freshness.get("dataset_updated", dataset_updated)

            citation = result.get("citation", {})
            if citation and not source_name:
                source_name = citation.get("source", "")

            if isinstance(results_list, list):
                for row in results_list:
                    if isinstance(row, dict) and row.get("year"):
                        data_year = str(row["year"])
                        break

        if has_data and has_methodology:
            level = "HIGH"
            basis = (
                "Retrieved data with methodology context. Scope and "
                "geographic resolution verified via tool response metadata."
            )
        elif has_data and data_kinds and data_kinds <= {"administrative", "registry"}:
            level = "HIGH"
            basis = (
                "Retrieved administrative/registry records (authoritative "
                "as a record of what was funded, filed or reported -- not "
                "survey estimates and not evidence of outcomes)."
            )
        elif has_data:
            level = "MODERATE"
            basis = (
                "Retrieved data but methodology context was partial or "
                "not explicitly retrieved. The answer is data-grounded "
                "but interpretation gaps may exist."
            )
        else:
            level = "LOW"
            basis = (
                "Tools were called but did not return the requested data. "
                "The answer may rely on partial information."
            )

    # Scope override: if the rubric's scope field signals the question
    # is outside what the data can support, downgrade to LOW regardless
    # of whether tools returned data.
    rubric_text_to_check = ""
    if rubric:
        for field in [rubric.scope, rubric.causal_inference]:
            if field and field.strip().upper() != "N/A":
                rubric_text_to_check += " " + field
    if rubric_text_to_check:
        scope_lower = rubric_text_to_check.lower()
        out_of_scope_signals = [
            "out of scope", "outside the scope", "outside scope",
            "beyond the scope", "not within scope",
            "cannot support", "cannot answer", "cannot be answered",
            "does not cover", "not supported", "not designed to answer",
            "cannot determine", "cannot measure",
            "do not establish causal", "does not establish causal",
            "cannot establish causal", "causal relationship",
            "causal claim", "not establish causal",
            "long-term effect", "long-term health",
            "individual risk", "treatment outcome",
        ]
        if any(signal in scope_lower for signal in out_of_scope_signals):
            level = "LOW"
            basis = (
                "The question asks about something the available data "
                "cannot support (e.g., causal claims, predictions, "
                "health effects). Data was retrieved but does not answer "
                "the user's actual question."
            )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        "---",
        "**Data Confidence: %s**" % level,
        basis,
    ]
    if source_name:
        if source_url:
            lines.append(
                "\n**Data Freshness**\n"
                "**Source:** [%s](%s)" % (source_name, source_url)
            )
        else:
            lines.append("\n**Data Freshness**\n**Source:** %s" % source_name)
        year_part = "**Data Year:** %s" % data_year if data_year else ""
        updated_part = (
            "**Updated:** %s" % dataset_updated if dataset_updated else ""
        )
        retrieved_part = "**Retrieved:** %s" % now
        parts = [p for p in [year_part, updated_part, retrieved_part] if p]
        if parts:
            lines.append(" | ".join(parts))
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class DomainDataAgent(BaseAgent):
    """Data agent with tool-call enforcement and structured reasoning.

    Rename this class to match your domain (e.g., AirQualityAgent,
    WaterQualityAgent).
    """

    async def setup(self) -> None:
        await super().setup()
        # -----------------------------------------------------------------
        # Model-specific workarounds go here. Examples:
        #
        # - If your model's streaming tool-call parser is broken, you can
        #   monkey-patch self.llm.call_model_stream_raw to route through
        #   non-streaming completions and yield a single fake chunk.
        #
        # - If your model's chat template implicitly opens a <think> block,
        #   you can install a custom reasoning parser here.
        #
        # These are model-specific -- only add them if you observe issues
        # with your particular model endpoint.
        # -----------------------------------------------------------------

    async def step(self) -> StepResult:
        """Non-streaming entry point (used by tests, not the HTTP server)."""
        response = await self.call_model()
        response = await self.run_tool_calls(response)
        return StepResult.done(response.content)

    # -- Tool names for the insistor message. Update these to match your
    #    MCP server's actual tool names.
    _TOOL_INSIST_MSG = (
        "Your answer did not include any tool calls. The system prompt "
        "requires you to call at least one tool (for example query_data, "
        "describe_datasets, or get_methodology) to retrieve "
        "real data before answering. Please call the appropriate tool "
        "now -- do not answer from memory."
    )

    async def astep_stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        """Streaming entry point with two post-processing layers.

        1. Tool-call insistor: if the model produces an answer without
           calling any tool, discard the content, inject a correction
           message, and retry once.

        2. Reasoning rubric injection: after the tool-calling loop
           produces an answer, make a second structured-output call to
           produce the <reasoning> block, then append a deterministic
           confidence card.
        """
        max_insist_retries = 1

        for attempt in range(1 + max_insist_retries):
            content_buffer: list[str] = []
            used_tools = False
            tool_results: list[str] = []

            async for event in super().astep_stream(**kwargs):
                if isinstance(event, ContentDelta):
                    content_buffer.append(event.content)
                    continue
                if isinstance(event, ToolResultEvent):
                    used_tools = True
                    tool_results.append(event.content or "")
                    yield event
                    continue
                if isinstance(event, StreamComplete):
                    if not used_tools and attempt < max_insist_retries:
                        logger.warning(
                            "Model produced an answer without tool "
                            "calls (attempt %d); insisting.",
                            attempt + 1,
                        )
                        self.messages.append(
                            {"role": "user", "content": self._TOOL_INSIST_MSG}
                        )
                        break  # re-enter outer for-loop
                    # Either tools were used, or we've exhausted retries.
                    full = "".join(content_buffer)
                    rubric = None
                    if used_tools and full.strip() and "<reasoning>" not in full:
                        rubric = await self._build_rubric()
                        if rubric:
                            yield ContentDelta(
                                content=_format_rubric_block(rubric) + "\n\n"
                            )
                    if content_buffer:
                        yield ContentDelta(content=full)
                    # Append the programmatic confidence card if the
                    # model didn't already emit one.
                    if "Data Confidence" not in full:
                        card = _build_confidence_card(
                            used_tools, tool_results, rubric
                        )
                        yield ContentDelta(content="\n\n" + card)
                    yield event
                    return
                yield event

    async def _build_rubric(self) -> ReasoningRubric | None:
        """Run a structured-output call for the rubric.

        Returns the parsed Pydantic model, or None on any failure.
        """
        try:
            rubric = await self.call_model_json(
                ReasoningRubric,
                messages=self.messages
                + [{"role": "user", "content": _RUBRIC_REQUEST}],
            )
        except Exception as exc:
            logger.warning(
                "Rubric call failed (%s: %s); returning unstructured answer.",
                type(exc).__name__,
                exc,
            )
            return None
        logger.info("Rubric call succeeded.")
        return rubric


if __name__ == "__main__":
    from fipsagents.baseagent import load_config
    from fipsagents.server import OpenAIChatServer

    config = load_config("agent.yaml")
    server = OpenAIChatServer(
        agent_class=DomainDataAgent,
        config_path="agent.yaml",
        title=config.agent.name,
        version=config.agent.version,
    )
    server.run(host=config.server.host, port=config.server.port)
