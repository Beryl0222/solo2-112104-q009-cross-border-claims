# 跨境物流异常叙述与索赔责任材料整理

本工具把司机备注、承运商消息、收货方邮件和补充扫描记录整理成**可复核的案件材料**。
它只使用合成文本，**不判断法律责任、不自动提交索赔**；所有“推断”都单独标注，不能当作已确认事实。

## 它解决的问题

| 需求 | 实现 |
| --- | --- |
| 中英双语/简繁对“交接、签收、暂存”用法不一致 | 词表按概念匹配，区分 handover / delivery / acknowledgement / storage / scan；`not proof of delivery` 不会被读成“已交付” |
| 区分观察、转述、推断、正式扫描记录 | 每个分句带 `StatementMode`：observation / reported / inference / formal_scan / not_stated |
| 补发扫描可能改变异常发生在哪个环节 | 更正记录把被更正事件标为 `superseded`，被改派到其他货件的扫描标为 `attribution_withdrawn`；原事件保留不删 |
| 收货方推测被写成已确认事实 | 推断段落在时间线和报告里都带“推断 / 不可当已确认事实”，且不能作为时限起点或交付确认 |
| 判断时限从哪一次交接开始 | 列出全部候选起点，逐个给出采用/排除理由；起点变化后复核自动失效 |
| 新扫描到达后重核未提交判断与时限提醒 | 复核检查点记录决策输入的摘要，输入一变即显示“已过期/需重核”，但保留当时的依据 |
| 已提交资料不能被静默改写 | 提交版本不可变；补充材料只能追加 `v2/v3`，并列出采用版本、哈希与后续补证 |
| 承运商重编货件号 | 旧号→新号只在有“同时写明两个编号”的来源时才可核验；核验前不合并，字符串相似仅提示 |
| 仍需承运商确认的环节 | 待确认问题可登记、答复、撤回；报告末尾直接列出仍未确认的环节 |

## 环境与运行

Python 3.11 标准库，无第三方依赖。

```bash
# 导入合成来源（分组式 bundle，或 --shipment 显式指定货件号的列表）
python3 -m shipment_claims import data/scenario_handover.json --out archive.json

# 查看全部货件 / 单个货件（Markdown）
python3 -m shipment_claims report --archive archive.json --shipment SHP-2026-0417 --today 2026-11-22

# 结构化视图（JSON）
python3 -m shipment_claims json --archive archive.json --shipment SHP-2026-0417 --today 2026-11-22
```

文档里的日期参数 `--today` 只影响“临近/逾期”判定，便于复盘与测试。

## 典型处理流程

```bash
# 1. 登记旧号→新号对应关系；--verify 会重新检查证据
python3 -m shipment_claims link --archive archive.json \
  --old SHP-2026-0417-R --new SHP-2026-0417 \
  --evidence src-0417-mapping --rationale "承运商重编号通知" \
  --actor handler --verify

# 2. 登记待承运商确认的问题
python3 -m shipment_claims ask --archive archive.json \
  --shipment SHP-2026-0417 --question "东仓交接后是否有签收或出库扫描？" \
  --target carrier --actor handler --evidence src-0417-driver

# 3. 记录复核（时限 / 未提交责任草稿）
python3 -m shipment_claims review --archive archive.json \
  --shipment SHP-2026-0417 --kind deadline \
  --note "按更正扫描核对起点" --actor reviewer

# 4. 登记已提交版本；之后的新材料只能补证
python3 -m shipment_claims submit --archive archive.json \
  --claim-id CLM-0417 --shipment SHP-2026-0417 \
  --summary "东仓交接后无签收扫描" --actor handler \
  --source src-0417-driver src-0417-carrier

python3 -m shipment_claims supplement --archive archive.json \
  --claim-id CLM-0417 --summary "补入承运商更正扫描" \
  --actor handler --source src-0417-scan-correction
```

## 输入格式

分组式 bundle（推荐；每段仍可覆盖作者、时间、地点等字段）：

```json
{
  "shipments": {
    "SHP-1": [
      {"id": "s1", "kind": "driver_note", "lang": "zh",
       "author": "司机 甲", "received_at": "2026-11-02T09:30:00+08:00",
       "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。"}
    ]
  },
  "links": [
    {"old_id": "SHP-1-R", "new_id": "SHP-1",
     "evidence_ids": ["mapping-1"], "rationale": "重编号通知",
     "created_by": "handler", "verify": true}
  ],
  "pending": [
    {"shipment_id": "SHP-1", "question": "交接后是否签收？",
     "target_party": "carrier", "created_by": "handler",
     "evidence_ids": ["s1"]}
  ]
}
```

- `kind`：`driver_note`、`carrier_message`、`recipient_email`、`scan`、
  `scan_correction`、`manifest`、`warehouse_note`、`dock_note`、`carrier_mapping`
- 修订关系：`corrects: ["原来源id"]`、可选 `reassigns_to: "其他货件号"`、
  以及 `revision_of`
- 时间：`observed_at`（发生）与 `received_at`（收件）均可省略；省略时只从原文提取，
  缺日期就标“仅有时刻、日期未知”，不臆造日期
- 地点：`location` 缺省时从原文提取（`at East Depot` / `在东仓暂存区`）

旧的列表式 `data/claims.json` 也支持，但必须用 `--shipment` 显式给出货件号，工具不会猜。

## 设计约束（为什么这样处理）

1. **来源不可变。** 同一 `source_id` 重复导入相同内容为幂等；内容不同则报错，必须追加修订记录。
2. **分句取证。** 每段原文按分句提取，事件保留 `source_id ¶序号`、作者、角色、地点、时间与时间精度。
   一个分句没有自己的时刻时，不会借用同段其他分句的时刻。
3. **推断不等于事实。** 推断/转述分句不能作为时限起点，也不能算“交付确认”；
   `can_confirm_delivery` 只认可正式扫描或履约方（司机/承运商/仓库）的直接观察。
4. **更正可追溯。** 被更正或被改派的事件改为 `superseded` / `attribution_withdrawn`，
   原文与说明都保留，时间线里仍可见，但不再参与时限计算。
5. **编号合并需证据。** 只有某一条来源同时写明旧号与新号时才能核验；核验前旧号独立成案，
   相似字符串只在索引中提示。冲突或成环的对应关系会被拒绝，交人工处理。
6. **提交不可改写。** 提交版本带内容哈希与事件引用；后续变化以“补证”版本追加，
   并区分“提交后新增材料”和“提交前已存在但未选用”的材料。
7. **复核会过期。** 时限或责任草稿的复核检查点记录决策输入摘要；新扫描或更正一到，
   状态变为“已过期/需重核”，并显示当时采用的起点与依据。

## 报告包含什么

- 货件编号与旧号→新号的核验状态
- 按时间排序的事件时间线，逐条标注角色、作者、地点、观察/转述/推断/正式扫描
- 每段证据的原文、来源编号、内容哈希、修订关系
- 时限判断：采用起点、到期日、全部候选起点的采用/排除理由、占位规则声明
- 待承运商确认事项及仍未确认的环节
- 已提交索赔的版本、采用材料、版本哈希、补证后的变化
- 复核状态与需要重核的触发项、未并入的其他编号

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：中英简繁角色识别、模式判定、否定与改派、时间/时区与日期提取、
来源不可变、核验式编号合并、时限候选与过期复核、提交与补证、待确认事项、
持久化往返、报告内容与 CLI 全流程。

## 已知边界

- 这是规则/词表驱动的抽取，不理解复杂从句或指代；新表述可能需要补充 `lexicon.py` 词条。
- 时限天数是内部复核用的**占位规则**，不是法律意见；实际时限须由索赔人员按适用规则确认。
- 目前不生成索赔函件、不计算赔付金额、不连接任何外部系统。
