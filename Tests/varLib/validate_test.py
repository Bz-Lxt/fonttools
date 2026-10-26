"""Tests for the public variable-font cross-table validator."""

import hashlib
import os
from collections import OrderedDict
from io import BytesIO
from types import SimpleNamespace

import pytest

from fontTools import varLib
from fontTools.designspaceLib import AxisDescriptor
from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.varLib import build
from fontTools.varLib.featureVars import addFeatureVariations
from fontTools.varLib.validate import (
    FontValidationReport,
    ValidationIssue,
    validateFont,
)


DATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ttLib", "data")
VARLIB_DATA = os.path.join(os.path.dirname(__file__), "data")

I_TTF = os.path.join(DATA, "I.ttf")
VARC_6868 = os.path.join(DATA, "varc-6868.ttf")
VARC_AC01 = os.path.join(DATA, "varc-ac01-conditional.ttf")
VARC_STATIC = os.path.join(DATA, "varc-static-gvar.ttf")
COLR_DESIGNSPACE = os.path.join(VARLIB_DATA, "TestVariableCOLR.designspace")


def codes(report):
    return sorted({issue.code for issue in report})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def iFont():
    return TTFont(I_TTF, lazy=True)


@pytest.fixture
def colrFont():
    font, _, _ = build(COLR_DESIGNSPACE)
    return font


@pytest.fixture
def ac01Font():
    return TTFont(VARC_AC01)


# ---------------------------------------------------------------------------
# Legitimate fonts must validate clean
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [I_TTF, VARC_6868, VARC_AC01, VARC_STATIC],
)
def test_valid_shipped_fonts_lazy(path):
    font = TTFont(path, lazy=True)
    report = validateFont(font)
    assert report.ok, str(report)


def test_valid_eager_i_ttf():
    font = TTFont(I_TTF)
    assert validateFont(font).ok


def test_valid_built_colr_v1_font(colrFont):
    assert validateFont(colrFont).ok


def test_varc_hidden_axes_allowed():
    # varc-ac01 declares 8 fvar axes but gvar.axisCount is 6; the trailing
    # hidden axes are a legal VARC construction.
    font = TTFont(VARC_AC01, lazy=True)
    assert "gvar-axis-count" not in validateFont(font)


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


def test_report_is_immutable_sequence(colrFont):
    colrFont["COLR"].table.VarIndexMap.mapping[0] = 0x990000
    report = validateFont(colrFont)

    assert not report.ok
    assert bool(report) is False
    assert len(report) >= 1
    assert isinstance(report[0], ValidationIssue)
    assert "var-idx" in report
    assert [issue for issue in report] == list(report.issues)

    tagged = report.byTag("COLR")
    assert len(tagged) >= 1
    assert all(issue.tag == "COLR" for issue in tagged)

    withCode = report.withCode("var-idx")
    assert len(withCode) >= 1
    assert all(issue.code == "var-idx" for issue in withCode)
    assert len(report.withCode("no-such-code")) == 0

    issue = withCode[0]
    assert issue.code in str(issue)
    assert "COLR" in issue.locationString
    assert "no issues" in str(FontValidationReport())


def test_location_renders_indices_as_brackets():
    issue = ValidationIssue(
        code="x",
        tag="HVAR",
        location=("HVAR", "VarStore", "VarData", 2, "Item"),
        message="m",
    )
    assert issue.locationString == "HVAR/VarStore/VarData[2]/Item"


def test_type_error_on_garbage_input():
    with pytest.raises(TypeError):
        validateFont(42)
    with pytest.raises(TypeError):
        validateFont(None)


# ---------------------------------------------------------------------------
# gvar raw-byte corruption
# ---------------------------------------------------------------------------


def patchedGvar(source, mutate=None, newLength=None):
    """Open ``source`` with one gvar byte-mutation applied, never touching disk."""
    data = bytearray(open(source, "rb").read())
    numberOfTables = int.from_bytes(data[4:6], "big")
    records = {}
    for i in range(numberOfTables):
        r = 12 + 16 * i
        tag = bytes(data[r:r + 4])
        records[tag] = (int.from_bytes(data[r + 8:r + 12], "big"), r)
    if mutate is not None:
        mutate(data, records[b"gvar"][0])
    if newLength is not None:
        _, record = records[b"gvar"]
        data[record + 12:record + 16] = newLength.to_bytes(4, "big")
    return TTFont(BytesIO(bytes(data)), lazy=True)


def setU16(data, offset, value):
    data[offset:offset + 2] = value.to_bytes(2, "big")


def setU32(data, offset, value):
    data[offset:offset + 4] = value.to_bytes(4, "big")


def gvarOffsets(raw):
    glyphCount = int.from_bytes(raw[12:14], "big")
    flags = int.from_bytes(raw[14:16], "big")
    dataOffset = int.from_bytes(raw[16:20], "big")
    entrySize = 2 if not (flags & 1) else 4
    offsets = []
    for i in range(glyphCount + 1):
        position = 20 + i * entrySize
        value = int.from_bytes(raw[position:position + entrySize], "big")
        if entrySize == 2:
            value *= 2
        offsets.append(value + dataOffset)
    return offsets


def test_gvar_axis_count_mismatch():
    font = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 4, 9))
    assert "gvar-axis-count" in validateFont(font)


def test_gvar_truncated_header():
    font = patchedGvar(I_TTF, newLength=10)
    assert codes(validateFont(font).byTag("gvar")) == ["gvar-truncated"]


def test_gvar_data_offset_out_of_bounds():
    font = patchedGvar(I_TTF, lambda d, o: setU32(d, o + 16, 0xFFFFFFFF))
    assert "gvar-glyph-data-oob" in validateFont(font)


def test_gvar_shared_tuples_out_of_bounds():
    font = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 6, 0xFFFF))
    assert "gvar-shared-tuples-oob" in validateFont(font)


def test_gvar_glyph_count_mismatch_collects_multiple():
    font = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 12, 0xFFFF))
    gvarIssues = codes(validateFont(font).byTag("gvar"))
    assert "gvar-glyph-count" in gvarIssues
    assert "gvar-offset-array-oob" in gvarIssues


def test_gvar_flags_long_offsets():
    # I.ttf uses short (halved) offsets; flipping bit 0 makes the offset
    # array overlap the data section.
    font = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 14, 1))
    assert "gvar-offset-array-oob" in validateFont(font)


def test_gvar_shared_tuple_index_out_of_bounds():
    # varc-6868 defines 15 shared tuples; point glyph00003's first tuple at
    # shared index 255.
    probe = TTFont(VARC_6868, lazy=True)
    raw = probe.reader["gvar"]
    gid = probe.getGlyphOrder().index("glyph00003")
    blockStart = gvarOffsets(raw)[gid]

    def mutate(data, tableOffset):
        # tuple 0: dataSize u16 at block+4, flags u16 at block+6 (low byte +7).
        data[tableOffset + blockStart + 7] = 0xFF

    font = patchedGvar(VARC_6868, mutate)
    assert "gvar-shared-tuple-index" in validateFont(font)


def test_gvar_axis_count_varc_greater_than_fvar():
    font = patchedGvar(VARC_6868, lambda d, o: setU16(d, o + 4, 40))
    assert "gvar-axis-count" in validateFont(font)


def test_gvar_axis_count_varc_less_than_fvar_ok():
    # VARC fonts may omit trailing hidden axes: a smaller gvar axisCount is
    # not an axis-count error (byte-corruption may still cascade elsewhere).
    font = patchedGvar(VARC_6868, lambda d, o: setU16(d, o + 4, 6))
    assert "gvar-axis-count" not in validateFont(font)


# ---------------------------------------------------------------------------
# Dense ItemVariationStore consumers (HVAR/MVAR/GDEF/GPOS)
# ---------------------------------------------------------------------------


def test_hvar_varidx_map_out_of_range(iFont):
    iFont["HVAR"].table.AdvWidthMap.mapping[".notdef"] = 0x990000
    assert "var-idx" in validateFont(iFont)


def test_hvar_store_missing(iFont):
    iFont["HVAR"].table.VarStore = None
    assert "var-store-missing" in validateFont(iFont)


def test_hvar_region_axis_count(iFont):
    store = iFont["HVAR"].table.VarStore
    store.VarRegionList.RegionAxisCount = 99
    assert "var-store-region-axis-count" in validateFont(iFont)


def test_hvar_region_index(iFont):
    store = iFont["HVAR"].table.VarStore
    varData = next(d for d in store.VarData if d.VarRegionIndex)
    varData.VarRegionIndex[0] = 9999
    assert "var-region-index" in validateFont(iFont)


def test_hvar_item_arity(iFont):
    store = iFont["HVAR"].table.VarStore
    data = next(d for d in store.VarData if d.VarRegionCount)
    data.Item[0] = tuple(data.Item[0]) + (0,)
    assert "var-store-item-count" in validateFont(iFont)


def test_mvar_record_without_store():
    font = TTFont(I_TTF, lazy=True)
    wrapper = newTable("MVAR")
    wrapper.table = SimpleNamespace(
        VarStore=None,
        ValueRecord=[SimpleNamespace(ValueTag=b"test", VarIdx=0)],
    )
    font["MVAR"] = wrapper
    assert "var-store-missing" in validateFont(font)


def test_gpos_device_variation_without_gdef_store():
    font = TTFont(I_TTF)
    addOpenTypeFeaturesFromString(font, "feature kern { pos I -50; } kern;")
    valueRecord = font["GPOS"].table.LookupList.Lookup[0].SubTable[0].Value
    device = ot.Device()
    device.DeltaFormat, device.StartSize, device.EndSize = 0x8000, 0, 100
    valueRecord.XAdvDevice = device
    assert "var-store-missing" in validateFont(font)


def test_gpos_device_variation_out_of_range_then_valid():
    font = TTFont(I_TTF)
    addOpenTypeFeaturesFromString(font, "feature kern { pos I -50; } kern;")
    valueRecord = font["GPOS"].table.LookupList.Lookup[0].SubTable[0].Value
    device = ot.Device()
    device.DeltaFormat, device.StartSize, device.EndSize = 0x8000, 0, 100
    valueRecord.XAdvDevice = device

    store = font["HVAR"].table.VarStore
    gdef = newTable("GDEF")
    gdef.table = SimpleNamespace(VarStore=store)
    font["GDEF"] = gdef
    assert "var-idx" in validateFont(font)

    # VarIdx 0x10000 (outer 1, inner 0) exists in the HVAR store; no GPOS issue.
    device.StartSize, device.EndSize = 1, 0
    assert validateFont(font).byTag("GPOS").ok


# ---------------------------------------------------------------------------
# FeatureVariations
# ---------------------------------------------------------------------------


def test_feature_variations_clean(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    assert validateFont(colrFont).ok


def _featureVariations(font):
    return font["GSUB"].table.FeatureVariations


def _replaceCondition(font, condition):
    record = _featureVariations(font).FeatureVariationRecord[0]
    record.ConditionSet.ConditionTable[0] = condition


def test_feature_variations_axis_index(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    conditionSet = _featureVariations(
        colrFont
    ).FeatureVariationRecord[0].ConditionSet
    conditionSet.ConditionTable[0].AxisIndex = 99
    assert "condition-axis-index" in validateFont(colrFont)


def test_feature_variations_format2_rejected(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    _replaceCondition(colrFont, SimpleNamespace(Format=2, VarIdx=0))
    assert "condition-format2" in validateFont(colrFont)


def test_feature_variations_nested_formats_3_4_5(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    badAxis = SimpleNamespace(
        Format=1,
        AxisIndex=99,
        FilterRangeMinValue=0.5,
        FilterRangeMaxValue=1.0,
    )
    _replaceCondition(
        colrFont,
        SimpleNamespace(
            Format=3,
            ConditionCount=1,
            ConditionTable=[
                SimpleNamespace(
                    Format=4,
                    ConditionCount=1,
                    ConditionTable=[
                        SimpleNamespace(
                            Format=5, ConditionCount=1, ConditionTable=badAxis
                        )
                    ],
                )
            ],
        ),
    )
    assert "condition-axis-index" in validateFont(colrFont)


def test_feature_variations_null_condition_set_is_legal(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    _featureVariations(colrFont).FeatureVariationRecord[0].ConditionSet = None
    assert validateFont(colrFont).ok


def test_feature_variations_unknown_format_ignored(colrFont):
    addFeatureVariations(colrFont, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    _replaceCondition(colrFont, SimpleNamespace(Format=42))
    assert validateFont(colrFont).ok


def test_feature_variations_without_fvar():
    # Build a minimal fvar-bearing font, add a feature variation, drop fvar.
    font = TTFont()
    font.setGlyphOrder([".notdef", "A", "B"])
    font["name"] = newTable("name")
    axis = AxisDescriptor()
    axis.name = axis.tag = axis.labelNames["en"] = "wght"
    axis.minimum, axis.default, axis.maximum = 400, 400, 900
    varLib._add_fvar(font, OrderedDict(wght=axis), instances=())
    font["OS/2"] = newTable("OS/2")
    font["OS/2"].usMaxContext = 0
    addFeatureVariations(font, [([{"wght": (0.5, 1.0)}], {"A": "B"})])
    del font["fvar"]
    assert "fvar-required" in validateFont(font)


# ---------------------------------------------------------------------------
# COLR v1
# ---------------------------------------------------------------------------


def _colrVariablePaints(root):
    paints = []

    def walk(node):
        if node.getVariableAttrs() and node.VarIndexBase is not None:
            paints.append(node)
        for entry in node.iterSubTables():
            if entry.value is not None:
                walk(entry.value)

    for record in root.BaseGlyphList.BaseGlyphPaintRecord:
        walk(record.Paint)
    for paint in root.LayerList.Paint:
        walk(paint)
    return paints


def test_colr_map_entry_out_of_range(colrFont):
    root = colrFont["COLR"].table
    root.VarIndexMap.mapping[0] = 0x990000
    assert "var-idx" in validateFont(colrFont)


def test_colr_paint_base_without_map(colrFont):
    root = colrFont["COLR"].table
    root.VarIndexMap = None
    paint = _colrVariablePaints(root)[0]
    paint.VarIndexBase = 0x990000
    assert "var-idx" in validateFont(colrFont)


def test_colr_store_missing(colrFont):
    root = colrFont["COLR"].table
    root.VarStore = None
    assert "colr-var-store-missing" in validateFont(colrFont)


def test_colr_store_missing_for_unmapped_paint(colrFont):
    root = colrFont["COLR"].table
    root.VarIndexMap = None
    root.VarStore = None
    assert "colr-var-store-missing" in validateFont(colrFont)


def test_colr_offset_cycle_terminates(colrFont):
    # A damaged font may contain offset cycles; the DFS must not hang.
    root = colrFont["COLR"].table
    cycled = False
    for node in _colrVariablePaints(root):
        for entry in node.iterSubTables():
            if entry.index is None and entry.value is not None:
                setattr(node, entry.name, node)
                cycled = True
                break
        if cycled:
            break
    assert cycled
    validateFont(colrFont)  # returning at all is the assertion


# ---------------------------------------------------------------------------
# VARC
# ---------------------------------------------------------------------------


def _varc(font):
    return font["VARC"].table


def _component(font, glyphIndex=1, componentIndex=0):
    return (
        _varc(font).VarCompositeGlyphs.VarCompositeGlyph[glyphIndex]
        .components[componentIndex]
    )


def test_varc_sparse_region_axis_index(ac01Font):
    axis = (
        _varc(ac01Font).MultiVarStore.SparseVarRegionList.Region[0]
        .SparseVarRegionAxis[0]
    )
    axis.AxisIndex = 99
    assert "varc-sparse-region-axis-index" in validateFont(ac01Font)


def test_varc_region_index(ac01Font):
    _varc(ac01Font).MultiVarStore.MultiVarData[0].VarRegionIndex[0] = 99
    assert "varc-multi-region-index" in validateFont(ac01Font)


def test_varc_axis_indices_list_axis(ac01Font):
    _varc(ac01Font).AxisIndicesList.Item[0] = (99,)
    assert "varc-axis-index" in validateFont(ac01Font)


def test_varc_condition_index(ac01Font):
    # glyph 0, component 1 legitimately references condition 0.
    _component(ac01Font, 0, 1).conditionIndex = 99
    assert "varc-condition-index" in validateFont(ac01Font)


def test_varc_axis_indices_index(ac01Font):
    _component(ac01Font).axisIndicesIndex = 99
    assert "varc-axis-indices-index" in validateFont(ac01Font)


def test_varc_axis_values_count(ac01Font):
    # glyph 2 component 0 uses axis-indices tuple 0 (3 axes).
    component = _component(ac01Font, 2, 0)
    component.axisValues = component.axisValues[:2]
    assert "varc-axis-values-count" in validateFont(ac01Font)


def test_varc_varidx_outer(ac01Font):
    _component(ac01Font).axisValuesVarIndex = 0x00090000
    assert "var-idx" in validateFont(ac01Font)


def test_varc_varidx_inner(ac01Font):
    # MultiVarData 0 has 5 item rows; inner 5 is out of range.
    _component(ac01Font).axisValuesVarIndex = 0x00000005
    assert "var-idx" in validateFont(ac01Font)


def test_varc_axis_values_arity(ac01Font):
    # glyph 1 component 0 uses 5-axis tuple 1; VarIdx 2 is a 3-value-per-
    # region row (9 values over 3 regions), so the arities disagree.
    _component(ac01Font).axisValuesVarIndex = 0x00000002
    assert "varc-axis-values-arity" in validateFont(ac01Font)


def test_varc_coverage_count(ac01Font):
    _varc(ac01Font).Coverage.glyphs.append("totally-made-up-glyph")
    assert "varc-coverage-count" in validateFont(ac01Font)


def test_varc_condition_axis_index(ac01Font):
    _varc(ac01Font).ConditionList.ConditionTable[0].AxisIndex = 99
    assert "condition-axis-index" in validateFont(ac01Font)


def test_varc_multi_store_missing(ac01Font):
    _varc(ac01Font).MultiVarStore = None
    assert "varc-var-store-missing" in validateFont(ac01Font)


# ---------------------------------------------------------------------------
# Read-only / lazy / determinism / error isolation
# ---------------------------------------------------------------------------


GVAR_RELATED_TAGS = ("fvar", "gvar", "HVAR", "GPOS", "avar")


def test_validation_does_not_mutate_raw_tables():
    font = TTFont(I_TTF, lazy=True)
    rawBefore = {tag: hashlib.sha256(font.reader[tag]).digest()
        for tag in GVAR_RELATED_TAGS}
    report = validateFont(font)
    assert report.ok
    for tag in GVAR_RELATED_TAGS:
        assert hashlib.sha256(font.reader[tag]).digest() == rawBefore[tag]


def test_validation_is_lazy():
    font = TTFont(I_TTF, lazy=True)
    assert not font.isLoaded("gvar")
    assert not font.isLoaded("name")
    validateFont(font)
    assert not font.isLoaded("gvar")
    assert not font.isLoaded("name")


def test_validation_is_deterministic_and_collects_all():
    font1 = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 12, 0xFFFF))
    font2 = patchedGvar(I_TTF, lambda d, o: setU16(d, o + 12, 0xFFFF))

    signature1 = [(i.code, i.tag, i.locationString) for i in validateFont(font1)]
    signature2 = [(i.code, i.tag, i.locationString) for i in validateFont(font2)]
    assert signature1 == signature2
    assert len(set(signature1)) >= 2  # multiple distinct problems at once


def test_distinct_table_errors_collected_together():
    # gvar truncation plus an unrelated HVAR problem must both be reported.
    font = patchedGvar(I_TTF, newLength=10)
    font["HVAR"].table.AdvWidthMap.mapping[".notdef"] = 0x990000
    report = validateFont(font)
    assert "gvar-truncated" in report
    assert "var-idx" in report


def patchedTableLength(source, tag, newLength):
    data = bytearray(open(source, "rb").read())
    for i in range(int.from_bytes(data[4:6], "big")):
        record = 12 + 16 * i
        if bytes(data[record:record + 4]) == tag.encode():
            data[record + 12:record + 16] = newLength.to_bytes(4, "big")
    return TTFont(BytesIO(bytes(data)), lazy=True)


@pytest.mark.parametrize(
    "source,tag,length",
    [
        (I_TTF, "HVAR", 6),
        (I_TTF, "GPOS", 0),
        (VARC_AC01, "VARC", 20),
    ],
)
def test_truncated_otl_table_is_parse_error_not_ok(source, tag, length):
    font = patchedTableLength(source, tag, length)
    report = validateFont(font)
    issues = report.byTag(tag)
    assert len(issues) == 1
    assert issues[0].code == "table-parse-error"
    assert not report.ok


def test_validation_has_no_effect_on_saved_bytes():
    # Validation must not leave the object graph in a state that keeps
    # changing compilation: two serializations after validation are equal.
    font = TTFont(I_TTF)
    validateFont(font)
    first = BytesIO()
    font.save(first)
    second = BytesIO()
    font.save(second)
    assert first.getvalue() == second.getvalue()


def test_feature_variations_checked_without_loading_gsub():
    # Conditions are parsed from raw bytes; the whole GSUB table stays lazy.
    font, _, _ = build(COLR_DESIGNSPACE)
    addFeatureVariations(font, [([{"wght": (0.5, 1.0)}], {"A": "A.0"})])
    buffer = BytesIO()
    font.save(buffer)
    diskFont = TTFont(BytesIO(buffer.getvalue()), lazy=True)
    report = validateFont(diskFont)
    assert report.ok, str(report)
    assert not diskFont.isLoaded("GSUB")
    assert not diskFont.isLoaded("GPOS")
