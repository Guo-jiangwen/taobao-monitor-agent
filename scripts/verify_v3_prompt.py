"""用本地 Ollama(qwen2.5:7b) 回归验证 v3 提示词能不能稳定吐出合法 JSON。

为什么要这个脚本：
提示词不是写完就完事的。模型升级、提示词微调、换模型，都可能让输出格式飘 ——
而一次飘了，下游的条件分支/通知推送就直接漏告警。
改完提示词跑一遍这条脚本，比在 Dify 界面里手动试方便，也留了版本对比的证据。

用法：
    python scripts/verify_v3_prompt.py            # 跑全部样例
    python scripts/verify_v3_prompt.py --model qwen2.5:7b
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (ROOT / "prompts" / "analysis_v3.txt").read_text(encoding="utf-8")

OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"

# role 必须由采集器必传：缺了它，模型会自己脑补归属，
# 把「竞品下架」判成「自有下架」，动作从「接手流量」变成「恢复上架」——方向全反。
DOUBLE_EVENTS = [
    {
        "item_id": "SAMPLE-001", "source_id": "sample_local", "role": "self", "severity": "P0",
        "field": "price", "old_value": 89.0, "new_value": 80.1,
        "event": "价格变动 -10.0%",
    },
    {
        "item_id": "SAMPLE-009", "source_id": "sample_local", "role": "competitor", "severity": "P0",
        "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf",
        "event": "商品下架",
    },
]

CASES: list[tuple[str, object]] = [
    ("双事件（正常告警）", json.dumps(DOUBLE_EVENTS, ensure_ascii=False)),
    ("空事件", "[]"),
    ("单条竞品降价", json.dumps([
        {"item_id": "SAMPLE-009", "source_id": "sample_local", "role": "competitor",
         "severity": "P1", "field": "price", "old_value": 128.0, "new_value": 119.0,
         "event": "价格变动 -7.0%"},
    ], ensure_ascii=False)),
    ("role 缺失（考验模型会不会脑补归属）", json.dumps([
        {"item_id": "SAMPLE-009", "source_id": "sample_local", "severity": "P0",
         "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf",
         "event": "商品下架"},
    ], ensure_ascii=False)),
]


def chat(model: str, system: str, user: str) -> str:
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "options": {"temperature": 0.2},
    }
    req = urllib.request.Request(
        OLLAMA_CHAT, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


def normalize(input_text: str) -> str:
    """复刻 Dify「代码执行」节点对输入的兜底：role 缺失一律标 unknown。

    为什么必须先补再喂模型：
    归属是确定性判断，属于代码节点的活，不该让模型去猜。猜错会让动作方向整个反过来
    （自有下架要恢复上架，竞品下架要接手流量）。本地不过这一层，验的就不是生产行为了。
    """
    try:
        data = json.loads(input_text)
    except (json.JSONDecodeError, TypeError):
        return input_text
    if isinstance(data, list):
        for ev in data:
            if isinstance(ev, dict) and not ev.get("role"):
                ev["role"] = "unknown"
    return json.dumps(data, ensure_ascii=False)


def tolerant_parse(text: str) -> tuple[bool, object]:
    """复刻 Dify 代码执行节点里的容错解析：先抓第一个 {} 块，再 json.loads。"""
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return False, "没找到任何 JSON 块"
    try:
        return True, json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return False, f"JSON 解析失败: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="qwen2.5:7b")
    args = parser.parse_args()

    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=5) as resp:
            available = {m["name"] for m in json.loads(resp.read().decode("utf-8"))["models"]}
    except (urllib.error.URLError, OSError) as exc:
        print(f"[跳过] 本地 Ollama 没起来：{exc}")
        print("把脚本里的 OLLAMA 地址改成你实际用的模型服务即可。")
        return 0

    if not any(args.model in name for name in available):
        print(f"[跳过] 模型 {args.model} 不在本地，可用：{sorted(available)}")
        return 0

    print(f"模型: {args.model}\n提示词: prompts/analysis_v3.txt\n" + "=" * 60)

    failures = 0
    for name, user_input in CASES:
        print(f"\n### {name}")
        raw = chat(args.model, SYSTEM_PROMPT, f"本轮监控采集到的变化事件如下：\n\n{normalize(user_input)}\n\n按系统提示词的要求输出 JSON。")
        print("--- 原始输出 ---")
        print(raw.strip()[:900])

        ok, parsed = tolerant_parse(raw)
        print(f"--- 容错解析: {'通过' if ok else '失败'} ---")
        if not ok:
            failures += 1
            continue
        if isinstance(parsed, dict):
            events = parsed.get("events") or []
            cats = [e.get("category") for e in events if isinstance(e, dict)]
            print(f"事件数={len(events)} category={cats} summary={parsed.get('summary')}")

            # 复刻代码执行节点：need_push 是纯规则，由 severity / priority_keep 推导
            pushed = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                keep = ev.get("priority_keep")
                is_p0 = str(ev.get("severity", "")).upper() == "P0"
                expect = is_p0 or keep is False
                if expect:
                    pushed.append(ev.get("item_id"))
                # 模型如果自己输出了 need_push，且跟规则算出来的不一致，就是隐患
                self_set = ev.get("need_push")
                flag = ""
                if self_set is not None and bool(self_set) != expect:
                    flag = "  <<< 模型自判 need_push 与规则冲突！下游会漏告警"
                print(f"  - {ev.get('item_id')} [{ev.get('category')}] "
                      f"action={ev.get('action')} | 规则判推送={'是' if expect else '否'}{flag}")
            print(f"  汇总 need_push={'是' if pushed else '否'} pushed_items={pushed}")

    print("\n" + "=" * 60)
    print(f"结论：{'全部通过' if failures == 0 else f'{failures} 个样例的输出不是合法 JSON，需要先修提示词'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
