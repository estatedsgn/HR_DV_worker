import asyncio

from app.models.dialog import Dialog
from app.repositories.dialog import normalize_username


def test_normalize_username_for_dialog_lookup() -> None:
    assert normalize_username("@IamNekiy") == "iamnekiy"
    assert normalize_username(" IamNekiy ") == "iamnekiy"


def test_polling_can_update_intake_dialog_before_peer_lookup(monkeypatch) -> None:
    # Regression guard for the manual @iamnekiy flow: CRMChat returns usernames
    # without "@", while lead intake stores the user-facing value with "@".
    from app.services.crmchat_connector import TelegramDialogSnapshot, TelegramPeer
    from app.services.telegram_polling import update_dialog_from_snapshot

    dialog = Dialog(
        account_id="00000000-0000-0000-0000-000000000001",
        crmchat_dialog_id="intake:smoke:lead-1",
        telegram_username="@iamnekiy",
        status="open",
    )
    snapshot = TelegramDialogSnapshot(
        peer=TelegramPeer(
            peer_type="user",
            peer_id="454645862",
            access_hash="hash",
            username="IamNekiy",
        )
    )

    update_dialog_from_snapshot(dialog, snapshot)

    assert dialog.telegram_peer_type == "user"
    assert dialog.telegram_peer_id == "454645862"
    assert dialog.telegram_username == "IamNekiy"
