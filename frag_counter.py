import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import pytz

MSK = pytz.timezone("Europe/Moscow")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ============================================================
# НАСТРОЙКИ
# ============================================================
HDE_BASE = "https://ggsel.helpdeskeddy.com/api/v2"
HDE_AUTH = ("jivo@ggsel.net", "26fc4db0-8683-4fe6-92b0-6e2daaae8a5c")
TG_TOKEN = "8984090136:AAFjLjrT0iLoBBMCv2RLJlbtXs8Wdu5RJIA"
TG_URL   = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

# HDE id -> (имя, telegram chat_id, час начала смены, час конца смены)
OPERATORS = {
    # --- 08:00 - 16:00 ---
    153: ("Андрей",     None,       8, 16),
    301: ("Иван К",     None,       8, 16),
    535: ("Дмитрий",    None,       8, 16),
    100: ("Мария",      456062447,       8, 16),
    # --- 14:00 - 22:00 ---
    536: ("Данила",     None,      14, 22),
    200: ("Диана",      None,      14, 22),
    62:  ("Игорь",      None,      14, 22),
    537: ("Виктор",     None,      14, 22),
    343: ("Роман",      None,      14, 22),
    538: ("Ксения",     None,      14, 22),
    # --- 18:00 - 02:00 ---
    258: ("Максим",     None,      18,  2),
    391: ("Влада",      None,      18,  2),
    369: ("Александра", 907994201, 18,  2),
    # --- 02:00 - 10:00 ---
    539: ("Иван С",     None,       2, 10),
    540: ("Иван М",     None,       2, 10),
}
# ============================================================

ANSWER_EVENTS = {"ticket_answer", "ticket_answer_chat"}



def shift_window(sh_start: int, sh_end: int) -> tuple[datetime, datetime]:
    now = datetime.now(MSK).replace(tzinfo=None)
    duration = sh_end - sh_start
    if duration <= 0:
        duration += 24
    return now - timedelta(hours=duration), now

def parse_hde_dt(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%H:%M:%S %d.%m.%Y")
    except Exception:
        return None

def format_time(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    elif seconds < 3600:
        return f"{seconds // 60} мин"
    else:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}ч {m}мин" if m else f"{h}ч"


async def get_ticket_candidates(client: httpx.AsyncClient, operator_id: int,
                                start: datetime, end: datetime) -> list[dict]:
    """Берёт все закрытые заявки оператора за период."""
    fmt = "%Y-%m-%d %H:%M:%S"
    tickets = []
    page = 1
    while True:
        r = await client.get(f"{HDE_BASE}/tickets", params={
            "owner_list":        operator_id,
            "status_list":       "closed",
            "from_date_updated": start.strftime(fmt),
            "to_date_updated":   end.strftime(fmt),
            "per_page":          100,
            "page":              page,
        })
        if r.status_code != 200:
            log.error(f"tickets {r.status_code}: {r.text[:100]}")
            break
        data = r.json()
        items = data.get("data", {})
        if isinstance(items, dict):
            items = list(items.values())
        tickets.extend(items)
        pag = data.get("pagination", {})
        if page >= pag.get("total_pages", 1):
            break
        page += 1
    return tickets


async def check_ticket(client: httpx.AsyncClient, ticket: dict,
                       operator_id: int, start: datetime, end: datetime) -> dict | None:
    """
    Проверяет по аудиту что оператор сам назначил себя + ответил + закрыл в период смены.
    Заявка считается один раз, даже если цикл назначение→ответ→закрытие повторялся.
    Возвращает данные для статистики или None если заявка не подходит.
    """
    ticket_id = ticket["id"]
    r = await client.get(f"{HDE_BASE}/tickets/{ticket_id}/audit")
    if r.status_code != 200:
        return None

    events = list(r.json().get("data", {}).values())
    events.sort(key=lambda e: parse_hde_dt(e["date_created"]) or datetime.min)

    op_answered        = False
    op_closed          = False
    first_op_answer_at = None
    assigned_at        = None
    op_name            = OPERATORS.get(operator_id, ("",))[0]

    for e in events:
        dt  = parse_hde_dt(e["date_created"])
        uid = e.get("user_id")
        evt = e.get("event")

        if not dt or not (start <= dt <= end):
            continue

        # Назначение — вручную оператором или автоматом системой на него
        if evt == "owner_update" and assigned_at is None:
            text_ru = e.get("text", {}).get("ru", "")
            if uid == operator_id or (uid == -2 and op_name and op_name in text_ru):
                assigned_at = dt

        if evt in ANSWER_EVENTS and uid == operator_id:
            op_answered = True
            if first_op_answer_at is None:
                first_op_answer_at = dt

        if evt == "ticket_close" and uid == operator_id:
            op_closed = True

    if not (op_answered and op_closed):
        return None

    # Время ответа: от назначения исполнителем до первого ответа
    response_seconds = None
    if assigned_at and first_op_answer_at and first_op_answer_at >= assigned_at:
        diff = (first_op_answer_at - assigned_at).total_seconds()
        if diff >= 0:
            response_seconds = diff

    # Оценка из тела заявки
    rate = ticket.get("rate")
    rate_val = int(rate) if rate and str(rate).isdigit() else None

    return {
        "ticket_id":        ticket_id,
        "response_seconds": response_seconds,
        "rate":             rate_val,
    }


async def get_stats(operator_id: int, start: datetime, end: datetime) -> dict:
    async with httpx.AsyncClient(auth=HDE_AUTH, timeout=30) as client:
        candidates = await get_ticket_candidates(client, operator_id, start, end)
        log.info(f"  Кандидатов: {len(candidates)}")

        # Проверяем аудит пачками по 10
        results = []
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i+10]
            batch_res = await asyncio.gather(*[
                check_ticket(client, t, operator_id, start, end)
                for t in batch
            ])
            results.extend(batch_res)

    # Дедупликация по ticket_id (заявка считается один раз за смену)
    seen = set()
    valid = []
    for r in results:
        if r is None:
            continue
        if r["ticket_id"] in seen:
            continue
        seen.add(r["ticket_id"])
        valid.append(r)

    closed = len(valid)

    # Среднее время ответа
    times = [r["response_seconds"] for r in valid if r["response_seconds"] is not None]
    avg_response = round(sum(times) / len(times)) if times else None

    # Оценки
    rates = [r["rate"] for r in valid if r["rate"] is not None]
    avg_rate  = round(sum(rates) / len(rates), 1) if rates else None
    rate_count = len(rates)

    times = [r["response_seconds"] for r in valid if r.get("response_seconds") is not None]
    avg_response = round(sum(times) / len(times)) if times else None

    return {
        "closed":       closed,
        "avg_response": avg_response,
        "avg_rate":     avg_rate,
        "rate_count":   rate_count,
    }


async def send_report(operator_id: int, name: str, chat_id: int,
                      sh_start: int, sh_end: int):
    if chat_id is None:
        log.info(f"{name}: chat_id не задан, пропускаем")
        return

    start, end = shift_window(sh_start, sh_end)
    log.info(f"Считаем {name}: {start.strftime('%H:%M')} — {end.strftime('%H:%M')}")

    stats = await get_stats(operator_id, start, end)

    if stats["closed"] == 0:
        log.info(f"{name}: 0 заявок, не отправляем")
        return

    # Время ответа
    resp_str = (f"⚡ Среднее время ответа: {format_time(stats['avg_response'])}"
                if stats["avg_response"] else "⚡ Время ответа: нет данных")

    # Оценки
    if stats["avg_rate"] is not None:
        rate_str = f"⭐ Средняя оценка: {stats['avg_rate']} ({stats['rate_count']} шт.)"
    else:
        rate_str = "⭐ Оценок за смену нет"

    text = (
        f"🎮 <b>FragCounter — итоги смены</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"🕐 Смена: {sh_start:02d}:00 — {sh_end:02d}:00\n"
        f"📅 {end.strftime('%d.%m.%Y')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Закрыто заявок: <b>{stats['closed']}</b>\n"
        f"{resp_str}\n"
        f"{rate_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🏆 +100 social credit! 🏆"
    )

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(TG_URL, json={
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        })
        if r.status_code == 200:
            log.info(f"✓ Отправлено {name}")
        else:
            log.error(f"Ошибка отправки {name}: {r.text}")


async def run_shift(end_hour: int):
    tasks = [
        send_report(op_id, name, chat_id, sh_start, sh_end)
        for op_id, (name, chat_id, sh_start, sh_end) in OPERATORS.items()
        if sh_end == end_hour
    ]
    await asyncio.gather(*tasks)


async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    end_hours = set(sh_end for _, (_, _, _, sh_end) in OPERATORS.items())
    for end_hour in end_hours:
        names = [n for _, (n, _, _, se) in OPERATORS.items() if se == end_hour]
        scheduler.add_job(run_shift, "cron", hour=end_hour % 24, minute=0,
                          args=[end_hour], id=f"shift_{end_hour}")
        log.info(f"Задача в {end_hour:02d}:00 → {names}")

    scheduler.start()
    log.info("Планировщик запущен")

    while True:
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())