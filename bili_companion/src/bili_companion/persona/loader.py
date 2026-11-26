"""Assemble trusted persona instructions separately from bridge memory data."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class PersonaBundle:
    system_instruction: str
    memory_data: str

    @property
    def character_count(self) -> int:
        return len(self.system_instruction) + len(self.memory_data)


class PersonaLoader:
    """Repository defaults; installed applications supply their persona directory.

    The budget counts Unicode characters of these two returned strings, not API
    tokens or the caller's complete prompt. Oversized records are skipped intact.
    This separation is an input boundary, not a claim to solve prompt injection.
    """

    def __init__(self, persona_dir: str | Path | None = None, *,
                 max_characters: int = 6000, memory_characters: int = 1000):
        if max_characters < 2 or memory_characters < 2:
            raise ValueError("Persona and memory budgets must be at least 2 characters")
        self.persona_dir = (Path(persona_dir) if persona_dir is not None else
                            Path(__file__).resolve().parents[3] / "personas")
        self.max_characters = max_characters
        self.memory_characters = memory_characters

    def live_room_note(self, room_id):
        """Only the observation note for this room may enter a media request."""
        path = self.persona_dir.parent / "private" / "persona" / "regular_live_room.json"
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(profile, dict) or profile.get("room_id") != room_id:
            return ""
        note = profile.get("observation_note")
        return note.strip() if isinstance(note, str) and len(note) <= 1000 else ""

    def load(self, bridge_memories: Sequence[Mapping] = ()) -> PersonaBundle:
        core = (self.persona_dir / "hakimi_core.md").read_text(encoding="utf-8").strip()
        overlay = (self.persona_dir / "bilibili_overlay.md").read_text(encoding="utf-8").strip()
        if not core or not overlay:
            raise ValueError("Core persona and platform overlay must both be nonempty")
        instruction = core + "\n\n" + overlay
        remaining = min(self.memory_characters, self.max_characters - len(instruction))
        if remaining < 2:
            raise ValueError("Character budget cannot fit the complete trusted persona")
        selected = []
        seen = set()
        for item in bridge_memories:
            if not isinstance(item, Mapping):
                raise ValueError("Bridge memories must be attributed mappings")
            source = item.get("source")
            if source not in {"qq", "bilibili"} or item.get("is_digest") is not True:
                raise ValueError("Bridge memories require a known source and is_digest=true")
            if not isinstance(item.get("summary"), str) or not item["summary"].strip():
                raise ValueError("Bridge memory summary must be nonempty text")
            if not isinstance(item.get("source_refs"), list) or not isinstance(item.get("coverage"), list):
                raise ValueError("Bridge memories require source_refs and coverage lists")
            if not all(isinstance(ref, str) for ref in item["source_refs"]):
                raise ValueError("source_refs must contain strings")
            # Fixed allowlist: imported payloads cannot introduce tool/system keys.
            record = {key: item[key] for key in (
                "source", "summary", "source_refs", "coverage", "attribution",
                "occurred_at", "packet_id", "source_id", "revision", "is_digest", "truncated"
            ) if key in item}
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if encoded in seen:
                continue
            trial = json.dumps(selected + [record], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            if len(trial) <= remaining:
                selected.append(record)
                seen.add(encoded)
        data = json.dumps(selected, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return PersonaBundle(instruction, data)
