"""Session generation guard used to drop callbacks from stale sessions."""


from __future__ import annotations


class SessionGuard:
    """Liveness token for one session generation.

    secsgem delivers callbacks on its own threads, so a message can surface
    after the session that received it has already been torn down. Every
    equipment callback checks its token first, and an event that arrives on a
    retired session is refused rather than accepted: acknowledging it would
    tell the tool we have an event that no live session is going to journal,
    and the tool would then be free to forget it.
    """

    __slots__ = ("active", "generation")

    def __init__(self, generation: int):
        self.active = True
        self.generation = generation
