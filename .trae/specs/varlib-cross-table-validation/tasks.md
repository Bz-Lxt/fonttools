# fontTools 变体字体跨表一致性校验器 - 实施计划

实施说明：所有代码在 `Lib/fontTools/varLib/validate.py`（新建）；测试在 `Tests/varLib/validate_test.py`（新建）。运行命令统一为 `cd <repo> && PYTHONPATH=Lib /opt/homebrew/bin/python3.11 -m pytest <path> -q`。每个实现任务自带对应用例（实现与测试同一切片）。

## Task 1: 模块骨架、报告模型、轴权威与 gvar 原始字节校验
- **Status**: `completed` — validate.py 报告模型/gvar 原始字节解析（含 tuple 头边界，tupleSize 含 4 字节头）；TR-1.1~1.3 由 validate_test.py 11 个 gvar 用例覆盖，全绿。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 新建 `Lib/fontTools/varLib/validate.py`：模块/公共函数 docstring、`__all__ = ["validateFont", "FontValidationReport", "ValidationIssue"]`。
  - `ValidationIssue`（frozen dataclass：code/tag/location/message + location 字符串渲染属性）；`FontValidationReport`（issues 元组、`ok`/`__bool__`/`__iter__`/`__len__`/`byTag`/`withCode`/`__str__`）。
  - `validateFont(ttFont)`：TypeError 守卫；内部 issue collector；确定轴权威（fvar→gvar 原始 axisCount），gvar 原始 axisCount 解析函数同时供 VARC 使用。
  - gvar 检查：经 `ttFont.reader.getTableData("gvar")` 原始字节解析（无 reader 的内存字体回退已加载对象属性），覆盖 header 合法性、axisCount（无 VARC 必须等于 fvar；有 VARC 允许 ≤ fvar；>fvar 报错）、glyphCount 与 glyphOrder 长度、共享元组区边界、字形偏移数组单调性与越界（short/long offsets）、每字形 tuple header 的 shared tuple 索引与 tupleSize 越界、截断产生 parse-error。绝不调用 gvar.decompile。
- **Acceptance Criteria Addressed**: AC-1, AC-3, AC-5, AC-11, AC-12
- **Test Requirements**:
  - `rule` TR-1.1: 对 varc-6868 与一个普通 VF，gvar 原始解析报告为空；构造 gvar axisCount/glyphCount/sharedTuple 索引/数据越界/截断的内存字节副本（save→BytesIO→按 SFNT 目录定位改字节→重新 lazy 打开），每个用例断言正确 code 与 location；证据：pytest 输出。
  - `rule` TR-1.2: varc-ac01（gvar 6 vs fvar 8）不报轴数错误；构造 gvar 轴数 >fvar 的 VARC 字体报错。
  - `rule` TR-1.3: lazy=True 校验后 `font.isLoaded("gvar")` 为 False。
  - `rubric` TR-1.4: code 命名稳定、location 精确到字形名/tuple 索引、message 可读；scale 1-5；anchors 1=仅异常文本/3=仅表名/5=精确索引+可读；threshold >= 4；证据：用例断言与源码走读。
- **Notes**: gid_size 对 gvar 固定为 2；tuple flags：EMBEDDED=0x8000、INTERMEDIATE=0x4000；tupleSize 算法同 `TupleVariation.getTupleSize_`。

## Task 2: 密集 ItemVariationStore 通用检查与 VarIdx 消费者
- **Status**: `completed` — _checkItemStore/_walkDevices/HVAR/MVAR/avar2/CFF2/BASE/GDEF 全部实现；HVAR/MVAR/GPOS Device 用例覆盖。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 实现密集 VarStore 通用检查器（Format、RegionAxisCount==N、Region 轴数、VarRegionIndex 越界、Item 长度==VarRegionCount、VarDataCount），返回可供 VarIdx 解析的内部引用（outer/inner 边界）。
  - 注册全部存储位置：GDEF(>=1.3)、BASE(>=1.1)、MVAR、HVAR、VVAR、avar v2（wrapper.table.VarStore/VarIdxMap）、COLR（Task 4 接入）、CFF2 topDict.VarStore.otVarStore（结构层）。
  - VarIdx 消费者：HVAR/VVAR 全部 map（AdvWidthMap/LsbMap/RsbMap、AdvHeightMap/TsbMap/BsbMap/VOrgMap）的 `.mapping`；MVAR ValueRecord[].VarIdx；avar2 VarIdxMap.mapping；0xFFFFFFFF 哨兵合法。
  - 0x8000 Device 遍历器（参考 varStore.py `_visit` 语义：Device 特判、ValueRecord 特判、postRead 表跳过、list/tuple/UserList 递归、转换器递归，带逐属性异常隔离与路径记录）；根：GDEF/GPOS/MATH/JSTF 的 `.table`（解析目标为 GDEF.VarStore，缺失即报错）与 BASE.table（解析目标为 BASE 自身 VarStore）。
- **Acceptance Criteria Addressed**: AC-6, AC-7, AC-11
- **Test Requirements**:
  - `rule` TR-2.1: 在内存对象上对 HVAR map、MVAR record、GDEF Device（可用 feaLib 在内存 TTFont 上构建带 VariationIndex Device 的 GPOS，或直接 newTable 构造）、avar2 map、CFF2 store 注入越界 RegionAxisCount/VarRegionIndex/outer/inner，断言 code/location；哨兵 0xFFFFFFFF 不报。
  - `rule` TR-2.2: Device 0x8000 存在但 GDEF 无 VarStore 时报缺失错误；GPOS/MATH/JSTF 中的 Device 同样解析到 GDEF store。
  - `rubric` TR-2.3: 遍历器对 LazyList(UserList) 正确递归且不破坏惰性缓存语义；scale 1-5；anchors 1=漏掉惰性数组/3=功能对但有额外加载/5=完整且无副作用；threshold >= 4；证据：lazy 断言。
- **Notes**: VarStore 访问一律 getattr 容错；表不存在/版本不足视为无存储。

## Task 3: FeatureVariations 条件树检查
- **Status**: `completed` — Format1/2/3/4/5/未知/None/无 fvar 全覆盖；另改为从原始字节只解析 FeatureVariations 子表（OTTableReader+decompile），不强制整表 GSUB/GPOS 反编译，保留惰性。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 对 GSUB/GPOS `table.FeatureVariations`（None 跳过）：遍历 FeatureVariationRecord[].ConditionSet（None=永真，合法）；递归 ConditionTable：Format1 AxisIndex<N；Format3/4 子条件数组；Format5 单子条件；Format2 在该语境报“变量条件无对应存储”错误；未知 Format 前向兼容跳过。
  - FeatureVariations 存在但无 fvar 报 `fvar-required`。
- **Acceptance Criteria Addressed**: AC-8, AC-12
- **Test Requirements**:
  - `rule` TR-3.1: 用 TTX 字符串/newTable 在内存构造含 Format1、嵌套 Format3(Format4(Format1))、Format5、ConditionSet=None、未知 Format 的 FeatureVariations；越界 AxisIndex 全部定位，合法形态无 issue；Format2 与无 fvar 报错。
  - `rubric` TR-3.2: 递归路径 location 能区分记录/条件集/嵌套条件索引；scale 1-5；threshold >= 4；证据：断言。

## Task 4: COLR v1 变量引用检查
- **Status**: `completed` — DFS+id 防环、VarIndexMap clamp 语义、ClipBox；含 offset 环用例与 build COLRv1 合法字体用例。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - COLR version<1 跳过；对 `font["COLR"].table`：VarStore 走 Task 2 通用检查；遍历 BaseGlyphList.BaseGlyphPaintRecord[].Paint 与 LayerList.Paint[] 整棵子树（基于 iterSubTables 自建带路径 DFS，id 集合防环），每个节点经 `getVariableAttrs()` 找变量字段，`VarIndexBase != NO_VARIATION_INDEX` 时按字段最大偏移跨度校验 [base, base+span] 全部落在 VarStore；ClipList.clips 的 ClipBox 同法处理。
  - VarIndexMap 存在时逐值校验；有变量引用而 VarStore=None 报 `colr-var-store-missing`。
- **Acceptance Criteria Addressed**: AC-6, AC-7, AC-10
- **Test Requirements**:
  - `rule` TR-4.1: 用 `Tests/varLib/data/master_ttx_varcolr_ttf/TestVariableCOLR-{Regular,Bold}.ttx`（经 varLib.build 对 TTX master 构建，不依赖 defcon）得到合法 COLRv1 VF，报告为空。
  - `rule` TR-4.2: 内存篡改某变量 Paint 的 VarIndexBase（outer/inner 越界）、VarIndexMap 值、ClipBoxFormat2，并构造 VarStore=None 但保留变量 Paint；断言各自 code/location。
  - `rule` TR-4.3: 构造子表 offset 环（直接让两个对象互引）验证 DFS 不死循环。
  - `rubric` TR-4.4: location 含 BaseGlyph 名与 Paint 路径；scale 1-5；threshold >= 4。

## Task 5: VARC / 静态无 fvar 场景与公共导出
- **Status**: `completed` — 三份 varc 字体（含无 fvar、隐藏轴）全部 ok；14 个 VARC/FV 篡改用例；varLib __init__ 导出公共 API。
- **Priority**: high
- **Depends On**: Task 2, Task 3
- **Description**:
  - 校验前 `ttFont.getGlyphOrder()`；VARC 表检查（FR-9）：Coverage 数==VarCompositeGlyphs 数；AxisIndicesList tuple 轴索引<N（无 fvar 用 gvar 轴数）；MultiVarStore sparse 版（SparseVarRegionAxis.AxisIndex、MultiVarData.VarRegionIndex、Item 长度 % VarRegionCount）；ConditionList 复用条件递归（Format1 用 N；Format2 VarIdx 解析到 MultiVarStore，存储缺失报错）；逐 VarComponent 检查 conditionIndex、axisIndicesIndex、axisValues 长度、axisValuesVarIndex/transformVarIndex outer/inner、axisValues 的 deltas 宽度==轴数×regionCount。
  - CFF2 topDict VarStore 接入通用检查（Task 2 已预留）。
  - 在 `Lib/fontTools/varLib/__init__.py` 导出 `validateFont, FontValidationReport, ValidationIssue`（validate.py 不 import varLib 包，避免循环）。
- **Acceptance Criteria Addressed**: AC-1, AC-6, AC-7, AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-5.1: varc-6868、varc-ac01-conditional、varc-static-gvar 三份字体报告为空（含无 fvar 隐藏轴路径）。
  - `rule` TR-5.2: 在三份字体内存对象上注入：sparse AxisIndex 越界、VarRegionIndex 越界、AxisIndicesList 轴索引越界、conditionIndex/axisIndicesIndex 越界、axisValues 长度不符、组件 VarIdx outer/inner 越界、Coverage 数不符、Format2 条件 VarIdx 越界；断言 code/location。
  - `rule` TR-5.3: `from fontTools.varLib import validateFont` 可用。
  - `rubric` TR-5.4: 惰性 VARC INDEX 按需访问、无 fvar 路径无误报；scale 1-5；threshold >= 4。

## Task 6: 非修改性/确定性/隔离元测试与全量回归
- **Status**: `completed` — 原始字节 sha256、isLoaded(gvar/name/GSUB/GPOS)、连跑确定性、多表错误并存、save 幂等；Tests/varLib(925+57) + Tests/ttLib(1878) 全绿，10 skip 均为可选依赖缺失。
- **Priority**: high
- **Depends On**: Task 5
- **Description**:
  - 非修改性：校验前后对全部相关 tag 比对 `reader.getTableData` sha256；非 lazy 字体 save 到 BytesIO 与原文件逐字节一致。
  - 惰性：记录校验前后 isLoaded 集合，断言 gvar 不加载、无关表（name/cmap/...）不加载。
  - 隔离与确定性：gvar 截断字节副本 + 另一表对象错误并存时仍收集两方面 issue；连跑两次结果相等；损坏表不导致 validateFont 抛异常。
  - 回归：运行 `Tests/varLib` 全部与 `Tests/ttLib`（至少 VARC/COLR/gvar 相关文件）。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-10
- **Test Requirements**:
  - `rule` TR-6.1: 字节哈希、save 幂等、isLoaded 集合、确定性（两次 issue 列表相等）、截断隔离用例全部通过。
  - `rule` TR-6.2: `PYTHONPATH=Lib /opt/homebrew/bin/python3.11 -m pytest Tests/varLib Tests/ttLib -q` 无新增失败。
  - `rubric` TR-6.3: 校验过程对字体对象零副作用（审查无 setattr/compile/save 调用点）；scale 1-5；anchors 1=存在写路径/3=仅惰性缓存行为变化/5=零副作用；threshold >= 4；证据：源码审查+测试。

## Task 7: 独立审查
- **Status**: `completed` — review.md 已物化；第 1 轮 fail（截断 OTL 表静默 ok）已修复并经第 2 轮复核 closed，Review result == pass；validate_test.py 60 passed，varLib+ttLib 回归 2810 passed / 10 skipped。
- **Priority**: medium
- **Depends On**: Task 6
- **Description**:
  - 队列清空后创建 review.md，委派一个全新只读上下文按独立审查合同核验全部 AC/TR（规则优先，rubric 独立打分）；fail 时把可执行发现物化为 Issue 回到 Implement。
- **Acceptance Criteria Addressed**: AC-1 至 AC-12
- **Test Requirements**:
  - `rule` TR-7.1: 每个 AC 均有独立证据（审查者亲自运行命令/读代码，不仅引用实现者证据）；Review result == pass 方可结束。
  - `rubric` TR-7.2: 审查覆盖只读/惰性/全量收集三条核心承诺的反例尝试；scale 1-5；threshold >= 4。
