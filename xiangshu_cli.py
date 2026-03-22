#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
象数功效查询 CLI 工具
用法：
  python3 xiangshu_cli.py symptom <关键词>
  python3 xiangshu_cli.py number <象数>
  python3 xiangshu_cli.py list [--limit N]
  python3 xiangshu_cli.py export [输出路径]   # 将PDF解析结果导出为JSON
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xiangshu_core import XiangShuQuery

_DIR = os.path.dirname(os.path.abspath(__file__))
PDF = os.path.join(_DIR, "八卦象数疗法大全（9.4）.pdf")
# 可通过 XIANGSHU_DATA 环境变量指定预解析 JSON 路径（用于 skill 分发）
DATA_JSON = os.environ.get("XIANGSHU_DATA", os.path.join(_DIR, "xiangshu_data.json"))


def _load():
    # 优先使用预解析 JSON（无需 PDF，便于 skill 分发）
    if os.path.exists(DATA_JSON):
        return XiangShuQuery.load_from_json(DATA_JSON)
    if not os.path.exists(PDF):
        print(json.dumps({"error": f"找不到数据文件：{DATA_JSON} 或 {PDF}"}, ensure_ascii=False))
        sys.exit(1)
    return XiangShuQuery(PDF)


def cmd_symptom(keywords: str, limit: int = 15):
    q = _load()
    results = q.search_by_keyword(keywords)
    total = len(results)
    out = []
    for xs, entry in results[:limit]:
        out.append({
            "xiangshu": xs,
            "matched_symptoms": entry.get("matched_symptoms", entry["symptoms"]),
            "notes": entry.get("notes", []),
            "pages": entry["pages"],
        })
    print(json.dumps({"total": total, "results": out}, ensure_ascii=False, indent=2))


def cmd_number(xiangshu: str):
    q = _load()
    r = q.search_by_number(xiangshu)
    if r:
        print(json.dumps({
            "found": True,
            "xiangshu": xiangshu,
            "symptoms": r["symptoms"],
            "notes": r.get("notes", []),
            "pages": r["pages"],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"found": False, "xiangshu": xiangshu}, ensure_ascii=False))


def cmd_list(limit: int = 20):
    q = _load()
    items = q.list_all(limit)
    out = [{"xiangshu": xs, "symptoms": e["symptoms"][:3]} for xs, e in items]
    print(json.dumps({"total": len(q.xiangshu_data), "results": out},
                     ensure_ascii=False, indent=2))


def cmd_export(out_path: str = DATA_JSON):
    """将 PDF 解析结果导出为 JSON，供 skill 使用（无需 PDF）。"""
    if not os.path.exists(PDF):
        print(json.dumps({"error": f"找不到PDF：{PDF}"}, ensure_ascii=False))
        sys.exit(1)
    q = XiangShuQuery(PDF)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'version': 2, 'data': q.xiangshu_data}, f,
                  ensure_ascii=False, separators=(',', ':'))
    print(f"[导出] 已写入 {out_path}，共 {len(q.xiangshu_data)} 条象数", file=sys.stderr)
    print(json.dumps({"exported": out_path, "count": len(q.xiangshu_data)}, ensure_ascii=False))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd == "symptom" and len(args) >= 2:
        cmd_symptom(" ".join(args[1:]))
    elif cmd == "number" and len(args) >= 2:
        cmd_number(" ".join(args[1:]))
    elif cmd == "list":
        limit = int(args[1]) if len(args) > 1 else 20
        cmd_list(limit)
    elif cmd == "export":
        out = args[1] if len(args) > 1 else DATA_JSON
        cmd_export(out)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        sys.exit(1)
