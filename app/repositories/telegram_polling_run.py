from app.models.telegram_polling_run import TelegramPollingRun
from app.repositories.base import BaseRepository


class TelegramPollingRunRepository(BaseRepository[TelegramPollingRun]):
    model = TelegramPollingRun
