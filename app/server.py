"""Flask Web 入口：提供溯源页面与 /api/analyze JSON 接口。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request

from lineage import analyze
from lineage.demo_data import DEMO_SCHEMA, DEMO_SQL
from lineage.errors import LineageError

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html",
                           demo_schema=DEMO_SCHEMA,
                           demo_sql=DEMO_SQL)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(silent=True) or {}
    schema_text = payload.get("schema", "")
    sql_text = payload.get("sql", "")
    try:
        result = analyze(schema_text, sql_text)
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return jsonify({"ok": False,
                        "error": f"分析失败：{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "result": result})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
