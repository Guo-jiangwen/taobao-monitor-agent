"""端到端验证 Dify DSL：把 dify/taobao-monitor-v3.yml 里的代码节点和提示词真跑一遍。

为什么需要这个：
DSL 里嵌的 Python 和提示词，只有在 Dify 里导入、跑起来才知道对不对。一旦错了，
你在界面里只能看到一个含糊的报错。这个脚本在本地把同一条链路复刻一遍：
  开始 → 输入归一化(Code) → LLM(Ollama) → 结果解析(Code) → 条件分支

它验证三件事：
1. 两个代码节点的 main() 真能跑通（Dify 的入口是 def main(参数名)，没有 __inputs）
2. LLM 提示词渲染后能拿到事件数据，且输出能被容错解析
3. 条件分支的 need_push 判定结果符合预期

用法：
    python scripts/verify_dify_dsl.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "dify" / "taobao-monitor-v3.yml"
OLLAMA_CHAT = "http://127.0.0.1:11434/api/chat"

CASES = [
    (
        "双事件：自有降价 P0 + 竞品下架 P0",
        [
            {"item_id": "SAMPLE-001", "role": "self", "severity": "P0", "field": "price",
             "old_value": 89.0, "new_value": 80.1, "event": "价格变动 -10.0%"},
            {"item_id": "SAMPLE-009", "role": "competitor", "severity": "P0",
             "field": "listing_status", "old_value": "on_sale", "new_value": "off_shelf",
             "event": "商品下架"},
        ],
        True,
    ),
    ("空事件", [], False),
    (
        "role 缺失（归一化节点应补成 unknown）",
        [{"item_id": "SAMPLE-009", "severity": "P0", "field": "listing_status",
          "old_value": "on_sale", "new_value": "off_shelf", "event": "商品下架"}],
        True,
    ),
    (
        "低优先级：竞品降价 P2",
        [{"item_id": "SAMPLE-009", "role": "competitor", "severity": "P2", "field": "price",
          "old_value": 128.0, "new_value": 124.0, "event": "价格变动 -3.1%"}],
        None,  # 不断言走向：模型有权推翻 P2 分级，推翻则按规则就该推送，两种都合理
    ),
]


def load_dsl() -> dict:
    import yaml

    return yaml.safe_load(DSL.read_text(encoding="utf-8"))


def extract_main(code: str):
    """把节点里的代码当模块跑，取出 main 函数（复刻 Dify 的调用约定）。"""
    ns: dict = {}
    exec(compile(code, "<dify-code-node>", "exec"), ns)  # noqa: S102
    if "main" not in ns:
        raise RuntimeError("代码节点里没有 main 函数 —— Dify 的入口必须是 def main(...)")
    return ns["main"]


def render(template: str, values: dict[str, str]) -> str:
    """渲染 Dify 的 {{#节点id.变量#}} 占位符。"""
    def sub(m: re.Match) -> str:
        key = f"{m.group(1)}.{m.group(2)}"
        if key not in values:
            raise RuntimeError(f"模板引用了不存在的变量：{key}")
        return values[key]

    return re.sub(r"\{\{#([^.]+)\.([^#]+)#\}\}", sub, template)


def call_llm(model: str, system: str, user: str) -> str:
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
        return json.loads(resp.read().decode("utf-8")).get("message", {}).get("content", "")


def check_parse_robustness(parse) -> int:
    """直接喂畸形输出给解析节点 —— 不靠模型碰运气，确定性地验容错逻辑。

    实测过模型把 summary 塞进 events 数组（生成没有 item_id 的伪元素），
    这种脏数据不报错但会污染事件数，所以必须有确定性的用例盯住。
    """
    print("\n### 解析节点容错（静态用例，不调模型）")
    bad_inputs = [
        ("summary 被塞进 events 数组",
         '{"events":[{"item_id":"A1","severity":"P2","category":"竞品降价","priority_keep":true},'
         '{"summary":"竞品降价，建议跟价"}],"summary":""}',
         1, "竞品降价，建议跟价"),
        ("被 ```json 包住",
         '```json\n{"events":[{"item_id":"A1","severity":"P0","category":"自有下架","priority_keep":true}]}\n```',
         1, ""),
        ("完全是废话",
         "我觉得这个商品最近表现不错，建议继续观察。",
         0, ""),
        ("events 不是数组",
         '{"events":"oops","summary":"x"}',
         0, "x"),
    ]
    failures = 0
    for name, raw, want_count, want_summary in bad_inputs:
        out = parse(llm_output=raw)
        got_count = out["event_count"]
        got_summary = out["summary"]
        ok = got_count == want_count and got_summary == want_summary
        print(f"  [{'通过' if ok else '失败'}] {name}: event_count={got_count}"
              f"（期望 {want_count}） summary={got_summary!r}（期望 {want_summary!r}）")
        if not ok:
            failures += 1
    return failures


def main() -> int:
    import urllib.error as ue

    dsl = load_dsl()
    nodes = {n["id"]: n["data"] for n in dsl["workflow"]["graph"]["nodes"]}

    code_nodes = {d["title"]: d for d in nodes.values() if d["type"] == "code"}
    normalize = extract_main(code_nodes["输入归一化"]["code"])
    parse = extract_main(code_nodes["结果解析与规则判定"]["code"])

    llm = next(d for d in nodes.values() if d["type"] == "llm")
    model = llm["model"]["name"]
    system_prompt = next(p["text"] for p in llm["prompt_template"] if p["role"] == "system")
    user_template = next(p["text"] for p in llm["prompt_template"] if p["role"] == "user")

    if_node = next(d for d in nodes.values() if d["type"] == "if-else")
    cond = if_node["cases"][0]["conditions"][0]

    print(f"DSL: {DSL.name}  (Dify DSL version {dsl['version']})")
    print(f"模型: {model}   条件分支: {cond['variable_selector']} {cond['comparison_operator']} {cond['value']}")
    print("=" * 66)

    failures = 0

    # 先跑不依赖模型的容错用例
    failures += check_parse_robustness(parse)

    # 再确认 Ollama 在
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5):
            pass
    except (urllib.error.URLError, OSError) as exc:
        print(f"\n[跳过 LLM 部分] Ollama 没起来：{exc}")
        print("=" * 66)
        print("结论：解析节点容错用例通过，但 LLM 链路未验证"
              if failures == 0 else f"结论：{failures} 个用例失败")
        return 1 if failures else 0

    for name, events, expect_push in CASES:
        print(f"\n### {name}")
        raw_input = json.dumps(events, ensure_ascii=False)

        # 节点 2：输入归一化
        norm_out = normalize(diff_events=raw_input)
        if not isinstance(norm_out, dict) or "events_json" not in norm_out:
            print("  [失败] 归一化节点没有返回 events_json")
            failures += 1
            continue
        norm_json = norm_out["events_json"]
        roles = [e.get("role") for e in json.loads(norm_json)]
        print(f"  归一化后 role = {roles}")

        # 节点 3：LLM
        user_prompt = render(user_template, {"1740000000002.events_json": norm_json})
        llm_out = call_llm(model, system_prompt, user_prompt)
        print(f"  LLM 原始输出: {llm_out.strip()[:260]}")

        # 节点 4：结果解析 + 规则判定
        parsed = parse(llm_output=llm_out)
        if not isinstance(parsed, dict):
            print("  [失败] 解析节点没有返回 dict")
            failures += 1
            continue

        cats = [e.get("category") for e in parsed["events"]]
        print(f"  事件数={parsed['event_count']} category={cats}")
        for ev in parsed["events"]:
            print(f"    - {ev.get('item_id')} [{ev.get('category')}] action={ev.get('action')} "
                  f"need_push={ev.get('need_push')}")
        want_txt = "不断言" if expect_push is None else str(expect_push)
        print(f"  条件分支判定 need_push = {parsed['need_push']}  "
              f"(期望 {want_txt})  pushed_items={parsed['pushed_items']}")

        actual = bool(parsed["need_push"])
        if expect_push is None:
            print(f"  [跳过断言] 该用例的分级复核由模型裁量，need_push={actual}，两种走向都合理")
        elif actual != expect_push:
            print("  [失败] 条件分支走向与期望不符")
            failures += 1
        else:
            print("  [通过] 走了 '" + ("true（立即告警）" if actual else "false（进日报）") + "' 分支")

    print("\n" + "=" * 66)
    print("结论：DSL 内嵌代码与提示词全部跑通" if failures == 0
          else f"结论：{failures} 个用例不符合预期，先修 DSL 再导入")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
