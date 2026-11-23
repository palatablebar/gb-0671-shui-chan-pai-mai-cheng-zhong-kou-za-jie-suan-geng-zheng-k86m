# 深度登记 水产拍卖结算

规模：中型。使用者：水产批发市场结算员。核心规则：先毛皮配对，再扣杂计价，按更正链选择唯一生效版，只支付差额。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 称重采信 | 查看并执行称重采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 货品换算 | 查看并执行货品换算 | `POST /api/conversion` | `app/modules/conversion/rules.py` | `tests/test_conversion.py` |
| M3 毛皮配对 | 查看并执行毛皮配对 | `POST /api/pairing` | `app/modules/pairing/rules.py` | `tests/test_pairing.py` |
| M4 扣杂竞价 | 查看并执行扣杂竞价 | `POST /api/auction` | `app/modules/auction/rules.py` | `tests/test_auction.py` |
| M5 更正裁决 | 查看并执行更正裁决 | `POST /api/correction` | `app/modules/correction/rules.py` | `tests/test_correction.py` |
| M6 应付对账 | 查看并执行应付对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 月度代扣税 | 查看并申报船户月度代扣 | `POST /api/tax`、`POST /api/tax/declare` | `app/tax.py`、`app/tax_ledger.py`、`app/modules/tax/rules.py` | `tests/test_tax.py` |
| M8 结算归档 | 查看并执行结算归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文票据 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-04 | 分段表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header_real` |
| TRAP-05 | 缺列记录 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_columns_real` |
| TRAP-07 | 空值哨兵 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_sentinel_real` |
| TRAP-08 | 前导零票号 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_leading_zero_real` |
| TRAP-14 | 重复编号不同原值 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict_real` |
| TRAP-18 | 三类空态 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-15 | 吨与千克混合 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_tonnes_counterexample` |
| TRAP-06 | 全角千分位 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_fullwidth_thousands` |
| TRAP-10 | 金额半分舍入 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/auction/rules.py` | `tests/test_auction.py::test_half_cent_rounding` |
| TRAP-26 | 陈旧更正分支 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/correction/rules.py` | `tests/test_correction.py::test_stale_branch` |
| TRAP-27 | 月末更正跨税档 | 整月重算改写已申报税单，或只改本票致后续少扣 | 结算序增量登记，已申报锚点不改写 | `app/tax.py`、`app/tax_ledger.py` | `tests/test_tax.py::test_month_end_correction_keeps_declared_and_charges_margin` |
| TRAP-28 | 同一更正重复申报 | 重复扣税或退款 | `entry_id` 幂等账本，重跑只命中复用 | `app/tax_ledger.py` | `tests/test_tax.py::test_rerun_does_not_double_charge` |
| TRAP-29 | 缺纳税人身份/断链/账本不符 | 静默出账 | 户级暂缓并留 blocked 证据 | `app/tax.py`、`app/tax_ledger.py` | `tests/test_tax.py::test_declare_refused_when_owner_held` |
| TRAP-30 | 申报中途失败 | 丢失原税单或部分落账 | 整批事务回滚，失败另记日志 | `app/tax_ledger.py` | `tests/test_tax.py::test_declare_failure_keeps_existing_tax` |

## 对账规则

输入=采纳原始行+隔离行+空行+同值重复行+重复表头。计价版本总额与生效票总额分别展示，避免把更正前后重复求和。最终应付=已付+待付，负待付为退款。

## 月度代扣税规则

户级累计计税依据=生效票成交额之和，与应付对账同源。每笔登记取 `T(登记后) − T(登记前)`，已申报锚点不改写，逐笔增量之和等于 `T(月末累计)`。页面同时展示采用的结算版本、登记前后累计计税依据、本票已申报税额、本次应扣或应退、账本状态和重跑命中。任一船户缺纳税人身份、更正链未闭合、户级与票级或账本不符时整户暂缓且整批拒绝申报；申报账本 `entry_id` 幂等，失败整批回滚并保留原税单。

## 已知不覆盖

无称重设备实时接入，采用现场导出文件。无支付执行，只生成可复核结算证据。
