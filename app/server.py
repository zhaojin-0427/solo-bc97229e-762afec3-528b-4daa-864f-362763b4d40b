"""Flask Web 入口：作业式溯源分析。

页面与接口只读写作业队列（SQLite），真正的计算由唯一的执行器进程
（worker.py）完成：所有 Web 进程共用一个执行名额，因此计算期间
页面与状态查询始终可响应。

接口：
- POST   /api/jobs            提交作业（固定源表说明与 SQL），返回作业号
- GET    /api/jobs            作业列表（状态、排队位置、取消标记）
- GET    /api/jobs/<id>       作业详情（状态、失败原因、输入、逐列结果）
- POST   /api/jobs/<id>/cancel  取消（排队中直接取消；执行中请求中止）
- POST   /api/jobs/<id>/retry   以原输入重试为新作业（仅终态作业）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request

from jobstore import JobStore
from lineage.demo_data import DEMO_SCHEMA, DEMO_SQL

app = Flask(__name__)
store = JobStore()


def _brief(job):
    return {
        "id": job["id"],
        "status": job["status"],
        "error": job["error"],
        "cancel_requested": bool(job["cancel_requested"]),
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "queue_ahead": job.get("queue_ahead"),
        "sql_preview": " ".join(job["sql_text"].split())[:80],
    }


def _detail(job):
    data = _brief(job)
    data["schema"] = job["schema_text"]
    data["sql"] = job["sql_text"]
    data["result"] = (json.loads(job["result_json"])
                      if job["result_json"] else None)
    return data


@app.route("/")
def index():
    return render_template("index.html",
                           demo_schema=DEMO_SCHEMA,
                           demo_sql=DEMO_SQL)


@app.post("/api/jobs")
def submit_job():
    payload = request.get_json(silent=True) or {}
    schema_text = (payload.get("schema") or "").strip()
    sql_text = (payload.get("sql") or "").strip()
    if not schema_text or not sql_text:
        return jsonify({"ok": False, "error": "源表结构与 SQL 均不能为空"}), 400
    job_id = store.create_job(schema_text, sql_text)
    return jsonify({"ok": True, "job_id": job_id, "status": "queued"}), 202


@app.get("/api/jobs")
def list_jobs():
    jobs, running_id = store.list_jobs()
    return jsonify({
        "ok": True,
        "running_id": running_id,
        "jobs": [_brief(j) for j in jobs],
    })


@app.get("/api/jobs/<int:job_id>")
def job_detail(job_id):
    job = store.get_job(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "作业不存在"}), 404
    return jsonify({"ok": True, "job": _detail(job)})


@app.post("/api/jobs/<int:job_id>/cancel")
def cancel_job(job_id):
    code, message = store.cancel_job(job_id)
    if code == "not_found":
        return jsonify({"ok": False, "error": message}), 404
    if code == "terminal":
        return jsonify({"ok": False, "error": message}), 409
    return jsonify({"ok": True, "status": code, "message": message})


@app.post("/api/jobs/<int:job_id>/retry")
def retry_job(job_id):
    new_id, error = store.retry_job(job_id)
    if error:
        status = 404 if "不存在" in error else 409
        return jsonify({"ok": False, "error": error}), status
    return jsonify({"ok": True, "job_id": new_id, "status": "queued"}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
