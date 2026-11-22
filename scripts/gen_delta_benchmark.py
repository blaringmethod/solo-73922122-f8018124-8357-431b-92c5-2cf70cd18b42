#!/usr/bin/env python3
"""生成差量存储基准目录 CSV：同一 URL 的 N 次抓存，每次 40 条等长出链，
相邻两次只换一条（滑动窗口）。默认 120 版，与 tests/test_delta.py 同源。

用法：python3 scripts/gen_delta_benchmark.py [输出路径] [版本数]
"""
import csv
import json
import sys
from datetime import datetime, timedelta, timezone

URL = "http://bench.site/page"
N_LINKS = 40
BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def links(i):
    return [f"http://bench.site/l{k:04d}" for k in range(i, i + N_LINKS)]


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "delta_benchmark.csv"
    versions = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "captured_at", "outlinks"])
        for i in range(versions):
            ts = (BASE + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            w.writerow([URL, ts, "|".join(links(i))])
    full = sum(len(json.dumps(links(i), ensure_ascii=False)) for i in range(versions))
    print(f"已生成 {out}：{versions} 版 × {N_LINKS} 条等长出链，"
          f"逐版完整 JSON 基线 {full} 字节")


if __name__ == "__main__":
    main()
