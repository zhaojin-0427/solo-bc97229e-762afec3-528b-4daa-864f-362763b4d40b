"""执行器进程：全部署唯一，串行消耗作业队列（唯一的执行名额）。

职责：
- 启动恢复：把上次遗留的 running 作业标记为 interrupted，队列其余作业
  保持 queued 并继续按提交顺序执行；
- 每个作业派生独立子进程（lineage.job_child）执行真正的溯源计算，
  因此取消/超时时可以直接 SIGKILL，立刻停止实际计算并释放名额；
- 取消：Web 侧把 cancel_requested 置 1，本进程轮询发现后杀掉子进程，
  作业落为 cancelled；
- 超时：执行超过 LINEAGE_JOB_TIMEOUT（默认 30 秒）即杀掉子进程，
  作业落为 timeout；
- 迟到结果：终态迁移是条件 UPDATE，若作业已被先行落为终态，
  结果直接丢弃，不会改写终态。

运行：python worker.py   （环境变量见 README）
"""

import json
import os
import signal
import subprocess
import sys
import time

from jobstore import (JobStore, STATUS_CANCELLED, STATUS_FAILED,
                      STATUS_SUCCEEDED, STATUS_TIMEOUT)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
POLL_INTERVAL = float(os.environ.get("LINEAGE_WORKER_POLL", "0.2"))
JOB_TIMEOUT = float(os.environ.get("LINEAGE_JOB_TIMEOUT", "30"))
JOB_DELAY = float(os.environ.get("LINEAGE_JOB_DELAY", "0"))

_current_proc = None  # 当前作业子进程，供信号处理清理


def _on_term(signum, frame):
    """收到终止信号：杀掉当前子进程后退出。

    遗留的 running 作业由下一个执行器实例在启动时标记为 interrupted。
    """
    if _current_proc is not None and _current_proc.poll() is None:
        _current_proc.kill()
    sys.exit(128 + signum)


def log(msg):
    print(f"[worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def tmp_dir(store):
    return os.path.join(os.path.dirname(os.path.abspath(store.db_path)), "tmp")


def cleanup_tmp(store):
    """清理上次运行遗留的临时输入/输出文件。"""
    path = tmp_dir(store)
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        try:
            os.unlink(os.path.join(path, name))
        except OSError:
            pass


def _outcome_from_files(output_path, returncode, stderr):
    """子进程已退出：从输出文件还原 (status, error, result)。"""
    try:
        with open(output_path, encoding="utf-8") as f:
            envelope = json.load(f)
    except OSError:
        tail = (stderr or "").strip().splitlines()
        detail = tail[-1] if tail else "无输出"
        return STATUS_FAILED, f"执行子进程异常退出（退出码 {returncode}）：{detail}", None
    except json.JSONDecodeError:
        return STATUS_FAILED, "执行子进程输出了无法解析的结果", None
    if envelope.get("ok"):
        return STATUS_SUCCEEDED, None, envelope.get("result")
    return STATUS_FAILED, envelope.get("error") or "未知错误", None


def execute_job(store, job, timeout=JOB_TIMEOUT, delay=JOB_DELAY):
    """执行单个作业：派生子进程 -> 监控取消/超时/退出 -> 条件落终态。"""
    job_id = job["id"]
    workdir = tmp_dir(store)
    os.makedirs(workdir, exist_ok=True)
    input_path = os.path.join(workdir, f"job-{job_id}.input.json")
    output_path = os.path.join(workdir, f"job-{job_id}.output.json")
    with open(input_path, "w", encoding="utf-8") as f:
        json.dump({"schema": job["schema_text"], "sql": job["sql_text"],
                   "delay": delay}, f, ensure_ascii=False)

    global _current_proc
    proc = subprocess.Popen(
        [sys.executable, "-m", "lineage.job_child", input_path, output_path],
        cwd=APP_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True)
    _current_proc = proc
    start = time.monotonic()
    reason = None
    while True:
        if store.cancel_requested(job_id):
            reason = "cancelled"
            break
        if time.monotonic() - start >= timeout:
            reason = "timeout"
            break
        if proc.poll() is not None:
            reason = "exited"
            break
        time.sleep(0.1)

    if reason in ("cancelled", "timeout"):
        proc.kill()  # 停止实际计算，立即释放执行名额
    _, stderr = proc.communicate()
    _current_proc = None

    if reason == "cancelled":
        status, error, result = STATUS_CANCELLED, "用户在执行中取消了作业", None
    elif reason == "timeout":
        status, error, result = (
            STATUS_TIMEOUT, f"执行超过 {timeout:g} 秒，已强制中止", None)
    else:
        status, error, result = _outcome_from_files(
            output_path, proc.returncode, stderr)

    for path in (input_path, output_path):
        try:
            os.unlink(path)
        except OSError:
            pass

    applied = store.finish_job(job_id, status, error=error, result=result)
    elapsed = time.monotonic() - start
    if applied:
        log(f"作业 {job_id} -> {status}（耗时 {elapsed:.1f}s）"
            + (f"：{error}" if error else ""))
    else:
        # 作业已被其它迁移落为终态（如几乎同时的取消），迟到结果丢弃
        log(f"作业 {job_id} 已处于终态，迟到结果已丢弃（本次判定 {status}）")
    return applied


def run_once(store, timeout=JOB_TIMEOUT, delay=JOB_DELAY):
    """认领并执行一个作业；返回是否有作业被执行。"""
    store.sweep_cancelled_queued()
    job = store.claim_next()
    if job is None:
        return False
    log(f"开始执行作业 {job['id']}")
    execute_job(store, job, timeout=timeout, delay=delay)
    return True


def run_forever(store):
    recovered = store.recover_interrupted()
    if recovered:
        log(f"启动恢复：{recovered} 个执行中作业已标记为中断（interrupted）")
    cleanup_tmp(store)
    log(f"执行器就绪：每 {POLL_INTERVAL}s 轮询，单作业超时 {JOB_TIMEOUT:g}s"
        + (f"，演示人工耗时 {JOB_DELAY:g}s" if JOB_DELAY > 0 else ""))
    while True:
        if not run_once(store):
            time.sleep(POLL_INTERVAL)


def main():
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    run_forever(JobStore())


if __name__ == "__main__":
    main()
