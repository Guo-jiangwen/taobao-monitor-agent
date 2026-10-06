#!/usr/bin/env python
"""监控 CLI。

用法：
    python main.py once            # 跑一轮采集（cron 调用）
    python main.py once --cycles 3 # 连跑 3 轮，用来验证「首轮建基线、次轮出 diff」
    python main.py once --cycles 4 --no-dedupe
                                   # 调试用：关掉去重缓存强制每轮真取，
                                   # 否则同一进程内连跑会全部命中缓存、diff 恒为空
    python main.py cron            # 常驻，按 config.json 里的 cron_expr 轮询执行
    python main.py report          # 打印最近变化事件与健康报告
    python main.py resume <source> # 人工确认后恢复被自动暂停的源
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.pipeline import MonitorPipeline  # noqa: E402


def _print_report(pipeline: MonitorPipeline, limit: int = 30) -> None:
    events = pipeline.repo.recent_events(limit)
    print("=== 最近变化事件 ===")
    if not events:
        print("（无）")
    for e in events:
        print(f"[{e['severity']}] {e['item_id']} {e['field']}: {e['old_value']} -> {e['new_value']} | {e['event']}")
    print("=== 采集健康 ===")
    print(json.dumps(pipeline.governor.health_report(), ensure_ascii=False, indent=2))


def _cycles() -> int:
    """--cycles N：连跑 N 轮，用来验证首轮建基线、次轮出 diff。

    顺序扫描而不是 zip 两两配对：命令行里混进 --no-dedupe 这类单值 flag 后，
    zip(args[::2], args[1::2]) 会整体错位，把 flag 名当成值解析。
    """
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == "--cycles" and i + 1 < len(args):
            return max(1, int(args[i + 1]))
    return 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    no_dedupe = "--no-dedupe" in sys.argv
    pipeline = MonitorPipeline(dedupe_ttl=0 if no_dedupe else None)


    if cmd == "once":
        for i in range(_cycles()):
            out = pipeline.collect_cycle()
            print(f"--- cycle {i + 1} ---")
            print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if cmd == "cron":
        expr = pipeline.cfg.get("scheduler", {}).get("cron_expr", "17 * * * *")
        interval = int(pipeline.cfg.get("scheduler", {}).get("interval_seconds", 3600))
        print(f"常驻监控启动，调度式 {expr}，间隔 {interval}s；Ctrl+C 退出", file=sys.stderr)
        try:
            while True:
                out = pipeline.collect_cycle()
                print(f"[{time.strftime('%F %T')}] {out['stats']}", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("已退出", file=sys.stderr)
        return 0

    if cmd == "report":
        _print_report(pipeline)
        return 0

    if cmd == "resume":
        source = sys.argv[2] if len(sys.argv) > 2 else ""
        pipeline.governor.resume(source)
        print(f"{source} 已恢复")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
