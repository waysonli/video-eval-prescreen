# 视频评测自动化初筛 · 操作手册

一套可实操的初筛验证工具包，用于验证「自动化初筛 + 人工复核边界样本」这个思路在真实数据上是否成立。

定位是**验证性原型**，不是生产系统。产出的核心不是代码，而是一份能回答「这套初筛能不能信、能省多少人力」的数据结论。

---

## 文件清单

| 文件 | 作用 |
|---|---|
| `workflow/video_eval_prescreen_workflow.yml` | Dify 工作流 DSL，导入即用 |
| `scripts/run_prescreen.py` | 抽帧 + 批量调用工作流 |
| `scripts/validate_prescreen.py` | 初筛结果与人工结果对比分析 |
| `templates/prompts_template.csv` | 样本 prompt 映射模板 |
| `templates/human_eval_template.csv` | 人工评测结果录入模板 |
| `demo/自动化初筛工作流_demo.html` | 流程演示原型，用于向团队说明思路 |

---

## 零、环境准备（一次性）

```bash
# 1. 安装依赖
brew install ffmpeg
pip3 install requests

# 2. 确认安装成功
ffmpeg -version | head -1
ffprobe -version | head -1

# 3. 建工作目录
mkdir -p ~/prescreen/{videos,results}
cd ~/prescreen
# 把 scripts/、workflow/、templates/ 下的文件放到这里
```

**Dify 侧准备**

1. 登录 Dify → 工作室 → 创建应用 → 导入 DSL 文件 → 选 `workflow/video_eval_prescreen_workflow.yml`
2. 导入后 `vlm` 和 `judge` 两个节点会标红（因为 DSL 里写死的模型你不一定配置过），点开节点重选模型即可
3. 在「设置 → 模型供应商」里配好至少两个模型的 API Key（见下方选型建议）
4. 右上角「发布」→「访问 API」→ 复制 API 密钥

```bash
export DIFY_API_KEY="app-你复制的密钥"
export DIFY_BASE_URL="https://api.dify.ai/v1"   # 私有部署改成自己的
```

建议把这两行写进 `~/.zshrc`，免得每次开终端都要设。

---

## 一、阶段一：单条跑通（半天）

**目的**：确认整条链路通了，不是验证效果。

```bash
# 先放 1 个视频进 videos/
python3 scripts/run_prescreen.py --videos ./videos --out ./results --limit 1
```

**检查三件事**

1. `results/frames/样本ID/` 下是否生成了 f01.jpg ~ f08.jpg —— 抽帧是否正常
2. 终端是否输出 `ok` 和分流结果 —— 工作流是否跑通
3. 打开 Dify 的「日志与标注」页，看 VLM 节点的原始输出 —— **这一步最关键**

第 3 步要重点看：VLM 有没有老实按 JSON 格式输出？有没有自由发挥写了一堆描述？如果格式不听话，回 Dify 改 `vlm` 节点的 system prompt，把格式要求写得更硬。

**这个阶段最常见的三个报错**

| 报错 | 原因 | 处理 |
|---|---|---|
| `401 Unauthorized` | API Key 没设或设错 | 检查 `echo $DIFY_API_KEY` |
| `未找到 ffmpeg` | 没装或不在 PATH | `brew install ffmpeg` |
| 工作流 `failed`，日志显示 vision 报错 | `frames` 变量没绑上 | Dify 里手动打开 vlm 节点的视觉开关，变量选 `start / frames` |

---

## 二、阶段二：小批量调 Prompt（1–2 天）

**目的**：调 VLM 的提问清单和 Judge 的评分卡描述，让模型的输出稳定可用。

```bash
python3 scripts/run_prescreen.py --videos ./videos --out ./results --limit 20
```

**这个阶段不看准确率，只看稳定性**。判断标准：

- 20 条里有几条 JSON 解析失败？超过 2 条就要改 prompt
- 同一条样本连跑三次，分数一样吗？不一样说明 `temperature` 没设 0 或者锚点描述太模糊
- `parse_msg` 里频繁出现哪些字段缺失？说明 VLM 老是漏答某个问题，把那个问题拆得更具体

**改 prompt 的顺序**：先改 VLM 的提问清单（信息源头），再改 Judge 的评分卡（下游判断）。反过来改会把上游问题误判成下游问题。

---

## 三、阶段三：跑验证集（1 天）

**目的**：拿一批**已经有人工评测结果**的样本跑初筛，这是整套流程的核心。

**验证集怎么选**（决定结论可不可信）

| 要求 | 说明 |
|---|---|
| 数量 ≥ 100 条 | 少于 100 条统计意义不足，报告会自动提示 |
| 覆盖全部分数档 | 不能全是好样本，1–5 分都要有 |
| 包含红线样本 | 至少 5–10 条触发红线的，验证红线规则有没有生效 |
| 人工结果先于初筛产生 | 不能先看初筛结果再打人工分，会污染验证 |

```bash
# 准备 prompt 映射（可选但推荐）
cp templates/prompts_template.csv prompts.csv
# 按模板格式填入真实 sample_id 和 prompt

# 跑全量
python3 scripts/run_prescreen.py \
    --videos ./videos \
    --out ./results \
    --prompts ./prompts.csv \
    --scene 通用视频 \
    --workers 3
```

跑完终端直接给分流分布：

```
分流分布:
  edge_review       58 条  58.0%
  high_conf_good    31 条  31.0%
  high_conf_bad     11 条  11.0%

可自动处理占比 42.0%，需人工复核 58.0%
```

**中途断了直接重跑同样的命令**，已成功的样本会自动跳过。

---

## 四、阶段四：对比验证（半天）

**目的**：算出召回率、误杀率、分维度偏差，得到「能不能上」的结论。

```bash
# 录入人工结果
cp templates/human_eval_template.csv human_eval.csv
# 按模板填，sample_id 必须和视频文件名一致

python3 scripts/validate_prescreen.py \
    --prescreen ./results/results.jsonl \
    --human ./human_eval.csv \
    --out ./results/validation_report.md
```

**人工结果 CSV 的填法**

- `sample_id`：必填，和视频文件名（去掉扩展名）一致
- `verdict`：填 `qualified` / `unqualified`。**留空也行**，脚本会按「任一维度低于 3 分即不合格」自动推导
- 各维度分数：填 1–5，该维度不适用填 0（比如画面无文字时的文字渲染）
- `备注`：不参与计算，方便自己回看

**报告的六个部分怎么读**

| 章节 | 回答什么问题 | 最该看的数 |
|---|---|---|
| 一、样本匹配 | 数据对齐了吗 | 未匹配数应接近 0 |
| 二、分流分布 | 能省多少人力 | 自动处理占比 |
| 三、准确性 | 自动判定可信吗 | **召回率、误杀率** |
| 四、分维度偏差 | 哪个维度不能信 | 系统性偏差绝对值 > 0.3 的维度 |
| 五、阈值扫描 | 阈值该怎么设 | 漏放数为 0 的最窄区间 |
| 六、可用性结论 | 能不能上 | 四条准入条件 |

---

## 五、阶段五：调参迭代（持续）

根据报告结论回头调，按这个优先级：

**优先级 1：系统性偏差 > 0.3 的维度**

这是最值得修的问题，因为它是稳定跑偏而非随机噪声，改 prompt 就能修好。

去 Dify 的 `judge` 节点，找到那个维度的评分卡锚点，把描述改得更量化。比如原来写「画质基本稳定」，改成「关键帧间无可察觉的清晰度变化，无闪烁」。

**优先级 2：平均绝对偏差 > 0.6 的维度**

说明这个维度的判断超出了模型能力，改 prompt 也救不回来。处理方式是**在 `conf` 节点里把它设为强制转人工项**：

```python
FORCE_REVIEW_DIMS = ("音画匹配", "动作连续性")   # 加在常量区
# 在 review_dims 计算处加一行
review_dims.update(d for d in FORCE_REVIEW_DIMS if d in valid)
```

**优先级 3：误杀率高**

放宽 `EDGE_LOW`（比如 2.5 → 2.0），让更多低分样本转人工而不是直接判死。

**优先级 4：漏放高**

提高 `HIGH_GOOD`（比如 4.0 → 4.3），收紧「明显好」的准入。

**每次改完重跑阶段三 + 阶段四**，对比两版报告的召回率和误杀率变化。改一个参数跑一轮，不要一次改三个——出了变化你分不清是哪个起的作用。

---

## 六、关键参数速查

**`scripts/run_prescreen.py` 顶部**

| 参数 | 默认 | 什么时候调 |
|---|---|---|
| `SCENE_THRESHOLD` | 0.3 | 单镜头视频误判出切换 → 调高到 0.4–0.5；多镜头漏检 → 调低到 0.2 |
| `INTERVAL_SEC` | 1.0 | 视频短（<3s）调到 0.5；视频长调到 2.0 |
| `MAX_FRAMES` | 8 | 跟 Dify start 节点的 `max_length` 保持一致，改一个要改两处 |
| `--workers` | 3 | 遇到大量 429 报错就降到 1–2 |

**Dify `conf` 节点**

| 常量 | 默认 | 含义 |
|---|---|---|
| `EDGE_LOW` | 2.5 | 边界区下沿，调低 → 更多样本转人工 |
| `EDGE_HIGH` | 3.5 | 边界区上沿，调高 → 更多样本转人工 |
| `HIGH_GOOD` | 4.0 | 判「明显好」的最低分，调高 → 漏放减少 |
| `HIGH_BAD` | 2.0 | 判「明显差」的最高均分，调低 → 误杀减少 |

**`scripts/validate_prescreen.py` 顶部**

| 常量 | 默认 | 含义 |
|---|---|---|
| `UNQUALIFIED_THRESHOLD` | 3.0 | 人工 verdict 留空时的自动推导阈值 |
| `CONSISTENCY_TOLERANCE` | 1.0 | 一致率的判定容差，严格些可设 0.5 |

---

## 七、模型选型建议

**VLM 预标注节点**（这一步决定整条流水线的上限）

| 模型 | 定位 | 注意 |
|---|---|---|
| Gemini 2.x Flash | 调试首选，多图便宜、上下文长 | 国内需代理 |
| GPT-4o | 指令遵循最稳，JSON 格式最听话 | 多图成本偏高 |
| Qwen2.5-VL-72B | 国内可用，中文场景理解好，有开源版 | 复杂空间关系推理略弱 |
| Doubao-vision / GLM-4V | 国内合规、成本低 | 结构化输出需更强格式约束 |

**调试路径**：先用 Gemini Flash 或 GPT-4o 把提问清单和字段定义验证对，再换国内模型看效果掉多少。先解决「问题设计得对不对」，再解决「用哪个模型」——顺序反了会把 prompt 问题误判成模型问题。

**LLM-as-Judge 节点**

这一步不看图、只处理结构化文本，不需要多模态也不需要贵模型。DeepSeek-V3 或 Qwen-Max 足够，成本约为 VLM 的十分之一。

两条硬性要求：

- `temperature` 必须设 0，否则「多次调用一致性」这个置信度信号失效
- **Judge 模型最好和被评测的生成模型不同厂商**，避免自我偏好

---

## 八、常见问题

**Q：JSON 解析失败率高**
先看 Dify 日志里 VLM 的原始输出。多数情况是模型加了 markdown 代码块标记或者写了前言——脚本已经做了容错，如果还是失败，在 system prompt 末尾加一句「你的回复的第一个字符必须是 `{`，最后一个字符必须是 `}`」。

**Q：所有样本都被判为 edge_review**
检查 Judge 是否大量返回 0 分。0 分意味着「信息不足」，说明 VLM 那一步的字段大量填了「无法判断」——问题在抽帧质量或提问清单，不在分流阈值。

**Q：抽帧全是相似的帧**
`SCENE_THRESHOLD` 太高导致场景检测没命中，全靠固定间隔帧补齐。调低到 0.2 再试。

**Q：跑到一半大量 429**
`--workers` 降到 1，或者换一个配额更高的模型。

**Q：报告说样本不足 30 条**
验证集太小，结论不可信。这不是脚本的问题，是数据的问题——补样本。

---
