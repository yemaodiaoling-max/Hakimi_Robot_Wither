from dataclasses import dataclass, field
from typing import Literal
import math


@dataclass(frozen=True)
class Scope:
    account_id: str
    kind: Literal["video", "live_room", "private"]
    identity: str
    platform: str = "bilibili"

    def __post_init__(self):
        if not self.account_id or not self.identity or self.kind not in {"video", "live_room", "private"}:
            raise ValueError("invalid scope")

    def key(self):
        import json
        return json.dumps([self.platform, self.account_id, self.kind, self.identity], separators=(",", ":"))


@dataclass(frozen=True)
class Evidence:
    id: str
    scope: Scope
    session_id: str
    text: str
    observed_at: float  # 原事件UTC时间，不能使用模型完成时间替代。
    media_start: float
    media_end: float
    modality: str
    attribution: Literal["observation", "hearsay", "inference", "feeling"]
    confidence: float = 1.0
    version: str = "1"
    public: bool = False

    def __post_init__(self):
        if (not all(math.isfinite(x) for x in (self.observed_at, self.media_start, self.media_end, self.confidence))
                or not self.id or not self.session_id or not self.text.strip()
                or self.media_start < 0 or self.media_end < self.media_start
                or not 0 <= self.confidence <= 1
                or self.attribution not in {"observation", "hearsay", "inference", "feeling"}
                or self.modality not in {"audio", "video", "subtitle", "danmaku", "metadata", "mixed"}):
            raise ValueError("invalid evidence")


@dataclass(frozen=True)
class Candidate:
    room_id: str
    session_id: str
    text: str
    evidence_ids: tuple[str, ...]
    expires_at: float
    dedup_key: str
    watch_epoch: int

    def __post_init__(self):
        if not math.isfinite(self.expires_at) or self.watch_epoch < 0:
            raise ValueError('invalid candidate time or epoch')


@dataclass(frozen=True)
class Observation:
    evidence: tuple[Evidence, ...]
    missing_modalities: tuple[str, ...] = ()
    coverage: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True)
class Action:
    kind: Literal["watch", "skip", "candidate", "quiet"]
    payload: dict = field(default_factory=dict)
