# Дайвинчик → запись лида: что и куда сохраняется

Когда авто-свайпер ловит **взаимную симпатию** (intent `match`), он сохраняет
контакт в двух местах: в durable-файл и в БД проекта (воронку рекрутера). Ниже —
что именно пишется.

## 1. Откуда берётся контакт

Дайвинчик вешает ссылку на профиль мэтча **на имя** (Telegram-сущность
`messageEntityTextUrl` / `messageEntityMentionName`), а не текстом `@handle`.
`extract_lead()` собирает из текста, кнопок и **entities**:

| Поле | Источник | Пример |
|------|----------|--------|
| `telegram_username` | `@...`, `t.me/...` в тексте/url/entity | `@yulia_k` |
| `telegram_user_id` | `tg://user?id=...` или `messageEntityMentionName.userId` | `555000111` |
| `link` | первый entity-url или `tg://user?id=<id>` | `https://t.me/yulia_k` |
| `display_name` | первая непустая строка сообщения | `Юля` |
| `external_id` | `@username` → иначе `id:<user_id>` → иначе `msg:<id>` | `@yulia_k` / `id:555000111` |

## 2. Durable-таблица (всегда) — `daivinchik_leads.jsonl`

Пишется **на каждый** мэтч до обращения к БД, чтобы контакт не потерялся даже
если БД недоступна. Одна JSON-строка на лид:

```json
{"ts":"2026-06-07T08:16:21+00:00","telegram_username":"@yulia_k","telegram_user_id":null,
 "link":"https://t.me/yulia_k","display_name":"Юля","external_id":"@yulia_k",
 "profile_text":"Начинай общаться 🥳\nЮля","match_message_id":1031}
```

## 3. База данных (если есть `@username`)

Через `LeadIntakeService.enqueue_lead(source="daivinchik", …)` — тот же путь, что
у обычного приёма лидов рекрутера. **Идемпотентно** по паре
`(source, external_lead_id)`: повторный мэтч того же контакта не создаёт дубль.

Создаются строки:

| Таблица | Что записывается | Ключевые поля |
|---------|------------------|---------------|
| `lead_intake_events` | факт приёма лида | `source='daivinchik'`, `external_lead_id`, `telegram_username`, `status='accepted'`, `payload` (display_name, profile_text, telegram_user_id, link, match_message_id) |
| `dialogs` | диалог под контакт | `crmchat_dialog_id='intake:daivinchik:<external_id>'`, `telegram_username`, `status='open'` |
| `leads` | карточка лида в воронке | `dialog_id`, `qualification_status='new'`, `funnel_state='NEW_LEAD'` |
| `dialog_sequence_runs` | запуск кампании первого касания | `dialog_id`, `campaign_id`, `status='active'`, `current_step_position=0` |
| `outbound_jobs` *(если в кампании есть fixed-message шаг)* | очередь первого сообщения | `dialog_id`, `status='queued'` — реально отправит уже `OutboundQueueWorker` автопилота |

После этого дальше с лидом работает **существующий воркер** рекрутера
(автопилот): резолвит контакт, шлёт первое сообщение, ведёт по воронке.

### Случай «только id, без @username»

Если Дайвинчик дал ссылку вида `tg://user?id=...` (у человека нет публичного
@username) — авто-резолв для отправки невозможен. Тогда лид:
- **записывается** в `daivinchik_leads.jsonl` (с `link`/`telegram_user_id`),
- **не** заводится в воронку (нечего резолвить),
- супервизору уходит алерт «обработай вручную».

## 4. Дневной лимит лидов

Не больше `DAIVINCHIK_DAILY_LEAD_LIMIT` (по умолчанию **7**) мэтчей в сутки.
Счётчик `leads_today` / `leads_date` хранится в `daivinchik_state.json` и
сбрасывается в полночь по `DAIVINCHIK_TIMEZONE`. Как только за день набрано 7 —
бот **останавливается до следующего дня** (по Дайвинчику дальше не идёт).

## 5. Как проверить запись вручную

```bat
:: прогоняет реальный путь enqueue_lead, печатает строки из всех таблиц и
:: удаляет тестовые данные после проверки (алерт в Telegram отключён)
.venv\Scripts\python.exe scripts\verify_daivinchik_lead.py
```

Запрос лидов из БД напрямую:

```sql
SELECT external_lead_id, telegram_username, status, created_at
FROM lead_intake_events WHERE source = 'daivinchik' ORDER BY created_at DESC;
```
