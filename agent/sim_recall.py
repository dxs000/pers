"""Шаг 63: симуляция отбора воспоминаний. 30 строк, 14 дней, 3 разговора в день.
Нужна тестовая база: `uv run db.py --init`, затем `uv run sim_recall.py`.
До шага: 3 разных из 30, каждое в 100% реплик. После: 29 из 30."""
import sys, collections, random
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from engine import PgEngine as Engine
random.seed(1)
eng = Engine(test=True)
eng.conn.execute("TRUNCATE memories RESTART IDENTITY CASCADE")
eng.conn.execute("UPDATE agent SET born_at = '1956-03-01' WHERE id = 1")
t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
for i in range(30):
    src = "dream" if i == 29 else "told"
    eng.add_memory(datetime(1960 + i * 2, 6, 1, tzinfo=timezone.utc), "era",
                   f"воспоминание {i:02d}", src, 1.6 if src == "dream" else 1.0,
                   now=t0 - timedelta(days=30 - i))
seen = collections.Counter(); per_day = []
for day in range(14):
    today = set()
    for talk in (9, 14, 20):
        for k in range(6):
            now = t0 + timedelta(days=day, hours=talk, minutes=3 * k)
            turn = eng.snapshot(now)
            seen.update(m["id"] for m in turn.memories); today |= {m["id"] for m in turn.memories}
            with eng.unit():
                eng.surface(turn.memories, now)
                if random.random() < 0.2:          # иногда рассказывает одно из всплывших
                    eng.note_told([random.choice(turn.memories)], now)
    per_day.append(len(today))
total = sum(seen.values())
print(f"показов: {total}, разных воспоминаний: {len(seen)} из 30")
print("разных за день:", per_day)
for mid, n in seen.most_common(4):
    print(f"  {mid}: {100*n/(total/3):.0f}% реплик")
print("сон (mem_30):", f"{100*seen['mem_30']/(total/3):.0f}% реплик")
