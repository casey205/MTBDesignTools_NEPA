import os
import matplotlib
try:
    matplotlib.use("Qt5Agg", force=True)
except Exception:
    pass

from qgis.gui import QgsVertexMarker
from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt import uic
from qgis.PyQt.QtWidgets import (
    QDockWidget, QMessageBox, QVBoxLayout, QFileDialog
)
from qgis.utils import iface as qgis_iface
from qgis.core import (
    QgsRectangle,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsPointXY,
    QgsUnitTypes,
    QgsGeometry,
    QgsWkbTypes,
    QgsSpatialIndex,
    QgsVectorFileWriter,
    QgsFields,
    QgsField,
    QgsFeature,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
)

try:
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
except ImportError:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import numpy as np

FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "MTBDesignToolsNEPA_dockwidget_base.ui")
)

# -------------------------------------------------
# Helper functions (shared across tabs)
# -------------------------------------------------

def expand_rect(rect: QgsRectangle, factor: float = 1.15) -> QgsRectangle:
    if rect.isNull() or rect.isEmpty():
        return rect
    cx = (rect.xMinimum() + rect.xMaximum()) / 2.0
    cy = (rect.yMinimum() + rect.yMaximum()) / 2.0
    w = (rect.xMaximum() - rect.xMinimum()) * factor
    h = (rect.yMaximum() - rect.yMinimum()) * factor
    return QgsRectangle(cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def layer_by_name(name: str):
    layers = QgsProject.instance().mapLayersByName(name)
    return layers[0] if layers else None


def trail_layer():
    """Return the active trail layer (Trail_Design preferred, Trail_Alignment for legacy)."""
    return (
        layer_by_name("Trail_Design")
        or layer_by_name("Trail_Alignment")
        or None
    )


def merge_selected_geometries(layer):
    geoms = [f.geometry() for f in layer.selectedFeatures() if f.geometry()]
    if not geoms:
        return None
    merged = geoms[0]
    for g in geoms[1:]:
        merged = merged.combine(g)
    return merged


def _reverse_geometry(geom):
    if geom.isMultipart():
        parts = geom.asMultiPolyline()
        reversed_parts = [list(reversed(part)) for part in reversed(parts)]
        return QgsGeometry.fromMultiPolylineXY(reversed_parts)
    else:
        coords = geom.asPolyline()
        return QgsGeometry.fromPolylineXY(list(reversed(coords)))


def distance_multiplier(layer, unit_text: str) -> float:
    crs_units = layer.crs().mapUnits()
    if unit_text == "Meters":
        return QgsUnitTypes.fromUnitToUnitFactor(crs_units, QgsUnitTypes.DistanceMeters)
    if unit_text == "Feet":
        return QgsUnitTypes.fromUnitToUnitFactor(crs_units, QgsUnitTypes.DistanceFeet)
    if unit_text == "Miles":
        return QgsUnitTypes.fromUnitToUnitFactor(crs_units, QgsUnitTypes.DistanceMiles)
    if unit_text == "Kilometers":
        return QgsUnitTypes.fromUnitToUnitFactor(crs_units, QgsUnitTypes.DistanceKilometers)
    return 1.0


def _class_from_layer_name(layer_name: str) -> str:
    """Infer WNF stream class from layer name (e.g. 'Class 2', 'Class_3', 'Streams_2')."""
    import re
    m = re.search(r'class\s*[_\-]?\s*([1-5])', layer_name, re.IGNORECASE)
    if m:
        return f"Class {m.group(1)}"
    m = re.search(r'\b([1-5])\b', layer_name)
    if m:
        return f"Class {m.group(1)}"
    return layer_name


def _fish_bearing_from_class(class_str: str) -> str:
    """Flag fish-bearing status based on WNF stream class per the NEPA RFP."""
    s = class_str.lower()
    if "2" in s:
        return "Yes"       # Class 2 = fish-bearing per WNF/NWFP
    if "1" in s:
        return "Verify"    # Class 1 may be perennial non-fish — needs field confirmation
    if "3" in s or "4" in s:
        return "Verify"    # Intermittent — may have fish in portions
    if "5" in s:
        return "No"        # Ephemeral
    return "Unknown"


def _extract_points_from_geom(geom):
    """Extract all QgsPointXY values from any geometry type (Point, MultiPoint, Collection)."""
    pts = []
    if geom is None or geom.isEmpty():
        return pts
    geom_type = geom.type()
    if geom_type == QgsWkbTypes.PointGeometry:
        if geom.isMultipart():
            pts = list(geom.asMultiPoint())
        else:
            pts = [geom.asPoint()]
    elif geom_type == QgsWkbTypes.UnknownGeometry:
        for sub in geom.asGeometryCollection():
            pts.extend(_extract_points_from_geom(sub))
    else:
        # Intersection of two lines should always yield points/multipoints;
        # try collection decomposition as fallback
        try:
            for sub in geom.asGeometryCollection():
                pts.extend(_extract_points_from_geom(sub))
        except Exception:
            pass
    return pts


# -------------------------------------------------
# Elevation Profile Canvas (unchanged from MTBDesignTools)
# -------------------------------------------------

class ElevationProfileCanvas(FigureCanvas):

    IMBA_WINDOW_SAMPLES = 12
    IMBA_MIN_ZONE_LEN = 6

    def __init__(self, parent=None):
        self.fig = Figure(constrained_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)
        self.distance_label = "Native"

    def _imba_class(self, slope_pct, mode):
        g = abs(slope_pct)
        if g < 5:
            return "easy"
        elif g < 10:
            return "moderate"
        elif g < 15:
            return "difficult"
        else:
            return "extreme"

    def _imba_color_for_class(self, cls):
        return {
            "easy": "#2ca02c",
            "moderate": "#1f77b4",
            "difficult": "#111111",
            "extreme": "#d62728",
        }.get(cls, "#1f77b4")

    def _compute_smoothed_slopes(self, distances_native, elevations, window):
        dist = np.asarray(distances_native, dtype=float)
        elev = np.asarray(elevations, dtype=float)
        n = len(dist)
        slopes = np.zeros(n, dtype=float)
        for i in range(n):
            i0 = max(i - window, 0)
            i1 = min(i + window, n - 1)
            if i1 <= i0:
                continue
            dz = elev[i1] - elev[i0]
            dx = dist[i1] - dist[i0]
            slopes[i] = (dz / dx) * 100.0 if dx != 0 else 0.0
        return slopes

    def _build_imba_zones(self, slopes, mode):
        zones = []
        current = None
        start = 0
        for i, s in enumerate(slopes):
            if (mode == "Climbing" and s <= 0) or (mode == "Descending" and s >= 0):
                cls = "neutral"
            else:
                cls = self._imba_class(s, mode)
            if cls != current:
                if current is not None:
                    zones.append((start, i - 1, current))
                start = i
                current = cls
        if current is not None:
            zones.append((start, len(slopes) - 1, current))
        return zones

    def _filter_short_zones(self, zones, min_len):
        return [
            (s, e, c)
            for s, e, c in zones
            if c != "neutral" and (e - s + 1) >= min_len
        ]

    def plot(self, distances_display, elevations,
             distances_native=None, imba_enabled=False, imba_mode="Climbing"):
        self.ax.clear()
        distances_display = np.asarray(distances_display, float)
        elevations = np.asarray(elevations, float)

        if not imba_enabled or distances_native is None or len(distances_display) <= 1:
            self.ax.plot(distances_display, elevations, linewidth=2.2)
            ymin = self.ax.get_ylim()[0]
            self.ax.fill_between(distances_display, elevations, ymin, alpha=0.18)
        else:
            slopes = self._compute_smoothed_slopes(
                distances_native, elevations, self.IMBA_WINDOW_SAMPLES
            )
            zones = self._filter_short_zones(
                self._build_imba_zones(slopes, imba_mode),
                self.IMBA_MIN_ZONE_LEN
            )
            self.ax.plot(distances_display, elevations, linewidth=1.6, alpha=0.25)
            self.ax.set_xlim(distances_display.min(), distances_display.max())
            self.ax.set_ylim(elevations.min(), elevations.max())
            ymin = self.ax.get_ylim()[0]
            for s, e, cls in zones:
                x = distances_display[s:e+1]
                y = elevations[s:e+1]
                color = self._imba_color_for_class(cls)
                self.ax.plot(x, y, color=color, linewidth=3.0)
                if cls == "extreme":
                    self.ax.fill_between(x, y, ymin, color=color, alpha=0.20)

        self.ax.autoscale(enable=True, axis="y", tight=True)
        self.ax.margins(x=0, y=0.02)
        self.ax.set_xlabel(f"Distance ({self.distance_label})")
        self.ax.set_ylabel("Elevation")
        self.ax.grid(True, axis="y", alpha=0.3)
        self.ax.grid(False, axis="x")
        self.ax.spines["top"].set_visible(False)
        self.ax.spines["right"].set_visible(False)
        self.draw_idle()


# -------------------------------------------------
# Dock Widget
# -------------------------------------------------

class MTBDesignToolsNEPADockWidget(QDockWidget, FORM_CLASS):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.iface = qgis_iface
        self.setupUi(self)

        self._crossings_data = []
        self._crossings_exported = False  # tracks whether annotations have been exported
        self._crossings_snapshot = None   # (timestamp_str, [layer_name, ...])

        # Named analysis slots: each category (NSO, Hydro, Wetlands, …) stores its
        # own triage results independently so they never overwrite one another.
        self._habitat_slots = {
            name: {"data": [], "snapshot": None, "laa_types": {}, "display_text": ""}
            for name in self.DEFAULT_HABITAT_SLOTS
        }

        self._setup_profile_tab()
        self._setup_crossings_tab()
        self._setup_habitat_tab()
        self._setup_report_tab()

        # Tab-switch guard for unsaved crossing annotations
        self.mainTabWidget.currentChanged.connect(self._on_tab_changed)

        # Refresh pickers when project layers change
        QgsProject.instance().layersAdded.connect(self._on_layers_changed)
        QgsProject.instance().layersRemoved.connect(self._on_layers_changed)

        # Restore saved state when a project is opened, and load immediately
        # for projects that are already open when the plugin loads
        QgsProject.instance().readProject.connect(self._load_from_project)
        self._load_from_project()

    # ──────────────────────────────────────────────
    # Setup helpers
    # ──────────────────────────────────────────────

    def _setup_profile_tab(self):
        self.summaryText.show()
        self.summaryText.setVisible(True)
        self.summaryText.setReadOnly(True)
        self.summaryText.setMinimumHeight(30)
        self.summaryText.setMaximumHeight(52)
        self.summaryText.setStyleSheet(
            "background-color: #f5f5f5; border: 1px solid #999; padding: 4px;"
        )

        self.zoomButton.clicked.connect(self.zoom_to_selected)
        self.profileButton.clicked.connect(self.generate_profile)
        self.refreshDemButton.clicked.connect(self.populate_dem_dropdown)
        self.refreshTrailButton.clicked.connect(self.populate_trail_dropdown)
        self.trailDropdown.currentIndexChanged.connect(self._on_trail_selected)

        self.populate_dem_dropdown()
        self.populate_trail_dropdown()

        # Embed matplotlib canvas inside the profile tab
        container = self.profileLabel.parentWidget()
        self.profileCanvas = ElevationProfileCanvas(container)
        layout = container.layout()
        if layout is None:
            layout = QVBoxLayout(container)
            container.setLayout(layout)
        layout.addWidget(self.summaryText)
        layout.addWidget(self.profileCanvas)
        self.profileLabel.hide()

        self.profileCanvas.mpl_connect("motion_notify_event", self.on_profile_hover)
        self.profileCanvas.mpl_connect("button_press_event", self.on_profile_click)

        self.profile_distances = []
        self.profile_distances_native = []
        self.profile_points = []
        self.profile_elevations = []

        self._x_to_index_scale = None
        self._last_hover_idx = None
        self._smoothed_slopes = None
        self.hover_marker = None
        self.profile_hover_point = None
        self.slope_text = None
        self._hover_pinned = False
        self._pin_text = None

        self.imbaModeComboBox.setCurrentText("Climbing")
        self.imbaModeComboBox.setEnabled(False)
        self.imbaSlopeCheckBox.stateChanged.connect(
            lambda s: self.imbaModeComboBox.setEnabled(s == 2)
        )

    def _setup_crossings_tab(self):
        self.refreshStreamsButton.clicked.connect(self._refresh_crossings_tab)
        self.streamsGroupCombo.currentIndexChanged.connect(self.populate_streams_list)
        self.selectAllStreamsButton.clicked.connect(self._select_all_streams)
        self.runCrossingsButton.clicked.connect(self.run_crossing_analysis)
        self.exportCrossingsButton.clicked.connect(self.export_crossings)
        self._refresh_crossings_tab()

        from qgis.PyQt.QtGui import QFont
        self.crossingsResultsText.setFont(QFont("Courier New", 9))

    def _on_layers_changed(self, *_args):
        self.populate_trail_dropdown()
        self.populate_dem_dropdown()
        self._refresh_crossings_tab()
        self.populate_habitat_list()

    def _refresh_crossings_tab(self, *_args):
        """Repopulate group combo then hydro layer list."""
        self._populate_streams_groups()
        self.populate_streams_list()

    def _populate_streams_groups(self):
        """Fill streamsGroupCombo with all QGIS layer-tree group names."""
        from qgis.core import QgsLayerTreeGroup

        prev = self.streamsGroupCombo.currentText()
        self.streamsGroupCombo.blockSignals(True)
        self.streamsGroupCombo.clear()
        self.streamsGroupCombo.addItem("(All layers)", None)

        def _walk(node, prefix=""):
            for child in node.children():
                if isinstance(child, QgsLayerTreeGroup):
                    full_name = f"{prefix}/{child.name()}" if prefix else child.name()
                    self.streamsGroupCombo.addItem(full_name, full_name)
                    _walk(child, full_name)

        _walk(QgsProject.instance().layerTreeRoot())

        idx = self.streamsGroupCombo.findText(prev)
        self.streamsGroupCombo.setCurrentIndex(idx if idx >= 0 else 0)
        self.streamsGroupCombo.blockSignals(False)

    # ──────────────────────────────────────────────
    # Shared: Trail + DEM dropdowns
    # ──────────────────────────────────────────────

    def populate_trail_dropdown(self, *_args):
        self.trailDropdown.blockSignals(True)
        self.trailDropdown.clear()
        self.trailDropdown.addItem("— select on map or in Attribute Table —", userData=None)

        layer = trail_layer() or self.iface.activeLayer()
        if layer is None:
            self.trailDropdown.blockSignals(False)
            return

        fields = [f.name() for f in layer.fields()]
        name_field = next(
            (f for f in ["Trail_Name", "trail_name", "name", "Name"] if f in fields),
            None,
        )

        seen = set()
        for feat in layer.getFeatures():
            label = str(feat.attribute(name_field)) if name_field else f"Feature {feat.id()}"
            if not label or label in ("NULL", "None", ""):
                label = f"Feature {feat.id()}"
            if label not in seen:
                seen.add(label)
                self.trailDropdown.addItem(label, userData=feat.id())

        self.trailDropdown.blockSignals(False)

    def _on_trail_selected(self, index: int):
        fid = self.trailDropdown.currentData()
        if fid is None:
            return

        layer = trail_layer() or self.iface.activeLayer()
        if layer is None:
            return

        fields = [f.name() for f in layer.fields()]
        name_field = next(
            (f for f in ["Name", "name", "Trail_Name", "trail_name"] if f in fields), None
        )
        chosen_name = self.trailDropdown.currentText()

        if name_field and chosen_name and not chosen_name.startswith("Feature "):
            expr = f'"{name_field}" = \'{chosen_name.replace("\'", "\\'")}\''
            layer.selectByExpression(expr)
        else:
            layer.select(fid)

        canvas = self.iface.mapCanvas()
        if layer.selectedFeatureCount() > 0:
            rect = None
            for f in layer.selectedFeatures():
                g = f.geometry()
                if g:
                    r = g.boundingBox()
                    rect = r if rect is None else rect.combineExtentWith(r)
            if rect:
                canvas.setExtent(expand_rect(rect))
                canvas.refresh()

    def populate_dem_dropdown(self, *_args):
        self.demDropdown.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsRasterLayer):
                self.demDropdown.addItem(lyr.name(), lyr.id())

    # ──────────────────────────────────────────────
    # Tab 0: Profile & Difficulty
    # ──────────────────────────────────────────────

    def _format_num(self, v, nd=2):
        try:
            return f"{float(v):.{nd}f}"
        except Exception:
            return "—"

    def _compute_avg_climb_descent(self):
        if self._smoothed_slopes is None:
            return None, None
        s = np.asarray(self._smoothed_slopes, dtype=float)
        s = s[np.isfinite(s)]
        if len(s) == 0:
            return None, None
        climbs = s[s > 0]
        descs = s[s < 0]
        avg_climb = float(np.mean(climbs)) if len(climbs) else None
        avg_desc = float(np.mean(np.abs(descs))) if len(descs) else None
        return avg_climb, avg_desc

    def _imba_difficulty_from_grade(self, grade_pct):
        if grade_pct is None or not np.isfinite(grade_pct):
            return "—"
        g = abs(grade_pct)
        if g <= 5:
            return "Easy (Green Circle)"
        elif g <= 10:
            return "Moderate (Blue Square)"
        elif g <= 15:
            return "Difficult (Black Diamond)"
        else:
            return "Extreme (Double Black Diamond)"

    def _update_summary_text(self, layer, unit_text):
        trail_name = "—"
        trail_type = "—"

        if layer and layer.selectedFeatureCount() > 0:
            count = layer.selectedFeatureCount()
            if count == 1:
                feat = list(layer.selectedFeatures())[0]
                fields = [f.name() for f in layer.fields()]
                name_field = next(
                    (fn for fn in ["Name", "name", "Trail_Name", "trail_name"] if fn in fields), None
                )
                if name_field:
                    val = feat.attribute(name_field)
                    trail_name = str(val) if val else "—"
                type_field = next(
                    (fn for fn in ["Type", "type", "trail_type", "Trail_Type"] if fn in fields), None
                )
                if type_field:
                    val = feat.attribute(type_field)
                    trail_type = str(val) if val else "—"
            else:
                trail_name = "(multiple)"
                trail_type = "(multiple)"

        total_len = None
        if self.profile_distances and len(self.profile_distances) > 1:
            total_len = float(self.profile_distances[-1] - self.profile_distances[0])
        avg_climb, avg_desc = self._compute_avg_climb_descent()

        len_str = self._format_num(total_len, 2) if total_len is not None else "—"
        climb_str = self._format_num(avg_climb, 1) if avg_climb is not None else "—"
        desc_str = self._format_num(avg_desc, 1) if avg_desc is not None else "—"
        climb_diff = self._imba_difficulty_from_grade(avg_climb)
        desc_diff = self._imba_difficulty_from_grade(avg_desc)

        unit_label = {"Miles": "mi", "Feet": "ft", "Meters": "m", "Kilometers": "km"}.get(unit_text, "mi")
        txt = (
            f"Trail: {trail_name} | Type: {trail_type} | Len: {len_str} {unit_label} | "
            f"Climb Avg Grade: {climb_str}% (IMBA: {climb_diff}) | "
            f"Descent Avg Grade: {desc_str}% (IMBA: {desc_diff})"
        )

        if hasattr(self.summaryText, "setPlainText"):
            self.summaryText.setPlainText(txt)
        else:
            self.summaryText.setText(txt)

    def generate_profile(self):
        self._clear_hover_marker()
        self._hover_pinned = False
        self._remove_pin_label()

        layer = trail_layer() or self.iface.activeLayer()
        raster = QgsProject.instance().mapLayer(self.demDropdown.currentData())

        if not layer or not raster:
            QMessageBox.information(
                self, "MTB Design Tools - NEPA",
                "No trail layer or elevation model found.\n\n"
                "Make sure a Trail_Design layer and an elevation model are loaded."
            )
            return

        if layer.selectedFeatureCount() == 0:
            QMessageBox.information(
                self, "MTB Design Tools - NEPA",
                "No trail features selected.\n\n"
                "Pick a trail from the Trail dropdown above, or select features "
                "on the map / in the Attribute Table, then click Generate Profile."
            )
            return

        line = merge_selected_geometries(layer)
        if line is None:
            QMessageBox.warning(self, "MTB Design Tools - NEPA", "Invalid trail geometry.")
            return

        if self.reverseDirectionCheckBox.isChecked():
            line = _reverse_geometry(line)

        unit_text = self.distanceUnitsDropdown.currentText() if hasattr(self, "distanceUnitsDropdown") else None
        if not unit_text or unit_text == "Native":
            crs_unit_map = {
                QgsUnitTypes.DistanceFeet: "Feet",
                QgsUnitTypes.DistanceMeters: "Meters",
            }
            unit_text = crs_unit_map.get(layer.crs().mapUnits(), "Miles")

        multiplier = distance_multiplier(layer, unit_text)

        self.profile_distances.clear()
        self.profile_distances_native.clear()
        self.profile_points.clear()
        self.profile_elevations.clear()

        provider = raster.dataProvider()
        d = 0.0
        while d <= line.length():
            pt = line.interpolate(d).asPoint()
            val, ok = provider.sample(QgsPointXY(pt), 1)
            if ok:
                self.profile_distances_native.append(d)
                self.profile_distances.append(d * multiplier)
                self.profile_points.append(QgsPointXY(pt))
                self.profile_elevations.append(val)
            d += 2.0

        self._smoothed_slopes = self.profileCanvas._compute_smoothed_slopes(
            self.profile_distances_native, self.profile_elevations, window=12
        )

        if len(self.profile_distances) > 1:
            self._x_to_index_scale = (
                (len(self.profile_distances) - 1) /
                (self.profile_distances[-1] - self.profile_distances[0])
            )
        else:
            self._x_to_index_scale = None

        self._last_hover_idx = None
        self.profileCanvas.distance_label = unit_text
        self.profileCanvas.plot(
            self.profile_distances,
            self.profile_elevations,
            distances_native=self.profile_distances_native,
            imba_enabled=self.imbaSlopeCheckBox.isChecked(),
            imba_mode=self.imbaModeComboBox.currentText()
        )

        self.profile_hover_point, = self.profileCanvas.ax.plot(
            [], [], marker="o", color="red", markersize=6, zorder=10
        )
        self.slope_text = self.profileCanvas.ax.text(
            0.02, 0.95, "",
            transform=self.profileCanvas.ax.transAxes,
            fontsize=10,
            bbox=dict(facecolor="white", alpha=0.7, edgecolor="none")
        )

        self._update_summary_text(layer, unit_text)

    def zoom_to_selected(self):
        canvas = self.iface.mapCanvas()
        layer = trail_layer() or self.iface.activeLayer()

        if layer is None or layer.selectedFeatureCount() == 0:
            QMessageBox.information(self, "MTB Design Tools - NEPA", "Select trail features first.")
            return

        rect = None
        for f in layer.selectedFeatures():
            g = f.geometry()
            if g:
                r = g.boundingBox()
                rect = r if rect is None else rect.combineExtentWith(r)

        if rect:
            canvas.setExtent(expand_rect(rect))
            canvas.refresh()

    # ──────────────────────────────────────────────
    # Tab 0: Hover / click interaction
    # ──────────────────────────────────────────────

    def on_profile_click(self, event):
        if event.button != 1 or event.xdata is None:
            return
        if not self.profile_distances or self._x_to_index_scale is None:
            return

        if self._hover_pinned:
            self._hover_pinned = False
            self._remove_pin_label()
            if self.profile_hover_point:
                self.profile_hover_point.set_color("red")
            if self.slope_text:
                self.slope_text.set_text("")
            self.profileCanvas.draw_idle()
            return

        idx = int((event.xdata - self.profile_distances[0]) * self._x_to_index_scale)
        idx = max(0, min(idx, len(self.profile_distances) - 1))

        self._hover_pinned = True
        self._last_hover_idx = idx
        self._update_hover_marker(self.profile_points[idx])

        if self.profile_hover_point:
            self.profile_hover_point.set_data(
                [self.profile_distances[idx]], [self.profile_elevations[idx]]
            )
            self.profile_hover_point.set_color("goldenrod")

        if self._smoothed_slopes is not None and self.slope_text:
            self.slope_text.set_text(f"📌 {self._smoothed_slopes[idx]:.1f}%")

        self._show_pin_label()
        self.profileCanvas.draw_idle()

    def on_profile_hover(self, event):
        if self._hover_pinned:
            return
        if event.xdata is None or not self.profile_distances or self._x_to_index_scale is None:
            self._clear_hover_marker()
            return

        idx = int((event.xdata - self.profile_distances[0]) * self._x_to_index_scale)
        idx = max(0, min(idx, len(self.profile_distances) - 1))
        if idx == self._last_hover_idx:
            return
        self._last_hover_idx = idx

        self._update_hover_marker(self.profile_points[idx])

        if self.profile_hover_point:
            self.profile_hover_point.set_color("red")
            self.profile_hover_point.set_data(
                [self.profile_distances[idx]], [self.profile_elevations[idx]]
            )

        if self._smoothed_slopes is not None and self.slope_text:
            self.slope_text.set_text(f"Slope: {self._smoothed_slopes[idx]:.1f}%  (click to pin)")
            self.profileCanvas.draw_idle()

    def _update_hover_marker(self, point):
        if self.hover_marker is None:
            self.hover_marker = QgsVertexMarker(self.iface.mapCanvas())
            self.hover_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
            self.hover_marker.setIconSize(12)
            self.hover_marker.setPenWidth(2)
            self.hover_marker.setColor(Qt.red)
        self.hover_marker.setCenter(point)

    def _clear_hover_marker(self):
        if self.hover_marker:
            self.iface.mapCanvas().scene().removeItem(self.hover_marker)
            self.hover_marker = None

    def _show_pin_label(self):
        self._remove_pin_label()
        self._pin_text = self.profileCanvas.ax.text(
            0.5, 0.97,
            "📌 Pinned — move mouse freely, click again to unpin",
            transform=self.profileCanvas.ax.transAxes,
            fontsize=8, ha="center", va="top", color="goldenrod",
            bbox=dict(facecolor="white", alpha=0.8, edgecolor="goldenrod", boxstyle="round,pad=0.2"),
        )

    def _remove_pin_label(self):
        if self._pin_text is not None:
            try:
                self._pin_text.remove()
            except Exception:
                pass
            self._pin_text = None

    # ──────────────────────────────────────────────
    # Tab 1: Stream Crossings
    # ──────────────────────────────────────────────

    def populate_streams_list(self, *_args):
        """Populate the hydro layer list, scoped to the selected group."""
        from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer
        from qgis.PyQt.QtWidgets import QListWidgetItem

        self.streamsListWidget.clear()

        group_path = self.streamsGroupCombo.currentData()

        if group_path:
            def _find_group(node, target_path, current_path=""):
                for child in node.children():
                    if isinstance(child, QgsLayerTreeGroup):
                        cp = f"{current_path}/{child.name()}" if current_path else child.name()
                        if cp == target_path:
                            return child
                        result = _find_group(child, target_path, cp)
                        if result:
                            return result
                return None

            group_node = _find_group(QgsProject.instance().layerTreeRoot(), group_path)
            if group_node:
                candidate_layers = sorted(
                    [tl.layer() for tl in group_node.findLayers()
                     if tl.layer() and isinstance(tl.layer(), QgsVectorLayer)],
                    key=lambda l: l.name()
                )
            else:
                candidate_layers = []
        else:
            candidate_layers = sorted(
                [l for l in QgsProject.instance().mapLayers().values()
                 if isinstance(l, QgsVectorLayer)],
                key=lambda l: l.name()
            )

        for lyr in candidate_layers:
            if lyr.geometryType() == QgsWkbTypes.LineGeometry:
                item = QListWidgetItem(lyr.name())
                item.setData(Qt.UserRole, lyr.id())
                self.streamsListWidget.addItem(item)

    def _select_all_streams(self):
        self.streamsListWidget.selectAll()

    def _get_selected_stream_layers(self):
        layers = []
        for item in self.streamsListWidget.selectedItems():
            lyr = QgsProject.instance().mapLayer(item.data(Qt.UserRole))
            if lyr:
                layers.append(lyr)
        return layers

    def run_crossing_analysis(self):
        from qgis.PyQt.QtWidgets import QApplication
        from collections import defaultdict

        trail_lyr = trail_layer() or self.iface.activeLayer()
        stream_layers = self._get_selected_stream_layers()

        if not trail_lyr:
            QMessageBox.warning(
                self, "Stream Crossings",
                "No trail layer found.\nLoad a Trail_Design (or Trail_Alignment) layer."
            )
            return
        if not stream_layers:
            QMessageBox.warning(
                self, "Stream Crossings",
                "Select one or more hydro layers from the list.\n"
                "Use Ctrl+click to select multiple layers (e.g. Class 1 through Class 5)."
            )
            return

        trail_features = (
            list(trail_lyr.selectedFeatures())
            if trail_lyr.selectedFeatureCount() > 0
            else list(trail_lyr.getFeatures())
        )
        if not trail_features:
            QMessageBox.information(self, "Stream Crossings", "No trail features to analyse.")
            return

        trail_fields = [f.name() for f in trail_lyr.fields()]
        trail_name_field = next(
            (f for f in ["Name", "name", "Trail_Name", "trail_name"] if f in trail_fields), None
        )
        miles_multiplier = distance_multiplier(trail_lyr, "Miles")

        self.crossingsResultsText.setPlainText(
            f"Running analysis across {len(stream_layers)} hydro layer(s)…"
        )
        QApplication.processEvents()

        all_crossings = []
        crs_notes = []

        for streams_lyr in stream_layers:
            stream_class_label = _class_from_layer_name(streams_lyr.name())

            stream_fields = [f.name() for f in streams_lyr.fields()]
            stream_name_field = next(
                (f for f in ["GNIS_Name", "GNIS_name", "Name", "name",
                             "StreamName", "stream_name", "STREAM_NAM"] if f in stream_fields),
                None
            )

            # Reproject streams to trail CRS if needed
            needs_transform = trail_lyr.crs() != streams_lyr.crs()
            transform = QgsCoordinateTransform(
                streams_lyr.crs(), trail_lyr.crs(), QgsProject.instance()
            ) if needs_transform else None
            if needs_transform:
                crs_notes.append(
                    f"{streams_lyr.name()}: {streams_lyr.crs().authid()} → {trail_lyr.crs().authid()}"
                )

            # Build reprojected geometry cache + spatial index
            stream_geom_cache = {}
            stream_feature_cache = {}
            for sf in streams_lyr.getFeatures():
                geom = sf.geometry()
                if not geom:
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
                stream_geom_cache[sf.id()] = geom
                stream_feature_cache[sf.id()] = sf

            stream_index = QgsSpatialIndex()
            for fid, geom in stream_geom_cache.items():
                tmp = QgsFeature(fid)
                tmp.setGeometry(geom)
                stream_index.addFeature(tmp)

            # Find crossings for each trail feature
            for trail_feat in trail_features:
                trail_geom = trail_feat.geometry()
                if not trail_geom:
                    continue
                t_name = (
                    str(trail_feat.attribute(trail_name_field))
                    if trail_name_field else f"Trail {trail_feat.id()}"
                )

                for sid in stream_index.intersects(trail_geom.boundingBox()):
                    sf = stream_feature_cache.get(sid)
                    stream_geom = stream_geom_cache.get(sid)
                    if not sf or not stream_geom:
                        continue
                    if not trail_geom.intersects(stream_geom):
                        continue

                    pts = _extract_points_from_geom(trail_geom.intersection(stream_geom))

                    s_name = (
                        str(sf.attribute(stream_name_field))
                        if stream_name_field else f"Stream {sf.id()}"
                    )
                    if s_name in ("NULL", "None", ""):
                        s_name = f"Stream {sf.id()}"

                    for pt in pts:
                        dist_native = trail_geom.lineLocatePoint(
                            QgsGeometry.fromPointXY(QgsPointXY(pt.x(), pt.y()))
                        )
                        all_crossings.append({
                            "trail": t_name,
                            "stream_id": sf.id(),
                            "stream_name": s_name,
                            "stream_class": stream_class_label,
                            "fish_bearing": _fish_bearing_from_class(stream_class_label),
                            "dist_miles": round(dist_native * miles_multiplier, 3),
                            "x": round(pt.x(), 2),
                            "y": round(pt.y(), 2),
                            "point": QgsPointXY(pt.x(), pt.y()),
                        })

        # Sort by trail name, then distance along trail
        all_crossings.sort(key=lambda c: (c["trail"], c["dist_miles"]))

        self._crossings_data = all_crossings
        self._crossings_exported = False  # new data — annotations not yet exported
        import datetime as _dt
        self._crossings_snapshot = (
            _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            [lyr.name() for lyr in stream_layers],
        )
        self._save_to_project()
        self._update_report_status()
        self._add_crossings_to_map(all_crossings, trail_lyr)
        self._display_crossings_results(all_crossings, trail_lyr, stream_layers, crs_notes)
        self.exportCrossingsButton.setEnabled(bool(all_crossings))

    # Crossing type options for fish-bearing (Class 1 & 2) crossings
    # Named analysis categories — each stores its own triage results independently
    # so NSO, Hydrology, Wetlands, etc. don't overwrite each other.
    DEFAULT_HABITAT_SLOTS = [
        "NSO / Wildlife",
        "Hydrology / Riparian",
        "Wetlands",
        "Species Occurrences",
        "LRMP Allocations",
        "Botany / SHAB",
    ]

    LAA_TYPES = [
        "NSO Habitat",
        "Critical Habitat",
        "RA32 Habitat",
        "LRMP Allocation",
        "General Sensitive Area",
        "(Skip — exclude from LAA export)",
    ]

    # Shapefile field name for each LAA type (max 10 chars)
    LAA_FIELD_MAP = {
        "NSO Habitat":          "NSO_Hab",
        "Critical Habitat":     "Crit_Hab",
        "RA32 Habitat":         "RA32_Hab",
        "LRMP Allocation":      "LRMP_Alloc",
        "General Sensitive Area": "Sensitive",
    }

    CROSSING_TYPES = [
        "Proposed new crossing",
        "Existing road bridge (no new work)",
        "Existing culvert (no new work)",
        "Existing ford / primitive crossing",
        "Proposed bridge",
        "Proposed culvert",
        "Proposed hardened crossing",
    ]

    def _display_crossings_results(self, crossings, trail_lyr=None, stream_layers=None, crs_notes=None):
        from collections import defaultdict
        from qgis.PyQt.QtWidgets import QComboBox, QTableWidgetItem
        from qgis.PyQt.QtCore import Qt as _Qt

        if not crossings:
            self.crossingsResultsText.setPlainText(
                "No stream crossings found.\n\n"
                "Check that the trail and hydro layers overlap spatially,\n"
                "that the correct layers are selected in the list above,\n"
                "and that at least one hydro layer is loaded in the project."
            )
            self.crossingsFishLabel.setVisible(False)
            self.crossingsFishTable.setVisible(False)
            self.crossingsNFBLabel.setVisible(False)
            self.crossingsNFBNotesEdit.setVisible(False)
            return

        lines = []
        if trail_lyr:
            lines.append(f"Trail layer  : {trail_lyr.name()}")
        if stream_layers:
            lines.append(f"Hydro layers : {', '.join(l.name() for l in stream_layers)}")
        if crs_notes:
            for note in crs_notes:
                lines.append(f"  ⚠ Reprojected: {note}")
        lines.append("")

        trail_groups = defaultdict(list)
        for c in crossings:
            trail_groups[c["trail"]].append(c)

        total_fb  = sum(1 for c in crossings if c["fish_bearing"] == "Yes")
        total_c3  = sum(1 for c in crossings if c.get("stream_class") == "Class 3")
        total_c45 = sum(1 for c in crossings if c.get("stream_class") in ("Class 4", "Class 5"))
        total_nfb = len(crossings) - total_fb

        lines.append("═" * 60)
        lines.append(
            f"SUMMARY  —  {len(crossings)} total crossings  |  {len(trail_groups)} trail(s)"
        )
        lines.append("═" * 60)
        lines.append(f"  Class 1 & 2 (fish-bearing)       : {total_fb:>3}  → specify crossing type below")
        lines.append(f"  Class 3     (field verify)        : {total_c3:>3}  → recommend field survey")
        lines.append(f"  Class 4 & 5 (non-fish-bearing)   : {total_c45:>3}  → GIS documentation only")
        lines.append(f"  {'─'*46}")
        lines.append(f"  Total                             : {len(crossings):>3}")
        lines.append("═" * 60)

        for trail_name in sorted(trail_groups):
            tcs = trail_groups[trail_name]
            class_counts = defaultdict(int)
            fish_count = 0
            c3_count = 0
            for c in tcs:
                class_counts[c["stream_class"]] += 1
                if c["fish_bearing"] == "Yes":
                    fish_count += 1
                if c.get("stream_class") == "Class 3":
                    c3_count += 1

            class_str = "  ".join(
                f"{cls}: {cnt}" for cls, cnt in sorted(class_counts.items())
            )
            lines.append(f"\n  {trail_name}")
            lines.append(f"    Crossings : {len(tcs)}   ({class_str})")
            if fish_count:
                lines.append(
                    f"    ⚠ Fish-bearing (Class 1/2): {fish_count} — specify crossing type in table below"
                )
            if c3_count:
                lines.append(
                    f"    ⚑ Class 3: {c3_count} — field verify fish distribution boundary"
                )

        lines += [
            "",
            "✓ 'Stream Crossings - MTB NEPA' layer added to map canvas.",
            "  Use 'Export to Shapefile' to save for the NEPA project record.",
        ]

        self.crossingsResultsText.setPlainText("\n".join(lines))

        # ── Fish-bearing crossing table (Class 1 & 2 only) ────────────
        fish_crossings = [c for c in crossings if c["fish_bearing"] == "Yes"]
        has_fish = bool(fish_crossings)

        self.crossingsFishLabel.setVisible(has_fish)
        self.crossingsFishTable.setVisible(has_fish)
        self.crossingsNFBLabel.setVisible(True)
        self.crossingsNFBNotesEdit.setVisible(True)

        if has_fish:
            tbl = self.crossingsFishTable
            tbl.setRowCount(0)
            tbl.setColumnCount(7)
            tbl.setHorizontalHeaderLabels(
                ["#", "Trail", "Mi", "Stream", "Class", "Crossing Type", "Notes"]
            )
            tbl.horizontalHeader().setStretchLastSection(True)
            tbl.setColumnWidth(0, 30)
            tbl.setColumnWidth(1, 110)
            tbl.setColumnWidth(2, 45)
            tbl.setColumnWidth(3, 90)
            tbl.setColumnWidth(4, 55)
            tbl.setColumnWidth(5, 185)

            for row_idx, c in enumerate(fish_crossings):
                tbl.insertRow(row_idx)

                def _ro(text):
                    item = QTableWidgetItem(str(text))
                    item.setFlags(item.flags() & ~_Qt.ItemIsEditable)
                    return item

                tbl.setItem(row_idx, 0, _ro(row_idx + 1))
                tbl.setItem(row_idx, 1, _ro(c["trail"][:30]))
                tbl.setItem(row_idx, 2, _ro(f"{c['dist_miles']:.3f}"))
                tbl.setItem(row_idx, 3, _ro(c["stream_name"][:20]))
                tbl.setItem(row_idx, 4, _ro(c["stream_class"]))

                combo = QComboBox()
                combo.addItems(self.CROSSING_TYPES)
                # Restore previously set type if re-running
                prev_type = c.get("crossing_type", "")
                if prev_type in self.CROSSING_TYPES:
                    combo.setCurrentText(prev_type)
                tbl.setCellWidget(row_idx, 5, combo)

                notes_item = QTableWidgetItem(c.get("crossing_notes", ""))
                tbl.setItem(row_idx, 6, notes_item)

            tbl.resizeRowsToContents()

    def _read_fish_crossing_annotations(self):
        """Read crossing type and notes back from the fish table into _crossings_data."""
        from qgis.PyQt.QtWidgets import QComboBox
        if not self._crossings_data:
            return
        fish_crossings = [c for c in self._crossings_data if c["fish_bearing"] == "Yes"]
        tbl = self.crossingsFishTable
        for row_idx in range(min(tbl.rowCount(), len(fish_crossings))):
            combo = tbl.cellWidget(row_idx, 5)
            notes_item = tbl.item(row_idx, 6)
            fish_crossings[row_idx]["crossing_type"]  = combo.currentText() if combo else ""
            fish_crossings[row_idx]["crossing_notes"] = notes_item.text() if notes_item else ""

    def _add_crossings_to_map(self, crossings, trail_lyr):
        """Create (or replace) a temporary memory layer of crossing points on the map canvas."""
        from qgis.core import (
            QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsMarkerSymbol
        )

        # Remove any previous run's layer
        for lyr in QgsProject.instance().mapLayersByName("Stream Crossings - MTB NEPA"):
            QgsProject.instance().removeMapLayer(lyr.id())

        if not crossings:
            return

        crs_str = trail_lyr.crs().authid() if trail_lyr else "EPSG:4326"
        mem_layer = QgsVectorLayer(f"Point?crs={crs_str}", "Stream Crossings - MTB NEPA", "memory")
        provider = mem_layer.dataProvider()
        provider.addAttributes([
            QgsField("Trail",      QVariant.String, len=60),
            QgsField("StreamName", QVariant.String, len=60),
            QgsField("StrClass",   QVariant.String, len=20),
            QgsField("FishBear",   QVariant.String, len=10),
            QgsField("DistMiles",  QVariant.Double),
            QgsField("Easting",    QVariant.Double),
            QgsField("Northing",   QVariant.Double),
        ])
        mem_layer.updateFields()

        feats = []
        fields = mem_layer.fields()
        for c in crossings:
            feat = QgsFeature(fields)
            feat.setGeometry(QgsGeometry.fromPointXY(c["point"]))
            feat.setAttribute("Trail",      c["trail"][:60])
            feat.setAttribute("StreamName", c["stream_name"][:60])
            feat.setAttribute("StrClass",   c["stream_class"][:20])
            feat.setAttribute("FishBear",   c["fish_bearing"][:10])
            feat.setAttribute("DistMiles",  c["dist_miles"])
            feat.setAttribute("Easting",    c["x"])
            feat.setAttribute("Northing",   c["y"])
            feats.append(feat)
        provider.addFeatures(feats)

        # Categorized style: color by stream class
        class_colors = {
            "Class 1": "#74b9ff",
            "Class 2": "#0052cc",
            "Class 3": "#a8a8a8",
            "Class 4": "#c8c8c8",
            "Class 5": "#e8e8e8",
        }
        categories = []
        for cls, color in class_colors.items():
            sym = QgsMarkerSymbol.createSimple({
                "name": "circle", "color": color, "size": "4",
                "outline_color": "#222222", "outline_width": "0.4",
            })
            categories.append(QgsRendererCategory(cls, sym, cls))
        default_sym = QgsMarkerSymbol.createSimple({
            "name": "circle", "color": "#ff6b6b", "size": "4",
        })
        categories.append(QgsRendererCategory("", default_sym, "Other/Unknown"))
        mem_layer.setRenderer(QgsCategorizedSymbolRenderer("StrClass", categories))

        QgsProject.instance().addMapLayer(mem_layer)
        self.iface.mapCanvas().refresh()

    def export_crossings(self):
        if not self._crossings_data:
            QMessageBox.information(self, "Export", "Run the crossing analysis first.")
            return

        # Capture any crossing type / notes the user has entered
        self._read_fish_crossing_annotations()

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Stream Crossings Shapefile", "", "Shapefile (*.shp)"
        )
        if not path:
            return

        trail_lyr = trail_layer() or self.iface.activeLayer()
        crs = trail_lyr.crs() if trail_lyr else QgsCoordinateReferenceSystem("EPSG:4326")

        fields = QgsFields()
        fields.append(QgsField("Trail",      QVariant.String, len=60))
        fields.append(QgsField("StreamName", QVariant.String, len=60))
        fields.append(QgsField("StrClass",   QVariant.String, len=20))
        fields.append(QgsField("FishBear",   QVariant.String, len=10))
        fields.append(QgsField("DistMiles",  QVariant.Double))
        fields.append(QgsField("CrossType",  QVariant.String, len=60))
        fields.append(QgsField("Notes",      QVariant.String, len=200))
        fields.append(QgsField("Easting",    QVariant.Double))
        fields.append(QgsField("Northing",   QVariant.Double))

        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.Point, crs, "ESRI Shapefile"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(
                self, "Export Error", f"Could not create shapefile:\n{writer.errorMessage()}"
            )
            return

        for c in self._crossings_data:
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(c["point"]))
            feat.setFields(fields)
            feat.setAttribute("Trail",      c["trail"][:60])
            feat.setAttribute("StreamName", c["stream_name"][:60])
            feat.setAttribute("StrClass",   c["stream_class"][:20])
            feat.setAttribute("FishBear",   c["fish_bearing"][:10])
            feat.setAttribute("DistMiles",  c["dist_miles"])
            feat.setAttribute("CrossType",  c.get("crossing_type", "")[:60])
            feat.setAttribute("Notes",      c.get("crossing_notes", "")[:200])
            feat.setAttribute("Easting",    c["x"])
            feat.setAttribute("Northing",   c["y"])
            writer.addFeature(feat)

        del writer
        self._crossings_exported = True  # annotations are now saved
        fb_count = sum(1 for c in self._crossings_data if c["fish_bearing"] == "Yes")
        QMessageBox.information(
            self, "Export Complete",
            f"Exported {len(self._crossings_data)} crossing(s) to:\n{path}\n\n"
            f"Fish-bearing (Class 1/2): {fb_count}\n"
            "Attributes: Trail, StreamName, StrClass, FishBear,\n"
            "            DistMiles, CrossType, Notes, Easting, Northing"
        )

    # ──────────────────────────────────────────────
    # Tab 2: Habitat Overlap / Trail Triage
    # ──────────────────────────────────────────────

    # ── Named-slot helpers ───────────────────────────────────────────────

    def _active_slot_name(self):
        """Return the current category name shown in the slot combo (stripped)."""
        return self.habitatSlotCombo.currentText().strip()

    def _register_slot_in_combo(self, name):
        """Add a typed slot name to the combo item list if not already present,
        so it appears in the dropdown for easy re-selection this session."""
        if name and self.habitatSlotCombo.findText(name) < 0:
            self.habitatSlotCombo.blockSignals(True)
            self.habitatSlotCombo.addItem(name)
            self.habitatSlotCombo.blockSignals(False)

    def _active_slot(self):
        """Return (and lazily create) the dict for the currently selected slot."""
        name = self._active_slot_name()
        if name not in self._habitat_slots:
            self._habitat_slots[name] = {
                "data": [], "snapshot": None, "laa_types": {}, "display_text": ""
            }
        return self._habitat_slots[name]

    def _on_slot_changed(self, *_):
        """Restore stored results display when the user switches analysis categories.
        Uses a non-creating peek so typing a new name doesn't create empty slots."""
        name = self._active_slot_name().strip()
        if not name:
            return
        slot = self._habitat_slots.get(name, {})  # peek only — don't create yet
        text = slot.get("display_text", "")
        if text:
            self.habitatResultsText.setPlainText(text)
        else:
            self.habitatResultsText.setPlainText(
                f"No analysis run for '{name}' yet.\n"
                "Select sensitive layers above, then click Run Triage Analysis."
            )
        has_data = bool(slot.get("data"))
        self.exportHabitatButton.setEnabled(has_data)
        self.exportLAAButton.setEnabled(has_data)

    # ────────────────────────────────────────────────────────────────────

    def _setup_habitat_tab(self):
        # Populate the named-slot combo before connecting its signal so we
        # don't trigger _on_slot_changed with an empty widget.
        self.habitatSlotCombo.blockSignals(True)
        for name in self.DEFAULT_HABITAT_SLOTS:
            self.habitatSlotCombo.addItem(name)
        self.habitatSlotCombo.setEditable(True)
        self.habitatSlotCombo.setInsertPolicy(
            self.habitatSlotCombo.NoInsert  # we control when items are added
        )
        self.habitatSlotCombo.lineEdit().setPlaceholderText(
            "Type a category name or pick from list…"
        )
        self.habitatSlotCombo.blockSignals(False)
        # currentIndexChanged covers dropdown picks; editingFinished covers typed names
        self.habitatSlotCombo.currentIndexChanged.connect(self._on_slot_changed)
        self.habitatSlotCombo.lineEdit().editingFinished.connect(self._on_slot_changed)

        self.refreshHabitatButton.clicked.connect(self._refresh_habitat_tab)
        self.habitatGroupCombo.currentIndexChanged.connect(self.populate_habitat_list)
        self.selectAllHabitatButton.clicked.connect(self._select_all_habitat)
        self.habitatListWidget.itemSelectionChanged.connect(self._on_habitat_selection_changed)
        self.runHabitatButton.clicked.connect(self.run_habitat_analysis)
        self.exportHabitatButton.clicked.connect(self.export_habitat_triage)
        self.exportLAAButton.clicked.connect(self.export_laa_shapefile)
        self.habitatBufferCheckBox.toggled.connect(self._on_buffer_toggled)
        self._refresh_habitat_tab()

        from qgis.PyQt.QtGui import QFont
        self.habitatResultsText.setFont(QFont("Courier New", 9))

    def _refresh_habitat_tab(self, *_args):
        """Repopulate group combo then layer list."""
        self._populate_habitat_groups()
        self.populate_habitat_list()

    def _on_buffer_toggled(self, checked):
        """Enable or disable the buffer spinbox and units label."""
        self.habitatBufferSpinBox.setEnabled(checked)
        self.habitatBufferUnitsLabel.setEnabled(checked)

    def _populate_habitat_groups(self):
        """Fill habitatGroupCombo with all QGIS layer-tree group names."""
        from qgis.core import QgsLayerTreeGroup

        prev = self.habitatGroupCombo.currentText()
        self.habitatGroupCombo.blockSignals(True)
        self.habitatGroupCombo.clear()
        self.habitatGroupCombo.addItem("(All layers)", None)

        def _walk(node, prefix=""):
            for child in node.children():
                if isinstance(child, QgsLayerTreeGroup):
                    full_name = f"{prefix}/{child.name()}" if prefix else child.name()
                    self.habitatGroupCombo.addItem(full_name, full_name)
                    _walk(child, full_name)

        _walk(QgsProject.instance().layerTreeRoot())

        # Restore previous selection if still available
        idx = self.habitatGroupCombo.findText(prev)
        self.habitatGroupCombo.setCurrentIndex(idx if idx >= 0 else 0)
        self.habitatGroupCombo.blockSignals(False)

    def populate_habitat_list(self, *_args):
        """Populate the sensitive layers list, scoped to the selected group."""
        from qgis.core import QgsLayerTreeGroup, QgsLayerTreeLayer
        from qgis.PyQt.QtWidgets import QListWidgetItem

        self.habitatListWidget.clear()

        group_path = self.habitatGroupCombo.currentData()

        if group_path:
            # Walk the layer tree to find the named group, then collect its layers
            def _find_group(node, target_path, current_path=""):
                for child in node.children():
                    if isinstance(child, QgsLayerTreeGroup):
                        cp = f"{current_path}/{child.name()}" if current_path else child.name()
                        if cp == target_path:
                            return child
                        result = _find_group(child, target_path, cp)
                        if result:
                            return result
                return None

            group_node = _find_group(QgsProject.instance().layerTreeRoot(), group_path)
            if group_node:
                candidate_layers = []
                for tree_lyr in group_node.findLayers():
                    lyr = tree_lyr.layer()
                    if lyr and isinstance(lyr, QgsVectorLayer):
                        candidate_layers.append(lyr)
                candidate_layers.sort(key=lambda l: l.name())
            else:
                candidate_layers = []
        else:
            # All layers
            candidate_layers = sorted(
                [l for l in QgsProject.instance().mapLayers().values()
                 if isinstance(l, QgsVectorLayer)],
                key=lambda l: l.name()
            )

        for lyr in candidate_layers:
            if lyr.geometryType() in (QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry):
                item = QListWidgetItem(lyr.name())
                item.setData(Qt.UserRole, lyr.id())
                self.habitatListWidget.addItem(item)

    def _select_all_habitat(self):
        self.habitatListWidget.selectAll()

    def _on_habitat_selection_changed(self):
        """When exactly one sensitive layer is selected, auto-fill the category
        combo with that layer's name so the output files match automatically."""
        selected = self.habitatListWidget.selectedItems()
        if len(selected) == 1:
            layer_name = selected[0].text()
            self.habitatSlotCombo.blockSignals(True)
            self.habitatSlotCombo.setCurrentText(layer_name)
            self.habitatSlotCombo.blockSignals(False)
            # Update the results display for the newly named slot without creating it
            self._on_slot_changed()

    def _get_selected_habitat_layers(self):
        layers = []
        for item in self.habitatListWidget.selectedItems():
            lyr = QgsProject.instance().mapLayer(item.data(Qt.UserRole))
            if lyr:
                layers.append(lyr)
        return layers

    def run_habitat_analysis(self):
        from qgis.PyQt.QtWidgets import QApplication
        from collections import defaultdict

        trail_lyr = trail_layer() or self.iface.activeLayer()
        sensitive_layers = self._get_selected_habitat_layers()

        if not trail_lyr:
            QMessageBox.warning(
                self, "Habitat Overlap",
                "No trail layer found.\nLoad a Trail_Design (or Trail_Alignment) layer."
            )
            return
        if not sensitive_layers:
            QMessageBox.warning(
                self, "Habitat Overlap",
                "Select one or more sensitive area layers from the list.\n"
                "Load USFS corporate GIS layers (NSO habitat, Riparian Reserves,\n"
                "Critical Habitat, wetlands, etc.) into the project first."
            )
            return

        trail_features = (
            list(trail_lyr.selectedFeatures())
            if trail_lyr.selectedFeatureCount() > 0
            else list(trail_lyr.getFeatures())
        )
        if not trail_features:
            QMessageBox.information(self, "Habitat Overlap", "No trail features to analyse.")
            return

        trail_fields = [f.name() for f in trail_lyr.fields()]
        trail_name_field = next(
            (f for f in ["Name", "name", "Trail_Name", "trail_name"] if f in trail_fields), None
        )
        miles_mult = distance_multiplier(trail_lyr, "Miles")

        buffer_ft = (
            self.habitatBufferSpinBox.value()
            if self.habitatBufferCheckBox.isChecked() else 0
        )
        crs_units = trail_lyr.crs().mapUnits()
        ft_to_native = QgsUnitTypes.fromUnitToUnitFactor(
            QgsUnitTypes.DistanceFeet, crs_units
        )
        buffer_native = buffer_ft * ft_to_native

        self.habitatResultsText.setPlainText(
            f"Running segment triage across {len(sensitive_layers)} layer(s)…"
        )
        QApplication.processEvents()

        # Build reprojected geometry cache + spatial index per sensitive layer
        # Each layer stores "direct zone" (for RED) and "buffer zone" (for YELLOW)
        layer_zones = {}
        for sens_lyr in sensitive_layers:
            needs_transform = trail_lyr.crs() != sens_lyr.crs()
            transform = QgsCoordinateTransform(
                sens_lyr.crs(), trail_lyr.crs(), QgsProject.instance()
            ) if needs_transform else None

            is_line_lyr = sens_lyr.geometryType() == QgsWkbTypes.LineGeometry

            geom_direct = {}  # fid → direct zone geometry (polygon for intersection)
            geom_buffer = {}  # fid → buffered zone geometry

            for sf in sens_lyr.getFeatures():
                g = sf.geometry()
                if not g:
                    continue
                if transform:
                    g = QgsGeometry(g)
                    g.transform(transform)

                if is_line_lyr:
                    # For line layers: a small buffer creates the "direct" zone,
                    # a larger buffer creates the "adjacent" zone
                    direct = g.buffer(max(buffer_native * 0.05, ft_to_native * 2), 4)
                    buf = g.buffer(buffer_native, 6) if buffer_native > 0 else direct
                else:
                    direct = g
                    buf = g.buffer(buffer_native, 6) if buffer_native > 0 else g

                geom_direct[sf.id()] = direct
                geom_buffer[sf.id()] = buf

            idx_direct = QgsSpatialIndex()
            for fid, g in geom_direct.items():
                tmp = QgsFeature(fid)
                tmp.setGeometry(g)
                idx_direct.addFeature(tmp)

            idx_buffer = QgsSpatialIndex()
            for fid, g in geom_buffer.items():
                tmp = QgsFeature(fid)
                tmp.setGeometry(g)
                idx_buffer.addFeature(tmp)

            layer_zones[sens_lyr.id()] = (
                idx_direct, geom_direct,
                idx_buffer, geom_buffer,
                sens_lyr.name()
            )

        results = []
        for trail_feat in trail_features:
            trail_geom = trail_feat.geometry()
            if not trail_geom:
                continue

            t_name = (
                str(trail_feat.attribute(trail_name_field))
                if trail_name_field else f"Trail {trail_feat.id()}"
            )
            trail_miles = trail_geom.length() * miles_mult
            trail_bbox = trail_geom.boundingBox()

            # Build union of ALL direct zones and buffer zones across all layers
            # Also track per-layer miles for the detail table
            all_direct_union = QgsGeometry()
            all_buffer_union = QgsGeometry()
            layer_detail = {}  # {layer_name: {"red_mi": x, "yellow_mi": y}}

            for sens_lyr in sensitive_layers:
                idx_d, geom_d, idx_b, geom_b, lyr_name = layer_zones[sens_lyr.id()]

                # Collect direct zone polygons that touch the trail
                lyr_direct_union = QgsGeometry()
                for fid in idx_d.intersects(trail_bbox):
                    g = geom_d.get(fid)
                    if g and trail_geom.intersects(g):
                        lyr_direct_union = (
                            lyr_direct_union.combine(g)
                            if not lyr_direct_union.isEmpty() else QgsGeometry(g)
                        )

                # Collect buffer zone polygons near the trail
                expanded_bbox = trail_geom.buffer(buffer_native, 4).boundingBox() if buffer_native > 0 else trail_bbox
                lyr_buffer_union = QgsGeometry()
                for fid in idx_b.intersects(expanded_bbox):
                    g = geom_b.get(fid)
                    if g and trail_geom.intersects(g):
                        lyr_buffer_union = (
                            lyr_buffer_union.combine(g)
                            if not lyr_buffer_union.isEmpty() else QgsGeometry(g)
                        )

                # Per-layer segment miles
                if not lyr_direct_union.isEmpty():
                    lyr_red_geom = trail_geom.intersection(lyr_direct_union)
                    lyr_red_mi = lyr_red_geom.length() * miles_mult if not lyr_red_geom.isEmpty() else 0.0
                else:
                    lyr_red_mi = 0.0

                if not lyr_buffer_union.isEmpty():
                    lyr_buf_geom = trail_geom.intersection(lyr_buffer_union)
                    if not lyr_direct_union.isEmpty():
                        lyr_ylw_geom = lyr_buf_geom.difference(lyr_direct_union)
                    else:
                        lyr_ylw_geom = lyr_buf_geom
                    lyr_ylw_mi = lyr_ylw_geom.length() * miles_mult if not lyr_ylw_geom.isEmpty() else 0.0
                else:
                    lyr_ylw_mi = 0.0

                if lyr_red_mi > 0.001 or lyr_ylw_mi > 0.001:
                    layer_detail[lyr_name] = {
                        "red_mi": round(lyr_red_mi, 4),
                        "yellow_mi": round(lyr_ylw_mi, 4),
                    }

                # Accumulate into global unions
                if not lyr_direct_union.isEmpty():
                    all_direct_union = (
                        all_direct_union.combine(lyr_direct_union)
                        if not all_direct_union.isEmpty() else QgsGeometry(lyr_direct_union)
                    )
                if not lyr_buffer_union.isEmpty():
                    all_buffer_union = (
                        all_buffer_union.combine(lyr_buffer_union)
                        if not all_buffer_union.isEmpty() else QgsGeometry(lyr_buffer_union)
                    )

            # Compute trail segment geometries (actual line portions)
            if all_direct_union.isEmpty():
                red_geom = QgsGeometry()
            else:
                red_geom = trail_geom.intersection(all_direct_union)

            if all_buffer_union.isEmpty():
                yellow_geom = QgsGeometry()
                green_geom = trail_geom
            else:
                buf_portion = trail_geom.intersection(all_buffer_union)
                if all_direct_union.isEmpty():
                    yellow_geom = buf_portion
                else:
                    yellow_geom = buf_portion.difference(all_direct_union)
                green_geom = trail_geom.difference(all_buffer_union)

            def _safe_miles(g):
                return g.length() * miles_mult if (g and not g.isEmpty()) else 0.0

            red_miles = round(_safe_miles(red_geom), 4)
            yellow_miles = round(_safe_miles(yellow_geom), 4)
            green_miles = round(_safe_miles(green_geom), 4)

            # Overall triage = worst segment present
            if red_miles > 0.001:
                triage = "RED"
            elif yellow_miles > 0.001:
                triage = "YELLOW"
            else:
                triage = "GREEN"

            results.append({
                "trail":        t_name,
                "triage":       triage,
                "miles":        trail_miles,
                "red_miles":    red_miles,
                "yellow_miles": yellow_miles,
                "green_miles":  green_miles,
                "layer_detail": layer_detail,
                "red_geom":     red_geom,
                "yellow_geom":  yellow_geom,
                "green_geom":   green_geom,
                "geom":         trail_geom,
                "fid":          trail_feat.id(),
            })

        _order = {"RED": 0, "YELLOW": 1, "GREEN": 2}
        results.sort(key=lambda r: (_order[r["triage"]], r["trail"]))

        import datetime as _dt
        slot_name = self._active_slot_name()
        self._register_slot_in_combo(slot_name)  # ensure typed name is in dropdown
        slot = self._active_slot()
        slot["data"] = results
        slot["snapshot"] = (
            _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            [lyr.name() for lyr in sensitive_layers],
        )
        self._save_to_project()
        self._update_report_status()
        self._add_habitat_layer_to_map(results, trail_lyr)
        self._display_habitat_results(results, sensitive_layers, buffer_ft)
        self.exportHabitatButton.setEnabled(bool(results))
        self.exportLAAButton.setEnabled(bool(results))

    def _display_habitat_results(self, results, sensitive_layers, buffer_ft):
        from collections import defaultdict

        if not results:
            self.habitatResultsText.setPlainText("No results.")
            return

        total_red_mi    = sum(r["red_miles"]    for r in results)
        total_yellow_mi = sum(r["yellow_miles"] for r in results)
        total_green_mi  = sum(r["green_miles"]  for r in results)
        total_mi        = sum(r["miles"]        for r in results)
        n_red    = sum(1 for r in results if r["triage"] == "RED")
        n_yellow = sum(1 for r in results if r["triage"] == "YELLOW")
        n_green  = sum(1 for r in results if r["triage"] == "GREEN")

        lines = [
            f"Sensitive layers : {', '.join(l.name() for l in sensitive_layers)}",
            f"Buffer           : {'None — direct overlap only (FS corporate layers)' if buffer_ft == 0 else f'{buffer_ft} ft proximity zone'}",
            "",
            "═" * 66,
            "SEGMENT-LEVEL TRIAGE SUMMARY",
            "═" * 66,
            f"  {'Category':<30} {'Trails':>7}  {'Miles in zone':>14}",
            f"  {'─'*56}",
            f"  {'🔴 RED   (direct intersection)':<30} {n_red:>7}  {total_red_mi:>14.3f}",
            f"  {'🟡 YELLOW (within ' + str(buffer_ft) + ' ft)' if buffer_ft > 0 else '🟡 YELLOW (buffer disabled)':<30} {n_yellow:>7}  {total_yellow_mi:>14.3f}",
            f"  {'🟢 GREEN  (clear)':<30} {n_green:>7}  {total_green_mi:>14.3f}",
            f"  {'─'*56}",
            f"  {'Total project':<30} {len(results):>7}  {total_mi:>14.3f}",
            "",
            "  GREEN trails with zero RED/YELLOW miles → CE candidates.",
            "  RED miles = exact conflict length to reroute or analyze.",
            "",
        ]

        # ── By sensitive layer summary ─────────────────────────────────
        from collections import defaultdict
        layer_summary = defaultdict(lambda: {"trails": set(), "red_mi": 0.0, "yellow_mi": 0.0})
        for r in results:
            for lyr_name, d in r.get("layer_detail", {}).items():
                layer_summary[lyr_name]["red_mi"]    += d.get("red_mi",    0.0)
                layer_summary[lyr_name]["yellow_mi"] += d.get("yellow_mi", 0.0)
                if d.get("red_mi", 0.0) > 0.001 or d.get("yellow_mi", 0.0) > 0.001:
                    layer_summary[lyr_name]["trails"].add(r["trail"])

        # Include layers with zero conflict so every analyzed layer is visible
        for lyr in sensitive_layers:
            if lyr.name() not in layer_summary:
                layer_summary[lyr.name()] = {"trails": set(), "red_mi": 0.0, "yellow_mi": 0.0}

        lines += ["─" * 66, "BY SENSITIVE LAYER", "─" * 66]
        lyr_col = min(42, max(12, max(len(n) for n in layer_summary) + 1))
        lines.append(
            f"  {'Layer':<{lyr_col}} {'Trails':>6}  {'RED mi':>8}  {'YLW mi':>8}"
        )
        lines.append("  " + "─" * (lyr_col + 28))
        for lyr_name in [l.name() for l in sensitive_layers]:
            d = layer_summary[lyr_name]
            n_trails = len(d["trails"])
            flag = "🔴" if d["red_mi"] > 0.001 else ("🟡" if d["yellow_mi"] > 0.001 else "🟢")
            lines.append(
                f"  {flag} {lyr_name[:lyr_col - 1]:<{lyr_col - 1}} {n_trails:>6}  "
                f"{d['red_mi']:>8.3f}  {d['yellow_mi']:>8.3f}"
            )
        lines.append("")

        # Per-trail detail table
        lines += ["─" * 66, "PER-TRAIL BREAKDOWN", "─" * 66]
        col = min(28, max(12, max(len(r["trail"]) for r in results) + 1))
        hdr = (
            f"  {'Trail':<{col}} {'Total':>6}  "
            f"{'RED mi':>7}  {'YLW mi':>7}  {'GRN mi':>7}  Conflict layers"
        )
        lines.append(hdr)
        lines.append("  " + "─" * (len(hdr) - 2))

        for r in results:
            icon = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}[r["triage"]]
            conflict = ""
            if r["layer_detail"]:
                parts = []
                for lyr_name, d in sorted(r["layer_detail"].items()):
                    if d["red_mi"] > 0.001:
                        parts.append(f"{lyr_name} ({d['red_mi']:.3f}mi)")
                    elif d["yellow_mi"] > 0.001:
                        parts.append(f"{lyr_name} ~{d['yellow_mi']:.3f}mi")
                conflict = ", ".join(parts)

            lines.append(
                f"{icon} {r['trail'][:col]:<{col}} {r['miles']:>6.2f}  "
                f"{r['red_miles']:>7.3f}  {r['yellow_miles']:>7.3f}  "
                f"{r['green_miles']:>7.3f}  {conflict}"
            )

        lines += [
            "",
            "─" * 66,
            f"✓ 'Triage - {self._active_slot_name()}' layer added to map (segments colored RED/YLW/GRN).",
            "  Use 'Export Triage Shapefile' to save for the NEPA scoping memo.",
        ]

        text = "\n".join(lines)
        self.habitatResultsText.setPlainText(text)
        # Cache the display text so switching slots restores the correct view
        self._active_slot()["display_text"] = text

    def _add_habitat_layer_to_map(self, results, trail_lyr):
        from qgis.core import (
            QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsLineSymbol
        )

        layer_name = f"Triage - {self._active_slot_name()}"
        for lyr in QgsProject.instance().mapLayersByName(layer_name):
            QgsProject.instance().removeMapLayer(lyr.id())

        if not results:
            return

        crs_str = trail_lyr.crs().authid() if trail_lyr else "EPSG:4326"
        mem_layer = QgsVectorLayer(
            f"MultiLineString?crs={crs_str}", layer_name, "memory"
        )
        provider = mem_layer.dataProvider()
        provider.addAttributes([
            QgsField("Trail",   QVariant.String, len=60),
            QgsField("SegType", QVariant.String, len=10),  # RED / YELLOW / GREEN
            QgsField("Miles",   QVariant.Double),
            QgsField("Layers",  QVariant.String, len=250),
        ])
        mem_layer.updateFields()
        fields = mem_layer.fields()

        seg_configs = [
            ("red_geom",    "RED",    "#e74c3c", "1.4"),
            ("yellow_geom", "YELLOW", "#f39c12", "1.1"),
            ("green_geom",  "GREEN",  "#27ae60", "0.8"),
        ]

        mi_mult = distance_multiplier(trail_lyr, "Miles")
        feats = []
        for r in results:
            for geom_key, seg_type, _color, _width in seg_configs:
                seg_geom = r.get(geom_key)
                if not seg_geom or seg_geom.isEmpty():
                    continue
                seg_mi = seg_geom.length() * mi_mult
                if seg_mi < 0.0001:
                    continue

                # Attribution: which layers contributed to this segment
                lyr_parts = []
                for lyr_name, d in r["layer_detail"].items():
                    if seg_type == "RED" and d["red_mi"] > 0.001:
                        lyr_parts.append(lyr_name)
                    elif seg_type == "YELLOW" and d["yellow_mi"] > 0.001:
                        lyr_parts.append(lyr_name)

                feat = QgsFeature(fields)
                feat.setGeometry(seg_geom)
                feat.setAttribute("Trail",   r["trail"][:60])
                feat.setAttribute("SegType", seg_type)
                feat.setAttribute("Miles",   round(seg_mi, 4))
                feat.setAttribute("Layers",  ", ".join(lyr_parts)[:250])
                feats.append(feat)

        provider.addFeatures(feats)

        # Categorized style by SegType
        styles = {
            "RED":    ("#e74c3c", "1.4"),
            "YELLOW": ("#f39c12", "1.1"),
            "GREEN":  ("#27ae60", "0.8"),
        }
        categories = []
        for seg_type, (color, width) in styles.items():
            sym = QgsLineSymbol.createSimple({"color": color, "width": width})
            categories.append(QgsRendererCategory(seg_type, sym, seg_type))

        mem_layer.setRenderer(QgsCategorizedSymbolRenderer("SegType", categories))
        QgsProject.instance().addMapLayer(mem_layer)
        self.iface.mapCanvas().refresh()

    # ── LAA Pre-Report Shapefile Export ────────────────────────────────

    def export_laa_shapefile(self):
        """Entry point: assign layer types, then run the LAA segmentation export."""
        slot = self._active_slot()
        if not slot["snapshot"]:
            QMessageBox.warning(self, "LAA Export",
                f"Run the Habitat Overlap analysis for '{self._active_slot_name()}' first.")
            return

        _, snap_layer_names = slot["snapshot"]

        # Resolve snapshot layer names to live QgsVectorLayer objects
        sensitive_layers = []
        missing = []
        for lyr_name in snap_layer_names:
            lyrs = QgsProject.instance().mapLayersByName(lyr_name)
            if lyrs:
                sensitive_layers.append(lyrs[0])
            else:
                missing.append(lyr_name)

        if missing:
            resp = QMessageBox.question(
                self, "LAA Export — Missing Layers",
                "Some layers from the last analysis are no longer loaded:\n"
                f"  {', '.join(missing)}\n\n"
                "Proceed with the remaining layers?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp == QMessageBox.No:
                return

        if not sensitive_layers:
            QMessageBox.warning(self, "LAA Export",
                "No layers from the last analysis are currently loaded.\n"
                "Reload the layers and re-run Habitat Overlap.")
            return

        # Show type-assignment dialog
        layer_types, overlap_only = self._show_laa_type_dialog(sensitive_layers)
        if layer_types is None:
            return  # user cancelled

        # Persist the assignments into this slot
        self._active_slot()["laa_types"] = layer_types
        self._save_to_project()

        # Ask where to save
        path, _ = QFileDialog.getSaveFileName(
            self, "Export LAA Pre-Report Shapefile", "", "Shapefile (*.shp)"
        )
        if not path:
            return

        self._run_laa_export(sensitive_layers, layer_types, path, overlap_only=overlap_only)

    def _show_laa_type_dialog(self, sensitive_layers):
        """
        Show a dialog for the user to assign an LAA type to each layer.
        Returns {layer_name: type_str} or None if cancelled.
        """
        from qgis.PyQt.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTableWidget, QTableWidgetItem, QComboBox, QAbstractItemView,
            QHeaderView, QSizePolicy,
        )
        from qgis.PyQt.QtCore import Qt

        dlg = QDialog(self)
        dlg.setWindowTitle("LAA Pre-Report — Assign Layer Types")
        dlg.setMinimumWidth(540)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(
            "Assign an LAA category to each sensitive layer used in the last analysis.\n"
            "Layers marked 'Skip' will not appear in the output shapefile.\n\n"
            "Required for LAA pre-reporting (RFP Task 2.1):\n"
            "  NSO Habitat · Critical Habitat · RA32 Habitat · LRMP Allocation"
        ))

        tbl = QTableWidget(len(sensitive_layers), 2, dlg)
        tbl.setHorizontalHeaderLabels(["Layer", "LAA Type"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        tbl.horizontalHeader().resizeSection(1, 220)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(True)

        for row, lyr in enumerate(sensitive_layers):
            tbl.setItem(row, 0, QTableWidgetItem(lyr.name()))
            combo = QComboBox()
            combo.addItems(self.LAA_TYPES)
            saved = self._active_slot()["laa_types"].get(lyr.name(), "General Sensitive Area")
            if saved in self.LAA_TYPES:
                combo.setCurrentText(saved)
            tbl.setCellWidget(row, 1, combo)

        layout.addWidget(tbl)

        info = QLabel(
            "Output: one shapefile row per homogeneous trail segment.\n"
            "Each segment labeled Yes/No for each LAA category it falls within."
        )
        info.setStyleSheet("color: #555; font-style: italic;")
        layout.addWidget(info)

        from qgis.PyQt.QtWidgets import QCheckBox
        overlap_chk = QCheckBox(
            "Only export trails with at least one LAA overlap\n"
            "(uncheck to include all trails with full Yes/No attribute context)"
        )
        overlap_chk.setChecked(True)
        overlap_chk.setToolTip(
            "Checked: shapefile contains only trail segments that fall inside a sensitive area.\n"
            "Unchecked: all trail segments are exported; non-overlapping segments show No for all LAA fields."
        )
        layout.addWidget(overlap_chk)

        btn_row = QHBoxLayout()
        btn_export = QPushButton("Export LAA Shapefile")
        btn_cancel = QPushButton("Cancel")
        btn_row.addWidget(btn_export)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        btn_export.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)

        from qgis.PyQt.QtWidgets import QDialog as _QD
        if dlg.exec_() != _QD.Accepted:
            return None, True

        result = {}
        for row, lyr in enumerate(sensitive_layers):
            combo = tbl.cellWidget(row, 1)
            result[lyr.name()] = combo.currentText() if combo else "General Sensitive Area"
        return result, overlap_chk.isChecked()

    def _run_laa_export(self, sensitive_layers, layer_types, path, overlap_only=True):
        """
        Segment each trail at polygon boundaries from all active typed layers,
        label each segment by LAA category, and write the output shapefile.

        overlap_only: if True, only segments with at least one LAA=Yes are written.
        """
        trail_lyr = trail_layer() or self.iface.activeLayer()
        if not trail_lyr:
            QMessageBox.warning(self, "LAA Export", "No trail layer found.")
            return

        trail_features = (
            list(trail_lyr.selectedFeatures())
            if trail_lyr.selectedFeatureCount() > 0
            else list(trail_lyr.getFeatures())
        )
        if not trail_features:
            QMessageBox.warning(self, "LAA Export", "No trail features found.")
            return

        trail_fields = [f.name() for f in trail_lyr.fields()]
        trail_name_field = next(
            (f for f in ["Name", "name", "TRAIL_NAME", "TrailName", "trail_name"]
             if f in trail_fields), None
        )
        miles_mult = distance_multiplier(trail_lyr, "Miles")

        # Filter to layers not marked Skip; build spatial index per layer
        active_layer_data = {}  # {lyr_name: {type, geoms, index}}
        for lyr in sensitive_layers:
            lyr_type = layer_types.get(lyr.name(), "General Sensitive Area")
            if lyr_type == "(Skip — exclude from LAA export)":
                continue

            needs_xform = trail_lyr.crs() != lyr.crs()
            xform = QgsCoordinateTransform(
                lyr.crs(), trail_lyr.crs(), QgsProject.instance()
            ) if needs_xform else None

            geoms = []
            for feat in lyr.getFeatures():
                g = feat.geometry()
                if not g or g.isEmpty():
                    continue
                if xform:
                    g = QgsGeometry(g)
                    g.transform(xform)
                geoms.append(g)

            idx = QgsSpatialIndex()
            for i, g in enumerate(geoms):
                tmp = QgsFeature(i)
                tmp.setGeometry(g)
                idx.addFeature(tmp)

            active_layer_data[lyr.name()] = {
                "type":  lyr_type,
                "geoms": geoms,
                "index": idx,
            }

        if not active_layer_data:
            QMessageBox.warning(self, "LAA Export",
                "All layers were marked Skip. Assign at least one LAA type and try again.")
            return

        # Determine which LAA types are actually used (for field creation)
        used_types = sorted({d["type"] for d in active_layer_data.values()})

        # Build output schema
        fields = QgsFields()
        fields.append(QgsField("Trail",    QVariant.String, len=60))
        fields.append(QgsField("Seg_Mi",   QVariant.Double))
        for laa_type in used_types:
            fname = self.LAA_FIELD_MAP.get(laa_type, laa_type[:10])
            fields.append(QgsField(fname, QVariant.String, len=5))
        fields.append(QgsField("In_Layers", QVariant.String, len=250))

        crs = trail_lyr.crs()
        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.MultiLineString, crs, "ESRI Shapefile"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(self, "LAA Export",
                f"Could not create shapefile:\n{writer.errorMessage()}")
            return

        total_segments = 0
        for trail_feat in trail_features:
            trail_geom = trail_feat.geometry()
            if not trail_geom or trail_geom.isEmpty():
                continue
            t_name = (
                str(trail_feat.attribute(trail_name_field))
                if trail_name_field else f"Trail {trail_feat.id()}"
            )

            segments = self._segment_trail_for_laa(trail_geom, active_layer_data)

            for seg_geom, type_in, in_layers in segments:
                seg_mi = seg_geom.length() * miles_mult
                if seg_mi < 0.00005:
                    continue

                # When overlap_only is set, skip segments with no LAA conflict
                if overlap_only and not any(type_in.values()):
                    continue

                feat = QgsFeature()
                feat.setGeometry(seg_geom)
                feat.setFields(fields)
                feat.setAttribute("Trail",    t_name[:60])
                feat.setAttribute("Seg_Mi",   round(seg_mi, 5))
                for laa_type in used_types:
                    fname = self.LAA_FIELD_MAP.get(laa_type, laa_type[:10])
                    feat.setAttribute(fname, "Yes" if type_in.get(laa_type) else "No")
                feat.setAttribute("In_Layers", ", ".join(in_layers)[:250])
                writer.addFeature(feat)
                total_segments += 1

        del writer

        # Add to map canvas
        laa_layer = QgsVectorLayer(path, "LAA Pre-Report — Trail Segments", "ogr")
        if laa_layer.isValid():
            QgsProject.instance().addMapLayer(laa_layer)

        scope = "overlapping" if overlap_only else "all"
        QMessageBox.information(
            self, "LAA Export Complete",
            f"Exported {total_segments} {scope} trail segment(s) to:\n{path}\n\n"
            f"LAA fields: {', '.join(self.LAA_FIELD_MAP.get(t, t) for t in used_types)}\n\n"
            "Layer added to map canvas. Style by LAA field in QGIS to review."
        )

    def _segment_trail_for_laa(self, trail_geom, active_layer_data, densify_dist=5.0):
        """
        Densify the trail and classify each edge-midpoint against all typed layers.
        Group consecutive edges with identical classification into polyline segments.

        Returns list of (seg_geom, type_in_dict, in_layers_list) tuples where:
          seg_geom     — QgsGeometry (LineString)
          type_in_dict — {laa_type_str: bool}
          in_layers_list — [layer_names that contain this segment]
        """
        dense = trail_geom.densifyByDistance(densify_dist)
        vertices = list(dense.vertices())  # QgsPoint objects

        if len(vertices) < 2:
            return []

        laa_types = list({d["type"] for d in active_layer_data.values()})

        # Classify each edge by testing its midpoint
        edge_data = []  # [(type_in_dict, [in_layer_names])]
        for i in range(len(vertices) - 1):
            v1, v2 = vertices[i], vertices[i + 1]
            mid_x = (v1.x() + v2.x()) / 2.0
            mid_y = (v1.y() + v2.y()) / 2.0
            mid_pt = QgsGeometry.fromPointXY(QgsPointXY(mid_x, mid_y))

            # Expand bbox slightly for spatial index query
            mid_rect = mid_pt.boundingBox()
            mid_rect.grow(densify_dist * 0.05 + 0.001)

            type_in = {t: False for t in laa_types}
            in_layers = []

            for lyr_name, data in active_layer_data.items():
                lyr_type = data["type"]
                idx = data["index"]
                geoms = data["geoms"]
                for fid in idx.intersects(mid_rect):
                    if geoms[fid].contains(mid_pt):
                        type_in[lyr_type] = True
                        if lyr_name not in in_layers:
                            in_layers.append(lyr_name)
                        break

            edge_data.append((type_in, in_layers))

        # Group consecutive edges with identical type_in classification
        segments = []
        group_start = 0
        group_type_in = edge_data[0][0]
        group_layers = list(edge_data[0][1])

        def _flush_group(start, end, g_type_in, g_layers):
            seg_pts = [QgsPointXY(v.x(), v.y()) for v in vertices[start: end + 1]]
            if len(seg_pts) >= 2:
                seg_geom = QgsGeometry.fromPolylineXY(seg_pts)
                segments.append((seg_geom, dict(g_type_in), list(g_layers)))

        for i in range(1, len(edge_data)):
            curr_type_in, curr_layers = edge_data[i]
            if curr_type_in != group_type_in:
                _flush_group(group_start, i, group_type_in, group_layers)
                group_start = i
                group_type_in = curr_type_in
                group_layers = list(curr_layers)
            else:
                for ln in curr_layers:
                    if ln not in group_layers:
                        group_layers.append(ln)

        # Flush last group
        _flush_group(group_start, len(vertices) - 1, group_type_in, group_layers)

        return segments

    def export_habitat_triage(self):
        if not self._active_slot()["data"]:
            QMessageBox.information(self, "Export",
                f"No results for '{self._active_slot_name()}' yet. Run the triage analysis first.")
            return

        import re as _re, os as _os
        _safe = _re.sub(r'[^\w]+', '_', self._active_slot_name()).strip('_')
        _default = _os.path.join(_os.path.expanduser("~"), f"Triage_{_safe}.shp")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Trail Triage Shapefile", _default, "Shapefile (*.shp)"
        )
        if not path:
            return

        trail_lyr = trail_layer() or self.iface.activeLayer()
        crs = trail_lyr.crs() if trail_lyr else QgsCoordinateReferenceSystem("EPSG:4326")

        fields = QgsFields()
        fields.append(QgsField("Trail",   QVariant.String, len=60))
        fields.append(QgsField("SegType", QVariant.String, len=10))
        fields.append(QgsField("Miles",   QVariant.Double))
        fields.append(QgsField("Layers",  QVariant.String, len=250))

        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.MultiLineString, crs, "ESRI Shapefile"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(
                self, "Export Error", f"Could not create shapefile:\n{writer.errorMessage()}"
            )
            return

        miles_mult = distance_multiplier(trail_lyr, "Miles")
        seg_configs = [
            ("red_geom",    "RED"),
            ("yellow_geom", "YELLOW"),
            ("green_geom",  "GREEN"),
        ]

        slot_data = self._active_slot()["data"]
        for r in slot_data:
            for geom_key, seg_type in seg_configs:
                seg_geom = r.get(geom_key)
                if not seg_geom or seg_geom.isEmpty():
                    continue
                seg_mi = seg_geom.length() * miles_mult
                if seg_mi < 0.0001:
                    continue

                lyr_parts = []
                for lyr_name, d in r["layer_detail"].items():
                    if seg_type == "RED" and d["red_mi"] > 0.001:
                        lyr_parts.append(lyr_name)
                    elif seg_type == "YELLOW" and d["yellow_mi"] > 0.001:
                        lyr_parts.append(lyr_name)

                feat = QgsFeature()
                feat.setGeometry(seg_geom)
                feat.setFields(fields)
                feat.setAttribute("Trail",   r["trail"][:60])
                feat.setAttribute("SegType", seg_type)
                feat.setAttribute("Miles",   round(seg_mi, 4))
                feat.setAttribute("Layers",  ", ".join(lyr_parts)[:250])
                writer.addFeature(feat)

        del writer

        red_mi    = sum(r["red_miles"]    for r in slot_data)
        yellow_mi = sum(r["yellow_miles"] for r in slot_data)
        green_mi  = sum(r["green_miles"]  for r in slot_data)
        QMessageBox.information(
            self, "Export Complete",
            f"Exported trail segments to:\n{path}\n\n"
            f"🔴 RED    : {red_mi:.3f} mi\n"
            f"🟡 YELLOW : {yellow_mi:.3f} mi\n"
            f"🟢 GREEN  : {green_mi:.3f} mi\n\n"
            "Attributes: Trail, SegType, Miles, Layers"
        )

    # ──────────────────────────────────────────────
    # Tab 3: NEPA Report
    # ──────────────────────────────────────────────

    def _setup_report_tab(self):
        from qgis.PyQt.QtGui import QFont
        import datetime

        self.generateReportButton.clicked.connect(self.generate_report)
        self.exportReportButton.clicked.connect(self.export_report)
        self.reportPreviewText.setFont(QFont("Courier New", 9))

        # Pre-fill date field with today
        self.reportDateEdit.setText(datetime.date.today().strftime("%B %Y"))
        self._update_report_status()

    def _update_report_status(self):
        """Refresh the Analysis Status panel in the Report tab."""
        if self._crossings_snapshot:
            ts, layers = self._crossings_snapshot
            n = len(self._crossings_data)
            layer_str = ", ".join(layers) if layers else "—"
            self.reportCrossingsStatus.setText(
                f"✓  Stream Crossings  |  {ts}  |  {n} crossings  |  {layer_str}"
            )
            self.reportCrossingsStatus.setStyleSheet("color: #1a7a1a;")
        else:
            self.reportCrossingsStatus.setText(
                "✗  Stream Crossings — not yet run (go to Stream Crossings tab)"
            )
            self.reportCrossingsStatus.setStyleSheet("color: #cc2200;")

        # Build a per-slot HTML status block — green for run, red for not yet run
        from qgis.PyQt.QtCore import Qt
        html_lines = []
        for name, slot in self._habitat_slots.items():
            snap = slot.get("snapshot")
            data = slot.get("data", [])
            if snap:
                ts, layers = snap
                red_mi = sum(r["red_miles"] for r in data)
                n_trails = len(data)
                layer_str = ", ".join(layers[:2]) + ("…" if len(layers) > 2 else "")
                text = (
                    f"✓  {name}  |  {ts}  |  {n_trails} trails  |  "
                    f"{red_mi:.3f} mi RED  |  {layer_str}"
                )
                html_lines.append(f'<span style="color:#1a7a1a;">{text}</span>')
            else:
                html_lines.append(
                    f'<span style="color:#cc2200;">✗  {name} — not yet run</span>'
                )

        if html_lines:
            self.reportHabitatStatus.setTextFormat(Qt.RichText)
            self.reportHabitatStatus.setText("<br/>".join(html_lines))
            self.reportHabitatStatus.setStyleSheet("")
        else:
            self.reportHabitatStatus.setTextFormat(Qt.PlainText)
            self.reportHabitatStatus.setText("✗  Habitat Triage — no categories run yet")
            self.reportHabitatStatus.setStyleSheet("color: #888;")

    # ── Stream Crossings tab index in mainTabWidget ──────────────────
    _CROSSINGS_TAB_IDX = 1

    def _on_tab_changed(self, new_idx):
        """Prompt to export crossing annotations when leaving the Stream Crossings tab."""
        # Only fire when navigating AWAY from the crossings tab
        if new_idx == self._CROSSINGS_TAB_IDX:
            return
        # Only prompt if there are fish-bearing crossings in the table and they haven't been exported
        if not self.crossingsFishTable.isVisible():
            return
        if self.crossingsFishTable.rowCount() == 0:
            return
        if self._crossings_exported:
            return

        from qgis.PyQt.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self,
            "Unsaved Crossing Annotations",
            "You have crossing type selections for fish-bearing streams that haven't been "
            "exported yet.\n\nExport to shapefile now to preserve them?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if reply == QMessageBox.Yes:
            self.export_crossings()

    def generate_report(self):
        import datetime
        from collections import defaultdict

        # ── Confirmation dialog if fish-bearing crossing annotations are present ──
        if self._crossings_data and self.crossingsFishTable.isVisible() and \
                self.crossingsFishTable.rowCount() > 0:
            self._read_fish_crossing_annotations()
            fb_list = [c for c in self._crossings_data if c.get("fish_bearing") == "Yes"]
            existing_types = {
                "Existing road bridge (no new work)",
                "Existing culvert (no new work)",
                "Existing ford / primitive crossing",
            }
            existing = [c for c in fb_list if c.get("crossing_type", "") in existing_types]
            proposed = [c for c in fb_list if c.get("crossing_type", "") not in existing_types]

            summary_lines = [
                f"Fish-bearing crossings found: {len(fb_list)}",
                f"  Existing structures (no new work) : {len(existing)}",
                f"  Proposed new crossings            : {len(proposed)}",
                "",
            ]
            for c in fb_list:
                ctype = c.get("crossing_type", "Proposed new crossing")
                notes = f"  [{c.get('crossing_notes', '')}]" if c.get("crossing_notes") else ""
                summary_lines.append(f"  • {c['trail']} @ {c['dist_miles']:.3f} mi — {ctype}{notes}")

            summary_lines += ["", "Generate the screening memo with these crossing types?"]

            from qgis.PyQt.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Confirm Crossing Types",
                "\n".join(summary_lines),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply == QMessageBox.No:
                return

        proj_name  = self.reportProjectNameEdit.text().strip() or "[Project Name]"
        forest     = self.reportForestEdit.text().strip()      or "[Forest / Unit]"
        location   = self.reportLocationEdit.text().strip()    or "[Location]"
        preparer   = self.reportPreparerEdit.text().strip()    or "[Preparer]"
        date_str   = self.reportDateEdit.text().strip()        or datetime.date.today().strftime("%B %Y")
        action      = self.reportActionEdit.toPlainText().strip()
        pdfs_raw    = self.reportPDFsEdit.toPlainText().strip()
        methodology = self.reportMethodologyEdit.toPlainText().strip()
        data_gaps_raw = self.reportDataGapsEdit.toPlainText().strip()

        pdfs = [line.strip() for line in pdfs_raw.splitlines() if line.strip()]

        W = 70  # page width for text formatting

        def bar(char="═"):
            return char * W

        def section(title):
            return f"\n{bar()}\n{title}\n{bar()}\n"

        lines = [
            bar("═"),
            "ENVIRONMENTAL SCREENING MEMO".center(W),
            "National Environmental Policy Act — Project Design Review".center(W),
            bar("═"),
            "",
            f"  Project  : {proj_name}",
            f"  Forest   : {forest}",
            f"  Location : {location}",
            f"  Prepared : {preparer}",
            f"  Date     : {date_str}",
            "",
        ]

        # ── Key Findings (auto-generated) ──
        key_findings = []

        if self._crossings_data:
            total_x   = len(self._crossings_data)
            fb_x      = sum(1 for c in self._crossings_data if c.get("fish_bearing") == "Yes")
            c3_x      = sum(1 for c in self._crossings_data if c.get("stream_class") == "Class 3")
            c45_x     = sum(1 for c in self._crossings_data
                            if c.get("stream_class") in ("Class 4", "Class 5"))
            key_findings.append(
                f"  • {total_x} stream crossings identified on proposed new trail construction "
                f"({fb_x} fish-bearing Class 1/2, {c3_x} Class 3 field-verify, {c45_x} Class 4/5 desktop)."
            )
            if fb_x == 0:
                key_findings.append(
                    "  • No fish-bearing (Class 1 or 2) stream crossings identified — "
                    "proposed alignment avoids all mapped fish-bearing streams."
                )

        # Aggregate key findings from all populated habitat slots
        populated_slots_kf = [
            (name, slot) for name, slot in self._habitat_slots.items()
            if slot.get("data")
        ]
        if populated_slots_kf:
            # Use the first populated slot for total-project miles (all slots analyze
            # the same trail network — they differ only in which sensitive layers are used)
            first_data_kf = populated_slots_kf[0][1]["data"]
            total_mi_kf = sum(r["miles"] for r in first_data_kf)
            n_trails_kf = len(first_data_kf)
            key_findings.append(
                f"  • {total_mi_kf:.2f} total project miles analyzed across "
                f"{n_trails_kf} trail segment(s) ({len(populated_slots_kf)} "
                f"screening categor{'y' if len(populated_slots_kf) == 1 else 'ies'} run)."
            )
            for slot_name_kf, slot_kf in populated_slots_kf:
                data_kf = slot_kf["data"]
                red_mi_kf    = sum(r["red_miles"]    for r in data_kf)
                green_mi_kf  = sum(r["green_miles"]  for r in data_kf)
                yellow_mi_kf = sum(r["yellow_miles"] for r in data_kf)
                pct_green = (green_mi_kf / total_mi_kf * 100) if total_mi_kf > 0 else 0
                red_trails_kf = [r for r in data_kf if r["triage"] == "RED"]
                if red_mi_kf > 0:
                    key_findings.append(
                        f"  • {slot_name_kf}: {red_mi_kf:.3f} mi direct conflict (RED) across "
                        f"{len(red_trails_kf)} segment(s); {pct_green:.0f}% clear."
                    )
                else:
                    key_findings.append(
                        f"  • {slot_name_kf}: All segments clear of direct sensitive area "
                        f"conflicts ({pct_green:.0f}% GREEN)."
                    )
                if yellow_mi_kf > 0:
                    key_findings.append(
                        f"    ↳ {yellow_mi_kf:.3f} mi within proximity buffer (YELLOW) — "
                        f"desktop review recommended."
                    )

        # CE / EA pathway signal — aggregate worst-case RED across all slots
        any_hab_data = any(s.get("data") for s in self._habitat_slots.values())
        if any_hab_data and self._crossings_data is not None:
            total_red_all = sum(
                sum(r["red_miles"] for r in s["data"])
                for s in self._habitat_slots.values() if s.get("data")
            )
            if total_red_all < 0.1:
                key_findings.append(
                    "  • GIS screening indicates potential Categorical Exclusion pathway — "
                    "subject to specialist concurrence."
                )
            else:
                key_findings.append(
                    "  • GIS screening indicates Environmental Assessment (EA) pathway required."
                )

        lines.append(section("KEY FINDINGS"))
        if key_findings:
            lines += key_findings
        else:
            lines += [
                "  [Run Stream Crossings and Habitat Overlap analyses to generate",
                "   auto-populated key findings.]",
            ]

        # ── Proposed Action ──
        lines.append(section("1. PROPOSED ACTION"))
        if action:
            for para in action.splitlines():
                lines.append(f"  {para}")
        else:
            lines.append("  [No proposed action description entered.]")

        # ── Project Design Features ──
        lines.append(section("2. PROJECT DESIGN FEATURES / AVOIDANCE MEASURES"))
        if pdfs:
            for i, pdf in enumerate(pdfs, 1):
                lines.append(f"  {i:>2}. {pdf}")
        else:
            lines.append("  [No project design features entered.]")

        # ── Desktop Screening Methodology ──
        lines.append(section("3. DESKTOP SCREENING METHODOLOGY / DATA SOURCES"))
        if methodology:
            for para in methodology.splitlines():
                lines.append(f"  {para}")
        else:
            lines += [
                "  [No methodology description entered.]",
                "",
                "  Recommended content:",
                "  • Stream layer name and date received from FS",
                "  • Trail alignment layer used for analysis",
                "  • Road segment exclusion rationale",
                "  • Preliminary crossing count from RFP scope vs. desktop result",
                "  • Note requesting layer version confirmation with MRRD Hydrologist",
            ]

        # ── Stream Crossings ──
        lines.append(section("4. STREAM CROSSING ANALYSIS"))
        if self._crossings_snapshot:
            snap_time, snap_layers = self._crossings_snapshot
            lines.append(f"  Analysis run : {snap_time}")
            lines.append(f"  Hydro layers : {', '.join(snap_layers)}")
            lines.append("")
        if not self._crossings_data:
            lines.append("  Stream crossing analysis not yet run.")
            lines.append("  Run the Stream Crossings tab first.")
        else:
            # Capture any crossing types / notes the user has entered in the table
            self._read_fish_crossing_annotations()

            total    = len(self._crossings_data)
            fb_list  = [c for c in self._crossings_data if c.get("fish_bearing") == "Yes"]
            c3_list  = [c for c in self._crossings_data if c.get("stream_class") == "Class 3"]
            c45_list = [c for c in self._crossings_data
                        if c.get("stream_class") in ("Class 4", "Class 5")]
            fb_count  = len(fb_list)
            c3_count  = len(c3_list)
            c45_count = len(c45_list)
            nfb_count = total - fb_count

            lines += [
                f"  {'Class':<30} {'Count':>6}  Survey Approach",
                f"  {'─'*62}",
                f"  {'Class 1 & 2 (fish-bearing)':<30} {fb_count:>6}  Field-documented; specify crossing type",
                f"  {'Class 3 (field verify)':<30} {c3_count:>6}  Recommend field survey — fish distribution boundary",
                f"  {'Class 4 & 5 (non-fish-bearing)':<30} {c45_count:>6}  GIS documentation only",
                f"  {'─'*62}",
                f"  {'Total':<30} {total:>6}",
                "",
            ]

            # ── Fish-bearing detail table ──────────────────────────────
            if fb_list:
                # Summarise by crossing type
                existing_types = {
                    "Existing road bridge (no new work)",
                    "Existing culvert (no new work)",
                    "Existing ford / primitive crossing",
                }
                existing = [c for c in fb_list if c.get("crossing_type","") in existing_types]
                proposed = [c for c in fb_list if c.get("crossing_type","") not in existing_types]

                lines += [
                    f"  Fish-Bearing Crossing Summary:",
                    f"    Existing structures (no new ground disturbance) : {len(existing)}",
                    f"    Proposed new crossings                          : {len(proposed)}",
                    "",
                    f"  {'#':<4} {'Trail':<28} {'Mi':>6}  {'Class':<10} {'Type':<35} Notes",
                    f"  {'─'*100}",
                ]
                for i, c in enumerate(fb_list, 1):
                    ctype = c.get("crossing_type", "Proposed new crossing")
                    notes = c.get("crossing_notes", "")
                    lines.append(
                        f"  {i:<4} {c['trail'][:28]:<28} {c['dist_miles']:>6.3f}  "
                        f"{c['stream_class']:<10} {ctype:<35} {notes}"
                    )

                # Finding sentence
                lines.append("")
                if len(existing) == fb_count:
                    lines.append(
                        f"  FINDING: All {fb_count} fish-bearing stream crossing(s) use existing"
                    )
                    lines.append(
                        "  structures. No new ground disturbance proposed at Class 1 or Class 2 streams."
                    )
                elif len(proposed) > 0:
                    lines.append(
                        f"  NOTE: {len(proposed)} proposed new crossing(s) at fish-bearing streams."
                    )
                    lines.append(
                        "  Hardened crossing structures required per Project Design Features."
                    )
                    lines.append(
                        "  ESA Section 7 informal consultation (Fisheries) likely required."
                    )
            else:
                lines.append(
                    "  FINDING: No fish-bearing (Class 1 or Class 2) stream crossings identified."
                )
                lines.append(
                    "  The proposed alignment avoids all mapped fish-bearing streams."
                )

            # ── Class 3 field survey recommendation ───────────────────
            if c3_count > 0:
                lines += [
                    "",
                    f"  Class 3 Stream Crossings (n={c3_count}) — Field Survey Recommended:",
                    "  Class 3 streams are mapped as non-fish-bearing based on preliminary",
                    "  LiDAR-derived stream classification. Per Task 2.2.1(b), field surveys",
                    f"  are recommended at all {c3_count} Class 3 crossing(s) to verify fish",
                    "  distribution boundaries and confirm non-fish-bearing status.",
                    "  Class 4 and 5 crossings are proposed for GIS documentation only.",
                ]

            # ── Class 3-5 optional notes ───────────────────────────────
            nfb_note = self.crossingsNFBNotesEdit.toPlainText().strip()
            if nfb_count > 0 and nfb_note:
                lines += ["", f"  Non-Fish-Bearing Crossings (Class 3–5, n={nfb_count}):"]
                lines.append(f"  {nfb_note}")

        # ── Habitat / Trail Triage — one subsection per named category ──
        lines.append(section("5. SENSITIVE AREA OVERLAP — TRAIL TRIAGE"))

        any_triage_run = any(s.get("data") for s in self._habitat_slots.values())
        if not any_triage_run:
            lines += [
                "  Habitat overlap analysis not yet run.",
                "  Go to the Habitat Overlap tab, select an analysis category and",
                "  sensitive layers, then click Run Triage Analysis.",
            ]
        else:
            subsection_bar = "─" * 62
            for slot_name_5, slot_5 in self._habitat_slots.items():
                data_5 = slot_5.get("data", [])
                snap_5 = slot_5.get("snapshot")

                lines += [
                    "",
                    f"  ── {slot_name_5} " + "─" * max(2, 58 - len(slot_name_5)),
                ]

                if snap_5:
                    ts_5, layers_5 = snap_5
                    lines.append(f"  Analysis run    : {ts_5}")
                    lines.append(f"  Sensitive layers: {', '.join(layers_5)}")
                    lines.append("")

                if not data_5:
                    lines.append(
                        "  Not yet run — select this category in the Habitat Overlap tab and run."
                    )
                    continue

                total_mi_5  = sum(r["miles"]        for r in data_5)
                red_mi_5    = sum(r["red_miles"]    for r in data_5)
                yellow_mi_5 = sum(r["yellow_miles"] for r in data_5)
                green_mi_5  = sum(r["green_miles"]  for r in data_5)
                pct_clear_5 = (green_mi_5 / total_mi_5 * 100) if total_mi_5 > 0 else 0

                lines += [
                    f"  Total mileage analyzed    : {total_mi_5:.3f} mi",
                    f"  Miles - direct conflict   : {red_mi_5:.3f} mi  "
                    f"({red_mi_5 / total_mi_5 * 100:.1f}%)" if total_mi_5 > 0
                    else f"  Miles - direct conflict   : {red_mi_5:.3f} mi",
                    f"  Miles - within buffer zone: {yellow_mi_5:.3f} mi  "
                    f"({yellow_mi_5 / total_mi_5 * 100:.1f}%)" if total_mi_5 > 0
                    else f"  Miles - within buffer zone: {yellow_mi_5:.3f} mi",
                    f"  Miles - clear of all areas: {green_mi_5:.3f} mi  ({pct_clear_5:.1f}%)",
                    "",
                    f"  {'Trail':<30} {'Total':>6}  {'RED mi':>7}  {'YLW mi':>7}  {'GRN mi':>7}",
                    f"  {subsection_bar}",
                ]
                for r in data_5:
                    icon = {"RED": "[R]", "YELLOW": "[Y]", "GREEN": "[G]"}[r["triage"]]
                    lines.append(
                        f"  {icon} {r['trail'][:28]:<28} {r['miles']:>6.2f}  "
                        f"{r['red_miles']:>7.3f}  {r['yellow_miles']:>7.3f}  "
                        f"{r['green_miles']:>7.3f}"
                    )
                    layer_detail_5 = r.get("layer_detail", {})
                    if layer_detail_5:
                        for lyr_name_5 in sorted(layer_detail_5):
                            d5 = layer_detail_5[lyr_name_5]
                            red_l5    = d5.get("red_mi",    0.0)
                            yellow_l5 = d5.get("yellow_mi", 0.0)
                            lines.append(
                                f"       {lyr_name_5[:38]:<38}  "
                                f"RED: {red_l5:>6.3f} mi  YLW: {yellow_l5:>6.3f} mi"
                            )

        # ── Data Gaps ──
        lines.append(section("6. DATA GAPS / OUTSTANDING QUESTIONS"))
        data_gaps = [g.strip() for g in data_gaps_raw.splitlines() if g.strip()]
        if data_gaps:
            for i, gap in enumerate(data_gaps, 1):
                lines.append(f"  {i:>2}. {gap}")
        else:
            lines += [
                "  [No data gaps entered.]",
                "",
                "  Consider noting:",
                "  • Stream layer version confirmation needed (MRRD Hydrologist)",
                "  • Wetland delineation status",
                "  • Class 3 fish distribution boundary verification",
                "  • Pending specialist survey results",
            ]

        # ── NEPA Recommendation ──
        lines.append(section("7. NEPA RECOMMENDATION"))
        populated_slots_rec = [
            (nm, sl) for nm, sl in self._habitat_slots.items() if sl.get("data")
        ]
        if populated_slots_rec and self._crossings_data is not None:
            # Build worst-triage per trail across all populated slots.
            # Same trail may appear in multiple categories; take the most
            # conservative (highest-risk) triage and max RED/YELLOW miles.
            triage_rank = {"RED": 0, "YELLOW": 1, "GREEN": 2}
            trail_worst_rec = {}
            for _, sl_rec in populated_slots_rec:
                for r in sl_rec["data"]:
                    t = r["trail"]
                    if t not in trail_worst_rec:
                        trail_worst_rec[t] = {
                            "triage": r["triage"],
                            "miles": r["miles"],
                            "red_miles": r["red_miles"],
                            "green_miles": r["green_miles"],
                        }
                    else:
                        ex = trail_worst_rec[t]
                        if triage_rank[r["triage"]] < triage_rank[ex["triage"]]:
                            ex["triage"] = r["triage"]
                        ex["red_miles"]   = max(ex["red_miles"],   r["red_miles"])
                        ex["green_miles"] = min(ex["green_miles"], r["green_miles"])

            all_rec = list(trail_worst_rec.values())
            red_trails_rec   = [r for r in all_rec if r["triage"] == "RED"]
            total_mi_rec     = sum(r["miles"] for r in all_rec)
            green_mi_rec     = sum(r["green_miles"] for r in all_rec)
            red_mi_rec       = sum(r["red_miles"]   for r in all_rec)
            cats_run = len(populated_slots_rec)

            lines += [
                f"  Based on {cats_run} screening categor{'y' if cats_run == 1 else 'ies'} "
                f"across {len(all_rec)} trail segment(s) / {total_mi_rec:.2f} mi:",
                "",
            ]

            if red_mi_rec < 0.1 and len(red_trails_rec) == 0:
                lines += [
                    "  CATEGORICAL EXCLUSION (CE) — All trail segments are clear of direct",
                    "  sensitive area conflicts across all screening categories. Based on this",
                    f"  GIS screening, the proposed project ({total_mi_rec:.2f} mi) meets the",
                    "  criteria for a Categorical Exclusion under 36 CFR 220.6,",
                    "  subject to specialist concurrence.",
                ]
            elif red_mi_rec < 0.5:
                lines += [
                    "  TIERED APPROACH RECOMMENDED — The majority of project miles are clear",
                    f"  of sensitive area conflicts ({green_mi_rec:.2f} mi GREEN). A limited",
                    f"  Environmental Assessment (EA) focused on {red_mi_rec:.3f} mi of direct",
                    "  conflict segments may be sufficient, with CE treatment applied to",
                    "  the remainder of the alignment.",
                    "",
                    "  Recommended next steps:",
                    "  1. Field verification of RED-flagged segments across all categories",
                    "  2. Targeted specialist surveys (Wildlife, Botany, Hydrology)",
                    "  3. Evaluate reroute options to eliminate remaining RED miles",
                ]
            else:
                lines += [
                    "  ENVIRONMENTAL ASSESSMENT (EA) — Direct sensitive area conflicts",
                    f"  ({red_mi_rec:.3f} mi across all screening categories) require EA-level",
                    "  NEPA analysis. Specialist surveys and formal consultation (ESA",
                    "  Section 7) likely required prior to project approval.",
                    "",
                    "  Recommended next steps:",
                    "  1. Field-verify all RED-flagged segments",
                    "  2. Full specialist survey program per RFP",
                    "  3. Evaluate reroutes to reduce direct conflict mileage",
                ]
        else:
            lines += [
                "  Run Stream Crossings and Habitat Overlap analyses (in all relevant",
                "  categories) to generate an automated NEPA pathway recommendation.",
            ]

        lines += [
            "",
            bar("─"),
            "  This memo was generated by MTBDesignTools NEPA (GIS screening tool).",
            "  It is a preliminary desktop analysis only and does not substitute",
            "  for field surveys, specialist reports, or formal NEPA documentation.",
            bar("─"),
            "",
            f"  Prepared: {preparer}  |  {date_str}",
        ]

        memo_text = "\n".join(lines)
        self.reportPreviewText.setPlainText(memo_text)
        self._report_text = memo_text
        self._save_to_project()
        self.exportReportButton.setEnabled(True)

    # ── Project persistence ─────────────────────────────────────────────

    def _save_to_project(self):
        """Persist report fields and analysis data to the QGIS project file."""
        import json

        fields = {
            "project_name": self.reportProjectNameEdit.text(),
            "forest":       self.reportForestEdit.text(),
            "location":     self.reportLocationEdit.text(),
            "preparer":     self.reportPreparerEdit.text(),
            "date":         self.reportDateEdit.text(),
            "action":       self.reportActionEdit.toPlainText(),
            "pdfs":         self.reportPDFsEdit.toPlainText(),
            "methodology":  self.reportMethodologyEdit.toPlainText(),
            "data_gaps":    self.reportDataGapsEdit.toPlainText(),
        }

        # Crossings — strip non-serializable QgsPointXY object
        crossings_serial = [
            {k: v for k, v in c.items() if k != "point"}
            for c in self._crossings_data
        ]

        snap_c = None
        if self._crossings_snapshot:
            snap_c = {"time": self._crossings_snapshot[0],
                      "layers": self._crossings_snapshot[1]}

        # Habitat slots — strip QgsGeometry objects from each slot's data
        _geom_keys = {"red_geom", "yellow_geom", "green_geom", "geom"}
        slots_serial = {}
        for slot_name_s, slot_s in self._habitat_slots.items():
            data_s = slot_s.get("data", [])
            snap_s = slot_s.get("snapshot")
            slots_serial[slot_name_s] = {
                "data": [
                    {k: v for k, v in r.items() if k not in _geom_keys}
                    for r in data_s
                ],
                "snapshot": (
                    {"time": snap_s[0], "layers": snap_s[1]} if snap_s else None
                ),
                "laa_types": slot_s.get("laa_types", {}),
            }

        state = {
            "fields":             fields,
            "crossings":          crossings_serial,
            "crossings_snapshot": snap_c,
            "habitat_slots":      slots_serial,
            "active_slot":        self._active_slot_name(),
        }

        try:
            QgsProject.instance().writeEntry(
                "MTBDesignToolsNEPA", "state", json.dumps(state)
            )
        except Exception:
            pass

    def _load_from_project(self):
        """Restore report fields and analysis data from the QGIS project file."""
        import json

        raw, ok = QgsProject.instance().readEntry("MTBDesignToolsNEPA", "state", "")
        if not ok or not raw:
            return

        try:
            state = json.loads(raw)
        except (ValueError, TypeError):
            return

        # Report text fields — only overwrite if a saved value exists
        fields = state.get("fields", {})
        restore_map = [
            ("project_name", self.reportProjectNameEdit,  "setText"),
            ("forest",       self.reportForestEdit,        "setText"),
            ("location",     self.reportLocationEdit,      "setText"),
            ("preparer",     self.reportPreparerEdit,      "setText"),
            ("date",         self.reportDateEdit,          "setText"),
            ("action",       self.reportActionEdit,        "setPlainText"),
            ("pdfs",         self.reportPDFsEdit,          "setPlainText"),
            ("methodology",  self.reportMethodologyEdit,   "setPlainText"),
            ("data_gaps",    self.reportDataGapsEdit,      "setPlainText"),
        ]
        for key, widget, method in restore_map:
            val = fields.get(key, "")
            if val:
                getattr(widget, method)(val)

        # Crossings data + snapshot
        crossings = state.get("crossings", [])
        if crossings:
            self._crossings_data = crossings

        snap_c = state.get("crossings_snapshot")
        if snap_c:
            self._crossings_snapshot = (snap_c["time"], snap_c["layers"])

        # Habitat slots — restore each named category
        slots_loaded = state.get("habitat_slots", {})
        for slot_name_l, slot_l in slots_loaded.items():
            if slot_name_l not in self._habitat_slots:
                self._habitat_slots[slot_name_l] = {
                    "data": [], "snapshot": None, "laa_types": {}, "display_text": ""
                }
            target = self._habitat_slots[slot_name_l]
            target["data"] = slot_l.get("data", [])
            snap_l = slot_l.get("snapshot")
            target["snapshot"] = (snap_l["time"], snap_l["layers"]) if snap_l else None
            target["laa_types"] = slot_l.get("laa_types", {})

        # Add any custom slot names (not in the default list) back to the combo
        self.habitatSlotCombo.blockSignals(True)
        for slot_name_l in slots_loaded:
            if self.habitatSlotCombo.findText(slot_name_l) < 0:
                self.habitatSlotCombo.addItem(slot_name_l)
        self.habitatSlotCombo.blockSignals(False)

        # Restore active slot selection
        active_name_l = state.get("active_slot", "")
        if active_name_l:
            idx_l = self.habitatSlotCombo.findText(active_name_l)
            if idx_l >= 0:
                self.habitatSlotCombo.blockSignals(True)
                self.habitatSlotCombo.setCurrentIndex(idx_l)
                self.habitatSlotCombo.blockSignals(False)
            else:
                # Custom name not in list yet — set as text directly
                self.habitatSlotCombo.blockSignals(True)
                self.habitatSlotCombo.setCurrentText(active_name_l)
                self.habitatSlotCombo.blockSignals(False)

        # Enable export buttons if the active slot has data
        active_slot_l = self._active_slot()
        if active_slot_l.get("data"):
            self.exportHabitatButton.setEnabled(True)
            self.exportLAAButton.setEnabled(True)

        # Backward-compatibility: migrate old single-slot format saved by earlier versions
        if "habitat" in state and state.get("habitat") and not slots_loaded:
            first_slot = self._habitat_slots.get(self.DEFAULT_HABITAT_SLOTS[0])
            if first_slot is not None:
                first_slot["data"] = state["habitat"]
                snap_old = state.get("habitat_snapshot")
                first_slot["snapshot"] = (
                    (snap_old["time"], snap_old["layers"]) if snap_old else None
                )
                laa_old = state.get("laa_layer_types", {})
                first_slot["laa_types"] = laa_old
                if first_slot["data"]:
                    self.exportHabitatButton.setEnabled(True)
                    self.exportLAAButton.setEnabled(True)

        self._update_report_status()

    def export_report(self):
        if not getattr(self, "_report_text", None):
            QMessageBox.information(self, "Export", "Generate the report first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Screening Memo", "", "Text file (*.txt)"
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._report_text)
            QMessageBox.information(
                self, "Export Complete",
                f"Memo saved to:\n{path}"
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", str(exc))
