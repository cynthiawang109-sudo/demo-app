"""Board-workload benchmark for TF-102 (stdlib only).

Seeds a throwaway SQLite database with N tasks spread across the three
board columns (todo / doing / done), then measures the reads a board
page depends on, at three layers:

1. first paint - what the board must fetch before it can render. On a
   checkout WITHOUT a status filter that is every task in the table
   (the ticket's complaint); with the filter it is one page per column.
2. full column crawl, raw SQL on one connection - isolates the query
   and index cost from everything else.
3. full column crawl through TaskStore - the API's real per-request
   cost (a fresh connection per page, as the store is designed).

Run it on any checkout to compare before/after:

    python scripts/bench_board.py --rows 60000
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import TaskStore  # noqa: E402

STATUSES = ("todo", "doing", "done")
FILTERED_SQL = (
    "SELECT id, title, status FROM tasks "
    "WHERE status = ? AND id > ? ORDER BY id LIMIT ?"
)


def seed(store: TaskStore, rows: int) -> None:
    with store._connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tasks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "title TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO tasks (title, status) VALUES (?, ?)",
            [(f"Task {i}", STATUSES[i % 3]) for i in range(rows)],
        )


def store_supports_status(store: TaskStore) -> bool:
    return "status" in store.list_page.__code__.co_varnames


def time_all_pages(store: TaskStore) -> tuple[int, float]:
    cursor, total = 0, 0
    start = time.perf_counter()
    while True:
        items, cursor = store.list_page(cursor, 200)
        total += len(items)
        if cursor is None:
            break
    return total, (time.perf_counter() - start) * 1000


def time_first_paint(store: TaskStore) -> tuple[int, float]:
    if not store_supports_status(store):
        return time_all_pages(store)
    total = 0
    start = time.perf_counter()
    for status in STATUSES:
        items, _ = store.list_page(0, 50, status)
        total += len(items)
    return total, (time.perf_counter() - start) * 1000


def time_column_sql(store: TaskStore, status: str) -> tuple[int, float]:
    cursor, total = 0, 0
    start = time.perf_counter()
    with store._connect() as conn:
        while True:
            rows = conn.execute(FILTERED_SQL, (status, cursor, 51)).fetchall()
            total += len(rows[:50])
            if len(rows) <= 50:
                break
            cursor = rows[49]["id"]
    return total, (time.perf_counter() - start) * 1000


def time_column_store(store: TaskStore, status: str) -> tuple[int, float] | None:
    if not store_supports_status(store):
        return None
    cursor, total = 0, 0
    start = time.perf_counter()
    while True:
        items, cursor = store.list_page(cursor, 50, status)
        total += len(items)
        if cursor is None:
            break
    return total, (time.perf_counter() - start) * 1000


def query_plan(store: TaskStore) -> list[str]:
    with store._connect() as conn:
        rows = conn.execute(
            "EXPLAIN QUERY PLAN " + FILTERED_SQL, ("doing", 0, 51)
        ).fetchall()
    return [r["detail"] for r in rows]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=60000)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        store = TaskStore(str(Path(tmp) / "bench.db"))
        seed(store, args.rows)
        fp_items, fp_ms = time_first_paint(store)
        sql_items, sql_ms = time_column_sql(store, "doing")
        store_result = time_column_store(store, "doing")
        print(f"rows seeded:                 {args.rows}")
        print(f"board first paint:           {fp_items} items in {fp_ms:.1f} ms")
        print(f"'doing' crawl (raw SQL):     {sql_items} items in {sql_ms:.1f} ms")
        if store_result is None:
            print("'doing' crawl (store API):   not supported (no status filter)")
        else:
            st_items, st_ms = store_result
            print(f"'doing' crawl (store API):   {st_items} items in {st_ms:.1f} ms")
        print("filtered query plan:")
        for line in query_plan(store):
            print(f"  {line}")


if __name__ == "__main__":
    main()
