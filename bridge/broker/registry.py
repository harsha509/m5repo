"""The in-flight cards, and the parking spot for the hook requests waiting on them.

This is what replaces the firmware's single `pendingId` + one binary semaphore:
any number of cards can be open at once, each resolved by its own id, so two
sessions asking at the same time no longer serialize behind one another.
"""

import logging
import threading
import time
import uuid

log = logging.getLogger("broker.registry")


class Card:
    """One question on the device. Rendered per-device at poll time, because
    each board has a different column count."""

    def __init__(self, payload_for, ttl: float, card_id: str = ""):
        self.id = card_id or uuid.uuid4().hex[:16]
        self.payload_for = payload_for          # cols -> dict
        self.created = time.monotonic()
        self.ttl = ttl
        self.verdict = None
        self.resolved = threading.Event()
        self.delivered = set()                  # device ids that have seen it

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created > self.ttl

    def remaining(self) -> float:
        return max(0.0, self.ttl - (time.monotonic() - self.created))


class Registry:
    def __init__(self):
        self._cards = {}
        self._cv = threading.Condition()

    def open(self, card: Card) -> Card:
        with self._cv:
            self._cards[card.id] = card
            self._cv.notify_all()
        log.debug("card %s opened", card.id)
        return card

    def close(self, card_id: str) -> None:
        with self._cv:
            self._cards.pop(card_id, None)
            self._cv.notify_all()

    def resolve(self, card_id: str, verdict: dict) -> bool:
        """Returns False when the id is unknown — a stale device answering a
        card that already timed out must not disturb a live one."""
        with self._cv:
            card = self._cards.get(card_id)
            if card is None or card.resolved.is_set():
                return False
            card.verdict = verdict
            card.resolved.set()
            self._cv.notify_all()
        log.info("card %s resolved: %s", card_id, verdict.get("v"))
        return True

    def wait(self, card: Card) -> dict:
        """Blocks the hook request until a device answers or the TTL expires.
        An expired card yields None, which the caller turns into no decision."""
        card.resolved.wait(card.remaining())
        return card.verdict

    def next_for(self, device: str, timeout: float):
        """Long-poll: the oldest open card this device has not been shown yet."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for card in sorted(self._cards.values(), key=lambda c: c.created):
                    if card.resolved.is_set() or card.expired:
                        continue
                    if device in card.delivered:
                        continue
                    card.delivered.add(device)
                    return card
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(left)

    def pending_for(self, device: str):
        """The card this device was already shown and still owes an answer for.

        Re-sending it on every idle poll is what keeps it on screen: a snapshot
        carries no `prompt` key, and the device reads that as "clear it". It
        also self-heals a delivery the device dropped.
        """
        with self._cv:
            for card in sorted(self._cards.values(), key=lambda c: c.created):
                if card.resolved.is_set() or card.expired:
                    continue
                if device in card.delivered:
                    return card
        return None

    def open_count(self) -> int:
        with self._cv:
            return sum(1 for c in self._cards.values()
                       if not c.resolved.is_set() and not c.expired)
