"""GlyphSets returned by a TTFont."""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import contextmanager
from copy import copy
from types import SimpleNamespace
from fontTools.misc.fixedTools import otRound
from fontTools.misc.loggingTools import deprecateFunction
from fontTools.misc.transform import Transform
from fontTools.pens.transformPen import TransformPen, TransformPointPen
from fontTools.pens.recordingPen import (
    DecomposingRecordingPen,
    lerpRecordings,
    replayRecording,
)


class _TTGlyphSet(Mapping):
    """Generic dict-like GlyphSet class that pulls metrics from hmtx and
    glyph shape from TrueType or CFF.
    """

    def __init__(self, font, location, glyphsMapping, *, recalcBounds=True):
        self.recalcBounds = recalcBounds
        self.font = font
        self.defaultLocationNormalized = (
            {axis.axisTag: 0 for axis in self.font["fvar"].axes}
            if "fvar" in self.font
            else {}
        )
        self.location = location if location is not None else {}
        self.rawLocation = {}  # VarComponent-only location
        self.originalLocation = location if location is not None else {}
        self.depth = 0
        self.locationStack = []
        self.rawLocationStack = []
        self.glyphsMapping = glyphsMapping
        self.hMetrics = font["hmtx"].metrics
        self.vMetrics = getattr(font.get("vmtx"), "metrics", None)
        self.hvarTable = None
        if location:
            from fontTools.varLib.varStore import VarStoreInstancer

            self.hvarTable = getattr(font.get("HVAR"), "table", None)
            if self.hvarTable is not None:
                self.hvarInstancer = VarStoreInstancer(
                    self.hvarTable.VarStore, font["fvar"].axes, location
                )
            # TODO VVAR, VORG

    @contextmanager
    def pushLocation(self, location, reset: bool):
        self.locationStack.append(self.location)
        self.rawLocationStack.append(self.rawLocation)
        if reset:
            self.location = self.originalLocation.copy()
            self.rawLocation = self.defaultLocationNormalized.copy()
        else:
            self.location = self.location.copy()
            self.rawLocation = {}
        self.location.update(location)
        self.rawLocation.update(location)

        try:
            yield None
        finally:
            self.location = self.locationStack.pop()
            self.rawLocation = self.rawLocationStack.pop()

    @contextmanager
    def pushDepth(self):
        try:
            depth = self.depth
            self.depth += 1
            yield depth
        finally:
            self.depth -= 1

    def __contains__(self, glyphName):
        return glyphName in self.glyphsMapping

    def __iter__(self):
        return iter(self.glyphsMapping.keys())

    def __len__(self):
        return len(self.glyphsMapping)

    @deprecateFunction(
        "use 'glyphName in glyphSet' instead", category=DeprecationWarning
    )
    def has_key(self, glyphName):
        return glyphName in self.glyphsMapping


class _TTGlyphSetGlyf(_TTGlyphSet):
    def __init__(self, font, location, recalcBounds=True):
        self.glyfTable = font["glyf"]
        super().__init__(font, location, self.glyfTable, recalcBounds=recalcBounds)
        self.gvarTable = font.get("gvar")

    def __getitem__(self, glyphName):
        return _TTGlyphGlyf(self, glyphName, recalcBounds=self.recalcBounds)


class _TTGlyphSetGlyf(_TTGlyphSet):
    def __init__(self, font, location, recalcBounds=True):
        self.glyfTable = font["glyf"]
        super().__init__(font, location, self.glyfTable, recalcBounds=recalcBounds)
        self.gvarTable = font.get("gvar")

    def __getitem__(self, glyphName):
        return _TTGlyphGlyf(self, glyphName, recalcBounds=self.recalcBounds)


class _TTGlyphSetCFF(_TTGlyphSet):
    def __init__(self, font, location):
        tableTag = "CFF2" if "CFF2" in font else "CFF "
        self.charStrings = list(font[tableTag].cff.values())[0].CharStrings
        super().__init__(font, location, self.charStrings)
        self.blender = None
        if location:
            from fontTools.varLib.varStore import VarStoreInstancer

            varStore = getattr(self.charStrings, "varStore", None)
            if varStore is not None:
                instancer = VarStoreInstancer(
                    varStore.otVarStore, font["fvar"].axes, location
                )
                self.blender = instancer.interpolateFromDeltas

    def __getitem__(self, glyphName):
        return _TTGlyphCFF(self, glyphName)


class _TTGlyphSetVARC(_TTGlyphSet):
    def __init__(self, font, location, glyphSet):
        self.glyphSet = glyphSet
        super().__init__(font, location, glyphSet)
        self.varcTable = font["VARC"].table

    def __getitem__(self, glyphName):
        varc = self.varcTable
        if glyphName not in varc.Coverage.glyphs:
            return self.glyphSet[glyphName]
        return _TTGlyphVARC(self, glyphName)


class _TTGlyph(ABC):
    """Glyph object that supports the Pen protocol, meaning that it has
    .draw() and .drawPoints() methods that take a pen object as their only
    argument. Additionally there are 'width' and 'lsb' attributes, read from
    the 'hmtx' table.

    If the font contains a 'vmtx' table, there will also be 'height' and 'tsb'
    attributes.
    """

    def __init__(self, glyphSet, glyphName, *, recalcBounds=True):
        self.glyphSet = glyphSet
        self.name = glyphName
        self.recalcBounds = recalcBounds
        self.width, self.lsb = glyphSet.hMetrics[glyphName]
        if glyphSet.vMetrics is not None:
            self.height, self.tsb = glyphSet.vMetrics[glyphName]
        else:
            self.height, self.tsb = None, None
        if glyphSet.location and glyphSet.hvarTable is not None:
            varidx = (
                glyphSet.font.getGlyphID(glyphName)
                if glyphSet.hvarTable.AdvWidthMap is None
                else glyphSet.hvarTable.AdvWidthMap.mapping[glyphName]
            )
            self.width += glyphSet.hvarInstancer[varidx]
        # TODO: VVAR/VORG

    @abstractmethod
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        raise NotImplementedError

    def drawPoints(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.pointPen for details
        how that works.
        """
        from fontTools.pens.pointPen import SegmentToPointPen

        self.draw(SegmentToPointPen(pen))


class _TTGlyphGlyf(_TTGlyph):
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        glyph, offset = self._getGlyphAndOffset()

        with self.glyphSet.pushDepth() as depth:
            if depth:
                offset = 0  # Offset should only apply at top-level

            glyph.draw(pen, self.glyphSet.glyfTable, offset)

    def drawPoints(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.pointPen for details
        how that works.
        """
        glyph, offset = self._getGlyphAndOffset()

        with self.glyphSet.pushDepth() as depth:
            if depth:
                offset = 0  # Offset should only apply at top-level

            glyph.drawPoints(pen, self.glyphSet.glyfTable, offset)

    def _getGlyphAndOffset(self):
        if self.glyphSet.location and self.glyphSet.gvarTable is not None:
            glyph = self._getGlyphInstance()
        else:
            glyph = self.glyphSet.glyfTable[self.name]

        offset = self.lsb - glyph.xMin if hasattr(glyph, "xMin") else 0
        return glyph, offset

    def _getGlyphInstance(self):
        from fontTools.varLib.iup import iup_delta
        from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
        from fontTools.varLib.models import supportScalar

        glyphSet = self.glyphSet
        glyfTable = glyphSet.glyfTable
        variations = glyphSet.gvarTable.variations[self.name]
        hMetrics = glyphSet.hMetrics
        vMetrics = glyphSet.vMetrics
        coordinates, _ = glyfTable._getCoordinatesAndControls(
            self.name, hMetrics, vMetrics
        )
        origCoords, endPts = None, None
        for var in variations:
            scalar = supportScalar(glyphSet.location, var.axes)
            if not scalar:
                continue
            delta = var.coordinates
            if None in delta:
                if origCoords is None:
                    origCoords, control = glyfTable._getCoordinatesAndControls(
                        self.name, hMetrics, vMetrics
                    )
                    endPts = (
                        control[1] if control[0] >= 1 else list(range(len(control[1])))
                    )
                delta = iup_delta(delta, origCoords, endPts)
            coordinates += GlyphCoordinates(delta) * scalar

        glyph = copy(glyfTable[self.name])  # Shallow copy
        width, lsb, height, tsb = _setCoordinates(
            glyph, coordinates, glyfTable, recalcBounds=self.recalcBounds
        )
        self.lsb = lsb
        self.tsb = tsb
        if glyphSet.hvarTable is None:
            # no HVAR: let's set metrics from the phantom points
            self.width = width
            self.height = height
        return glyph


class _TTGlyphCFF(_TTGlyph):
    def draw(self, pen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        self.glyphSet.charStrings[self.name].draw(pen, self.glyphSet.blender)


class _TTGlyphVARC(_TTGlyph):
    def _draw(self, pen, isPointPen):
        """Draw the glyph onto ``pen``. See fontTools.pens.basePen for details
        how that works.
        """
        from fontTools.ttLib.tables.otTables import (
            VarComponentFlags,
            NO_VARIATION_INDEX,
        )

        glyphSet = self.glyphSet
        varc = glyphSet.varcTable
        idx = varc.Coverage.glyphs.index(self.name)
        glyph = varc.VarCompositeGlyphs.glyphs[idx]

        from fontTools.varLib.multiVarStore import MultiVarStoreInstancer

        instancer = MultiVarStoreInstancer(
            varc.MultiVarStore, self.glyphSet.font["fvar"].axes, self.glyphSet.location
        )
        instancer.setLocation(self.glyphSet.location)

        for comp in glyph.components:
            comp = copy(comp)  # Shallow copy
            locationValues, transformValues = comp.getComponentValues()

            if comp.locationVarIndex != NO_VARIATION_INDEX:
                assert len(locationValues)
                locationDeltas = instancer[comp.locationVarIndex]
                locationValues = list(locationValues + locationDeltas)
            if comp.transformVarIndex != NO_VARIATION_INDEX:
                assert len(transformValues)
                transformDeltas = instancer[comp.transformVarIndex]
                transformValues = list(transformValues + transformDeltas)

            comp.setComponentValues(locationValues, transformValues)

            with self.glyphSet.glyphSet.pushLocation(
                comp.location, comp.flags & VarComponentFlags.RESET_UNSPECIFIED_AXES
            ):
                with self.glyphSet.pushLocation(
                    comp.location, comp.flags & VarComponentFlags.RESET_UNSPECIFIED_AXES
                ):
                    try:
                        pen.addVarComponent(
                            comp.glyphName, comp.transform, self.glyphSet.rawLocation
                        )
                    except AttributeError:
                        t = comp.transform.toTransform()
                        glyph = self.glyphSet[comp.glyphName]
                        if isPointPen:
                            tPen = TransformPointPen(pen, t)
                            glyph.drawPoints(tPen)
                        else:
                            tPen = TransformPen(pen, t)
                            glyph.draw(tPen)

    def draw(self, pen):
        self._draw(pen, False)

    def drawPoints(self, pen):
        self._draw(pen, True)


def _setCoordinates(glyph, coord, glyfTable, *, recalcBounds=True):
    # Handle phantom points for (left, right, top, bottom) positions.
    assert len(coord) >= 4
    leftSideX = coord[-4][0]
    rightSideX = coord[-3][0]
    topSideY = coord[-2][1]
    bottomSideY = coord[-1][1]

    for _ in range(4):
        del coord[-1]

    if glyph.isComposite():
        assert len(coord) == len(glyph.components)
        glyph.components = [copy(comp) for comp in glyph.components]  # Shallow copy
        for p, comp in zip(coord, glyph.components):
            if hasattr(comp, "x"):
                comp.x, comp.y = p
    elif glyph.numberOfContours == 0:
        assert len(coord) == 0
    else:
        assert len(coord) == len(glyph.coordinates)
        glyph.coordinates = coord

    if recalcBounds:
        glyph.recalcBounds(glyfTable)

    horizontalAdvanceWidth = otRound(rightSideX - leftSideX)
    verticalAdvanceWidth = otRound(topSideY - bottomSideY)
    leftSideBearing = otRound(glyph.xMin - leftSideX)
    topSideBearing = otRound(topSideY - glyph.yMax)
    return (
        horizontalAdvanceWidth,
        leftSideBearing,
        verticalAdvanceWidth,
        topSideBearing,
    )


class LerpGlyphSet(Mapping):
    """A glyphset that interpolates between two other glyphsets.

    Factor is typically between 0 and 1. 0 means the first glyphset,
    1 means the second glyphset, and 0.5 means the average of the
    two glyphsets. Other values are possible, and can be useful to
    extrapolate. Defaults to 0.5.
    """

    def __init__(self, glyphset1, glyphset2, factor=0.5):
        self.glyphset1 = glyphset1
        self.glyphset2 = glyphset2
        self.factor = factor

    def __getitem__(self, glyphname):
        if glyphname in self.glyphset1 and glyphname in self.glyphset2:
            return LerpGlyph(glyphname, self)
        raise KeyError(glyphname)

    def __contains__(self, glyphname):
        return glyphname in self.glyphset1 and glyphname in self.glyphset2

    def __iter__(self):
        set1 = set(self.glyphset1)
        set2 = set(self.glyphset2)
        return iter(set1.intersection(set2))

    def __len__(self):
        set1 = set(self.glyphset1)
        set2 = set(self.glyphset2)
        return len(set1.intersection(set2))


class LerpGlyph:
    def __init__(self, glyphname, glyphset):
        self.glyphset = glyphset
        self.glyphname = glyphname

    def draw(self, pen):
        recording1 = DecomposingRecordingPen(self.glyphset.glyphset1)
        self.glyphset.glyphset1[self.glyphname].draw(recording1)
        recording2 = DecomposingRecordingPen(self.glyphset.glyphset2)
        self.glyphset.glyphset2[self.glyphname].draw(recording2)

        factor = self.glyphset.factor

        replayRecording(lerpRecordings(recording1.value, recording2.value, factor), pen)
