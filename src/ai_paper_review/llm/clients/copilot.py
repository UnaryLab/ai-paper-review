"""CopilotSDKClient — GitHub Copilot via the official Python SDK.

Uses the ``copilot`` Python package from ``pip install github-copilot-sdk``,
which talks to the Copilot CLI (bundled with the Python SDK) over JSON-RPC.
Authentication is inherited from Copilot CLI — share creds with VSCode
Copilot or run ``copilot auth login`` once.

No API key is needed. The ``model`` parameter is informational only; the
SDK uses whichever model Copilot CLI is currently configured with.

This client wraps the SDK's async streaming API in a synchronous
``complete()`` method so it plugs into the same pipeline used by the
HTTP-based providers. Each call creates a fresh ``CopilotClient`` and
session, waits for the ``session.idle`` event, and returns the
accumulated streamed content.

The :func:`_aclose_best_effort` helper is here (rather than in a shared
utils module) because it's only used by this client's teardown path —
colocating it keeps the copilot-specific complexity in one file.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("llm_client")


async def _aclose_best_effort(
    obj: Any,
    *,
    label: str,
    timeout: float = 10.0,
    method_names: tuple = ("close", "stop", "__aexit__"),
) -> None:
    """Close/stop an async resource by trying a sequence of method names.

    Used to guarantee teardown of Copilot SDK objects (session, client) even
    when the SDK's concrete API varies across versions or when normal
    control flow has been interrupted. Never raises — cleanup failures are
    logged so they're visible, but they must not mask the primary result
    of the surrounding try block.
    """
    import asyncio
    for name in method_names:
        method = getattr(obj, name, None)
        if method is None or not callable(method):
            continue
        try:
            # __aexit__ requires (exc_type, exc_val, tb); others are nullary.
            result = method(None, None, None) if name == "__aexit__" else method()
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=timeout)
            logger.debug("CopilotSDK: %s.%s() completed", label, name)
            return
        except asyncio.TimeoutError:
            logger.warning("CopilotSDK: %s.%s() timed out after %.1fs — "
                           "subprocess may leak", label, name, timeout)
            return
        except Exception as e:
            logger.debug("CopilotSDK: %s.%s() raised %s, trying next method",
                         label, name, type(e).__name__)
            continue
    logger.debug("CopilotSDK: no working close method found on %s "
                 "(tried %s)", label, method_names)


class CopilotSDKClient:
    """GitHub Copilot via the official Python SDK (``github-copilot-sdk``)."""

    def __init__(self, model: str = "copilot-vscode", api_key: str = "",
                 base_url: Optional[str] = None):
        # Lazy import — only required when this provider is actually selected.
        try:
            from copilot import CopilotClient as _SDKClient  # noqa: F401
            from copilot.session import PermissionHandler as _Perm  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "The GitHub Copilot Python SDK is not installed. "
                "Install it with `pip install github-copilot-sdk` "
                "(note: imports as `from copilot import CopilotClient`), "
                "then authenticate with GitHub Copilot CLI."
            ) from e
        self._SDKClient = _SDKClient
        self._PermissionHandler = _Perm
        self.model = model

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 4000,
        pdf_path: Optional[str] = None,
    ) -> str:
        """Synchronous wrapper around the async SDK.

        The SDK has no separate system-message field, so we prepend the
        system prompt to the user message with a clear separator. The
        ``pdf_path`` argument is accepted but ignored — Copilot CLI
        doesn't take binary PDFs, so the caller should have already
        passed text extracted via ``extract_pdf_markdown`` in ``user``.
        """
        import asyncio
        combined = f"{system}\n\n---\n\n{user}"
        return asyncio.run(self._complete_async(combined))

    async def _complete_async(self, prompt: str) -> str:
        """Run one turn through a fresh Copilot session and return the full reply.

        Handles more than just ``assistant.message_delta`` / ``session.idle`` —
        any event carrying an "error" in its type is captured and re-raised
        with context, and all unfamiliar event types are debug-logged so
        empty-output bugs can be diagnosed instead of silently returning "".

        Uses explicit try/finally cleanup rather than nested ``async with``
        because the paper review pipeline runs multiple reviewers in
        parallel (user-configurable per run, up to MAX_N_REVIEWERS),
        each spawning its own Copilot CLI subprocess. If a
        context-manager exit is skipped (exception during setup, event
        loop interruption, etc.), those subprocesses can leak and
        accumulate. The explicit teardown below runs unconditionally and
        logs any cleanup failures so leaks are visible rather than silent.
        """
        import asyncio

        client = self._SDKClient()
        session = None
        try:
            # Prefer the documented `start()` method; fall back to
            # __aenter__ for SDK builds that only expose the async-CM API.
            if hasattr(client, "start") and callable(client.start):
                res = client.start()
                if asyncio.iscoroutine(res):
                    await res
            elif hasattr(client, "__aenter__"):
                await client.__aenter__()

            session = await client.create_session(
                on_permission_request=self._PermissionHandler.approve_all,
                streaming=True,
            )

            done = asyncio.Event()
            chunks: list[str] = []
            seen_event_types: list[str] = []
            error_details: list[str] = []

            def on_event(event):
                try:
                    etype = event.type.value
                except AttributeError:
                    etype = str(getattr(event, "type", "unknown"))
                seen_event_types.append(etype)

                if etype == "assistant.message_delta":
                    try:
                        chunks.append(event.data.delta_content or "")
                    except AttributeError:
                        logger.debug("message_delta missing delta_content: %r", event)
                elif etype == "session.idle":
                    done.set()
                elif "error" in etype.lower() or "fail" in etype.lower():
                    err_msg = getattr(getattr(event, "data", None), "message", "") \
                              or getattr(getattr(event, "data", None), "error", "") \
                              or repr(getattr(event, "data", None))
                    error_details.append(f"{etype}: {err_msg}")
                    done.set()
                else:
                    logger.debug("CopilotSDK event (unhandled): type=%s data=%r",
                                 etype, getattr(event, "data", None))

            session.on(on_event)
            await session.send(prompt)

            # Guard against the session hanging forever if the SDK fails
            # to emit session.idle.
            try:
                await asyncio.wait_for(done.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"CopilotSDK: no session.idle after 300s. "
                    f"Events seen: {seen_event_types}. Chunks: {len(chunks)}. "
                    f"Check `gh auth status`, Copilot subscription, and premium quota."
                )

            output = "".join(chunks)

            if error_details:
                raise RuntimeError(
                    f"CopilotSDK reported error event(s): {'; '.join(error_details)}. "
                    f"Events seen: {seen_event_types}."
                )

            if not output:
                raise RuntimeError(
                    f"CopilotSDK returned empty response (no assistant.message_delta "
                    f"events before session.idle). Events seen: {seen_event_types}. "
                    f"Likely causes: premium request quota exhausted, Copilot "
                    f"subscription expired, or org policy disallows SDK access. "
                    f"Verify with `gh auth status` and check your Copilot quota."
                )

            logger.debug("CopilotSDK: got %d chars from %d deltas — tearing down session",
                         len(output), sum(1 for e in seen_event_types if e == "assistant.message_delta"))
            return output

        finally:
            # Release session BEFORE stopping the client so the CLI drops
            # per-session state cleanly. Each step is wrapped in
            # wait_for so a hung teardown can't deadlock the reviewer
            # thread, and errors are logged not raised — cleanup must
            # never mask the primary result.
            if session is not None:
                await _aclose_best_effort(
                    session, label="session", timeout=10.0,
                    method_names=("close", "__aexit__"),
                )
            await _aclose_best_effort(
                client, label="client", timeout=10.0,
                method_names=("stop", "close", "__aexit__"),
            )
