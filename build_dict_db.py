# -*- coding: utf-8 -*-
"""
将 ECDICT 的 ecdict.csv 转换为 SQLite 数据库 ecdict.db
只保留查词所需字段: word, phonetic, translation, exchange(词形变化)
并对 word 建索引以加速查询
"""
import csv
import os
import sqlite3
import sys

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecdict.csv")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecdict.db")


def build():
    if not os.path.exists(CSV_PATH):
        print(f"未找到 {CSV_PATH}, 请先下载 ECDICT 的 ecdict.csv")
        sys.exit(1)

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE stardict (
            word TEXT PRIMARY KEY,
            phonetic TEXT,
            translation TEXT,
            exchange TEXT
        )
    """)

    count = 0
    skipped = 0
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                word = (row.get("word") or "").strip()
                if not word:
                    skipped += 1
                    continue
                cur.execute(
                    "INSERT OR REPLACE INTO stardict (word, phonetic, translation, exchange) VALUES (?,?,?,?)",
                    (
                        word,
                        row.get("phonetic") or "",
                        row.get("translation") or "",
                        row.get("exchange") or "",
                    ),
                )
                count += 1
                if count % 50000 == 0:
                    conn.commit()
                    print(f"  已导入 {count} 条…")
            except Exception:
                skipped += 1
                continue

    conn.commit()
    # 建索引加速查询
    cur.execute("CREATE INDEX idx_word ON stardict(word)")
    conn.commit()
    conn.close()
    print(f"完成: 共导入 {count} 条记录, 跳过 {skipped} 条 -> {DB_PATH}")
    print(f"数据库大小: {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    build()
