# 独立审查报告：fontTools 变体字体跨表一致性校验器

- 审查方式：两轮独立只读审查（全新上下文 subagent；未引用实现者自测结论作为证据）。
- 审查对象：`Lib/fontTools/varLib/validate.py`（约 1600 行）、`Lib/fontTools/varLib/__init__.py`（一行导出）、`Tests/varLib/validate_test.py`。
- 环境命令：`cd <repo> && PYTHONPATH=Lib /opt/homebrew/bin/python3.11 -m pytest <path> -q`。

## 最终结论

**Review result: pass**（第 1 轮 fail 1 项 → 已修复 → 第 2 轮复核 closed）。

全部 12 条 AC 通过；两条 rubric（TR-1.4 可定位性、TR-6.3 零副作用）第 1 轮各 4/5，修复后复审确认；TR-7.2 反例尝试覆盖只读/惰性/全量收集三条承诺。

## 第 1 轮发现

| # | 严重度 | 问题 | 处置 |
|---|---|---|---|
| 1 | 重要（阻断） | `_otRoot` 对"表存在于 sfnt 目录但反编译失败"与"表不存在"均返回 None，导致磁盘截断的 HVAR/GPOS/VARC/GDEF 等被当作可选表缺失静默跳过，`validateFont` 返回 `ok=True`、0 issue；违反 AC-4/FR-10 及模块 docstring 承诺。复现：改目录声明 length（HVAR=6、GDEF=0、GPOS=0、VARC=20）后 lazy 打开均误报 ok。 | 已修复并复核关闭 |
| 2 | 次要（不阻断） | eager 字体首次 save 的 GPOS/GSUB/head 字节可与原文件不同。 | 确认为 fontTools 既有惰性反编译缓存行为（不调用 validate 也会发生），NFR-1 明确允许；reader 字节 sha256 不变、对象指纹零变化、第二次 save 起稳定，无需修改。 |
| 3 | 次要（不阻断） | `_checkIdxMap` 对 dict 形态（HVAR VarIdxMap，glyph 名为键）location 只给数字下标。 | 已顺带修复：dict 分支 location 保留 glyph 名（实测 `HVAR/AdvWidthMap/.notdef`）。 |

## 修复内容（第 1 轮后）

- `_otRoot` 严格化：表缺失返 None；表存在时解析异常上抛，不再吞异常。
- 新增 `_sectionRoot(collector, ttFont, tag)` 与 `_UNREADABLE` 哨兵：存在但解析失败时产出且仅产出一条 `table-parse-error`；HVAR/VVAR/MVAR/BASE/VARC 及 GDEF/GPOS/MATH/JSTF family walk 全部切换到该入口；GDEF 损坏时守卫确保不重复报告。FeatureVariations 内存回退保留在既有 try 内。
- `_checkIdxMap` 区分 dict（glyph 名键）与 list（位置下标）。
- 新增参数化用例 `test_truncated_otl_table_is_parse_error_not_ok`（HVAR=6 / GPOS=0 / VARC=20）。

## 第 2 轮复核证据

- 原 4 个复现全部变为 `ok=False` 且恰有一条对应 tag 的 `table-parse-error`；审查者另选 MVAR、avar v2 截断及注入坏 BASE/MATH/JSTF，均一致；表真正缺失仍静默跳过（I.ttf 无 MVAR/BASE/VARC 时零噪声）。
- 隔离性：同一字体同时注入"VARC 截断 + gvar axisCount=99"，gvar 错误与 VARC parse-error 并存且确定（重复运行 issue 序列相同）。
- 非修改/惰性：截断字体与正常字体校验前后所有 `reader[tag]` 字节 sha256 相同；`isLoaded("gvar")/("name")` 保持 False；全文无 setattr/save/compile，唯一 decompile 是 FeatureVariations 子表的 raw reader 路径。
- 兼容：`ast.parse(feature_version=(3,9))` 通过。
- 亲自复跑：validate_test.py **60 passed**；`Tests/varLib Tests/ttLib` **2810 passed, 10 skipped, 0 failed**。
- 非阻断观察：同一损坏 TTFont **实例**第二次调用 validateFont 不再报告该表——fontTools 对半成品反编译 wrapper 有缓存，属既有语义，在 NFR-1（不改对象状态）约束下无法干净规避；正常用法（打开→校验）与跨实例确定性不受影响。

## AC 证据矩阵（汇总两轮）

| AC | 结果 | 主要证据 |
|---|---|---|
| AC-1 公共 API | PASS | `from fontTools.varLib import validateFont, FontValidationReport, ValidationIssue`；非 TTFont 抛 TypeError；三份 varc 字体 + I.ttf + COLRv1 build 字体空报告。 |
| AC-2 不修改字体 | PASS | reader 字节 sha256 前后一致（含截断样本）；save 两次幂等；源码无写路径；首次 eager save 差异为 fontTools 既有行为。 |
| AC-3 懒加载兼容 | PASS | 校验后 gvar/name/GSUB/GPOS 均 isLoaded=False；FV 经原始字节 OTTableReader 只解析子表。 |
| AC-4 全量收集不中断 | PASS | gvar 截断 + HVAR 错值并存、VARC 截断 + gvar 轴错并存均完整收集；截断表 table-parse-error（第 2 轮修复）。 |
| AC-5 gvar 原始校验 | PASS | 11 个字节级篡改用例（axisCount/glyphCount/shared 索引/偏移单调/越界/tuple 头/截断/flags）；空 gvar（axisCount=0）合法；tupleSize 含 4 字节头。 |
| AC-6 存储结构一致性 | PASS | RAC 与轴数、Region 轴数、VarRegionIndex、Item 长度（密集 ==RC；扁平 %RC==0）、VarDataCount 越界用例。 |
| AC-7 VarIdx 解析 | PASS | HVAR/MVAR/COLR map/ClipBox/0x8000 Device（GPOS）/VARC 组件 outer/inner/arity；0xFFFFFFFF 哨兵合法；GDEF 缺失存储报 var-store-missing。 |
| AC-8 FeatureVariations | PASS | Format1 越界、嵌套 3→4→5、Format2 报 condition-format2（VARC 语境解析 MultiVarStore）、ConditionSet=None、未知 Format 跳过、无 fvar 报 fvar-required；raw 字节路径不加载 GSUB。 |
| AC-9 VARC | PASS | 14+ 用例：coverage/conditionIndex/axisIndicesIndex/sparse axisIndex/轴数/arity/多 region 越界；gvar≤fvar 隐藏轴合法。 |
| AC-10 无 fvar/静态 | PASS | varc-static-gvar（无 fvar，gvar=1）报告空；轴权威回退 gvar 原始 axisCount。 |
| AC-11 code 稳定性（rubric ≥4） | PASS 4/5（第 1 轮），维持 | code 命名稳定、语义单一、有测试锁定；dict map 改为带 glyph 名后定位性进一步提升。 |
| AC-12 location 可定位（rubric ≥4） | PASS，4/5→保持并增强 | 表/字段/索引/glyph 名分层渲染（int 渲染为 `[i]`）；如 `HVAR/AdvWidthMap/.notdef`、gvar tuple 索引、COLR BaseGlyph+Paint 路径、VARC glyph/component 索引。 |
