# Dify v3 工作流 —— 搭建说明

目标：把「自由发挥的文字建议」升级成「可被下游程序化判断的结构化输出」，
为条件分支告警推送做准备。

**推荐路径：直接导入 DSL，不要手搓节点。** 见下面第 0 节。

---

## 0. 先确认 Dify 能打开（打不开多半是地址问题）

本机实测（Dify 1.17.0，Docker 部署，nginx 占 80）：

| 你输的地址 | 结果 | 说明 |
|---|---|---|
| `http://localhost` | ✅ 正常，跳到登录页 | **正确的就是这个，不带端口** |
| `http://127.0.0.1` | ✅ 同上 | 等效 |
| `http://localhost:3000` | ❌ 502 | 前端容器端口**没**对外暴露，走 nginx |
| `http://localhost:5001` | ❌ 502 | API 端口同样不对外 |
| `https://localhost` | ❌ 连不上 | 443 没配证书 |

两个最常见原因：**① 多输了 `:3000`/`:5001`；② 浏览器/书签把地址升级成了 `https`。**
先试 `http://localhost`，不带端口、不带 s、不带斜杠。

排查命令：

```bash
docker ps --format "{{.Names}}\t{{.Status}}"     # 16 个容器都该是 Up
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/    # 期望 307
```

如果容器没起：`cd <dify>/docker && docker compose up -d`。

## 0.1 导入 DSL（一键建好整条链路）

文件：`dify/taobao-monitor-v3.yml`

1. 打开 `http://localhost`，进 **工作室**（Studio）
2. 右上角 **创建应用 → 导入 DSL 文件**，选上面那个 yml
3. 进工作流画布，**点开 LLM 节点确认模型**：应该是 `qwen2.5:7b`（Ollama）。
   若显示未配置，手动在模型下拉里重选一次（DSL 里的 provider 是
   `langgenius/ollama/ollama`，你本机装的正是这个）
4. 右上角「运行」，在**开始**节点的 `diff_events` 里贴调试数据（见第 1 节）

## 0.2 整条链路（7 个节点）

```
开始 → 输入归一化(Code) → LLM 运营分析 → 结果解析与规则判定(Code) → 是否需要立即告警(IF/ELSE)
                                                                     ├─ true  → 高优告警(End)
                                                                     └─ false → 进日报(End)
```

**为什么要有「输入归一化」这个前置代码节点：**

缺字段的处理是**确定性判断**，而模型做不了确定性判断 —— 实测教训：在提示词里写
「缺 role 时一律按 competitor 处理」，小模型会把这句话当成**默认行为**执行，
连传了 role 的 case 也照样输出「输入未提供 role」。所以缺字段必须在模型**之前**
由代码拦掉，提示词里只保留枚举值（`unknown`）的语义定义。

## 0.3 别踩这两个坑（我踩了）

**坑 1：Dify 代码节点的入口是 `def main(参数名)`，没有 `__inputs`。**

官方模板（`api/core/helper/code_executor/python3/python3_code_provider.py`）：

```python
def main(arg1: str, arg2: str):
    return {
        "result": arg1 + arg2,
    }
```

节点「输入变量」里声明的变量名，就是 `main` 的**形参名**。写 `__inputs.get("x")`
会直接报错。输入变量配置的格式是：

```yaml
variables:
- value_selector: [上游节点id, 上游变量名]
  value_type: string
  variable: 本节点里的形参名
```

**坑 2：`need_push` 不能交给 LLM 判。**

实测模型把 `severity=P0` 的事件判成 `need_push=false` —— 下游条件分支照这个跑，
**真 P0 会被静默吞掉**，告警推送等于没做。改成代码节点按规则算：

```
need_push = (severity == "P0") or (priority_keep is False)
```

同一条规矩：**模型只给归因 / 动作 / 复核理由，推送与否属于规则。**

## 1. 开始节点

- 变量名：`diff_events`，类型 **文本**，必填
- 调试值（贴进「开始」节点右下的输入框，**粘成一整行，别带换行**）：
```json
[{"item_id":"SAMPLE-001","role":"self","severity":"P0","field":"price","old_value":89.0,"new_value":80.1,"event":"价格变动 -10.0%"},{"item_id":"SAMPLE-009","role":"competitor","severity":"P0","field":"listing_status","old_value":"on_sale","new_value":"off_shelf","event":"商品下架"}]
```

## 2. 输入归一化（代码执行 · Python3）

**输入变量**：`diff_events` ← `开始 / diff_events`

```python
import json


def main(diff_events: str) -> dict:
    # 输入归一化：确定性判断前移到模型之前，别让 LLM 去猜字段缺失怎么办。
    try:
        data = json.loads(diff_events or "[]")
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []

    for ev in data:
        if isinstance(ev, dict) and not ev.get("role"):
            ev["role"] = "unknown"

    return {"events_json": json.dumps(data, ensure_ascii=False)}
```

**输出**：`events_json`（string）

## 3. LLM 运营分析

- 模型：`qwen2.5:7b`（v3 已验证够用，不用换）
- **系统提示词**：全文复制 `prompts/analysis_v3.txt`
- **用户提问**（漏了这格 AI 就凭空发挥）：

```
本轮监控采集到的变化事件如下：

{{#<输入归一化节点的id>.events_json#}}

按系统提示词的要求输出 JSON。
```

> 导入的 DSL 里这一格已经填好，节点 id 是 `1740000000002`。

## 4. 结果解析与规则判定（代码执行 · Python3）

**输入变量**：`llm_output` ← `LLM 运营分析 / text`

```python
import json
import re


def main(llm_output: str) -> dict:
    # 容错解析：LLM 输出不是 100% 干净的，一次解析失败就是一次漏告警。
    text = str(llm_output or "").strip()
    match = re.search(r"\{[\s\S]*\}", text)
    payload = {}
    if match:
        try:
            payload = json.loads(match.group(0))
        except Exception:
            payload = {}
    else:
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}

    events = payload.get("events") or []
    if not isinstance(events, list):
        events = []

    # 剔除模型偶发产生的「伪事件」：实测它会把 summary 误塞进 events 数组，
    # 生成 {"summary": "..."} 这种没有 item_id 的元素，会污染事件数。
    if not payload.get("summary"):
        for _x in events:
            if isinstance(_x, dict) and _x.get("summary") and not _x.get("item_id"):
                payload["summary"] = _x["summary"]
                break
    events = [e for e in events if isinstance(e, dict) and e.get("item_id")]

    # need_push 是纯规则，交给模型判会漏告警。
    pushed = []
    for ev in events:
        keep = ev.get("priority_keep")
        is_p0 = str(ev.get("severity", "")).upper() == "P0"
        ev["need_push"] = is_p0 or keep is False
        if ev["need_push"]:
            pushed.append(ev.get("item_id"))

    return {
        "events": events,
        "summary": payload.get("summary") or "",
        "event_count": len(events),
        "need_push": bool(pushed),
        "pushed_items": pushed,
    }
```

**输出**：`events` / `summary` / `event_count` / `need_push` / `pushed_items`

> `pushed_items` 是为了告警文案能直接点名商品，省得下游再解析 events。

## 5. 条件分支 → 两个结束节点

条件：

```
结果解析与规则判定.need_push  =  true
```

- **true** → **高优告警**（结束节点，输出 `summary` + `pushed_items`）
- **false** → **进日报**（结束节点，输出 `summary` + `event_count`）

现在两个 End 是占位。下一步接企微/钉钉/飞书时，把 true 分支换成 HTTP 请求节点。

## 6. 验收：怎么知道它是对的

**先本地跑，别在界面里瞎试：**

```bash
python scripts/verify_dify_dsl.py
```

这个脚本会把 DSL 里嵌的**代码和提示词真跑一遍**（复刻 `main` 入口 + 真调 Ollama），
包含 4 个模型用例 + 4 个确定性容错用例。全绿再导进 Dify。

界面里的验收清单：

| 场景 | 输入 | 期望 |
|---|---|---|
| 正常告警 | self 降价 P0 + competitor 下架 | 走 true 分支；竞品下架给「接手流量/抢关键词」类动作 |
| 空事件 | `[]` | 走 false 分支，summary = 本轮无异常 |
| role 缺失 | 去掉某条事件的 role | 归一化补成 unknown，category 只能是「归属未知」，动作不猜方向 |
| 低优先级 | competitor 降价 P2 | 走 false 分支，进日报 |

**看三个点**：① 两个 Code 节点有没有报错；② `need_push` 与 P0 是否一致；
③ 条件分支走的是哪条。

## 7. 改提示词的规矩（踩过的坑）

1. **先本地回归，再改 Dify**：`python scripts/verify_dify_dsl.py`。
   同一份提示词改一个字都可能让 7b 换种写法，没有回归就是在赌。
2. **别在提示词里写「字段缺失时怎么办」**：小模型会把兜底说明当成默认行为执行。
   缺字段由代码节点拦，提示词只定义枚举值语义。
3. **每次改完留基线**：`prompts/` 下按版本存（`analysis_v2.txt` / `analysis_v3.txt`），
   否则下次无从对比"这次改动到底起了什么作用"。
4. **确定性判断一律不进提示词**：分类、推送、字段兜底、格式校验 —— 全放 Code 节点。
   LLM 只负责归因、动作细化、优先级复核这三件"会变"的事。
