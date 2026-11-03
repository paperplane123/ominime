"""Strict local structured sources for post-send chat messages."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Iterable

from .chat_bubble_capture import VisualBubbleSource, bubble_profile_for_bundle
from .chat_window_capture import WindowFrame, session_anchors_match
from .kim_composer_capture import (
    NormalizedRect,
    RecognizedLine,
    VisionTextRecognizer,
)
from .post_send_capture import (
    MAX_TRUSTED_SUBMISSION_CHARS,
    SendIntent,
    SourceResult,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AXMessageNode:
    target_pid: int
    role: str
    text: str
    identity: str
    observed_at: float
    newly_observed: bool
    bounds: NormalizedRect
    secure: bool = False
    session_anchor: str | None = None
    window_id: int | None = None
    children: tuple[AXMessageNode, ...] = ()


class AccessibilityBubbleSource:
    def __init__(
        self,
        *,
        node_provider: Callable[[int], Iterable[AXMessageNode]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_nodes: int = 128,
        traversal_timeout: float = 0.15,
    ):
        self._clock = clock
        self._max_nodes = max_nodes
        self._traversal_timeout = traversal_timeout
        self._native_provider = _NativeAXNodeProvider(clock=clock, max_nodes=16)
        self._node_provider = node_provider or self._native_provider
        self._cancelled = threading.Event()

    def read(self, intent: SendIntent) -> SourceResult:
        self._cancelled.clear()
        deadline = self._clock() + self._traversal_timeout
        try:
            roots = tuple(self._node_provider(intent.target_pid))
        except Exception:
            logger.error("post-send AX tree read failed: ax_native_error")
            return SourceResult.unavailable("ax_native_error")
        if not roots:
            return SourceResult.unavailable("ax_tree_unavailable")

        queue = deque(roots)
        failures: list[str] = []
        candidates: list[AXMessageNode] = []
        visited = 0
        while queue:
            if self._cancelled.is_set():
                return SourceResult.unavailable("ax_cancelled")
            if self._clock() > deadline:
                return SourceResult.unavailable("ax_traversal_timeout")
            if visited >= self._max_nodes:
                return SourceResult.unavailable("ax_traversal_limit")
            node = queue.popleft()
            visited += 1
            queue.extend(node.children)
            failure = self._failure_reason(intent, node)
            if failure is None:
                candidates.append(node)
            else:
                failures.append(failure)

        if not candidates:
            return SourceResult.unavailable(
                failures[-1] if failures else "ax_message_unavailable"
            )
        candidate = min(candidates, key=lambda node: node.bounds.y)
        source_prefix = bubble_profile_for_bundle(intent.bundle_id).source_prefix
        return SourceResult.success(
            candidate.text.strip(),
            f"{source_prefix}_ax",
            f"ax:{intent.target_pid}:{candidate.identity}",
            confidence=1.0,
            observed_at=candidate.observed_at,
            target_pid=intent.target_pid,
            stability_key=candidate.identity,
            session_anchor=candidate.session_anchor,
            window_id=candidate.window_id,
        )

    def cancel(self) -> None:
        self._cancelled.set()
        cancel = getattr(self._node_provider, "cancel", None)
        if callable(cancel):
            cancel()

    @staticmethod
    def _failure_reason(
        intent: SendIntent,
        node: AXMessageNode,
    ) -> str | None:
        if node.target_pid != intent.target_pid:
            return "ax_target_pid_mismatch"
        baseline = intent.baseline
        if (
            not isinstance(baseline, WindowFrame)
            or not session_anchors_match(
                baseline.session_anchor,
                node.session_anchor,
            )
        ):
            return "ax_session_anchor_mismatch"
        if node.window_id != baseline.window_id:
            return "ax_window_identity_mismatch"
        if node.secure:
            return "ax_secure_element"
        if node.role != "AXStaticText":
            return "ax_not_static_text"
        if not node.text.strip():
            return "ax_empty_text"
        if len(node.text.strip()) > MAX_TRUSTED_SUBMISSION_CHARS:
            return "ax_content_too_long"
        if not node.identity:
            return "ax_missing_identity"
        if node.observed_at < intent.submitted_at:
            return "ax_stale_node"
        if not node.newly_observed:
            return "ax_not_new"
        if node.bounds.x + node.bounds.width < 0.72:
            return "ax_not_outgoing"
        if intent.validation_text and _normalized_text(node.text) != _normalized_text(
            intent.validation_text
        ):
            return "ax_validation_mismatch"
        return None


class _NativeAXNodeProvider:
    """Fail closed until native AX can supply complete trust evidence.

    The default path therefore trusts only two evidence kinds: per-app tuned AX
    adapters (injected node providers) and the visual OCR gate that requires an
    exact match against the submitted text.  A generic traversal cannot prove
    newness, session ownership, or bubble geometry for arbitrary apps, so it
    stays disabled for every bundle, including generic post-send capture.
    """

    def __init__(self, *, clock: Callable[[], float], max_nodes: int = 128):
        self._clock = clock
        self._max_nodes = max_nodes
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def __call__(self, target_pid: int) -> tuple[AXMessageNode, ...]:
        self._cancelled.clear()
        # Generic macOS AX does not identify a message as newly observed, bind
        # it to our window/session anchor, or expose trustworthy bubble bounds.
        # Reading text without those signals cannot produce a success, so the
        # default provider avoids traversing the private UI tree at all.
        return ()


def _normalized_text(text: str) -> str:
    return "\n".join(
        " ".join(line.split()) for line in text.strip().splitlines()
    )


def default_message_sources(
    *,
    frame_provider: Callable[[int], WindowFrame | None],
    ax_node_provider: Callable[[int], Iterable[AXMessageNode]] | None = None,
    ocr_provider: Callable[
        [object, NormalizedRect], Iterable[RecognizedLine]
    ]
    | None = None,
    difference_provider: Callable[
        [object, object, NormalizedRect], Iterable[NormalizedRect]
    ]
    | None = None,
) -> tuple[AccessibilityBubbleSource, VisualBubbleSource]:
    if ocr_provider is None:
        native_ocr = VisionTextRecognizer()
        native_ocr.prepare()
        ocr_provider = native_ocr
    return (
        AccessibilityBubbleSource(node_provider=ax_node_provider),
        VisualBubbleSource(
            frame_provider=frame_provider,
            ocr_provider=ocr_provider,
            difference_provider=difference_provider,
        ),
    )
