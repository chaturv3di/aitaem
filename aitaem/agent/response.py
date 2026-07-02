from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

from aitaem.agent.trace import RunTrace, Status

PayloadT = TypeVar("PayloadT")


class BotResponse(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(frozen=True)

    status: Status
    narrative: str
    trace: RunTrace
    reason: str | None = None
    payload: PayloadT | None = None
