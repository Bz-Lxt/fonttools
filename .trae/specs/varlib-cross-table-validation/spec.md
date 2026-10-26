# fontTools 变体字体跨表一致性校验器 - 产品需求文档

## Overview
- **Summary**: 在 fontTools 中新增公共模块 `fontTools.varLib.validate`，对第三方可变 TTFont 执行跨表（fvar / gvar / 各变体存储 / FeatureVariations / COLR / VARC / CFF2）的轴数与索引一致性校验，返回结构化的公共校验报告。
- **Purpose**: 单表能反编译并不代表跨表一致；现有代码在多处使用硬 `assert`（如 `gvar.decompile`），坏字体会直接抛异常崩溃，且没有任何 API 能一次定位全部跨表错误。校验器需在不修改字体、兼容懒加载的前提下，稳定收集全部错误。
- **Target Users**: 字体处理/质检工具开发者、字体子集化与实例化流水线、需要验收第三方可变字体（含 COLRv1 / VARC 新格式）的库使用方。

## Goals
- 一次运行收集全部可定位的跨表错误，不因单表损坏或首个错误而中止。
- 严格只读：不 compile、不 save、不 setattr 任何字体对象；原始字节级校验不触发表反编译。
- 兼容 `TTFont(lazy=True)`：不强制全量 decompile；按需遍历仅物化实际访问到的 otTables 子树。
- 公共、稳定、结构化的结果 API：稳定的错误码（code）、表标签（tag）、位置路径（location）与可读消息。
- 与上游代码风格一致，无新第三方依赖。

## Non-Goals
- 不修复、不规范化字体（无 fix/write 能力）。
- 不做单表内部的语义合法性全量审计（如 fvar min≤default≤max、avar 映射点、COLR 颜色值、glyf 轮廓合法性）。
- 不深度校验 CFF2 charstring 内的 vsindex 引用与 blend 算子（仅校验 topDict VarStore 结构层）。
- 不校验 cvar、STAT、HVAR 以外的设计空间语义。
- 不新建文档站条目（Doc/source .rst），公开性通过模块 docstring 与 `fontTools.varLib` 导出保证。

## Background & Context
- 仓库即 fontTools 本体（4.67.1.dev0）；上游 main 无 validate 模块，属全新功能。
- 已核实的关键结构：
  - fvar：`font["fvar"].axes`，轴数 = `len(axes)`。
  - gvar：header 中 `axisCount`（原始字节偏移 4-5，big-endian H）；`decompile` 在 `_g_v_a_r.py` 第 154-159 行有硬 assert（`len(axisTags) == self.axisCount`、glyphCount 相等），坏字体直接崩溃——校验器必须绕过该路径，直接解析 `reader.getTableData("gvar")` 原始字节。
  - ItemVariationStore：`VarRegionList.RegionAxisCount` 必须等于 fvar 轴数；`VarData.VarRegionIndex[] < RegionCount`；`len(Item)==VarRegionCount`；VarIdx 编码 `outer=v>>16, inner=v&0xFFFF`；`NO_VARIATION_INDEX=0xFFFFFFFF`。出现于 GDEF(>=1.3)、BASE(>=1.1)、MVAR、HVAR、VVAR、COLRv1、avar v2、CFF2 topDict。
  - VarIdx 消费者：HVAR/VVAR 的 DeltaSetIndexMap（`.mapping` 列表）、MVAR `ValueRecord[].VarIdx`、avar2 `VarIdxMap.mapping`、GDEF/GPOS/MATH/JSTF（含 GDEF 自身）中 Device 表 `DeltaFormat==0x8000` 时 `(StartSize<<16)|EndSize`（指向 GDEF.VarStore）、BASE Device（指向 BASE.VarStore）、COLR 变量 Paint/ClipBox 的 `VarIndexBase`（经可选 VarIndexMap 映射）。
  - MultiVarStore（VARC）：`SparseVarRegionAxis.AxisIndex < 轴数`；`MultiVarData.VarRegionIndex[] < RegionCount`；Item 为 tuple 列表，`len(Item) % VarRegionCount == 0`。
  - FeatureVariations（GSUB/GPOS 根）：ConditionSet.ConditionTable；Format1 有 `AxisIndex`；Format3/4 递归子条件；Format5 单子条件；ConditionSet 可为 None（永真）。
  - VARC：`font["VARC"].table`（MultiVarStore / ConditionList / AxisIndicesList / VarCompositeGlyphs / Coverage）；组件有 `conditionIndex`、`axisIndicesIndex`、`axisValues`、`axisValuesVarIndex`、`transformVarIndex`；遍历前必须调用 `font.getGlyphOrder()`（否则 `otTables.py:179` AttributeError）；VarCompositeGlyph 走 CFF2 INDEX 惰性 LazyList/UserList。
  - 合法特例：`Tests/ttLib/data/varc-ac01-conditional.ttf` fvar=8 轴而 gvar=6（VARC 组件内部隐藏轴）；`varc-static-gvar.ttf` 无 fvar，仅有 gvar(1 轴) + VARC。因此无 fvar 时轴权威回退 gvar 原始 axisCount；有 VARC 时 gvar 轴数允许 ≤ fvar 轴数。
- 运行环境：必须使用 `/opt/homebrew/bin/python3.11` 且 `PYTHONPATH=Lib`（系统默认 python3.9 与 site-packages 4.42.1 均不可用）。
- 遍历基础设施：`BaseTable.iterSubTables()`、`getVariableAttrs()`（基于 "VarIndexBase + N" 描述），参考实现 `instancer/colr.py` visit()、`varStore.py` _visit()。

## Functional Requirements

- **FR-1 公共 API**：`fontTools.varLib.validate.validateFont(ttFont) -> FontValidationReport`；从 `fontTools.varLib` 包命名空间可直接 import；输入非 TTFont 抛 `TypeError`。
- **FR-2 结果模型**：`ValidationIssue(code: str, tag: str, location: tuple[str|int, ...], message: str)`；`FontValidationReport` 提供 `issues` 元组、`ok` 属性与 `__bool__`（无 error 即真）、可迭代/`len`、`byTag(tag)`、`withCode(code)`、稳定的人类可读字符串汇总；`location` 以表标签为首元素，提供渲染后的点分/斜线路径字符串。
- **FR-3 轴权威**：fvar 存在则轴数 N=`len(fvar.axes)`；fvar 缺失时 N 取 gvar 原始字节 axisCount（仅对静态 VARC/CFF2 隐藏轴场景），并在内部标注来源；两者均不可得时需要轴数的检查降级为跳过而非误报。
- **FR-4 gvar 原始字节校验**：不经 `decompile`，直接解析 header/共享元组/每字形偏移与 tuple header，检查：axisCount 与 fvar（规则见 FR-3 特例）、glyphCount 与字体 glyph 数、共享元组区与字形数据偏移越界、偏移单调性、共享 tuple 索引 < sharedTupleCount、tuple 尺寸不超出字形数据块；数据截断/不可解析产出一个 parse-error issue。
- **FR-5 密集 ItemVariationStore 校验**：对 GDEF/BASE/MVAR/HVAR/VVAR/COLR/avar2/CFF2 中每个 VarStore 检查 Format=1、`RegionAxisCount == N`（N 可得时）、每个 Region 的轴数、`VarRegionIndex < RegionCount`、`len(Item) == VarRegionCount`、VarDataCount 与数组一致。
- **FR-6 VarIdx 消费者校验**：HVAR/VVAR 各 map、MVAR ValueRecord、avar2 VarIdxMap、COLR VarIndexMap/Paint/ClipBox、GDEF/GPOS/MATH/JSTF 与 BASE 的 0x8000 Device、VARC 组件/条件中的每个 VarIdx：`0xFFFFFFFF` 视为合法“无变体”哨兵；其余要求 outer < VarDataCount、inner < 对应 VarData.Item 数；引用存在但所属 VarStore 为 None 时单独报错。
- **FR-7 FeatureVariations 校验**：GSUB/GPOS 的 FeatureVariations 存在时要求 fvar 存在；递归检查全部 ConditionTable（Format3/4/5），Format1 的 AxisIndex < N；Format2（变量条件，其 VarIdx 仅在 VARC MultiVarStore 语境有定义）在 FeatureVariations 语境报错；未知 Format 前向兼容跳过。
- **FR-8 COLR v1 校验**：version<1 跳过；遍历 BaseGlyphList、LayerList 的整棵 Paint 树（含 ClipList.clips 的 ClipBox），对所有 `VarIndexBase != NO_VARIATION_INDEX` 的节点按 `getVariableAttrs()` 偏移跨度解析并校验 VarStore 范围；显式 VarIndexMap 存在时逐值校验；有变量引用但 VarStore 为 None 报错；遍历需防 offset 环。
- **FR-9 VARC 校验**：Coverage glyph 数 == VarCompositeGlyphs 记录数；AxisIndicesList 每个 tuple 的轴索引 < N；MultiVarStore 按 FR-5 的 sparse 版本校验（AxisIndex、VarRegionIndex、Item 长度取模）；ConditionList 按 FR-7 同源规则（Format2 VarIdx 指向 MultiVarStore）；每个 VarComponent 校验 conditionIndex < ConditionCount、axisIndicesIndex < tuple 数、`len(axisValues)==引用 tuple 长度`、axisValuesVarIndex/transformVarIndex 可解析、引用的 Item tuple 宽度与轴数一致（`len(deltas) == numAxes * regionCount`）；遍历前确保 glyphOrder 已加载。
- **FR-10 错误隔离与全量收集**：每个顶层检查段（fvar/gvar/每表存储/FeatureVariations/COLR/VARC/CFF2）独立 try/except；单段异常转为 `table-parse-error` issue 后继续其余检查；正常输入下 validateFont 自身不抛异常（TypeError 除外）。
- **FR-11 结果确定性**：同一字体多次运行 issue 顺序与内容完全一致（遍历按表标签与索引确定序）。

## Non-Functional Requirements
- **NFR-1 只读**：校验路径中不得调用 compile/save/setattr/saveXML，不得修改 reader 字节与任何表对象状态（仅允许 fontTools 自身的惰性反编译缓存与 `getGlyphOrder()` 正常读缓存）。
- **NFR-2 惰性友好**：对 `lazy=True` 字体，gvar 检查不得导致 `font.isLoaded("gvar")` 为真；不相关表不得被加载；文档化说明被遍历的 otTables 子树（COLR/VARC/HVAR 等）会按需物化。
- **NFR-3 性能**：复杂度与被引用数据量线性相关；gvar 只做 header/偏移/tuple-header 级解析，不解析字形点数据。
- **NFR-4 兼容**：支持 Python 3.9+（仓库现行下限），仅用标准库与 fontTools 既有模块；无 fvar 的静态 VARC 字体与现有三份 varc 测试字体必须报告 ok。

## Constraints
- **Technical**：运行/测试解释器 `/opt/homebrew/bin/python3.11`，命令形态 `cd <repo> && PYTHONPATH=Lib /opt/homebrew/bin/python3.11 -m pytest ...`；不得 `pip install -e .` 或改动环境。
- **Business**：不修改任何测试字体文件；破坏性用例只能在内存对象或内存字节副本上注入。
- **Dependencies**：无 defcon/ufoLib2，测试字体构建只能用 TTX/TTF master（如 `Tests/varLib/data/master_ttx_varcolr_ttf/`）与手工 newTable，不得依赖 UFO build。

## Assumptions
- `0xFFFFFFFF` 在所有 VarIdx map/记录/字段中均为合法“无变体”哨兵（与 HarfBuzz/instancer 行为一致）。
- 有 VARC 时 gvar.axisCount 允许小于 fvar 轴数（隐藏轴）；无 VARC 时必须相等。
- 条件表未知 Format 视为前向兼容，跳过而非报错。
- CFF2 topDict VarStore 可经 `font["CFF2"].cff.topDictIndex[0].VarStore.otVarStore` 访问。

## Acceptance Criteria

### AC-1: 公共 API 与报告模型
- **Type**: `rule`
- **Given**: fontTools 环境
- **When**: 调用 `from fontTools.varLib import validateFont, FontValidationReport, ValidationIssue` 并对 TTFont 执行
- **Then**: 返回 FontValidationReport；issue 含稳定 code/tag/location/message；ok/bool/迭代/byTag/withCode/字符串汇总行为符合 FR-2；非 TTFont 输入抛 TypeError
- **Pass Condition**: 新增模块与导出存在，上述行为有断言覆盖并通过
- **Evidence**: `Tests/varLib/validate_test.py` 通过输出

### AC-2: 校验不修改字体
- **Type**: `rule`
- **Given**: 磁盘加载的合法或含坏表的字体（lazy 与非 lazy 两种）
- **When**: 运行 validateFont
- **Then**: 校验前后所有相关表的 `reader.getTableData(tag)` 字节完全一致；非 lazy 字体重新 save 到内存的字节与原文件一致；无任何字体对象被 setattr
- **Pass Condition**: 字节哈希一致与 save 幂等断言通过
- **Evidence**: validate_test.py 中非修改性用例

### AC-3: 懒加载兼容
- **Type**: `rule`
- **Given**: `TTFont(path, lazy=True)`
- **When**: 运行 validateFont
- **Then**: 校验后 `font.isLoaded("gvar")` 为 False（gvar 仅走原始字节）；不相关表（如 name、cmap）未加载；字体后续可正常反编译与 save；不含 gvar 硬 assert 崩溃路径
- **Pass Condition**: isLoaded 断言与后续可用性断言通过
- **Evidence**: validate_test.py lazy 用例

### AC-4: 全量收集与错误隔离
- **Type**: `rule`
- **Given**: 同时注入多个跨表错误的字体，以及某一表原始字节损坏/截断的字体
- **When**: 运行 validateFont
- **Then**: 一次调用返回全部已注入错误的定位 issue（不遇错即停）；损坏表产生 `table-parse-error`/对应 parse code，其他表错误仍被报告；validateFont 不向外抛异常；重复运行 issue 序列完全相同
- **Pass Condition**: 多错误并存与截断隔离用例通过，确定性用例通过
- **Evidence**: validate_test.py

### AC-5: fvar/gvar 跨表校验
- **Type**: `rule`
- **Given**: 分别篡改 gvar 原始字节的字体（axisCount、glyphCount、sharedTuple 索引越界、元组/偏移越界、表截断）
- **When**: 运行 validateFont
- **Then**: 每种篡改产生对应稳定 code 与精确到字形/tuple 索引的 location；普通 VF（无 VARC）gvar 轴数 ≠ fvar 报错；有 VARC 时 gvar 轴数 ≤ fvar 允许，> fvar 报错
- **Pass Condition**: 各篡改用例断言 code/location 正确，varc-ac01（6 vs 8）与 varc-6868（31 vs 31）均不报轴数错误
- **Evidence**: validate_test.py

### AC-6: 变体存储结构校验
- **Type**: `rule`
- **Given**: 对 HVAR/MVAR/GDEF/BASE/COLR/avar2/VARC 中存储的 RegionAxisCount、VarRegionIndex、SparseVarRegionAxis.AxisIndex、Item 长度等在内存对象上注入越界/不一致
- **When**: 运行 validateFont
- **Then**: 每个注入点产生定位到存储路径的 issue；合法字体不报
- **Pass Condition**: 覆盖每类存储至少一个越界用例并通过
- **Evidence**: validate_test.py

### AC-7: VarIdx 消费者校验
- **Type**: `rule`
- **Given**: 篡改 HVAR/VVAR map、MVAR ValueRecord、COLR Paint VarIndexBase/VarIndexMap/ClipBox、GDEF 系 0x8000 Device、VARC 组件 VarIndex（outer/inner 越界），以及“有引用但 VarStore=None”的字体
- **Then**: 每个越界 VarIdx 报 `var-idx` 类错误并定位；VarStore 缺失报独立 code；0xFFFFFFFF 哨兵不报；合法字体不报
- **Pass Condition**: 上述消费者各至少一个用例通过
- **Evidence**: validate_test.py

### AC-8: FeatureVariations 条件校验
- **Type**: `rule`
- **Given**: 含 GSUB/GPOS FeatureVariations 的字体（Format1 AxisIndex 越界、嵌套 Format3/4/5 内越界、Format2 非法出现、无 fvar）
- **Then**: 全部越界 AxisIndex 被递归定位；Format2 与无 fvar 报错；ConditionSet=None 与未知 Format 不报
- **Pass Condition**: 构造字体（TTX/newTable，无 UFO）相关断言通过
- **Evidence**: validate_test.py

### AC-9: VARC 专项校验
- **Type**: `rule`
- **Given**: 三份现有 varc 字体及在其内存对象上注入的 conditionIndex/axisIndicesIndex 越界、tuple 轴索引越界、axisValues 长度不符、组件 VarIdx 越界、Coverage 数不符
- **Then**: 合法三份字体（含无 fvar 的 varc-static-gvar）报告 ok；注入错误全部精确定位
- **Pass Condition**: 用例通过
- **Evidence**: validate_test.py

### AC-10: 合法字体回归干净
- **Type**: `rule`
- **Given**: varc-6868.ttf、varc-ac01-conditional.ttf、varc-static-gvar.ttf、由 TTX master 构建的 COLRv1 VF、仓库现有可变字体测试数据
- **When**: 运行 validateFont
- **Then**: 报告 ok 且 issue 为空
- **Pass Condition**: 全部合法字体断言通过；`Tests/varLib` 与相关 `Tests/ttLib` 用例回归无破坏
- **Evidence**: pytest 输出

### AC-11: 代码与 API 质量
- **Type**: `rubric`
- **Dimension**: 与 fontTools 代码风格/公共 API 约定的一致性（docstring、类型注解、命名、无新依赖、模块组织）
- **Scale**: 1-5
- **Anchors**: 1 = 风格冲突或引入新依赖；3 = 可用但缺 docstring/注解或命名不符约定；5 = 如同上游原生模块
- **Pass Threshold**: >= 4
- **Evidence**: 独立审查与源码走读

### AC-12: 错误报告可定位性
- **Type**: `rubric`
- **Dimension**: issue 的 code 稳定性、location 精确度与 message 可读性
- **Scale**: 1-5
- **Anchors**: 1 = 只有异常文本、无法定位；3 = 有表名但路径粗；5 = 稳定 code + 精确到记录/字段索引的路径 + 可读消息
- **Pass Threshold**: >= 4
- **Evidence**: 独立审查与断言示例

## Open Questions
- 无（关键设计歧义已在 Assumptions 中定案；如审查阶段发现新的合法特例，再回到本阶段修订）。
