"""ClaudeSDKClient — Anthropic Claude via the Claude Agent Python SDK.

Uses the ``claude-agent-sdk`` Python package (repo:
claude-agent-sdk-python) which talks to Claude Code's locally-installed
CLI. Authentication is inherited from that CLI: run ``claude auth login``
once and the SDK picks up the stored credentials.

No API key is needed. The SDK routes through Claude Code's subscription
(Pro/Max/Team) rather than the direct Anthropic API, so you can share
a single login across the CLI, VSCode/JetBrains extensions, and this
provider.

This client wraps the SDK's async ``query()`` generator in a synchronous
``complete()`` method so it plugs into the same pipeline used by the
HTTP-based providers. Each call streams messages until the SDK's
iterator ends, then returns the assistant text written after the last
tool call.

Each call runs in a fresh temporary directory, removed afterwards, with
no session transcript saved. A text call gets no tools. When
``pdf_path`` is given, the PDF is copied into that directory and the
CLI gets only its built-in ``Read`` tool, which handles PDFs natively.
With ``permission_mode="dontAsk"``, reads inside the directory are
approved and every other tool use is denied without a prompt.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import DEFAULT_MODEL
from .base import FatalLLMError

logger = logging.getLogger("llm_client")

# Merged into the CLI subprocess env. An exported ANTHROPIC_API_KEY or
# ANTHROPIC_AUTH_TOKEN takes priority over the subscription login in the
# CLI; an empty value makes the CLI treat it as unset. os.environ is left
# untouched so anthropic_api in the same process still sees the key.
CLI_ENV_OVERRIDES = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}

# AssistantMessage.error values that no retry can fix.
_FATAL_MESSAGE_ERRORS = ("authentication_failed", "billing_error")
# AssistantMessage.error values that RetryClient retries.
_RETRYABLE_MESSAGE_ERRORS = ("rate_limit", "server_error")


class ClaudeSDKClient:
    """Claude via the official Claude Agent Python SDK (``claude-agent-sdk``)."""

    def __init__(self, model: str = DEFAULT_MODEL,
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
                "then authenticate Claude Code CLI with `claude auth login`."
            )
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        """Synchronous wrapper around the async SDK.

        ``max_tokens`` is not enforced: the Agent SDK has no output-token
        limit option, so the CLI's own default applies.
        """
        with tempfile.TemporaryDirectory() as work_dir:
            return asyncio.run(self._complete_async(
                system, user, pdf_path, Path(work_dir).resolve()))

    async def _complete_async(
        self,
        system: str,
        user: str,
        pdf_path: Optional[str],
        work_dir: Path,
    ) -> str:
        """Stream one turn through the Claude Agent SDK and return the
        assistant text written after the last tool call.

        Duck-types the emitted message objects (looking for ``.content``
        lists whose items carry ``.text``) rather than importing
        ``AssistantMessage`` / ``TextBlock`` directly — that way minor
        version bumps of the SDK's class hierarchy don't break us.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, CLINotFoundError

        options_kwargs: dict = {
            "system_prompt": system,
            "model": self.model,
            "env": CLI_ENV_OVERRIDES,
            # Run outside the user's repo and load no settings files, so
            # no CLAUDE.md, hooks, or project settings reach the review.
            "cwd": str(work_dir),
            "setting_sources": [],
            "tools": [],
            "max_turns": 1,
            # Keep the paper out of ~/.claude/projects.
            "extra_args": {"no-session-persistence": None},
        }
        if pdf_path:
            pdf_copy = work_dir / "paper.pdf"
            shutil.copyfile(pdf_path, pdf_copy)
            # Read is the only tool. In dontAsk mode the CLI approves
            # reads inside the working directories and denies every
            # other request instead of prompting.
            options_kwargs["tools"] = ["Read"]
            options_kwargs["permission_mode"] = "dontAsk"
            options_kwargs["add_dirs"] = [str(work_dir)]
            # Read returns at most 20 PDF pages per call, so a long paper
            # takes several Read turns before the final answer.
            options_kwargs["max_turns"] = 10
            prompt = (
                f"{user}\n\n"
                f"The paper PDF is at: {pdf_copy}\n"
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
                # Detect hard rate-limit rejection before falling through to
                # the empty-response error, which gives a misleading message.
                rate_info = getattr(message, "rate_limit_info", None)
                if rate_info is not None:
                    status = getattr(rate_info, "status", None)
                    if status == "rejected":
                        limit_type = getattr(rate_info, "rate_limit_type", "?")
                        resets_at = getattr(rate_info, "resets_at", None)
                        when = (
                            datetime.fromtimestamp(resets_at).astimezone()
                            .strftime("%Y-%m-%d %H:%M %Z")
                            if resets_at else "unknown"
                        )
                        if getattr(rate_info, "overage_status", None) == "allowed":
                            # The call continues on overage usage.
                            logger.warning(
                                "ClaudeSDK: rate limit reached (type=%s), "
                                "continuing on overage usage.", limit_type)
                        # A limit that resets within 5 minutes is waited out
                        # here, then retried; a later or unknown reset stops
                        # the run.
                        elif resets_at and resets_at - time.time() <= 300:
                            await asyncio.sleep(
                                min(max(0, resets_at - time.time()) + 1, 300))
                            raise RuntimeError(
                                f"Claude subscription rate limit hit "
                                f"(type={limit_type}); resets at {when}."
                            )
                        else:
                            raise FatalLLMError(
                                "Claude subscription usage limit reached "
                                f"(type={limit_type}); "
                                f"resets at {when}. Wait until then, or switch this "
                                "stage to another provider."
                            )
                    if status == "allowed_warning":
                        logger.warning(
                            "ClaudeSDK: rate limit warning "
                            "(type=%s) — approaching limit.",
                            getattr(rate_info, "rate_limit_type", "?"),
                        )
                error = getattr(message, "error", None)
                if error in _FATAL_MESSAGE_ERRORS:
                    raise FatalLLMError(
                        f"Claude Code CLI reported {error}. Check the login "
                        "with `claude auth status`, or sign in with "
                        "`claude auth login`."
                    )
                if error in _RETRYABLE_MESSAGE_ERRORS:
                    # Worded so RetryClient matches it as a rate limit.
                    raise RuntimeError(
                        f"Claude Code CLI reported {error}; "
                        "retrying as a rate limit."
                    )
                if error:
                    raise RuntimeError(f"Claude Code CLI reported {error}.")
                # Only ResultMessage has is_error.
                if getattr(message, "is_error", False):
                    detail = (getattr(message, "errors", None)
                              or getattr(message, "result", None) or "")
                    status = getattr(message, "api_error_status", None)
                    if isinstance(status, int) and (status == 429 or status >= 500):
                        # Worded so RetryClient matches it as a rate limit.
                        raise RuntimeError(
                            f"Claude Code CLI API error (HTTP {status}): "
                            f"{detail}; retrying as a rate limit."
                        )
                    raise RuntimeError(
                        "Claude Code CLI run failed "
                        f"({getattr(message, 'subtype', '?')}): {detail}"
                    )
                content = getattr(message, "content", None)
                if content is None:
                    continue
                try:
                    iter(content)
                except TypeError:
                    continue
                for block in content:
                    # A tool-use block (it has .input) ends a turn, so
                    # text before it is not part of the final answer.
                    if hasattr(block, "input"):
                        chunks.clear()
                    text = getattr(block, "text", None)
                    if isinstance(text, str) and text:
                        chunks.append(text)
        except RuntimeError:
            # FatalLLMError and the errors raised above; SDK errors are
            # not RuntimeError and get wrapped below.
            raise
        except CLINotFoundError as e:
            raise FatalLLMError(
                f"Claude Code CLI not found ({e}). Install Claude Code, "
                "then sign in with `claude auth login`."
            ) from e
        except Exception as e:
            # Wrap with a diagnostic that points at the usual fix.
            raise RuntimeError(
                f"Claude Agent SDK call failed: {type(e).__name__}: {e}. "
                f"Verify with `claude auth status` that the CLI is authenticated, "
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
                "the prompt. Verify with `claude auth status`."
            )

        logger.debug("ClaudeSDK: got %d chars from %d messages",
                     len(output), len(seen_message_types))
        return output
