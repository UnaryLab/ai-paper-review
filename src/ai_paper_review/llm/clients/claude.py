"""ClaudeSDKClient — Anthropic Claude via the Claude Agent Python SDK.

Uses the ``claude-agent-sdk`` Python package (repo:
claude-agent-sdk-python) which talks to Claude Code's locally-installed
CLI. Authentication is inherited from that CLI — run ``claude /login``
once and the SDK picks up the stored credentials.

No API key is needed. The SDK routes through Claude Code's subscription
(Pro/Max/Team) rather than the direct Anthropic API, so you can share
a single login across the CLI, VSCode/JetBrains extensions, and this
provider.

This client wraps the SDK's async ``query()`` generator in a synchronous
``complete()`` method so it plugs into the same pipeline used by the
HTTP-based providers. Each call streams messages until the SDK's
iterator ends, then returns the accumulated assistant text.

When ``pdf_path`` is given, this client uses the Claude Code CLI's
built-in ``Read`` tool to read the PDF file directly — the prompt is
rewritten to tell Claude where the file lives, and the SDK is launched
with ``permission_mode="bypassPermissions"`` + ``add_dirs=[pdf_dir]``
so the read is approved without user interaction. This is the
file-reference pattern documented in the Agent SDK docs; Claude Code's
Read tool natively handles PDFs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("llm_client")


class ClaudeSDKClient:
    """Claude via the official Claude Agent Python SDK (``claude-agent-sdk``)."""

    def __init__(self, model: str = "claude-sonnet-4-5-20250929",
                 api_key: str = "", base_url: Optional[str] = None):
        # Probe at construction time so a missing SDK fails here with a
        # clear message rather than later inside complete(). find_spec
        # avoids actually importing the SDK (no init side effects, no
        # unused-import lint noise) — the real import lives in complete().
        import importlib.util
        if importlib.util.find_spec("claude_agent_sdk") is None:
            raise ImportError(
                "The Claude Agent Python SDK is not installed. "
                "Install it with `pip install claude-agent-sdk` "
                "(note: imports as `from claude_agent_sdk import query`), "
                "then authenticate Claude Code CLI with `claude /login`."
            )
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        """Synchronous wrapper around the async SDK."""
        import asyncio
        return asyncio.run(self._complete_async(system, user, pdf_path))

    async def _complete_async(
        self,
        system: str,
        user: str,
        pdf_path: Optional[str] = None,
    ) -> str:
        """Stream one turn through the Claude Agent SDK and return the
        concatenated assistant text.

        Duck-types the emitted message objects (looking for ``.content``
        lists whose items carry ``.text``) rather than importing
        ``AssistantMessage`` / ``TextBlock`` directly — that way minor
        version bumps of the SDK's class hierarchy don't break us.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions

        options_kwargs: dict = {
            "system_prompt": system,
            "model": self.model,
        }
        if pdf_path:
            abs_pdf = Path(pdf_path).resolve()
            # Claude Code's Read tool handles PDFs natively. Grant it on
            # the PDF's parent dir and pre-approve the tool use so the
            # SDK doesn't block on an interactive permission prompt.
            options_kwargs["allowed_tools"] = ["Read"]
            options_kwargs["permission_mode"] = "bypassPermissions"
            options_kwargs["add_dirs"] = [str(abs_pdf.parent)]
            prompt = (
                f"{user}\n\n"
                f"The paper PDF is at: {abs_pdf}\n"
                f"Use the Read tool to read the full PDF, then produce "
                f"your review strictly in the markdown format the system "
                f"prompt requires."
            )
        else:
            prompt = user

        # The SDK's JSON transport buffer defaults to 1 MB. Large PDF tool
        # results or long responses exceed that limit and raise a buffer
        # overflow error. Raise the ceiling to 32 MB — enough for any
        # realistic review response or PDF read-tool result.
        options_kwargs["max_buffer_size"] = 32 * 1024 * 1024

        options = ClaudeAgentOptions(**options_kwargs)

        chunks: list[str] = []
        seen_message_types: list[str] = []

        try:
            async for message in query(prompt=prompt, options=options):
                seen_message_types.append(type(message).__name__)
                content = getattr(message, "content", None)
                if content is None:
                    continue
                try:
                    iter(content)
                except TypeError:
                    continue
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        chunks.append(text)
        except Exception as e:
            # Wrap with a diagnostic that points at the usual fix.
            raise RuntimeError(
                f"Claude Agent SDK call failed: {type(e).__name__}: {e}. "
                f"Verify with `claude /status` that the CLI is authenticated, "
                f"and that your Claude subscription covers the selected model "
                f"({self.model!r})."
            ) from e

        output = "".join(chunks)

        if not output:
            raise RuntimeError(
                "Claude Agent SDK returned empty response (no text blocks "
                f"from {len(seen_message_types)} message(s): "
                f"{seen_message_types}). Likely causes: CLI not "
                "authenticated, subscription issue, or the model rejected "
                "the prompt. Verify with `claude /status`."
            )

        logger.debug("ClaudeSDK: got %d chars from %d messages",
                     len(output), len(seen_message_types))
        return output
