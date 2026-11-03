"""Local Vision recognition of a newly added outgoing chat bubble."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import logging
import time
from typing import Callable, Iterable

from .chat_window_capture import WindowFrame, session_anchors_match
from .kim_composer_capture import (
    KIM_CHAT_COMPOSER_FRACTION,
    KIM_CHAT_HEADER_FRACTION,
    KIM_CHAT_RIGHT_MARGIN_FRACTION,
    KIM_CHAT_SIDEBAR_FRACTION,
    LEGACY_KIM_BUNDLE_ID,
    NormalizedRect,
    RecognizedLine,
    VisionTextRecognizer,
    recognized_content_lines,
)
from .post_send_capture import (
    MAX_TRUSTED_SUBMISSION_CHARS,
    SendIntent,
    SourceResult,
)
from .wechat_composer_capture import (
    WECHAT_BUNDLE_ID,
    WECHAT_CHAT_HEADER_POINTS,
    WECHAT_COMPOSER_LEFT_POINTS,
    WECHAT_COMPOSER_RIGHT_POINTS,
    WECHAT_COMPOSER_TOP_POINTS,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BubbleProfile:
    name: str
    source_prefix: str
    bounds_provider: Callable[[WindowFrame], NormalizedRect]
    outgoing_right_threshold: float = 0.72

    def search_bounds(self, frame: WindowFrame) -> NormalizedRect:
        return self.bounds_provider(frame)


@dataclass(frozen=True)
class BubbleCandidate:
    text: str
    bounds: NormalizedRect
    changed_fraction: float
    outgoing_score: float


def _kim_bounds(frame: WindowFrame) -> NormalizedRect:
    return NormalizedRect(
        KIM_CHAT_SIDEBAR_FRACTION,
        KIM_CHAT_COMPOSER_FRACTION,
        1.0 - KIM_CHAT_SIDEBAR_FRACTION - KIM_CHAT_RIGHT_MARGIN_FRACTION,
        1.0 - KIM_CHAT_COMPOSER_FRACTION - KIM_CHAT_HEADER_FRACTION,
    )


def _wechat_bounds(frame: WindowFrame) -> NormalizedRect:
    return NormalizedRect(
        WECHAT_COMPOSER_LEFT_POINTS / frame.width,
        WECHAT_COMPOSER_TOP_POINTS / frame.height,
        (
            frame.width
            - WECHAT_COMPOSER_LEFT_POINTS
            - WECHAT_COMPOSER_RIGHT_POINTS
        )
        / frame.width,
        (
            frame.height
            - WECHAT_COMPOSER_TOP_POINTS
            - WECHAT_CHAT_HEADER_POINTS
        )
        / frame.height,
    )


KIM_BUBBLE_PROFILE = BubbleProfile("kim", "kim_postsend", _kim_bounds)
WECHAT_BUBBLE_PROFILE = BubbleProfile(
    "wechat", "wechat_postsend", _wechat_bounds
)

# Conservative generic geometry: keep the whole window width (left-side panels
# are filtered by the outgoing-right heuristic) and exclude a header band on
# top plus a composer band at the bottom.
GENERIC_HEADER_FRACTION = 0.04
GENERIC_COMPOSER_FRACTION = 0.18


def _generic_bounds(frame: WindowFrame) -> NormalizedRect:
    return NormalizedRect(
        0.0,
        GENERIC_COMPOSER_FRACTION,
        1.0,
        1.0 - GENERIC_COMPOSER_FRACTION - GENERIC_HEADER_FRACTION,
    )


GENERIC_BUBBLE_PROFILE = BubbleProfile(
    "generic", "generic_postsend", _generic_bounds
)

_BUBBLE_PROFILES = {
    WECHAT_BUNDLE_ID: WECHAT_BUBBLE_PROFILE,
    "com.tencent.WeChat": WECHAT_BUBBLE_PROFILE,
    LEGACY_KIM_BUNDLE_ID: KIM_BUBBLE_PROFILE,
    "com.electron.kim": KIM_BUBBLE_PROFILE,
    "Kim": KIM_BUBBLE_PROFILE,
}


def bubble_profile_for_bundle(bundle_id: str) -> BubbleProfile:
    """Route a bundle identifier to its tuned profile, or the generic one."""
    return _BUBBLE_PROFILES.get(bundle_id or "", GENERIC_BUBBLE_PROFILE)


class VisualBubbleSource:
    def __init__(
        self,
        *,
        frame_provider: Callable[[int], WindowFrame | None],
        ocr_provider: Callable[
            [object, NormalizedRect], Iterable[RecognizedLine]
        ]
        | None = None,
        difference_provider: Callable[
            [object, object, NormalizedRect], Iterable[NormalizedRect]
        ]
        | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._frame_provider = frame_provider
        self._ocr_provider = ocr_provider or VisionTextRecognizer()
        self._difference_provider = difference_provider or native_changed_regions
        self._clock = clock

    def cancel(self) -> None:
        for provider in (self._ocr_provider, self._difference_provider):
            cancel = getattr(provider, "cancel", None)
            if not callable(cancel):
                continue
            try:
                cancel()
            except Exception:
                logger.error(
                    "post-send visual provider cancellation failed: %s",
                    type(provider).__name__,
                )

    def read(self, intent: SendIntent) -> SourceResult:
        baseline = intent.baseline
        if not isinstance(baseline, WindowFrame) or baseline.image is None:
            return SourceResult.unavailable("baseline_unavailable")
        try:
            current = self._frame_provider(intent.target_pid)
            if current is None or current.image is None:
                return SourceResult.unavailable("post_send_frame_unavailable")
            if current.target_pid != intent.target_pid:
                return SourceResult.unavailable("target_pid_mismatch")
            window_recreated = baseline.window_id != current.window_id
            if (
                baseline.target_pid != intent.target_pid
                or baseline.width != current.width
                or baseline.height != current.height
                or (window_recreated and intent.bundle_id != WECHAT_BUNDLE_ID)
            ):
                return SourceResult.unavailable("window_identity_mismatch")
            if (
                not session_anchors_match(
                    baseline.session_anchor,
                    current.session_anchor,
                )
            ):
                return SourceResult.unavailable("session_anchor_mismatch")

            profile = self._profile(intent)
            search_bounds = profile.search_bounds(current)
            changed_regions = tuple(
                self._difference_provider(
                    baseline.image,
                    current.image,
                    search_bounds,
                )
            )
            lines = tuple(self._ocr_provider(current.image, search_bounds))
            result = self._result_for_lines(
                intent,
                current,
                profile,
                search_bounds,
                lines,
                changed_regions,
            )
            if window_recreated and not result.failure_reason:
                return replace(result, window_id=baseline.window_id)
            return result
        except Exception:
            logger.error("post-send visual bubble capture failed: ocr_native_error")
            return SourceResult.unavailable("ocr_native_error")

    @staticmethod
    def _profile(intent: SendIntent) -> BubbleProfile:
        return bubble_profile_for_bundle(intent.bundle_id)

    def _result_for_lines(
        self,
        intent: SendIntent,
        frame: WindowFrame,
        profile: BubbleProfile,
        search_bounds: NormalizedRect,
        lines: tuple[RecognizedLine, ...],
        changed_regions: tuple[NormalizedRect, ...],
    ) -> SourceResult:
        content_lines = tuple(
            line
            for line in recognized_content_lines(lines)
            if _center_inside(line, search_bounds)
        )
        outgoing_lines = tuple(
            line
            for line in content_lines
            if _is_outgoing(line, search_bounds, profile)
        )
        if not outgoing_lines:
            return SourceResult.unavailable("ocr_no_outgoing_bubble")

        changed_lines = tuple(
            line
            for line in outgoing_lines
            if _line_changed_fraction(line, changed_regions) >= 0.20
        )
        candidates = tuple(
            self._candidate(group, search_bounds, changed_regions)
            for group in _group_lines(changed_lines)
        )
        changed_candidates = tuple(
            candidate for candidate in candidates if candidate.changed_fraction > 0
        )
        if not changed_candidates:
            return SourceResult.unavailable("ocr_no_new_outgoing_bubble")
        candidate = min(changed_candidates, key=lambda item: item.bounds.y)

        if _edge_clipped(candidate.bounds, search_bounds):
            return SourceResult.unavailable("ocr_edge_clipped")
        rows = tuple(row.strip().casefold() for row in candidate.text.splitlines())
        if any(rows.count(row) >= 3 for row in set(rows)):
            return SourceResult.unavailable("ocr_repeated_text_untrusted")
        if len(candidate.text) > MAX_TRUSTED_SUBMISSION_CHARS:
            return SourceResult.unavailable("ocr_content_too_long")
        validation_text = _normalized_validation_text(intent.validation_text)
        candidate_text = _normalized_validation_text(candidate.text)
        if not validation_text:
            return SourceResult.unavailable("ocr_validation_unavailable")
        if candidate_text != validation_text:
            return SourceResult.unavailable("ocr_validation_mismatch")

        observed_at = self._clock()
        geometry = _geometry_fingerprint(frame.window_id, candidate.bounds)
        identity = (
            f"{intent.intent_id}:{intent.target_pid}:{frame.window_id}:"
            f"{geometry}:{int(observed_at * 1000)}"
        )
        return SourceResult.success(
            candidate.text,
            f"{profile.source_prefix}_ocr",
            identity,
            confidence=min(candidate.outgoing_score, candidate.changed_fraction),
            observed_at=observed_at,
            target_pid=intent.target_pid,
            stability_key=geometry,
            session_anchor=frame.session_anchor,
            window_id=frame.window_id,
        )

    @staticmethod
    def _candidate(
        lines: tuple[RecognizedLine, ...],
        search_bounds: NormalizedRect,
        changed_regions: tuple[NormalizedRect, ...],
    ) -> BubbleCandidate:
        ordered = sorted(lines, key=lambda line: (-(line.y + line.height), line.x))
        text = "\n".join(line.text.strip() for line in ordered).strip()
        bounds = _union_line_bounds(lines)
        changed_area = sum(
            _intersection_area(bounds, region) for region in changed_regions
        )
        area = max(bounds.width * bounds.height, 0.000001)
        outgoing = sum(
            _outgoing_score(line, search_bounds) for line in lines
        ) / len(lines)
        return BubbleCandidate(
            text=text,
            bounds=bounds,
            changed_fraction=min(1.0, changed_area / area),
            outgoing_score=outgoing,
        )


def _group_lines(
    lines: tuple[RecognizedLine, ...],
) -> tuple[tuple[RecognizedLine, ...], ...]:
    ordered = sorted(lines, key=lambda line: -(line.y + line.height / 2))
    groups: list[list[RecognizedLine]] = []
    for line in ordered:
        if not groups:
            groups.append([line])
            continue
        previous = groups[-1][-1]
        vertical_gap = previous.y - (line.y + line.height)
        right_delta = abs(
            (previous.x + previous.width) - (line.x + line.width)
        )
        if vertical_gap <= max(previous.height, line.height) * 1.5 and right_delta <= 0.08:
            groups[-1].append(line)
        else:
            groups.append([line])
    return tuple(tuple(group) for group in groups)


def _center_inside(line: RecognizedLine, bounds: NormalizedRect) -> bool:
    center_x = line.x + line.width / 2
    center_y = line.y + line.height / 2
    return (
        bounds.x <= center_x <= bounds.x + bounds.width
        and bounds.y <= center_y <= bounds.y + bounds.height
    )


def _outgoing_score(line: RecognizedLine, bounds: NormalizedRect) -> float:
    return ((line.x + line.width) - bounds.x) / max(bounds.width, 0.000001)


def _is_outgoing(
    line: RecognizedLine,
    bounds: NormalizedRect,
    profile: BubbleProfile,
) -> bool:
    relative_center = (
        (line.x + line.width / 2) - bounds.x
    ) / max(bounds.width, 0.000001)
    return (
        _outgoing_score(line, bounds) >= profile.outgoing_right_threshold
        and relative_center >= 0.60
    )


def _line_changed_fraction(
    line: RecognizedLine,
    changed_regions: tuple[NormalizedRect, ...],
) -> float:
    bounds = NormalizedRect(line.x, line.y, line.width, line.height)
    area = max(line.width * line.height, 0.000001)
    return min(
        1.0,
        sum(_intersection_area(bounds, region) for region in changed_regions)
        / area,
    )


def _normalized_validation_text(text: str) -> str:
    return "\n".join(
        " ".join(line.split()) for line in text.strip().splitlines()
    )


def _union_line_bounds(lines: tuple[RecognizedLine, ...]) -> NormalizedRect:
    left = min(line.x for line in lines)
    bottom = min(line.y for line in lines)
    right = max(line.x + line.width for line in lines)
    top = max(line.y + line.height for line in lines)
    return NormalizedRect(left, bottom, right - left, top - bottom)


def _intersection_area(first: NormalizedRect, second: NormalizedRect) -> float:
    left = max(first.x, second.x)
    bottom = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    top = min(first.y + first.height, second.y + second.height)
    return max(0.0, right - left) * max(0.0, top - bottom)


def _edge_clipped(candidate: NormalizedRect, search: NormalizedRect) -> bool:
    margin = 0.003
    return (
        candidate.x <= search.x + margin
        or candidate.y <= search.y + margin
        or candidate.x + candidate.width >= search.x + search.width - margin
        or candidate.y + candidate.height >= search.y + search.height - margin
    )


def _geometry_fingerprint(window_id: int, bounds: NormalizedRect) -> str:
    quantized = ":".join(
        str(round(value, 3))
        for value in (bounds.x, bounds.y, bounds.width, bounds.height)
    )
    return hashlib.sha256(f"{window_id}:{quantized}".encode()).hexdigest()[:16]


def native_changed_regions(
    before,
    after,
    bounds: NormalizedRect,
) -> tuple[NormalizedRect, ...]:
    """Return one coarse changed region from in-memory CGImage pixels."""
    import Quartz

    width = int(Quartz.CGImageGetWidth(after))
    height = int(Quartz.CGImageGetHeight(after))
    if (
        width <= 0
        or height <= 0
        or int(Quartz.CGImageGetWidth(before)) != width
        or int(Quartz.CGImageGetHeight(before)) != height
    ):
        return ()
    before_data = bytes(
        Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(before))
    )
    after_data = bytes(
        Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(after))
    )
    bytes_per_row = int(Quartz.CGImageGetBytesPerRow(after))
    bytes_per_pixel = max(1, int(Quartz.CGImageGetBitsPerPixel(after)) // 8)
    step = max(1, min(width, height) // 240)
    x0 = max(0, int(bounds.x * width))
    x1 = min(width, int((bounds.x + bounds.width) * width))
    top = max(0, int((1.0 - bounds.y - bounds.height) * height))
    bottom = min(height, int((1.0 - bounds.y) * height))
    changed_x: list[int] = []
    changed_y: list[int] = []
    for pixel_y in range(top, bottom, step):
        row_offset = pixel_y * bytes_per_row
        for pixel_x in range(x0, x1, step):
            offset = row_offset + pixel_x * bytes_per_pixel
            old = before_data[offset : offset + min(3, bytes_per_pixel)]
            new = after_data[offset : offset + min(3, bytes_per_pixel)]
            if len(old) != len(new) or sum(abs(a - b) for a, b in zip(old, new)) > 60:
                changed_x.append(pixel_x)
                changed_y.append(pixel_y)
    if len(changed_x) < 3:
        return ()
    left = min(changed_x) / width
    right = min(1.0, (max(changed_x) + step) / width)
    normalized_bottom = max(0.0, 1.0 - (max(changed_y) + step) / height)
    normalized_top = min(1.0, 1.0 - min(changed_y) / height)
    return (
        NormalizedRect(
            left,
            normalized_bottom,
            right - left,
            normalized_top - normalized_bottom,
        ),
    )
