from typing import Protocol
from bili_companion.domain import Observation, Action


class PerceptionModel(Protocol):
    async def observe(self, chunk: dict) -> Observation: ...


class DecisionModel(Protocol):
    async def decide(self, observation: Observation) -> Action: ...
