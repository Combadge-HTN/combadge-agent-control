from __future__ import annotations

from collections.abc import Callable

from .store import Store


def deliver_agent_notifications(store: Store, speak: Callable[[str], None]) -> int:
    """Acknowledge each successful delivery before attempting the next.

    A crash between speech and acknowledgement can still replay that one item;
    exact-once speech requires an idempotent acknowledgement from the receiver.
    """
    delivered = []
    for notification in store.pending_notifications():
        speak(notification["message"])
        store.mark_notifications_delivered([notification['id']])
        delivered.append(notification["id"])
    return len(delivered)
