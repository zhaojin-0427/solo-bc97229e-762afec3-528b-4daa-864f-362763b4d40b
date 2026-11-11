"""作业子进程入口：读输入文件 -> 执行溯源 -> 原子写结果文件。

由执行器（worker.py）以独立进程启动：

    python -m lineage.job_child <input.json> <output.json>

- 输入文件：{"schema": ..., "sql": ..., "delay": <演示用人工耗时秒数>}
- 输出文件：{"ok": true, "result": ...} 或 {"ok": false, "error": ...}
- 子进程不接触作业数据库，作业状态只由执行器进程写入；
  因此子进程可随时被 SIGKILL（取消/超时），不会留下半成品状态。
"""

import json
import os
import sys
import time

from . import analyze
from .errors import LineageError


def _interruptible_sleep(seconds):
    """可被打断的占位耗时：模拟真实环境中的耗时 CTE 分析。"""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def run(input_path, output_path):
    with open(input_path, encoding="utf-8") as f:
        payload = json.load(f)

    delay = float(payload.get("delay") or 0)
    if delay > 0:
        _interruptible_sleep(delay)

    try:
        result = analyze(payload["schema"], payload["sql"])
        envelope = {"ok": True, "result": result}
    except LineageError as exc:
        envelope = {"ok": False, "error": str(exc)}
    except Exception as exc:  # pragma: no cover - 防御性兜底
        envelope = {"ok": False,
                    "error": f"分析失败：{type(exc).__name__}: {exc}"}

    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False)
    os.replace(tmp_path, output_path)


def main():
    if len(sys.argv) != 3:
        print("usage: python -m lineage.job_child <input.json> <output.json>",
              file=sys.stderr)
        sys.exit(2)
    run(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
