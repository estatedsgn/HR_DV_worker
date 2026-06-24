from uuid import UUID

from sqlalchemy import case, func, or_, select

from app.models.dialog import Dialog
from app.models.dialog_sequence_run import DialogSequenceRun
from app.models.lead import Lead
from app.repositories.base import BaseRepository


class DialogRepository(BaseRepository[Dialog]):
    model = Dialog

    async def list_pollable_by_account(self, account_id: UUID) -> list[Dialog]:
        """Известные telegram-диалоги аккаунта, годные для прямого get_history.

        Деградационный путь, когда getDialogs виснет под троттлингом: историю можно
        тянуть точечно по сохранённому peer/accessHash, не обходя список диалогов.
        Свежие первыми — под троттлингом цикл длинный, живые переписки важнее.
        """
        result = await self.session.execute(
            select(Dialog)
            .where(
                Dialog.account_id == account_id,
                Dialog.crmchat_dialog_id.like("telegram:%"),
                Dialog.status != "ignored",
                Dialog.telegram_peer_id.isnot(None),
            )
            .order_by(Dialog.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_crmchat_dialog_id(self, crmchat_dialog_id: str) -> Dialog | None:
        result = await self.session.execute(
            select(Dialog).where(Dialog.crmchat_dialog_id == crmchat_dialog_id)
        )
        return result.scalar_one_or_none()

    async def get_by_telegram_peer(self, peer_type: str, peer_id: str) -> Dialog | None:
        result = await self.session.execute(
            select(Dialog).where(
                Dialog.telegram_peer_type == peer_type,
                Dialog.telegram_peer_id == peer_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_telegram_username(self, username: str) -> Dialog | None:
        normalized = normalize_username(username)
        result = await self.session.execute(
            select(Dialog)
            .where(func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized)
            .order_by(
                case((Dialog.crmchat_dialog_id.like("intake:%"), 0), else_=1),
                Dialog.created_at.asc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_latest_active_intake_by_telegram_username(self, username: str) -> Dialog | None:
        normalized = normalize_username(username)
        result = await self.session.execute(
            select(Dialog)
            .outerjoin(DialogSequenceRun, DialogSequenceRun.dialog_id == Dialog.id)
            .outerjoin(Lead, Lead.dialog_id == Dialog.id)
            .where(
                func.lower(func.replace(Dialog.telegram_username, "@", "")) == normalized,
                Dialog.crmchat_dialog_id.like("intake:%"),
                or_(
                    DialogSequenceRun.status.in_(["active", "waiting_outbound", "awaiting_reply", "awaiting_llm"]),
                    Lead.funnel_state.notin_(["CONVERTED", "LOST", "DO_NOT_CONTACT", "HUMAN_HANDOFF"]),
                ),
            )
            .order_by(
                case(
                    (
                        DialogSequenceRun.status.in_(["active", "waiting_outbound", "awaiting_reply", "awaiting_llm"]),
                        0,
                    ),
                    (Lead.funnel_state.notin_(["NEW_LEAD", "CONVERTED", "LOST", "DO_NOT_CONTACT", "HUMAN_HANDOFF"]), 1),
                    else_=2,
                ),
                DialogSequenceRun.created_at.desc().nullslast(),
                Dialog.created_at.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


def normalize_username(username: str) -> str:
    return username.strip().lower().lstrip("@")
