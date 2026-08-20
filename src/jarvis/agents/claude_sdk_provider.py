"""Claude Agent SDK provider - Blueprint 4.1 and 6.2.

The first real implementation of the `IntelligenceProvider` port from
`agents/provider.py`. Nothing in the Core changes to use this: only the
`provider=` argument to `JarvisCore` does, which is the whole point of
Principle 1 ("Claude ist ein austauschbarer Intelligence Provider").

The SDK's native job is to run its own agent loop with real tool execution -
Bash, file edits, web fetches - authorised by its own permission system. That
loop is exactly the direct model-to-OS access Principle 2 forbids ("Rechte ...
werden deterministisch von unserer Software kontrolliert; niemals nur durch
einen Prompt"), so this adapter never turns it on. Three independent controls
enforce that, any one of which would be sufficient on its own:

1. ``tools=[]`` removes every built-in SDK tool (Bash, Read, Edit, WebFetch,
   ...) from the model's toolset entirely - not merely un-allowed, absent.
2. Every capability we do expose is wrapped as an *inert* MCP tool
   (`_build_inert_tool`). Calling it never reaches the real capability
   handler, a real file, or a real secret - it only records what the model
   proposed and replies "queued". The actual effect happens afterwards, when
   the Agent Coordinator hands the proposal to the Execution Gateway, exactly
   like every other provider's proposals (schema re-validated, permission
   re-checked).
3. ``strict_mcp_config=True`` and ``setting_sources=[]`` stop the CLI from
   picking up any ambient `.mcp.json` or `~/.claude/settings.json` on the host
   that might otherwise add unrelated tools or permissions outside this
   adapter's control.

A `ThinkingBlock` in the response is discarded, not forwarded anywhere.
Blueprint 2.4: "Die Oberfläche zeigt keine versteckten privaten
Reasoning-Ketten des Modells."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import claude_agent_sdk as sdk

from jarvis.agents.provider import AgentRequest, AgentResponse, ProposedToolCall

log = logging.getLogger(__name__)

MCP_SERVER_NAME = "jarvis"

#: Blueprint 9.2 style budget: a single Core turn should not let the SDK loop
#: internally for long. The Agent Coordinator's own turn budget is what
#: actually bounds a mission; this is a smaller, independent safety net around
#: one `plan()` call.
DEFAULT_SDK_MAX_TURNS = 4

_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


class ProviderUnavailable(RuntimeError):
    """The SDK could not be reached (no CLI, no auth, transport failure)."""


class ProviderError(RuntimeError):
    """The SDK ran but reported an error result."""


def _json_schema(capability: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON Schema object from one `Capability.to_dict()` entry."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in capability["schema"]["params"]:
        prop: dict[str, Any] = {"type": _JSON_TYPES[param["type"]]}
        if param["description"]:
            prop["description"] = param["description"]
        if param["choices"]:
            prop["enum"] = param["choices"]
        properties[param["name"]] = prop
        if param["required"]:
            required.append(param["name"])
    return {"type": "object", "properties": properties, "required": required}


def _mcp_tool_name(capability_name: str) -> str:
    return f"mcp__{MCP_SERVER_NAME}__{capability_name}"


def _build_inert_tool(
    capability: dict[str, Any], proposals: list[ProposedToolCall]
) -> sdk.SdkMcpTool[Any]:
    """Wrap one capability as an SDK tool that only ever records a proposal.

    The returned tool never calls the real capability handler. It cannot: this
    module has no reference to the `CapabilityRegistry` or to any handler at
    all, only to capability *descriptions* handed in through `AgentRequest`
    (Blueprint 6.2's own boundary - the provider sees schema, not code).
    """
    name = capability["name"]
    schema = _json_schema(capability)

    @sdk.tool(name, capability["description"], schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        proposals.append(ProposedToolCall(capability=name, params=dict(args)))
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Recorded: {name} will be evaluated by the JARVIS "
                        "Permission Engine and, if approved, run by the "
                        "Execution Gateway. Not executed by this session."
                    ),
                }
            ]
        }

    return handler


def _build_options(
    request: AgentRequest,
    *,
    model: str,
    effort: str,
    max_turns: int,
    proposals: list[ProposedToolCall],
) -> sdk.ClaudeAgentOptions:
    inert_tools = [_build_inert_tool(cap, proposals) for cap in request.available_capabilities]
    allowed = [_mcp_tool_name(cap["name"]) for cap in request.available_capabilities]

    system_prompt = (
        "You are the reasoning component behind JARVIS, a personal AI operating "
        "system. You do not have direct access to the operating system, files, "
        "or the network. Every tool available to you only records a proposal; "
        "JARVIS's own Permission Engine and Execution Gateway decide, "
        "independently of you, whether and how it actually runs. Propose the "
        "single next concrete step toward the goal using one of the provided "
        "tools, or explain briefly why no tool applies. Do not invent "
        "capabilities that were not given to you."
    )

    return sdk.ClaudeAgentOptions(
        # Layer 1: no built-in tool exists in this session at all.
        tools=[],
        # Layer 2 (see module docstring): only our inert wrappers, and only
        # from the MCP server we constructed for this call.
        mcp_servers={
            MCP_SERVER_NAME: sdk.create_sdk_mcp_server(name=MCP_SERVER_NAME, tools=inert_tools)
        },
        allowed_tools=allowed,
        strict_mcp_config=True,
        setting_sources=[],
        system_prompt=system_prompt,
        model=model,
        effort=effort,  # type: ignore[arg-type]
        max_turns=max_turns,
        permission_mode="default",
    )


@dataclass(slots=True)
class ClaudeAgentSdkProvider:
    """Implements `jarvis.agents.provider.IntelligenceProvider`."""

    max_sdk_turns: int = DEFAULT_SDK_MAX_TURNS
    name: str = field(default="claude-agent-sdk", init=False)

    async def plan(self, request: AgentRequest, *, model: str, effort: str) -> AgentResponse:
        proposals: list[ProposedToolCall] = []
        options = _build_options(
            request,
            model=model,
            effort=effort,
            max_turns=self.max_sdk_turns,
            proposals=proposals,
        )

        summary_parts: list[str] = []
        cost_usd = 0.0

        try:
            async for message in sdk.query(prompt=request.goal, options=options):
                if isinstance(message, sdk.AssistantMessage):
                    for block in message.content:
                        if isinstance(block, sdk.TextBlock):
                            summary_parts.append(block.text)
                        # ThinkingBlock is deliberately not collected anywhere.
                elif isinstance(message, sdk.ResultMessage):
                    cost_usd = message.total_cost_usd or 0.0
                    if message.is_error:
                        raise ProviderError(
                            message.result or f"SDK reported error: {message.subtype}"
                        )
        except (sdk.CLINotFoundError, sdk.CLIConnectionError, sdk.ProcessError) as exc:
            log.error("Claude Agent SDK unreachable: %s", exc)
            raise ProviderUnavailable(str(exc)) from exc
        except sdk.CLIJSONDecodeError as exc:
            log.error("Claude Agent SDK returned malformed output: %s", exc)
            raise ProviderUnavailable(str(exc)) from exc

        summary = " ".join(p.strip() for p in summary_parts if p.strip())
        return AgentResponse(
            summary=summary or "No response text.",
            tool_calls=tuple(proposals),
            done=not proposals,
            cost_units=cost_usd,
            model=model,
        )
