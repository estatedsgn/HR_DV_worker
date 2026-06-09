# Деплой HR_DV_worker на сервер

Перенос проекта на VPS (вне РФ, оплата картой РФ — напр. Aeza/PQ.Hosting, локация
NL/DE, Ubuntu 22.04/24.04) с круглосуточной работой воркера и «поселённым» рядом
Claude Code, которым можно управлять с телефона/ПК, не заходя на сам сервер руками.

## Что где крутится

```
VPS (Ubuntu)
├─ Docker → Postgres (pgvector) + том данных + ежедневный бэкап-дамп
├─ systemd:
│   ├─ hrdv-autopilot.service   → scripts/run_autonomous.py  (Дайвинчик + автопилот)
│   └─ hrdv-supervisor.service  → python -m app.supervisor   (пульт вкл/выкл)
├─ Claude Code (Node.js) в tmux-сессии — живёт 24/7, отвечает когда пишешь
└─ Tailscale — приватная сеть для безопасного доступа с телефона (SSH наружу закрыт)
```

Воркер работает всегда. Claude — отдельный процесс, «думает» только когда ты пишешь.

> **Секреты никогда не коммитятся.** `.env`, `daivinchik_*` state/leads и `*.session`
> уже в `.gitignore`. На сервер `.env` копируется руками (см. шаг 3).

---

## Пошагово

### 0. Купить VPS
- Aeza или PQ.Hosting, локация **Нидерланды/Германия**, **Ubuntu 24.04**.
- Старт: **4 vCPU / 8 GB**, цель 50–100 аккаунтов: **8 vCPU / 12–16 GB / 120 GB NVMe**.
- Возьми тариф с возможностью апгрейда RAM в один клик.

### 1. Первичная настройка (как root)
```bash
ssh root@<server-ip>
# скопируй репозиторий или задай REPO_URL, чтобы скрипт сам склонировал:
export REPO_URL=https://github.com/<you>/HR_DV_worker.git   # опционально
curl -fsSL https://raw.githubusercontent.com/<you>/HR_DV_worker/<branch>/deploy/setup_server.sh -o setup_server.sh
bash setup_server.sh
```
Скрипт ставит: пакеты, Docker, Tailscale, Node.js + Claude Code, создаёт юзера `hrdv`,
готовит `/opt/HR_DV_worker`, включает фаервол (наружу открыт только SSH).

> Если репозиторий приватный — сначала склонируй вручную в `/opt/HR_DV_worker`
> (через deploy-ключ или `gh auth`), затем запусти `bash deploy/setup_server.sh`.

### 2. Подключить сервер в приватную сеть
```bash
tailscale up        # открой ссылку, авторизуйся тем же аккаунтом, что на телефоне
tailscale ip -4     # запомни приватный IP (100.x.x.x)
```
Поставь Tailscale на телефон/ПК тем же аккаунтом — дальше заходишь по `100.x.x.x`,
публичный SSH можно вообще закрыть.

### 3. Перенести секреты (вручную, НЕ через git)
Скопируй свой локальный `.env` на сервер:
```bash
scp .env hrdv@<server-ip>:/opt/HR_DV_worker/.env
```
Проверь `DATABASE_URL` — для дефолтного compose это
`postgresql+asyncpg://hr_dv_worker:hr_dv_worker@localhost:5432/hr_dv_worker`.
(Опционально перенеси `daivinchik_*state*.json` если хочешь сохранить состояние свайпера.)

### 4. Поднять приложение (как hrdv)
```bash
su - hrdv
cd /opt/HR_DV_worker
bash deploy/deploy_app.sh      # venv + зависимости + Postgres в Docker + миграции
```

### 5. Включить сервисы (как root)
```bash
cp /opt/HR_DV_worker/deploy/hrdv-autopilot.service  /etc/systemd/system/
cp /opt/HR_DV_worker/deploy/hrdv-supervisor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hrdv-autopilot hrdv-supervisor
systemctl status hrdv-autopilot          # проверить, что поднялось
journalctl -u hrdv-autopilot -f          # живые логи
```
Воркер теперь работает 24/7 и сам перезапустится после падения или ребута.

### 6. Поселить Claude и общаться с телефона
```bash
su - hrdv
cd /opt/HR_DV_worker
tmux new -s claude
claude            # выполни /login один раз (откроется ссылка авторизации)
```
Отсоединиться от сессии: `Ctrl+b`, затем `d`. Claude и tmux продолжают жить.

**С телефона/ПК:** поставь SSH-клиент (**Termius**), подключись по Tailscale-IP под
юзером `hrdv`, выполни `tmux attach -t claude` — попадёшь в ту же живую сессию и
пишешь как обычно. Закрыл приложение — сессия остаётся.

**Веб (опц.):** на claude.ai/code подключи GitHub-репозиторий для удобной правки
кода из браузера. Управление живым воркером — через SSH-сессию выше.

### 7. Бэкапы (как hrdv)
```bash
crontab -e
# добавь строку:
30 4 * * * /opt/HR_DV_worker/deploy/backup_postgres.sh >> /opt/HR_DV_worker/runtime_logs/backup.log 2>&1
```

---

## Обновление кода
```bash
su - hrdv && cd /opt/HR_DV_worker
bash deploy/update.sh          # git pull + зависимости/миграции + рестарт сервисов
```

## Шпаргалка
| Действие | Команда |
|---|---|
| Логи автопилота | `journalctl -u hrdv-autopilot -f` |
| Рестарт воркера | `sudo systemctl restart hrdv-autopilot` |
| Стоп/старт | `sudo systemctl stop/start hrdv-autopilot` |
| Статус Postgres | `docker compose ps` |
| Свободная RAM | `free -h` / `htop` |
| Зайти к Claude | `tmux attach -t claude` |
| Бэкап БД сейчас | `bash deploy/backup_postgres.sh` |

> Следи за свободной RAM: стабильно <1.5 GB и активный swap → пора апнуть тариф.
