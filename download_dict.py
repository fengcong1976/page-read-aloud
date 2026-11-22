# -*- coding: utf-8 -*-
"""多线程分块下载 ecdict.csv, 加速慢网络"""
import os
import sys
import time
import threading
import urllib.request

URL = "https://ghproxy.net/https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecdict.csv")
NUM_THREADS = 8
CHUNK = 1024 * 1024  # 1MB per request


def get_total_size():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return int(r.headers.get("Content-Length", 0))


def download_chunk(start, end, buf, idx):
    """下载 [start, end) 字节范围到 buf 的对应位置"""
    req = urllib.request.Request(URL, headers={
        "User-Agent": "Mozilla/5.0",
        "Range": f"bytes={start}-{end-1}",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
            buf[idx] = data
            return len(data)
    except Exception as e:
        print(f"  chunk {idx} ({start}-{end}) failed: {e}", flush=True)
        buf[idx] = None
        return 0


def main():
    total = get_total_size()
    print(f"Total size: {total/1024/1024:.1f} MB")

    existing = os.path.getsize(OUT) if os.path.exists(OUT) else 0
    print(f"Existing: {existing/1024/1024:.1f} MB")

    if existing >= total:
        print("Already complete!")
        return

    # 从 existing 开始分块
    start_byte = existing
    chunks = []
    pos = start_byte
    while pos < total:
        end = min(pos + CHUNK, total)
        chunks.append((pos, end))
        pos = end

    print(f"Remaining chunks: {len(chunks)} ({(total-start_byte)/1024/1024:.1f} MB)")

    # 用线程池并发下载
    buf = [None] * len(chunks)
    threads = []
    t0 = time.time()
    completed = [0]
    lock = threading.Lock()

    def worker(idx):
        s, e = chunks[idx]
        download_chunk(s, e, buf, idx)
        with lock:
            completed[0] += 1
            if completed[0] % 10 == 0 or completed[0] == len(chunks):
                elapsed = time.time() - t0
                done_bytes = sum(len(b) for b in buf if b is not None)
                speed = done_bytes / 1024 / 1024 / elapsed if elapsed > 0 else 0
                print(f"  {completed[0]}/{len(chunks)} chunks, "
                      f"{done_bytes/1024/1024:.1f} MB downloaded, "
                      f"{speed:.2f} MB/s", flush=True)

    # 限制并发数
    sem = threading.Semaphore(NUM_THREADS)

    def worker_limited(idx):
        with sem:
            worker(idx)

    for i in range(len(chunks)):
        t = threading.Thread(target=worker_limited, args=(i,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # 检查是否有失败的块
    failed = [i for i, b in enumerate(buf) if b is None]
    if failed:
        print(f"WARNING: {len(failed)} chunks failed, retrying sequentially...")
        for idx in failed:
            s, e = chunks[idx]
            ok = False
            for attempt in range(3):
                n = download_chunk(s, e, buf, idx)
                if n > 0:
                    ok = True
                    break
                time.sleep(2)
            if not ok:
                print(f"  chunk {idx} still failed after retries!")

    # 追加写入文件
    with open(OUT, "ab") as f:
        for i, data in enumerate(buf):
            if data is not None:
                f.write(data)

    final = os.path.getsize(OUT)
    print(f"\nDownload complete. Final size: {final/1024/1024:.1f} MB")
    if final == total:
        print("Size matches expected.")
    else:
        print(f"WARNING: size mismatch (expected {total}, got {final})")


if __name__ == "__main__":
    main()
