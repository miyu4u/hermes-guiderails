"""Delivery-independent announcement contracts."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Announcement:
    event_id: str
    session_id: str
    message: str
    surface: str


class IAnnounceChannel(Protocol):
    def supports(self, announcement: Announcement) -> bool:
        """Check capability without sending or otherwise mutating state."""
        ...

    def announce(self, announcement: Announcement) -> None:
        """Attempt delivery once; an exception does not imply non-delivery."""
        ...
