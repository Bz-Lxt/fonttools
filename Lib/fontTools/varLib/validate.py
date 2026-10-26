"""Public cross-table validation for variable fonts.

A variable font is only as consistent as its tables agree to be: the fact
that each of ``fvar``, ``gvar``, the various ItemVariationStores
(``GDEF``/``BASE``/``MVAR``/``HVAR``/``VVAR``/``COLR``/``avar`` v2/``CFF2``),
``FeatureVariations``, ``COLR`` v1 and ``VARC`` can be parsed on its own does
not mean that axis counts, axis indices and packed variation indices line up
between tables.  Some tables (notably ``gvar``) even use hard ``assert``
statements while decompiling, so a malformed third-party font crashes the
reader instead of producing a diagnosable error.

This module provides :func:`validateFont`, a strictly read-only checker that:

* never compiles, saves or mutates the font;
* is compatible with fonts opened with ``lazy=True`` (the ``gvar`` table is
  inspected through its raw bytes and is therefore never decompiled; other
  checks only materialize the ``otTables`` sub-trees they actually visit);
* collects *all* problems in one pass -- a damaged table is reported and the
  remaining tables are still checked;
* returns a structured, public :class:`FontValidationReport` with stable
  issue codes and precise locations.

Example usage::

    from fontTools.ttLib import TTFont
    from fontTools.varLib import validateFont

    font = TTFont("font.ttf", lazy=True)
    report = validateFont(font)
    if not report.ok:
        for issue in report:
            print(issue.code, issue.locationString, issue.message)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.otBase import OTTableReader


__all__ = [
	"validateFont",
	"FontValidationReport",
	"ValidationIssue",
]


NO_VARIATION_INDEX = 0xFFFFFFFF

# gvar tuple-variation flags (see tables._g_v_a_r)
_EMBEDDED_PEAK_TUPLE = 0x8000
_INTERMEDIATE_REGION = 0x4000
_TUPLE_INDEX_MASK = 0x0FFF

_GVAR_HEADER_SIZE = 20
_GVAR_GID_SIZE = 2  # glyphCount is a uint16 in gvar


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
	"""A single cross-table validation problem.

	Attributes:
		code: stable machine-readable identifier (e.g. ``"gvar-axis-count"``).
		tag: the top-level table tag the problem was found in (e.g. ``"HVAR"``).
		location: a stable path of field names (``str``) and record indices
			(``int``) starting with the table tag, e.g.
			``("HVAR", "VarStore", "VarData", 0, "VarRegionIndex", 2)``.
		message: human-readable description.
	"""

	code: str
	tag: str
	location: tuple
	message: str

	@property
	def locationString(self) -> str:
		"""Render :data:`location` as a ``Table/field[2]/...`` style string."""
		parts: list[str] = []
		for element in self.location:
			if isinstance(element, int):
				if parts:
					parts[-1] += f"[{element}]"
				else:
					parts.append(f"[{element}]")
			else:
				parts.append(str(element))
		return "/".join(parts)

	def __str__(self) -> str:
		return f"[{self.code}] {self.locationString}: {self.message}"


class FontValidationReport:
	"""Result of :func:`validateFont`.

	The report is an immutable sequence of :class:`ValidationIssue` objects.
	It is truthy when no issues were found, and supports a few convenience
	filters.
	"""

	def __init__(self, issues: Optional[list[ValidationIssue]] = None):
		self._issues = tuple(issues or ())

	@property
	def issues(self) -> tuple:
		"""All collected issues, in deterministic collection order."""
		return self._issues

	@property
	def ok(self) -> bool:
		"""``True`` when the report contains no issues."""
		return not self._issues

	def __bool__(self) -> bool:
		return self.ok

	def __iter__(self) -> Iterator[ValidationIssue]:
		return iter(self._issues)

	def __len__(self) -> int:
		return len(self._issues)

	def __getitem__(self, index):
		return self._issues[index]

	def __contains__(self, code: object) -> bool:
		return any(issue.code == code for issue in self._issues)

	def byTag(self, tag: str) -> "FontValidationReport":
		"""Return a sub-report containing only issues from ``tag``."""
		return FontValidationReport(
			[issue for issue in self._issues if issue.tag == tag]
		)

	def withCode(self, code: str) -> "FontValidationReport":
		"""Return a sub-report containing only issues with the given code."""
		return FontValidationReport(
			[issue for issue in self._issues if issue.code == code]
		)

	def __str__(self) -> str:
		if self.ok:
			return "Variable font validation report: no issues found."
		lines = [
			f"Variable font validation report: {len(self._issues)} issue(s) found."
		]
		lines.extend(str(issue) for issue in self._issues)
		return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _Collector:
	"""Accumulates issues during one validation run."""

	def __init__(self) -> None:
		self.issues: list[ValidationIssue] = []

	def add(self, tag: str, location: tuple, code: str, message: str) -> None:
		self.issues.append(
			ValidationIssue(code=code, tag=tag, location=tuple(location), message=message)
		)


def _otRoot(ttFont: TTFont, tag: str):
	"""Return the otTables root of an OTL-style table wrapper.

	BaseTTXConverter wrappers expose the parsed otTables structure as
	``.table``; other layouts do not.  Returns ``None`` when the table is
	absent.  A present-but-unparseable table raises (callers distinguish
	"missing" from "damaged" via :func:`_sectionRoot`); swallowing that
	exception would make a corrupt third-party font validate as healthy.
	"""
	if tag not in ttFont:
		return None
	return getattr(ttFont[tag], "table", ttFont[tag])


# Sentinel for "the table exists in the directory but failed to parse";
# sections must not traverse it the way they traverse a genuinely absent
# optional table, while the parse failure has already been reported once.
_UNREADABLE = object()


def _sectionRoot(collector: "_Collector", ttFont: TTFont, tag: str):
	"""Like :func:`_otRoot` but report present-but-unreadable tables.

	Returns ``None`` for an absent (optional) table, ``_UNREADABLE`` when
	the table exists in the sfnt directory but cannot be decompiled, and
	the parsed root otherwise.
	"""
	if tag not in ttFont:
		return None
	try:
		wrapper = ttFont[tag]
	except Exception as exception:
		collector.add(
			tag,
			(tag,),
			"table-parse-error",
			f"cannot read {tag}: {exception}",
		)
		return _UNREADABLE
	return getattr(wrapper, "table", wrapper)


def _rawTableData(ttFont: TTFont, tag: str):
	"""Return a table's raw, not-yet-decompiled bytes without loading it.

	Returns ``None`` when the font has no backing reader (e.g. a font built
	entirely in memory) or the raw data cannot be obtained.
	"""
	reader = getattr(ttFont, "reader", None)
	if reader is None or tag not in reader:
		return None
	try:
		return reader[tag]
	except Exception:
		return None


class _StoreRef:
	"""Resolved bounds of an ItemVariationStore, used to validate VarIdxes."""

	__slots__ = ("varDataCount", "itemCounts", "regionCount", "regionAxisCount")

	def __init__(self, varDataCount, itemCounts, regionCount, regionAxisCount):
		self.varDataCount = varDataCount
		self.itemCounts = itemCounts
		self.regionCount = regionCount
		self.regionAxisCount = regionAxisCount

	def resolves(self, varIdx) -> bool:
		if varIdx == NO_VARIATION_INDEX:
			return True
		outer = varIdx >> 16
		inner = varIdx & 0xFFFF
		if outer >= self.varDataCount:
			return False
		return inner < self.itemCounts[outer]


def _checkVarIdx(collector, tag, location, varIdx, ref, missingCode):
	"""Validate one packed VarIdx against a resolved store reference."""
	if varIdx is None or varIdx == NO_VARIATION_INDEX:
		return
	if ref is None:
		collector.add(
			tag,
			location,
			missingCode,
			"variation index is referenced but no variation store is present",
		)
		return
	if not ref.resolves(varIdx):
		outer = varIdx >> 16
		inner = varIdx & 0xFFFF
		collector.add(
			tag,
			location,
			"var-idx",
			f"variation index (outer={outer}, inner={inner}) is out of range of "
			f"the variation store ({ref.varDataCount} VarData subtable(s))",
		)


def _checkIdxMap(collector, tag, location, idxMap, ref, missingCode):
	mapping = getattr(idxMap, "mapping", None)
	if mapping is None:
		return
	if isinstance(mapping, dict):
		# VarIdxMap is keyed by glyph name; keep that key in the location.
		for glyphName, varIdx in mapping.items():
			_checkVarIdx(
				collector, tag, location + (glyphName,), varIdx, ref, missingCode
			)
	else:
		# DeltaSetIndexMap is a position-indexed list.
		for index, varIdx in enumerate(mapping):
			_checkVarIdx(
				collector, tag, location + (index,), varIdx, ref, missingCode
			)


# ---------------------------------------------------------------------------
# gvar (raw-bytes parser; never triggers the hard-asserting decompile path)
# ---------------------------------------------------------------------------


def _parseGvarHeader(data: bytes):
	"""Parse the gvar binary header without touching any font object.

	Returns ``(axisCount, sharedTupleCount, sharedTuplesOffset, glyphCount,
	flags, dataOffset)`` or ``None`` when the header is truncated.
	"""
	if len(data) < _GVAR_HEADER_SIZE:
		return None
	return (
		int.from_bytes(data[4:6], "big"),
		int.from_bytes(data[6:8], "big"),
		int.from_bytes(data[8:12], "big"),
		int.from_bytes(data[12:14], "big"),
		int.from_bytes(data[14:16], "big"),
		int.from_bytes(data[16:20], "big"),
	)


def _gvarRawAxisCount(ttFont: TTFont):
	"""Return gvar.axisCount from raw bytes (or a loaded object), or None."""
	data = _rawTableData(ttFont, "gvar")
	if data is not None:
		header = _parseGvarHeader(data)
		return None if header is None else header[0]
	if "gvar" in ttFont and ttFont.isLoaded("gvar"):
		try:
			return ttFont["gvar"].axisCount
		except Exception:
			return None
	return None


def _tupleSize(tupleFlags: int, axisCount: int) -> int:
	# Includes the (dataSize, flags) tuple header, matching
	# TupleVariation.TupleVariation.getTupleSize_.
	size = 4
	if tupleFlags & _EMBEDDED_PEAK_TUPLE:
		size += 2 * axisCount
	if tupleFlags & _INTERMEDIATE_REGION:
		size += 4 * axisCount
	return size


def _checkGvarRaw(collector, data: bytes, nFvarAxes, hasVarc, glyphOrder):
	header = _parseGvarHeader(data)
	if header is None:
		collector.add(
			"gvar",
			("gvar",),
			"gvar-truncated",
			f"gvar table is too short for a header "
			f"({len(data)} bytes, need {_GVAR_HEADER_SIZE})",
		)
		return

	axisCount, sharedTupleCount, sharedOffset, glyphCount, flags, dataOffset = header

	# An axis-count mismatch is only reported once we know the gvar table
	# actually carries tuple data: fontTools itself emits an empty gvar with
	# axisCount 0 for fonts whose glyphs do not vary.
	def reportAxisMismatch():
		if nFvarAxes is None:
			return
		if hasVarc:
			if axisCount > nFvarAxes:
				collector.add(
					"gvar",
					("gvar", "axisCount"),
					"gvar-axis-count",
					f"gvar axisCount {axisCount} is greater than the "
					f"{nFvarAxes} fvar axis/axes (VARC fonts may only omit "
					f"trailing hidden axes)",
				)
		elif axisCount != nFvarAxes:
			collector.add(
				"gvar",
				("gvar", "axisCount"),
				"gvar-axis-count",
				f"gvar axisCount {axisCount} does not match fvar axisCount "
				f"{nFvarAxes}",
			)

	hasTupleData = sharedTupleCount > 0
	if hasTupleData:
		reportAxisMismatch()

	if glyphOrder is not None and glyphCount != len(glyphOrder):
		collector.add(
			"gvar",
			("gvar", "glyphCount"),
			"gvar-glyph-count",
			f"gvar glyphCount {glyphCount} does not match the font's "
			f"{len(glyphOrder)} glyph(s)",
		)

	dataLength = len(data)

	# Shared (global) tuple coordinate records.
	sharedEnd = sharedOffset + sharedTupleCount * 2 * axisCount
	if sharedOffset > dataLength or sharedEnd > dataLength:
		collector.add(
			"gvar",
			("gvar", "sharedTuples"),
			"gvar-shared-tuples-oob",
			f"shared tuple records extend past the end of the table "
			f"(offset {sharedOffset}, {sharedTupleCount} record(s) of "
			f"{2 * axisCount} byte(s), table length {dataLength})",
		)

	# Glyph variation-data offset array.
	# flags bit 0 clear: short (uint16, halved) offsets; set: long uint32.
	shortOffsets = not bool(flags & 1)
	entrySize = 2 if shortOffsets else 4
	offsetArrayEnd = _GVAR_HEADER_SIZE + (glyphCount + 1) * entrySize
	if offsetArrayEnd > dataOffset or offsetArrayEnd > dataLength:
		collector.add(
			"gvar",
			("gvar", "glyphOffsets"),
			"gvar-offset-array-oob",
			f"glyph variation-data offset array needs {offsetArrayEnd} byte(s) "
			f"but the variation data starts at offset {dataOffset} and the "
			f"table is {dataLength} byte(s) long",
		)
		return

	offsets = []
	for i in range(glyphCount + 1):
		position = _GVAR_HEADER_SIZE + i * entrySize
		if shortOffsets:
			value = int.from_bytes(data[position:position + 2], "big") * 2
		else:
			value = int.from_bytes(data[position:position + 4], "big")
		offsets.append(value + dataOffset)

	for i in range(glyphCount):
		if offsets[i] > offsets[i + 1]:
			collector.add(
				"gvar",
				("gvar", "glyphOffsets", i),
				"gvar-glyph-offset-order",
				f"variation data offsets for glyph {i} are not monotonically "
				f"non-decreasing ({offsets[i]} > {offsets[i + 1]})",
			)
	if offsets[-1] > dataLength:
		collector.add(
			"gvar",
			("gvar", "glyphOffsets", glyphCount),
			"gvar-glyph-data-oob",
			f"last glyph variation data offset {offsets[-1]} extends past the "
			f"end of the table (length {dataLength})",
		)

	# Per-glyph tuple headers (bounds and shared-tuple indices only; the
	# point-number/private-data payloads are intentionally not parsed).
	for gid in range(glyphCount):
		start, end = offsets[gid], offsets[gid + 1]
		if end > dataLength or start > end:
			continue
		glyphName = glyphOrder[gid] if glyphOrder is not None and gid < len(
			glyphOrder
		) else gid
		blockLength = end - start
		if blockLength == 0:
			continue
		if blockLength < 4:
			collector.add(
				"gvar",
				("gvar", "glyphs", glyphName),
				"gvar-tuple-truncated",
				f"variation data block for glyph {glyphName!r} is {blockLength} "
				f"byte(s), shorter than the tuple variation header",
			)
			continue
		tupleCount = int.from_bytes(data[start:start + 2], "big") & 0x0FFF
		if tupleCount and not hasTupleData:
			hasTupleData = True
			reportAxisMismatch()
		tupleDataOffset = int.from_bytes(data[start + 2:start + 4], "big")
		tupleSectionEnd = start + tupleDataOffset
		if tupleDataOffset and (
			tupleSectionEnd < start + 4 or tupleSectionEnd > end
		):
			tupleSectionEnd = end
		position = start + 4
		for tupleIndex in range(tupleCount):
			if position + 4 > end:
				collector.add(
					"gvar",
					("gvar", "glyphs", glyphName, "tuples", tupleIndex),
					"gvar-tuple-truncated",
					f"tuple header {tupleIndex} for glyph {glyphName!r} "
					f"extends past its variation data block",
				)
				return
			tupleFlags = int.from_bytes(data[position + 2:position + 4], "big")
			size = _tupleSize(tupleFlags, axisCount)
			if position + size > tupleSectionEnd:
				collector.add(
					"gvar",
					("gvar", "glyphs", glyphName, "tuples", tupleIndex),
					"gvar-tuple-truncated",
					f"tuple {tupleIndex} for glyph {glyphName!r} needs "
					f"{size} byte(s) of coordinates but the tuple header "
					f"section only has {tupleSectionEnd - position} byte(s) "
					f"left",
				)
				return
			if not tupleFlags & _EMBEDDED_PEAK_TUPLE:
				sharedIndex = tupleFlags & _TUPLE_INDEX_MASK
				if sharedIndex >= sharedTupleCount:
					collector.add(
						"gvar",
						(
							"gvar", "glyphs", glyphName, "tuples", tupleIndex,
							"sharedTupleIndex",
						),
						"gvar-shared-tuple-index",
						f"tuple {tupleIndex} for glyph {glyphName!r} references "
						f"shared tuple {sharedIndex} but only {sharedTupleCount} "
						f"shared tuple(s) are defined",
					)
			position += size


def _checkGvar(collector, ttFont, nFvarAxes, hasVarc, glyphOrder):
	data = _rawTableData(ttFont, "gvar")
	if data is not None:
		_checkGvarRaw(collector, data, nFvarAxes, hasVarc, glyphOrder)
		return
	if not ( "gvar" in ttFont and ttFont.isLoaded("gvar")):
		return
	# Font built entirely in memory (no reader): the table object has
	# already been parsed (or constructed), so only the cross-table facts
	# that do not require raw bytes can be checked.
	try:
		gvar = ttFont["gvar"]
	except Exception as exception:
		collector.add(
			"gvar", ("gvar",), "table-parse-error", f"cannot read gvar: {exception}"
		)
		return
	variations = getattr(gvar, "variations", None)
	hasTupleData = bool(getattr(gvar, "sharedTuples", None)) or (
		variations is not None and any(variations.values())
	)
	if nFvarAxes is not None and hasTupleData:
		axisCount = getattr(gvar, "axisCount", None)
		if axisCount is not None:
			bad = axisCount > nFvarAxes if hasVarc else axisCount != nFvarAxes
			if bad:
				collector.add(
					"gvar",
					("gvar", "axisCount"),
					"gvar-axis-count",
					f"gvar axisCount {axisCount} does not agree with fvar "
					f"axisCount {nFvarAxes}",
				)
	if glyphOrder is not None and variations is not None:
		if len(variations) != len(glyphOrder):
			collector.add(
				"gvar",
				("gvar", "glyphCount"),
				"gvar-glyph-count",
				f"gvar carries variations for {len(variations)} glyph(s) but "
				f"the font has {len(glyphOrder)} glyph(s)",
			)


# ---------------------------------------------------------------------------
# ItemVariationStore (dense) and packed VarIdx consumers
# ---------------------------------------------------------------------------


def _checkItemStore(collector, tag, location, store, nAxes):
	"""Validate one ItemVariationStore; return a :class:`_StoreRef`."""
	fmt = getattr(store, "Format", None)
	if fmt != 1:
		collector.add(
			tag,
			location + ("Format",),
			"var-store-format",
			f"ItemVariationStore Format {fmt} is not supported (expected 1)",
		)
		return None

	regionList = getattr(store, "VarRegionList", None)
	if regionList is None:
		collector.add(
			tag,
			location + ("VarRegionList",),
			"var-store-region-list",
			"ItemVariationStore has no VarRegionList",
		)
		return None

	regionAxisCount = getattr(regionList, "RegionAxisCount", None)
	if nAxes is not None and regionAxisCount != nAxes:
		collector.add(
			tag,
			location + ("VarRegionList", "RegionAxisCount"),
			"var-store-region-axis-count",
			f"VarRegionList.RegionAxisCount {regionAxisCount} does not match "
			f"the {nAxes} font variation axis/axes",
		)

	regionCount = getattr(regionList, "RegionCount", 0)
	regions = getattr(regionList, "Region", None) or []
	if len(regions) != regionCount:
		collector.add(
			tag,
			location + ("VarRegionList", "Region"),
			"var-store-region-count",
			f"VarRegionList declares {regionCount} region(s) but contains "
			f"{len(regions)}",
		)
	for regionIndex, region in enumerate(regions):
		axes = getattr(region, "VarRegionAxis", None) or []
		if regionAxisCount is not None and len(axes) != regionAxisCount:
			collector.add(
				tag,
				location
				+ ("VarRegionList", "Region", regionIndex, "VarRegionAxis"),
				"var-store-region-axis-count",
				f"VarRegion {regionIndex} contains {len(axes)} region axis/"
				f"axes but RegionAxisCount is {regionAxisCount}",
			)

	varData = getattr(store, "VarData", None) or []
	declaredCount = getattr(store, "VarDataCount", len(varData))
	if declaredCount != len(varData):
		collector.add(
			tag,
			location + ("VarData",),
			"var-store-data-count",
			f"VarStore declares VarDataCount {declaredCount} but contains "
			f"{len(varData)} VarData subtable(s)",
		)

	itemCounts = []
	for dataIndex, data in enumerate(varData):
		dataLocation = location + ("VarData", dataIndex)
		regionIndices = getattr(data, "VarRegionIndex", None) or []
		for slot, regionIndex in enumerate(regionIndices):
			if regionIndex >= regionCount:
				collector.add(
					tag,
					dataLocation + ("VarRegionIndex", slot),
					"var-region-index",
					f"VarData {dataIndex} references VarRegion {regionIndex} "
					f"but the VarRegionList only defines {regionCount}",
				)
		regionCountForData = getattr(data, "VarRegionCount", len(regionIndices))
		items = getattr(data, "Item", None) or []
		# Item is the inner-index (delta-set) array; every item is a tuple
		# whose length must equal the number of regions referenced here.
		for itemIndex, item in enumerate(items):
			if len(item) != regionCountForData:
				collector.add(
					tag,
					dataLocation + ("Item", itemIndex),
					"var-store-item-count",
					f"VarData {dataIndex} item {itemIndex} contains "
					f"{len(item)} delta(s) but VarRegionCount is "
					f"{regionCountForData}",
				)
		itemCounts.append(len(items))

	return _StoreRef(len(varData), itemCounts, regionCount, regionAxisCount)


# ---------------------------------------------------------------------------
# Device-table traversal (DeltaFormat 0x8000 packs a VarIdx)
# ---------------------------------------------------------------------------


def _walkDevices(collector, tag, root, locationBase):
	"""Yield ``(location, device)`` for every 0x8000 Device below ``root``.

	Mirrors the traversal semantics of fontTools.varLib.varStore._visit
	(Device and ValueRecord special cases, postRead containers skipped) but
	keeps a stable location path, isolates per-attribute failures, and also
	descends into lazy ``UserList`` sequences.
	"""

	stack = [(root, locationBase)]
	while stack:
		obj, path = stack.pop()
		if obj is None or isinstance(obj, (str, bytes, int, float, bool)):
			continue
		if type(obj) is ot.Device:
			if getattr(obj, "DeltaFormat", None) == 0x8000:
				yield path, obj
			continue
		if isinstance(obj, ot.ValueRecord):
			for name, value in vars(obj).items():
				if isinstance(value, ot.Device):
					stack.append((value, path + (name,)))
			continue
		if isinstance(obj, (list, tuple)):
			for index in range(len(obj) - 1, -1, -1):
				stack.append((obj[index], path + (index,)))
			continue
		getConverters = getattr(obj, "getConverters", None)
		if not callable(getConverters) or hasattr(obj, "postRead"):
			continue
		try:
			converters = list(obj.getConverters())
		except Exception:
			continue
		for converter in reversed(list(converters)):
			name = converter.name
			try:
				value = getattr(obj, name, None)
			except Exception as exception:
				collector.add(
					tag,
					path + (name,),
					"table-parse-error",
					f"cannot traverse {name}: {exception}",
				)
				continue
			if value is not None:
				stack.append((value, path + (name,)))


def _deviceVarIdx(device) -> int:
	return (device.StartSize << 16) | device.EndSize


# ---------------------------------------------------------------------------
# Condition trees (shared by FeatureVariations and VARC.ConditionList)
# ---------------------------------------------------------------------------


def _checkCondition(
	collector, tag, location, condition, nAxes, multiStoreRef, featureVarContext
):
	"""Check one ConditionTable, recursing into Format 3/4/5 containers.

	``featureVarContext`` is True for GSUB/GPOS FeatureVariations (where
	Format 2 conditions have no defined store and must be rejected) and
	False for VARC ConditionLists (where Format 2 VarIdxes resolve against
	the VARC MultiVarStore).
	"""
	if condition is None:
		return
	fmt = getattr(condition, "Format", None)
	if fmt == 1:
		axisIndex = getattr(condition, "AxisIndex", None)
		if nAxes is not None and axisIndex is not None and axisIndex >= nAxes:
			collector.add(
				tag,
				location + ("AxisIndex",),
				"condition-axis-index",
				f"condition Format 1 references axis index {axisIndex} but the "
				f"font only defines {nAxes} axis/axes",
			)
	elif fmt == 2:
		varIdx = getattr(condition, "VarIdx", None)
		if featureVarContext:
			collector.add(
				tag,
				location + ("Format",),
				"condition-format2",
				"Format 2 (variation) conditions are only defined for VARC "
				"ConditionLists, not GSUB/GPOS FeatureVariations",
			)
		else:
			_checkVarIdx(
				collector,
				tag,
				location + ("VarIdx",),
				varIdx,
				multiStoreRef,
				"varc-var-store-missing",
			)
	elif fmt in (3, 4):
		children = getattr(condition, "ConditionTable", None) or []
		for index, child in enumerate(children):
			_checkCondition(
				collector,
				tag,
				location + ("ConditionTable", index),
				child,
				nAxes,
				multiStoreRef,
				featureVarContext,
			)
	elif fmt == 5:
		_checkCondition(
			collector,
			tag,
			location + ("ConditionTable",),
			getattr(condition, "ConditionTable", None),
			nAxes,
			multiStoreRef,
			featureVarContext,
		)
	# Any other format is forward-compatible and silently ignored.


def _checkConditionSet(
	collector, tag, location, conditionSet, nAxes, multiStoreRef, featureVarContext
):
	if conditionSet is None:
		# A NULL ConditionSet means "always true" and is legal.
		return
	conditions = getattr(conditionSet, "ConditionTable", None) or []
	for index, condition in enumerate(conditions):
		_checkCondition(
			collector,
			tag,
			location + ("ConditionTable", index),
			condition,
			nAxes,
			multiStoreRef,
			featureVarContext,
		)


# ---------------------------------------------------------------------------
# Individual table sections
# ---------------------------------------------------------------------------


def _checkHvarVvar(collector, ttFont, tag, mapNames, nAxes):
	root = _sectionRoot(collector, ttFont, tag)
	if root is None or root is _UNREADABLE:
		return
	store = getattr(root, "VarStore", None)
	storeLocation = (tag, "VarStore")
	ref = (
		_checkItemStore(collector, tag, storeLocation, store, nAxes)
		if store is not None
		else None
	)
	for mapName in mapNames:
		idxMap = getattr(root, mapName, None)
		if idxMap is not None:
			_checkIdxMap(
				collector,
				tag,
				(tag, mapName),
				idxMap,
				ref,
				"var-store-missing",
			)


def _checkMvar(collector, ttFont, nAxes):
	tag = "MVAR"
	root = _sectionRoot(collector, ttFont, tag)
	if root is None or root is _UNREADABLE:
		return
	store = getattr(root, "VarStore", None)
	ref = (
		_checkItemStore(collector, tag, (tag, "VarStore"), store, nAxes)
		if store is not None
		else None
	)
	for index, record in enumerate(getattr(root, "ValueRecord", None) or []):
		_checkVarIdx(
			collector,
			tag,
			(tag, "ValueRecord", index, "VarIdx"),
			getattr(record, "VarIdx", None),
			ref,
			"var-store-missing",
		)


def _checkAvar2(collector, ttFont, nAxes):
	tag = "avar"
	if tag not in ttFont:
		return
	try:
		wrapper = ttFont[tag]
	except Exception as exception:
		collector.add(tag, (tag,), "table-parse-error", f"cannot read avar: {exception}")
		return
	if getattr(wrapper, "majorVersion", 1) < 2:
		return
	root = getattr(wrapper, "table", None)
	if root is None:
		return
	store = getattr(root, "VarStore", None)
	ref = (
		_checkItemStore(collector, tag, (tag, "VarStore"), store, nAxes)
		if store is not None
		else None
	)
	idxMap = getattr(root, "VarIdxMap", None)
	if idxMap is not None:
		_checkIdxMap(
			collector, tag, (tag, "VarIdxMap"), idxMap, ref, "var-store-missing"
		)


def _checkCff2(collector, ttFont, nAxes):
	tag = "CFF2"
	if tag not in ttFont:
		return
	try:
		cff = ttFont[tag].cff
		varStoreData = getattr(cff.topDictIndex[0], "VarStore", None)
	except Exception as exception:
		collector.add(tag, (tag,), "table-parse-error", f"cannot read CFF2: {exception}")
		return
	if varStoreData is None:
		return
	store = getattr(varStoreData, "otVarStore", None)
	if store is not None:
		_checkItemStore(collector, tag, (tag, "VarStore"), store, nAxes)


def _checkGdefFamilyDevices(collector, ttFont, gdefRef):
	# Per the variation-store conventions, Device tables with
	# DeltaFormat 0x8000 anywhere in GDEF/GPOS/MATH/JSTF resolve against
	# the GDEF ItemVariationStore.
	for tag in ("GDEF", "GPOS", "MATH", "JSTF"):
		root = _sectionRoot(collector, ttFont, tag)
		if root is None or root is _UNREADABLE:
			continue
		try:
			devices = list(_walkDevices(collector, tag, root, (tag,)))
		except Exception as exception:
			collector.add(
				tag, (tag,), "table-parse-error", f"cannot traverse {tag}: {exception}"
			)
			continue
		for location, device in devices:
			_checkVarIdx(
				collector,
				tag,
				location,
				_deviceVarIdx(device),
				gdefRef,
				"var-store-missing",
			)


def _checkBase(collector, ttFont, nAxes):
	tag = "BASE"
	root = _sectionRoot(collector, ttFont, tag)
	if root is None or root is _UNREADABLE:
		return
	store = getattr(root, "VarStore", None)
	ref = (
		_checkItemStore(collector, tag, (tag, "VarStore"), store, nAxes)
		if store is not None
		else None
	)
	try:
		devices = list(_walkDevices(collector, tag, root, (tag,)))
	except Exception as exception:
		collector.add(
			tag, (tag,), "table-parse-error", f"cannot traverse BASE: {exception}"
		)
		return
	for location, device in devices:
		_checkVarIdx(
			collector,
			tag,
			location,
			_deviceVarIdx(device),
			ref,
			"var-store-missing",
		)


def _rawFeatureVariations(ttFont: TTFont, tag: str):
	"""Parse just the FeatureVariations subtable of a GSUB/GPOS table.

	FeatureVariations live behind an offset inside the GSUB/GPOS headers,
	but touching ``root.FeatureVariations`` on a lazily opened font forces
	the *entire* layout table to decompile (lazy offsets are resolved at
	table granularity).  Conditions are the only thing we need to inspect,
	so parse them straight from the raw bytes, materializing nothing else.

	Returns the parsed FeatureVariations object, ``None`` when the table
	absent/unversioned, and re-raises parse errors for the caller to report.
	"""
	raw = _rawTableData(ttFont, tag)
	if raw is None:
		return None
	# Header: Version u32, ScriptListOffset u16, FeatureListOffset u16,
	# LookupListOffset u16; the FeatureVariations u32 offset exists only
	# from Version 1.1 (see otData, aux="Version >= 0x00010001").
	if len(raw) < 14 or int.from_bytes(raw[0:4], "big") < 0x00010001:
		return None
	featureVariationsOffset = int.from_bytes(raw[10:14], "big")
	if featureVariationsOffset == 0:
		return None
	reader = OTTableReader(raw, None, featureVariationsOffset, tag)
	featureVariations = ot.FeatureVariations()
	featureVariations.decompile(reader, ttFont)
	return featureVariations


def _checkFeatureVariations(collector, ttFont, nFvarAxes):
	for tag in ("GSUB", "GPOS"):
		try:
			featureVariations = _rawFeatureVariations(ttFont, tag)
			if featureVariations is None and _rawTableData(ttFont, tag) is None:
				# Font built entirely in memory (no reader): the object
				# graph is already fully materialized, so direct attribute
				# access has no extra cost or side effect.
				root = _otRoot(ttFont, tag)
				featureVariations = (
					getattr(root, "FeatureVariations", None)
					if root is not None
					else None
				)
		except Exception as exception:
			collector.add(
				tag,
				(tag, "FeatureVariations"),
				"table-parse-error",
				f"cannot read FeatureVariations: {exception}",
			)
			continue
		if featureVariations is None:
			continue
		if nFvarAxes is None:
			collector.add(
				tag,
				(tag, "FeatureVariations"),
				"fvar-required",
				"FeatureVariations require an fvar table, but no fvar table "
				"is present",
			)
		records = getattr(featureVariations, "FeatureVariationRecord", None) or []
		for recordIndex, record in enumerate(records):
			location = (
				tag, "FeatureVariations", "FeatureVariationRecord", recordIndex,
				"ConditionSet",
			)
			_checkConditionSet(
				collector,
				tag,
				location,
				getattr(record, "ConditionSet", None),
				nFvarAxes,
				None,
				True,
			)


# ---------------------------------------------------------------------------
# COLR v1
# ---------------------------------------------------------------------------


def _mappedVarIdx(mapping, index):
	"""Resolve a VarIndexBase-derived index through a COLR VarIndexMap.

	Mirrors instancer/colr.py: with an explicit map the base index addresses
	the map (clamped to its last entry), and the map value is the packed
	VarIdx; without a map the base index is already a packed VarIdx.
	"""
	if not mapping:
		return index, False
	if isinstance(mapping, dict):
		value = mapping.get(index)
		if value is None:
			# Match the runtime clamping for out-of-range indices.
			value = mapping[max(mapping)] if mapping else None
	else:
		value = mapping[min(index, len(mapping) - 1)] if mapping else None
	return value, True


def _checkColrVarNode(collector, tag, location, node, storeRef, mapping):
	try:
		names = node.getVariableAttrs()
	except Exception:
		return
	if not names:
		return
	base = getattr(node, "VarIndexBase", None)
	if base is None or base == NO_VARIATION_INDEX:
		return
	try:
		converters = node.getConverters()
		maxOffset = max(converters[name].getVarIndexOffset() for name in names)
	except Exception:
		maxOffset = 0
	for offset in range(maxOffset + 1):
		varIdx, wasMapped = _mappedVarIdx(mapping, base + offset)
		if wasMapped and (varIdx is None or varIdx == NO_VARIATION_INDEX):
			# The explicit map routes this value to "no variation".
			continue
		if storeRef is None and varIdx != NO_VARIATION_INDEX:
			collector.add(
				tag,
				location + ("VarIndexBase", offset),
				"colr-var-store-missing",
				"COLR paint references variation data but COLR has no "
				"VarStore",
			)
			continue
		_checkVarIdx(
			collector,
			tag,
			location + ("VarIndexBase", offset),
			varIdx,
			storeRef,
			"colr-var-store-missing",
		)


def _checkColr(collector, ttFont, nAxes):
	tag = "COLR"
	if tag not in ttFont:
		return
	try:
		wrapper = ttFont[tag]
	except Exception as exception:
		collector.add(tag, (tag,), "table-parse-error", f"cannot read COLR: {exception}")
		return
	if getattr(wrapper, "version", 0) < 1:
		return
	root = getattr(wrapper, "table", None)
	if root is None:
		return

	store = getattr(root, "VarStore", None)
	storeRef = (
		_checkItemStore(collector, tag, (tag, "VarStore"), store, nAxes)
		if store is not None
		else None
	)

	idxMap = getattr(root, "VarIndexMap", None)
	mapping = getattr(idxMap, "mapping", None) if idxMap is not None else None
	if idxMap is not None:
		# Every explicit map entry is itself a packed VarIdx into the store.
		_checkIdxMap(
			collector,
			tag,
			(tag, "VarIndexMap"),
			idxMap,
			storeRef,
			"colr-var-store-missing",
		)

	roots = []
	baseGlyphList = getattr(root, "BaseGlyphList", None)
	if baseGlyphList is not None:
		for record in getattr(baseGlyphList, "BaseGlyphPaintRecord", None) or []:
			paint = getattr(record, "Paint", None)
			if paint is not None:
				roots.append(
					(paint, (tag, "BaseGlyphList", record.BaseGlyph, "Paint"))
				)
	layerList = getattr(root, "LayerList", None)
	if layerList is not None:
		for index, paint in enumerate(getattr(layerList, "Paint", None) or []):
			roots.append((paint, (tag, "LayerList", "Paint", index)))
	clipList = getattr(root, "ClipList", None)
	if clipList is not None:
		clips = getattr(clipList, "clips", None)
		if isinstance(clips, dict):
			for glyphName in sorted(clips):
				roots.append(
					(clips[glyphName], (tag, "ClipList", glyphName))
				)

	# Depth-first walk of the Paint/sub-table graph with cycle protection,
	# since offsets in damaged fonts may point backwards or in a loop.
	seen = set()
	stack = list(roots)
	while stack:
		node, location = stack.pop()
		if node is None:
			continue
		identity = id(node)
		if identity in seen:
			continue
		seen.add(identity)
		_checkColrVarNode(collector, tag, location, node, storeRef, mapping)
		try:
			subTables = list(node.iterSubTables())
		except Exception as exception:
			collector.add(
				tag,
				location,
				"table-parse-error",
				f"cannot traverse COLR subtree: {exception}",
			)
			continue
		for entry in reversed(subTables):
			if entry.value is None:
				continue
			childLocation = location + (entry.name,)
			if entry.index is not None:
				childLocation += (entry.index,)
			stack.append((entry.value, childLocation))


# ---------------------------------------------------------------------------
# VARC
# ---------------------------------------------------------------------------


def _checkMultiVarStore(collector, tag, location, store, nAxes):
	regionList = getattr(store, "SparseVarRegionList", None)
	if regionList is None:
		collector.add(
			tag,
			location + ("SparseVarRegionList",),
			"varc-multi-region-list",
			"MultiVarStore has no SparseVarRegionList",
		)
		return None, []

	regionCount = getattr(regionList, "RegionCount", 0)
	regions = getattr(regionList, "Region", None) or []
	if len(regions) != regionCount:
		collector.add(
			tag,
			location + ("SparseVarRegionList", "Region"),
			"varc-multi-region-count",
			f"SparseVarRegionList declares {regionCount} region(s) but "
			f"contains {len(regions)}",
		)
	for regionIndex, region in enumerate(regions):
		axes = getattr(region, "SparseVarRegionAxis", None) or []
		declaredAxes = getattr(region, "SparseRegionCount", len(axes))
		if declaredAxes != len(axes):
			collector.add(
				tag,
				location
				+ ("SparseVarRegionList", "Region", regionIndex),
				"varc-multi-region-count",
				f"SparseVarRegion {regionIndex} declares SparseRegionCount "
				f"{declaredAxes} but contains {len(axes)} axis/axes",
			)
		for axisSlot, axisRecord in enumerate(axes):
			axisIndex = getattr(axisRecord, "AxisIndex", None)
			if nAxes is not None and axisIndex is not None and axisIndex >= nAxes:
				collector.add(
					tag,
					location
					+ (
						"SparseVarRegionList", "Region", regionIndex,
						"SparseVarRegionAxis", axisSlot, "AxisIndex",
					),
					"varc-sparse-region-axis-index",
					f"SparseVarRegion {regionIndex} references axis index "
					f"{axisIndex} but the font only defines {nAxes} axis/axes",
				)

	varData = getattr(store, "MultiVarData", None) or []
	declaredCount = getattr(store, "MultiVarDataCount", len(varData))
	if declaredCount != len(varData):
		collector.add(
			tag,
			location + ("MultiVarData",),
			"varc-multi-data-count",
			f"MultiVarStore declares MultiVarDataCount {declaredCount} but "
			f"contains {len(varData)}",
		)

	itemCounts = []
	for dataIndex, data in enumerate(varData):
		dataLocation = location + ("MultiVarData", dataIndex)
		regionIndices = getattr(data, "VarRegionIndex", None) or []
		for slot, regionIndex in enumerate(regionIndices):
			if regionIndex >= regionCount:
				collector.add(
					tag,
					dataLocation + ("VarRegionIndex", slot),
					"varc-multi-region-index",
					f"MultiVarData {dataIndex} references SparseVarRegion "
					f"{regionIndex} but the SparseVarRegionList only defines "
					f"{regionCount}",
				)
		regionCountForData = getattr(data, "VarRegionCount", len(regionIndices))
		items = getattr(data, "Item", None) or []
		# Each item (inner-index row) is a flattened tuple holding the same
		# number of value(s) per referenced region, so its length must be a
		# multiple of VarRegionCount.  The value width may differ between
		# rows, which is what makes the store "multi".
		for itemIndex, item in enumerate(items):
			if regionCountForData and len(item) % regionCountForData:
				collector.add(
					tag,
					dataLocation + ("Item", itemIndex),
					"varc-multi-item-arity",
					f"MultiVarData {dataIndex} item {itemIndex} contains "
					f"{len(item)} value(s), which is not a multiple of "
					f"VarRegionCount {regionCountForData}",
				)
		itemCounts.append(len(items))

	return (
		_StoreRef(len(varData), itemCounts, regionCount, None),
		list(varData),
	)


def _checkVarc(collector, ttFont, nAxes):
	tag = "VARC"
	root = _sectionRoot(collector, ttFont, tag)
	if root is None or root is _UNREADABLE:
		return

	multiStore = getattr(root, "MultiVarStore", None)
	if multiStore is not None:
		multiRef, multiData = _checkMultiVarStore(
			collector, tag, (tag, "MultiVarStore"), multiStore, nAxes
		)
	else:
		multiRef, multiData = None, []

	conditionList = getattr(root, "ConditionList", None)
	if conditionList is not None:
		conditions = getattr(conditionList, "ConditionTable", None) or []
		for index, condition in enumerate(conditions):
			_checkCondition(
				collector,
				tag,
				(tag, "ConditionList", "ConditionTable", index),
				condition,
				nAxes,
				multiRef,
				False,
			)

	axisIndicesList = getattr(root, "AxisIndicesList", None)
	axisTuples = list(getattr(axisIndicesList, "Item", None) or [])
	for tupleIndex, axisTuple in enumerate(axisTuples):
		for slot, axisIndex in enumerate(axisTuple):
			if nAxes is not None and axisIndex is not None and axisIndex >= nAxes:
				collector.add(
					tag,
					(tag, "AxisIndicesList", "Item", tupleIndex, slot),
					"varc-axis-index",
					f"axis-indices tuple {tupleIndex} references axis index "
					f"{axisIndex} but the font only defines {nAxes} axis/axes",
				)

	coverage = getattr(root, "Coverage", None)
	coverageGlyphs = list(getattr(coverage, "glyphs", None) or [])
	glyphRecords = getattr(getattr(root, "VarCompositeGlyphs", None),
	                       "VarCompositeGlyph", None) or []
	if len(coverageGlyphs) != len(glyphRecords):
		collector.add(
			tag,
			(tag, "VarCompositeGlyphs"),
			"varc-coverage-count",
			f"Coverage lists {len(coverageGlyphs)} glyph(s) but "
			f"VarCompositeGlyphs contains {len(glyphRecords)} record(s)",
		)

	conditionCount = (
		getattr(conditionList, "ConditionCount", 0)
		if conditionList is not None
		else 0
	)

	for glyphIndex, glyphRecord in enumerate(glyphRecords):
		glyphName = (
			coverageGlyphs[glyphIndex]
			if glyphIndex < len(coverageGlyphs)
			else glyphIndex
		)
		components = getattr(glyphRecord, "components", None) or []
		for componentIndex, component in enumerate(components):
			location = (
				tag, "VarCompositeGlyphs", glyphName, "components", componentIndex
			)

			conditionIndex = getattr(component, "conditionIndex", None)
			if conditionIndex is not None and conditionIndex >= conditionCount:
				collector.add(
					tag,
					location + ("conditionIndex",),
					"varc-condition-index",
					f"component {componentIndex} of glyph {glyphName!r} "
					f"references condition {conditionIndex} but the "
					f"ConditionList only defines {conditionCount}",
				)

			axisIndicesIndex = getattr(component, "axisIndicesIndex", None)
			axisTuple = None
			if axisIndicesIndex is not None:
				if axisIndicesList is None or axisIndicesIndex >= len(axisTuples):
					collector.add(
						tag,
						location + ("axisIndicesIndex",),
						"varc-axis-indices-index",
						f"component {componentIndex} of glyph {glyphName!r} "
						f"references axis-indices tuple {axisIndicesIndex} but "
						f"only {len(axisTuples)} tuple(s) are defined",
					)
				else:
					axisTuple = axisTuples[axisIndicesIndex]

			axisValues = getattr(component, "axisValues", None)
			if (
				axisTuple is not None
				and axisValues is not None
				and len(axisValues) != len(axisTuple)
			):
				collector.add(
					tag,
					location + ("axisValues",),
					"varc-axis-values-count",
					f"component {componentIndex} of glyph {glyphName!r} has "
					f"{len(axisValues)} axis value(s) but axis-indices tuple "
					f"{axisIndicesIndex} has {len(axisTuple)} axis/axes",
				)

			for fieldName in ("axisValuesVarIndex", "transformVarIndex"):
				varIdx = getattr(component, fieldName, None)
				if varIdx is None or varIdx == NO_VARIATION_INDEX:
					continue
				if multiRef is None:
					collector.add(
						tag,
						location + (fieldName,),
						"varc-var-store-missing",
						f"component {componentIndex} of glyph {glyphName!r} "
						f"references a variation index but VARC has no "
						f"MultiVarStore",
					)
					continue
				if not multiRef.resolves(varIdx):
					_checkVarIdx(
						collector,
						tag,
						location + (fieldName,),
						varIdx,
						multiRef,
						"varc-var-store-missing",
					)
					continue
				if fieldName == "axisValuesVarIndex" and axisTuple is not None:
					outer = varIdx >> 16
					inner = varIdx & 0xFFFF
					data = multiData[outer]
					regionCountForData = getattr(data, "VarRegionCount", 0)
					item = data.Item[inner]
					if regionCountForData and len(item) % regionCountForData == 0:
						tupleWidth = len(item) // regionCountForData
						if tupleWidth != len(axisTuple):
							collector.add(
								tag,
								location + (fieldName,),
								"varc-axis-values-arity",
								f"variation data referenced by {fieldName} of "
								f"component {componentIndex} of glyph "
								f"{glyphName!r} carries {tupleWidth} value(s) "
								f"per region across {regionCountForData} "
								f"region(s), but the component supplies "
								f"{len(axisTuple)} axis value(s)",
							)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def validateFont(ttFont: TTFont) -> FontValidationReport:
	"""Validate cross-table variation consistency of a TTFont.

	The font is never modified, compiled or saved; see the module docstring
	for details.  Tables that cannot be parsed produce a
	``table-parse-error`` issue and validation continues with the remaining
	tables.

	Args:
		ttFont: any :class:`fontTools.ttLib.TTFont` instance, including fonts
			opened with ``lazy=True``.

	Returns:
		A :class:`FontValidationReport` (truthy when no issues were found).

	Raises:
		TypeError: if ``ttFont`` is not a :class:`TTFont`.
	"""
	if not isinstance(ttFont, TTFont):
		raise TypeError(f"expected TTFont, got {type(ttFont).__name__}")

	collector = _Collector()

	try:
		glyphOrder = ttFont.getGlyphOrder()
	except Exception as exception:
		glyphOrder = None
		collector.add(
			"font", ("glyphOrder",), "table-parse-error",
			f"cannot determine glyph order: {exception}",
		)

	nFvarAxes: Optional[int] = None
	if "fvar" in ttFont:
		try:
			nFvarAxes = len(ttFont["fvar"].axes)
		except Exception as exception:
			collector.add(
				"fvar",
				("fvar",),
				"table-parse-error",
				f"cannot read fvar axes: {exception}",
			)

	hasVarc = "VARC" in ttFont
	hiddenAxisCount = nFvarAxes if nFvarAxes is not None else _gvarRawAxisCount(
		ttFont
	)

	sections = (
		("gvar", lambda: _checkGvar(
			collector, ttFont, nFvarAxes, hasVarc, glyphOrder)),
		("HVAR", lambda: _checkHvarVvar(
			collector, ttFont, "HVAR",
			("AdvWidthMap", "LsbMap", "RsbMap"), hiddenAxisCount)),
		("VVAR", lambda: _checkHvarVvar(
			collector, ttFont, "VVAR",
			("AdvHeightMap", "TsbMap", "BsbMap", "VOrgMap"), hiddenAxisCount)),
		("MVAR", lambda: _checkMvar(collector, ttFont, hiddenAxisCount)),
		("avar", lambda: _checkAvar2(collector, ttFont, hiddenAxisCount)),
		("CFF2", lambda: _checkCff2(collector, ttFont, hiddenAxisCount)),
		("BASE", lambda: _checkBase(collector, ttFont, hiddenAxisCount)),
		("COLR", lambda: _checkColr(collector, ttFont, hiddenAxisCount)),
		("VARC", lambda: _checkVarc(collector, ttFont, hiddenAxisCount)),
		("FeatureVariations",
		 lambda: _checkFeatureVariations(collector, ttFont, nFvarAxes)),
	)
	for tag, section in sections:
		try:
			section()
		except Exception as exception:  # isolate unexpected section failures
			collector.add(
				tag,
				(tag,),
				"table-parse-error",
				f"error while validating {tag}: {exception}",
			)

	# GDEF is validated together with the Device-table consumers that
	# resolve against its VarStore (GPOS/MATH/JSTF included).
	try:
		gdefRoot = _sectionRoot(collector, ttFont, "GDEF")
		if gdefRoot is not _UNREADABLE:
			gdefStore = (
				getattr(gdefRoot, "VarStore", None) if gdefRoot is not None else None
			)
			gdefRef = (
				_checkItemStore(
					collector, "GDEF", ("GDEF", "VarStore"), gdefStore,
					hiddenAxisCount
				)
				if gdefStore is not None
				else None
			)
			_checkGdefFamilyDevices(collector, ttFont, gdefRef)
	except Exception as exception:
		collector.add(
			"GDEF",
			("GDEF",),
			"table-parse-error",
			f"error while validating GDEF: {exception}",
		)

	return FontValidationReport(collector.issues)
