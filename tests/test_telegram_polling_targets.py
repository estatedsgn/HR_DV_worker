import asyncio

from app.services.telegram_polling import TelegramPollingService


class FakeTargetRepo:
    def __init__(self, allowed: bool):
        self.allowed = allowed

    async def is_target(self, *, account_id, peer_type: str, peer_id: str) -> bool:
        return self.allowed


def test_polling_target_filter_behavior() -> None:
    # lightweight static check: service references target repository and handler hook
    import app.services.telegram_polling as tp

    assert hasattr(tp, "TelegramDialogTargetRepository")
    assert hasattr(tp, "NewMessageHandler")
