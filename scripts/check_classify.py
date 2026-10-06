"""快速单测：验证 DSL 里「输入归一化」节点的分类逻辑（不调模型）。

分类判定已经从提示词挪到代码，这个脚本用来确认代码分支都覆盖到了。
"""

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "dify" / "taobao-monitor-v3.yml"

CASES = [
    ({"item_id": "A", "role": "self", "field": "price", "old_value": 89.0, "new_value": 80.1}, "自有降价异常"),
    ({"item_id": "A", "role": "self", "field": "price", "old_value": 89.0, "new_value": 85.0}, "自有普通调价"),
    ({"item_id": "A", "role": "competitor", "field": "price", "old_value": 79.0, "new_value": 71.1}, "竞品降价"),
    ({"item_id": "A", "role": "self", "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf"}, "自有下架"),
    ({"item_id": "A", "role": "competitor", "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf"}, "竞品下架"),
    ({"item_id": "A", "role": "self", "field": "stock", "old_value": 10, "new_value": 0}, "自有断货"),
    ({"item_id": "A", "role": "competitor", "field": "stock", "old_value": 10, "new_value": 0}, "竞品断货"),
    ({"item_id": "A", "role": "self", "field": "main_image", "old_value": "v1", "new_value": "v2"}, "主图换版"),
    ({"item_id": "A", "role": "self", "field": "title", "old_value": "x", "new_value": "y"}, "标题改卖点词"),
    ({"item_id": "A", "role": "self", "field": "promotion", "old_value": "满减", "new_value": "秒杀"}, "促销机制变化"),
    ({"item_id": "A", "role": "self", "field": "rating", "old_value": 4.8, "new_value": 4.4}, "评分下滑"),
    ({"item_id": "A", "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf"}, "归属未知"),
]


def main() -> int:
    dsl = yaml.safe_load(DSL.read_text(encoding="utf-8"))
    graph = dsl["workflow"]["graph"]
    print(f"YAML OK: 节点 {len(graph['nodes'])}，连线 {len(graph['edges'])}")

    node = next(n for n in graph["nodes"] if n["data"].get("title") == "输入归一化")
    ns: dict = {}
    exec(compile(node["data"]["code"], "<normalize-node>", "exec"), ns)  # noqa: S102
    run = ns["main"]

    passed = 0
    for ev, want in CASES:
        out = run(diff_events=json.dumps([ev], ensure_ascii=False))
        got = json.loads(out["events_json"])[0]["category"]
        ok = got == want
        passed += ok
        print(f"  [{'OK ' if ok else 'BAD'}] role={ev.get('role', '(缺失)'):<11} "
              f"field={ev['field']:<14} -> {got:<12} (期望 {want})")

    print()
    print(f"分类逻辑: {passed}/{len(CASES)} 通过")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
