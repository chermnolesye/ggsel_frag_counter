import asyncio
import httpx
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
# chat_id = None пока оператор не написал боту
OPERATORS = {
    # --- 08:00 - 16:00 ---
    153: ("Андрей",    None,      8, 16),
    301: ("Иван К",    None,      8, 16),
    535: ("Дмитрий",  None,      8, 16),
    100: ("Мария",     None,      8, 16),
    # --- 14:00 - 22:00 ---
    536: ("Данила",    None,     14, 22),
    200: ("Диана",     None,     14, 22),
    62:  ("Игорь",     None,     14, 22),
    537: ("Виктор",   None,     14, 22),
    343: ("Роман",     None,     14, 22),
    538: ("Ксения",    None,     14, 22),
    # --- 18:00 - 02:00 ---
    258: ("Максим",    None,     18,  2),
    391: ("Влада",     None,     18,  2),
    369: ("Александра", 907994201, 18, 2),  # тест
    # --- 02:00 - 10:00 ---
    539: ("Иван С",    None,      2, 10),
    540: ("Иван М",    None,      2, 10),
}
# ============================================================

def shift_window(shift_start_h: int, shift_end_h: int) -> tuple[datetime, datetime]:
    """Возвращает (начало, конец) смены. Конец = сейчас, начало = конец - длительность."""
    now = datetime.now()
    duration = shift_end_h - shift_start_h
    if duration <= 0:
        duration += 24  # ночная смена (18→02 = 8ч, 02→10 = 8ч)
    return now - timedelta(hours=duration), now


async def get_stats(operator_id: int, start: datetime, end: datetime) -> dict:
    """Считает закрытые заявки оператора за период."""
    fmt = "%Y-%m-%d %H:%M:%S"
    closed = 0
    total_rate = 0
    rate_count = 0
    page = 1

    async with httpx.AsyncClient(auth=HDE_AUTH, timeout=30) as client:
        while True:
            params = {
                "owner_list":        operator_id,
                "status_list":       "closed",
                "from_date_updated": start.strftime(fmt),
                "to_date_updated":   end.strftime(fmt),
                "per_page":          100,
                "page":              page,
            }
            r = await client.get(f"{HDE_BASE}/tickets", params=params)

            if r.status_code != 200:
                log.error(f"HDE вернул {r.status_code}: {r.text[:200]}")
                break

            data = r.json()
            tickets = data.get("data", {})
            if isinstance(tickets, dict):
                tickets = list(tickets.values())

            for t in tickets:
                closed += 1
                rate = t.get("rate")
                if rate and str(rate).isdigit():
                    total_rate += int(rate)
                    rate_count += 1

            pagination = data.get("pagination", {})
            if page >= pagination.get("total_pages", 1):
                break
            page += 1

    avg_rate = round(total_rate / rate_count, 1) if rate_count else None
    return {"closed": closed, "avg_rate": avg_rate, "rate_count": rate_count}


async def send_report(operator_id: int, name: str, chat_id: int,
                      shift_start_h: int, shift_end_h: int):
    if chat_id is None:
        log.info(f"{name}: chat_id не задан, пропускаем")
        return

    start, end = shift_window(shift_start_h, shift_end_h)
    log.info(f"Статистика для {name}: {start.strftime('%H:%M')} — {end.strftime('%H:%M')}")

    stats = await get_stats(operator_id, start, end)

    if stats["avg_rate"] is not None:
        rate_str = f"⭐ Средняя оценка: {stats['avg_rate']} ({stats['rate_count']} шт.)"
    else:
        rate_str = "⭐ Оценок за смену нет"

    end_h = f"{shift_end_h:02d}:00"
    start_h = f"{shift_start_h:02d}:00"

    text = (
        f"🎮 <b>FragCounter — итоги смены</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👤 {name}\n"
        f"🕐 Смена: {start_h} — {end_h}\n"
        f"📅 {end.strftime('%d.%m.%Y')}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"✅ Закрыто заявок: <b>{stats['closed']}</b>\n"
        f"{rate_str}\n"
        f"━━━━━━━━━━━━━━━\n"
        f"GG WP! 🏆"
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
    """Запускает отправку для всех операторов с данным концом смены."""
    tasks = []
    for op_id, (name, chat_id, sh_start, sh_end) in OPERATORS.items():
        if sh_end == end_hour:
            tasks.append(send_report(op_id, name, chat_id, sh_start, sh_end))
    await asyncio.gather(*tasks)


async def main():
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    # Собираем уникальные часы конца смены
    end_hours = set(sh_end for _, (_, _, _, sh_end) in OPERATORS.items())

    for end_hour in end_hours:
        names = [n for _, (n, _, _, se) in OPERATORS.items() if se == end_hour]
        scheduler.add_job(
            run_shift,
            trigger="cron",
            hour=end_hour % 24,
            minute=0,
            args=[end_hour],
            id=f"shift_{end_hour}",
        )
        log.info(f"Задача в {end_hour:02d}:00 → {names}")

    scheduler.start()
    log.info("Планировщик запущен")

    # Тестовый запуск прямо сейчас (только операторы с chat_id)
    log.info("=== ТЕСТОВЫЙ ЗАПУСК ===")
    for op_id, (name, chat_id, sh_start, sh_end) in OPERATORS.items():
        if chat_id is not None:
            await send_report(op_id, name, chat_id, sh_start, sh_end)

    while True:
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
