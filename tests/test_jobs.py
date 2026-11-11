"""作业队列 / 执行器测试：仅依赖 Python 标准库。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import worker  # noqa: E402
from jobstore import JobStore  # noqa: E402

SCHEMA = "customer(cust_id, name, mobile*, city)\norders(order_id, cust_id, amount)\n"
SQL = ("WITH c AS (SELECT cust_id, mobile FROM customer WHERE city = 'BJ') "
       "SELECT cust_id, mobile FROM c")


class JobStoreTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(os.path.join(self._tmp.name, "jobs.db"))

    # ---- 提交与查询 ----

    def test_create_fixes_inputs_and_returns_id(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.assertIsInstance(job_id, int)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["schema_text"], SCHEMA)
        self.assertEqual(job["sql_text"], SQL)
        self.assertFalse(job["cancel_requested"])
        self.assertIsNone(self.store.get_job(9999))

    def test_list_order_and_queue_ahead(self):
        ids = [self.store.create_job(SCHEMA, f"SELECT {i} AS n FROM customer")
               for i in range(3)]
        jobs, running_id = self.store.list_jobs()
        self.assertIsNone(running_id)
        self.assertEqual([j["id"] for j in jobs], list(reversed(ids)))  # 展示倒序
        by_id = {j["id"]: j for j in jobs}
        self.assertEqual(by_id[ids[0]]["queue_ahead"], 0)
        self.assertEqual(by_id[ids[2]]["queue_ahead"], 2)

    # ---- 认领（FIFO / 单名额）----

    def test_claim_fifo_and_single_slot(self):
        first = self.store.create_job(SCHEMA, "SELECT cust_id FROM customer")
        second = self.store.create_job(SCHEMA, "SELECT name FROM customer")
        job = self.store.claim_next()
        self.assertEqual(job["id"], first)
        self.assertEqual(job["status"], "running")
        # 同一时刻只能再认领到下一个；running 不会被重复认领
        job2 = self.store.claim_next()
        self.assertEqual(job2["id"], second)
        self.assertIsNone(self.store.claim_next())
        _, running_id = self.store.list_jobs()
        self.assertEqual(running_id, first)

    def test_claim_skips_cancelled(self):
        first = self.store.create_job(SCHEMA, "SELECT cust_id FROM customer")
        second = self.store.create_job(SCHEMA, "SELECT name FROM customer")
        self.store.cancel_job(first)
        job = self.store.claim_next()
        self.assertEqual(job["id"], second)

    # ---- 取消 ----

    def test_cancel_queued_is_terminal(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        code, _ = self.store.cancel_job(job_id)
        self.assertEqual(code, "cancelled")
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNotNone(job["finished_at"])

    def test_cancel_running_marks_flag(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.store.claim_next()
        code, _ = self.store.cancel_job(job_id)
        self.assertEqual(code, "cancelling")
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "running")  # 终态由执行器落定
        self.assertTrue(job["cancel_requested"])

    def test_cancel_terminal_and_missing_rejected(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.store.cancel_job(job_id)
        code, _ = self.store.cancel_job(job_id)
        self.assertEqual(code, "terminal")
        code, _ = self.store.cancel_job(9999)
        self.assertEqual(code, "not_found")

    # ---- 终态不可改写（迟到结果）----

    def test_finish_is_conditional_late_result_dropped(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.store.claim_next()
        ok = self.store.finish_job(job_id, "succeeded", result={"outputs": [1]})
        self.assertTrue(ok)
        # 迟到的第二次写入（如取消后子进程才返回）不得改写终态
        ok = self.store.finish_job(job_id, "failed", error="late")
        self.assertFalse(ok)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertIsNone(job["error"])

    def test_cancelled_job_rejects_late_finish(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.store.claim_next()
        self.store.cancel_job(job_id)
        self.store.finish_job(job_id, "cancelled", error="用户在执行中取消了作业")
        ok = self.store.finish_job(job_id, "succeeded", result={"outputs": []})
        self.assertFalse(ok)
        self.assertEqual(self.store.get_job(job_id)["status"], "cancelled")

    # ---- 重启恢复 ----

    def test_recover_marks_running_interrupted_keeps_queue(self):
        first = self.store.create_job(SCHEMA, "SELECT cust_id FROM customer")
        second = self.store.create_job(SCHEMA, "SELECT name FROM customer")
        third = self.store.create_job(SCHEMA, "SELECT city FROM customer")
        self.store.claim_next()  # first -> running
        self.store.finish_job(second, "cancelled")  # 不影响：second 仍 queued
        recovered = self.store.recover_interrupted()
        self.assertEqual(recovered, 1)
        self.assertEqual(self.store.get_job(first)["status"], "interrupted")
        # 队列顺序保留：接下来认领到的仍是 second
        job = self.store.claim_next()
        self.assertEqual(job["id"], second)
        self.store.finish_job(second, "succeeded", result={})
        job = self.store.claim_next()
        self.assertEqual(job["id"], third)

    # ---- 重试 ----

    def test_retry_interrupted_creates_new_job_with_same_inputs(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        self.store.claim_next()
        self.store.recover_interrupted()
        new_id, err = self.store.retry_job(job_id)
        self.assertIsNone(err)
        self.assertGreater(new_id, job_id)
        new_job = self.store.get_job(new_id)
        self.assertEqual(new_job["status"], "queued")
        self.assertEqual(new_job["schema_text"], SCHEMA)
        self.assertEqual(new_job["sql_text"], SQL)

    def test_retry_non_terminal_rejected(self):
        job_id = self.store.create_job(SCHEMA, SQL)
        new_id, err = self.store.retry_job(job_id)
        self.assertIsNone(new_id)
        self.assertIn("不能重试", err)
        new_id, err = self.store.retry_job(9999)
        self.assertIsNone(new_id)
        self.assertIn("不存在", err)


class WorkerExecuteTestCase(unittest.TestCase):
    """执行器端到端：真实派生子进程跑溯源引擎。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(os.path.join(self._tmp.name, "jobs.db"))

    def _submit_and_claim(self, sql=SQL, schema=SCHEMA):
        job_id = self.store.create_job(schema, sql)
        return self.store.claim_next()

    def test_execute_success(self):
        job = self._submit_and_claim()
        applied = worker.execute_job(self.store, job, timeout=10, delay=0)
        self.assertTrue(applied)
        done = self.store.get_job(job["id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertIn('"outputs"', done["result_json"])
        self.assertIn("mobile", done["result_json"])

    def test_execute_lineage_error_becomes_failed(self):
        job = self._submit_and_claim(sql="SELECT nope FROM customer")
        worker.execute_job(self.store, job, timeout=10, delay=0)
        done = self.store.get_job(job["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("nope", done["error"])

    def test_execute_cancel_stops_computation(self):
        job = self._submit_and_claim()
        started = time.monotonic()
        holder = {}

        def run():
            holder["applied"] = worker.execute_job(
                self.store, job, timeout=30, delay=30)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.6)  # 子进程正在“耗时计算”
        code, _ = self.store.cancel_job(job["id"])
        self.assertEqual(code, "cancelling")
        t.join(timeout=10)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10)  # 实际计算被停止，名额立即释放
        done = self.store.get_job(job["id"])
        self.assertEqual(done["status"], "cancelled")
        self.assertIsNone(done["result_json"])

    def test_execute_timeout_stops_computation(self):
        job = self._submit_and_claim()
        started = time.monotonic()
        worker.execute_job(self.store, job, timeout=0.5, delay=30)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10)
        done = self.store.get_job(job["id"])
        self.assertEqual(done["status"], "timeout")
        self.assertIn("强制中止", done["error"])
        self.assertIsNone(done["result_json"])

    def test_run_once_picks_oldest_and_recovers(self):
        first = self.store.create_job(SCHEMA, "SELECT cust_id FROM customer")
        second = self.store.create_job(SCHEMA, SQL)
        self.store.claim_next()          # first 执行中
        self.store.recover_interrupted() # 模拟重启：first -> interrupted
        ran = worker.run_once(self.store, timeout=10, delay=0)
        self.assertTrue(ran)
        self.assertEqual(self.store.get_job(first)["status"], "interrupted")
        self.assertEqual(self.store.get_job(second)["status"], "succeeded")
        # 中断作业以原输入重试为新作业，随后被执行
        new_id, _ = self.store.retry_job(first)
        worker.run_once(self.store, timeout=10, delay=0)
        self.assertEqual(self.store.get_job(new_id)["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
