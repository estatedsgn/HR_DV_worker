from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings

from app.models.telegram_polling_run import TelegramPollingRun
from app.services.crmchat_connector import (
    CRMChatAPIError,
    TelegramDialogSnapshot,
    TelegramFloodWaitError,
    TelegramPeer,
    normalize_messages_response,
)
from app.services.telegram_polling import (
    TelegramPollingService,
    build_dialog_external_id,
    build_message_external_id,
    initial_dialog_status,
    parse_telegram_datetime,
    should_sync_dialog,
    snapshot_from_dialog,
)


class _DialogStub:
    """Лёгкий стаб Dialog (без ORM/сессии) для проверки snapshot_from_dialog."""

    def __init__(
        self,
        peer_type=None,
        peer_id=None,
        access_hash=None,
        username=None,
    ) -> None:
        self.telegram_peer_type = peer_type
        self.telegram_peer_id = peer_id
        self.telegram_access_hash = access_hash
        self.telegram_username = username


def test_normalize_messages_response_uses_fallback_peer_and_direction() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123", access_hash="hash")

    messages = normalize_messages_response(
        {
            "messages": [
                {"id": "10", "message": "hi", "date": "2026-05-18T00:00:00Z"},
                {"id": "11", "message": "hello", "out": True},
            ]
        },
        fallback_peer=peer,
    )

    assert len(messages) == 2
    assert messages[0].peer == peer
    assert messages[0].message_id == "10"
    assert messages[0].text == "hi"
    assert not messages[0].outgoing
    assert messages[1].outgoing


def test_polling_external_ids_are_stable() -> None:
    peer = TelegramPeer(peer_type="user", peer_id="123", access_hash="hash")

    class DialogSnapshot:
        pass

    snapshot = DialogSnapshot()
    snapshot.peer = peer

    assert (
        build_dialog_external_id("account-1", snapshot) == "telegram:account-1:user:123"
    )
    assert (
        build_message_external_id("workspace-1", "account-1", "user", "123", "10")
        == "telegram:workspace-1:account-1:user:123:10"
    )


def test_parse_telegram_datetime_accepts_unix_and_iso_values() -> None:
    assert parse_telegram_datetime("0") == datetime.fromtimestamp(0, UTC)
    assert parse_telegram_datetime("2026-05-18T00:00:00Z") == datetime(
        2026, 5, 18, tzinfo=UTC
    )
    assert parse_telegram_datetime(None) is None
    assert parse_telegram_datetime("not-a-date") is None


def test_initial_dialog_status_ignores_non_user_and_bot_dialogs() -> None:
    human = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="123", raw={"user": {"bot": False}})
    )
    bot = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="456", raw={"user": {"bot": True}})
    )
    channel = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="channel", peer_id="789")
    )

    assert initial_dialog_status(human) == "pending_review"
    assert initial_dialog_status(bot) == "ignored"
    assert initial_dialog_status(channel) == "ignored"


def test_should_sync_dialog_filters_by_username() -> None:
    target = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="123", username="IamNekiy")
    )
    other = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="456", username="someone_else")
    )
    missing = TelegramDialogSnapshot(peer=TelegramPeer(peer_type="user", peer_id="789"))

    assert should_sync_dialog(target, "@iamnekiy")
    assert not should_sync_dialog(other, "@iamnekiy")
    assert not should_sync_dialog(missing, "@iamnekiy")


def test_should_sync_dialog_accepts_username_without_at() -> None:
    target = TelegramDialogSnapshot(
        peer=TelegramPeer(peer_type="user", peer_id="123", username="IamNekiy")
    )

    assert should_sync_dialog(target, "iamnekiy")


def test_snapshot_from_dialog_builds_peer_from_db_fields() -> None:
    # Leads-only поллит точечно по peer из БД, минуя тяжёлый getDialogs.
    dialog = _DialogStub(
        peer_type="user",
        peer_id="123",
        access_hash="hash-1",
        username="IamNekiy",
    )

    snapshot = snapshot_from_dialog(dialog)

    assert snapshot is not None
    assert snapshot.peer.peer_type == "user"
    assert snapshot.peer.peer_id == "123"
    assert snapshot.peer.access_hash == "hash-1"
    assert snapshot.peer.username == "IamNekiy"


def test_snapshot_from_dialog_returns_none_without_usable_peer() -> None:
    assert snapshot_from_dialog(_DialogStub()) is None
    assert snapshot_from_dialog(_DialogStub(peer_type="user")) is None
    assert snapshot_from_dialog(_DialogStub(peer_id="123")) is None


def _bare_service() -> TelegramPollingService:
    """Сервис без БД/сети — только поля, которые читает _sync_snapshots."""
    svc = TelegramPollingService.__new__(TelegramPollingService)
    svc.only_username = None
    svc.only_usernames = None
    return svc


def _fresh_run() -> TelegramPollingRun:
    run = TelegramPollingRun(status="started", started_at=datetime.now(UTC))
    # default=0 у модели применяется при flush; в тесте обнуляем вручную.
    run.dialogs_synced = run.messages_seen = run.messages_created = 0
    return run


def _snaps(n: int) -> list[TelegramDialogSnapshot]:
    return [
        TelegramDialogSnapshot(
            peer=TelegramPeer(peer_type="user", peer_id=str(i), access_hash="h")
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_sync_snapshots_tolerates_single_dialog_failure() -> None:
    svc = _bare_service()
    run = _fresh_run()
    calls = {"n": 0}

    async def fake_sync(_ctx, _acc, _snap):
        calls["n"] += 1
        if calls["n"] == 2:  # второй диалог — битый peer
            raise CRMChatAPIError("PEER_ID_INVALID")
        return True, 1, 1

    svc._sync_dialog = fake_sync  # type: ignore[assignment]
    # Не должно бросать: один сбойный диалог пропускается, прогон продолжается.
    await svc._sync_snapshots(None, None, _snaps(3), run)
    assert calls["n"] == 3
    assert run.dialogs_synced == 2  # два успешных, один пропущен
    # Пропуск не молчаливый: он посчитан и виден мониторингу.
    assert run.dialogs_skipped == 1
    assert "id:1" in run.skip_details
    assert "PEER_ID_INVALID" in run.skip_details


@pytest.mark.asyncio
async def test_sync_snapshots_tolerates_all_timeouts() -> None:
    svc = _bare_service()
    run = _fresh_run()

    async def fake_sync(_ctx, _acc, _snap):
        raise httpx.ReadTimeout("")

    svc._sync_dialog = fake_sync  # type: ignore[assignment]
    # Полный троттлинг (все таймауты) — НЕ фатально, прогон завершится.
    await svc._sync_snapshots(None, None, _snaps(3), run)
    assert run.dialogs_synced == 0
    assert run.dialogs_skipped == 3


@pytest.mark.asyncio
async def test_sync_snapshots_aborts_cycle_on_account_throttle() -> None:
    from app.services.telegram_polling import _THROTTLE_ABORT_TIMEOUTS

    svc = _bare_service()
    run = _fresh_run()
    calls = {"n": 0}

    async def fake_sync(_ctx, _acc, _snap):
        calls["n"] += 1
        raise httpx.ReadTimeout("")

    svc._sync_dialog = fake_sync  # type: ignore[assignment]
    # 20 диалогов, всё таймаутит: НЕ долбим все 20 (это часы), обрываем рано.
    await svc._sync_snapshots(None, None, _snaps(20), run)
    assert calls["n"] == _THROTTLE_ABORT_TIMEOUTS
    assert run.dialogs_synced == 0
    assert run.dialogs_skipped == _THROTTLE_ABORT_TIMEOUTS


@pytest.mark.asyncio
async def test_sync_snapshots_tolerates_per_dialog_api_errors() -> None:
    svc = _bare_service()
    run = _fresh_run()

    async def fake_sync(_ctx, _acc, _snap):
        # Протухший peer на деградационном пути — единичная per-dialog ошибка.
        raise CRMChatAPIError("PEER_ID_INVALID")

    svc._sync_dialog = fake_sync  # type: ignore[assignment]
    # НЕ бросаем: системный сбой (отзыв ключа) упал бы раньше в bootstrap/getDialogs.
    await svc._sync_snapshots(None, None, _snaps(3), run)
    assert run.dialogs_synced == 0


def _throttle_account():
    return SimpleNamespace(flood_wait_until=None, health_status="healthy", last_error_message=None)


def test_throttle_backoff_trips_when_seen_but_zero_synced() -> None:
    svc = _bare_service()
    svc.settings = Settings()
    run = _fresh_run()
    run.dialogs_seen, run.dialogs_synced, run.dialogs_skipped = 75, 0, 75
    acc = _throttle_account()
    now = datetime.now(UTC)
    svc._apply_throttle_backoff(acc, run, now)
    assert acc.flood_wait_until is not None and acc.flood_wait_until > now
    assert acc.health_status == "rate_limited"
    assert run.status == "rate_limited"


def test_throttle_backoff_clears_on_recovery() -> None:
    svc = _bare_service()
    svc.settings = Settings()
    run = _fresh_run()
    run.dialogs_seen, run.dialogs_synced, run.dialogs_skipped = 20, 20, 0
    acc = SimpleNamespace(
        flood_wait_until=datetime.now(UTC) + timedelta(seconds=300),
        health_status="rate_limited",
        last_error_message="polling throttle",
    )
    svc._apply_throttle_backoff(acc, run, datetime.now(UTC))
    assert acc.flood_wait_until is None
    assert acc.health_status == "healthy"


def test_throttle_backoff_ignores_single_flaky_dialog() -> None:
    svc = _bare_service()
    svc.settings = Settings()
    run = _fresh_run()
    run.dialogs_seen, run.dialogs_synced, run.dialogs_skipped = 5, 0, 1  # below threshold
    acc = _throttle_account()
    svc._apply_throttle_backoff(acc, run, datetime.now(UTC))
    assert acc.flood_wait_until is None
    assert run.status != "rate_limited"


@pytest.mark.asyncio
async def test_sync_snapshots_propagates_flood_wait() -> None:
    svc = _bare_service()
    run = _fresh_run()

    async def fake_sync(_ctx, _acc, _snap):
        raise TelegramFloodWaitError(30)

    svc._sync_dialog = fake_sync  # type: ignore[assignment]
    with pytest.raises(TelegramFloodWaitError):
        await svc._sync_snapshots(None, None, _snaps(2), run)
