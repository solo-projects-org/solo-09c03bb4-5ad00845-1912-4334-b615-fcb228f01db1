"""公交清分数仓 · 换乘标志口径调整影响分析 Web 应用。"""
from __future__ import annotations

import json
import os

from flask import Flask, jsonify, render_template, request

from lineage import AnalysisError, analyze, build_models

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(BASE_DIR, "data", "sample_clearing.json")
SHADOW_SAMPLE_PATH = os.path.join(BASE_DIR, "data", "sample_shadow_cte.json")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB


@app.errorhandler(AnalysisError)
def handle_analysis_error(exc: AnalysisError):
    return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(413)
def handle_too_large(_exc):
    return jsonify({"ok": False, "error": "上传文件超过 8 MB 限制。"}), 400


def _parse_payload(raw: bytes) -> dict:
    if not raw.strip():
        raise AnalysisError("上传内容为空，请选择包含 source_tables 和 models 的 JSON 文件。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AnalysisError("文件不是 UTF-8 编码，请另存为 UTF-8 后重试。") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise AnalysisError("JSON 顶层必须是对象，包含 source_tables 和 models 两个数组。")
    return payload


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/sample")
def sample():
    with open(SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.get("/api/sample/shadow-cte")
def shadow_sample():
    """扣款/退款同名 CTE（外层 WITH 与派生表内层 WITH）作用域样例。"""
    with open(SHADOW_SAMPLE_PATH, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return jsonify(payload)


@app.post("/api/validate")
def validate():
    """校验上传的 JSON 并返回字段清单，供前端选择待改字段。"""
    payload = _parse_payload(request.get_data())
    source_schemas, infos, _ = build_models(payload)
    return jsonify({
        "ok": True,
        "source_tables": [
            {"table": t, "columns": cols} for t, cols in source_schemas.items()
        ],
        "models": [
            {"name": name, "upstream": sorted(info.upstream)}
            for name, info in infos.items()
        ],
        # 跨作用域同名 CTE：每个名字分别在哪层定义（同一 WITH 重名会直接 400）
        "shadowed_ctes": [
            {"model": name, **item}
            for name, info in infos.items() for item in info.shadowed_ctes
        ],
    })


@app.post("/api/analyze")
def run_analyze():
    payload = _parse_payload(request.get_data())
    target_table = request.args.get("table", "").strip()
    target_column = request.args.get("column", "").strip()
    if not target_table or not target_column:
        raise AnalysisError("请先选择待调整的源表字段。")
    result = analyze(payload, target_table, target_column)
    return jsonify({"ok": True, "result": result})


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=8000)
