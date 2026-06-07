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
        self._habitat_data = []

        self._setup_profile_tab()
        self._setup_crossings_tab()
        self._setup_habitat_tab()

        # Refresh pickers when project layers change
        QgsProject.instance().layersAdded.connect(self._on_layers_changed)
        QgsProject.instance().layersRemoved.connect(self._on_layers_changed)

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
        self.refreshStreamsButton.clicked.connect(self.populate_streams_list)
        self.selectAllStreamsButton.clicked.connect(self._select_all_streams)
        self.runCrossingsButton.clicked.connect(self.run_crossing_analysis)
        self.exportCrossingsButton.clicked.connect(self.export_crossings)
        self.populate_streams_list()

        from qgis.PyQt.QtGui import QFont
        self.crossingsResultsText.setFont(QFont("Courier New", 9))

    def _on_layers_changed(self, *_args):
        self.populate_trail_dropdown()
        self.populate_dem_dropdown()
        self.populate_streams_list()
        self.populate_habitat_list()

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
        """Populate the multi-select hydro layer list with all line layers in the project."""
        self.streamsListWidget.clear()
        for lyr in sorted(
            QgsProject.instance().mapLayers().values(), key=lambda l: l.name()
        ):
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                from qgis.PyQt.QtWidgets import QListWidgetItem
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
        self._add_crossings_to_map(all_crossings, trail_lyr)
        self._display_crossings_results(all_crossings, trail_lyr, stream_layers, crs_notes)
        self.exportCrossingsButton.setEnabled(bool(all_crossings))

    def _display_crossings_results(self, crossings, trail_lyr=None, stream_layers=None, crs_notes=None):
        from collections import defaultdict

        if not crossings:
            self.crossingsResultsText.setPlainText(
                "No stream crossings found.\n\n"
                "Check that the trail and hydro layers overlap spatially,\n"
                "that the correct layers are selected in the list above,\n"
                "and that at least one hydro layer is loaded in the project."
            )
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

        # ── Per-trail summary ──────────────────────────────────────────
        trail_groups = defaultdict(list)
        for c in crossings:
            trail_groups[c["trail"]].append(c)

        lines.append("═" * 60)
        lines.append(
            f"SUMMARY  —  {len(crossings)} crossings  |  {len(trail_groups)} trail(s)"
        )
        lines.append("═" * 60)

        for trail_name in sorted(trail_groups):
            tcs = trail_groups[trail_name]
            class_counts = defaultdict(int)
            fish_count = 0
            for c in tcs:
                class_counts[c["stream_class"]] += 1
                if c["fish_bearing"] == "Yes":
                    fish_count += 1

            class_str = "  ".join(
                f"{cls}: {cnt}" for cls, cnt in sorted(class_counts.items())
            )
            lines.append(f"\n  {trail_name}")
            lines.append(f"    Crossings : {len(tcs)}   ({class_str})")
            if fish_count:
                lines.append(
                    f"    ⚠ Fish-bearing (Class 2): {fish_count} — ESA analysis required"
                )

        # ── Detail table ───────────────────────────────────────────────
        lines += ["", "─" * 60,
                  "DETAIL  (sorted by trail, then distance along trail)",
                  "─" * 60]

        col_trail = min(28, max(20, max(len(c["trail"]) for c in crossings) + 1))
        hdr = (
            f"{'#':<4} {'Trail':<{col_trail}} {'Class':<10} "
            f"{'Fish':<8} {'Mi':<8} Stream"
        )
        lines.append(hdr)
        lines.append("-" * len(hdr))

        for i, c in enumerate(crossings, 1):
            lines.append(
                f"{i:<4} "
                f"{c['trail'][:col_trail]:<{col_trail}} "
                f"{c['stream_class']:<10} "
                f"{c['fish_bearing']:<8} "
                f"{c['dist_miles']:<8.3f} "
                f"{c['stream_name']}"
            )

        lines += [
            "",
            "─" * 60,
            "✓ 'Stream Crossings - MTB NEPA' layer added to map canvas.",
            "  Use 'Export to Shapefile' to save for the NEPA project record.",
            "  Class 2 crossings flagged 'Yes' for fish-bearing require ESA",
            "  analysis under Fisheries Task 2.2.",
        ]

        self.crossingsResultsText.setPlainText("\n".join(lines))

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
            feat.setAttribute("Easting",    c["x"])
            feat.setAttribute("Northing",   c["y"])
            writer.addFeature(feat)

        del writer
        QMessageBox.information(
            self, "Export Complete",
            f"Exported {len(self._crossings_data)} crossing(s) to:\n{path}\n\n"
            "Attributes: Trail, StreamName, StrClass, FishBear, DistMiles, Easting, Northing"
        )

    # ──────────────────────────────────────────────
    # Tab 2: Habitat Overlap / Trail Triage
    # ──────────────────────────────────────────────

    def _setup_habitat_tab(self):
        self.refreshHabitatButton.clicked.connect(self.populate_habitat_list)
        self.selectAllHabitatButton.clicked.connect(self._select_all_habitat)
        self.runHabitatButton.clicked.connect(self.run_habitat_analysis)
        self.exportHabitatButton.clicked.connect(self.export_habitat_triage)
        self.populate_habitat_list()

        from qgis.PyQt.QtGui import QFont
        self.habitatResultsText.setFont(QFont("Courier New", 9))

    def populate_habitat_list(self, *_args):
        """Populate the sensitive layers list with all polygon and line vector layers."""
        self.habitatListWidget.clear()
        for lyr in sorted(
            QgsProject.instance().mapLayers().values(), key=lambda l: l.name()
        ):
            if not isinstance(lyr, QgsVectorLayer):
                continue
            geom_type = lyr.geometryType()
            if geom_type in (QgsWkbTypes.PolygonGeometry, QgsWkbTypes.LineGeometry):
                from qgis.PyQt.QtWidgets import QListWidgetItem
                item = QListWidgetItem(lyr.name())
                item.setData(Qt.UserRole, lyr.id())
                self.habitatListWidget.addItem(item)

    def _select_all_habitat(self):
        self.habitatListWidget.selectAll()

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

        # Convert buffer from feet to trail CRS native units
        buffer_ft = self.habitatBufferSpinBox.value()
        crs_units = trail_lyr.crs().mapUnits()
        ft_to_native = QgsUnitTypes.fromUnitToUnitFactor(
            QgsUnitTypes.DistanceFeet, crs_units
        )
        buffer_native = buffer_ft * ft_to_native

        self.habitatResultsText.setPlainText(
            f"Running triage across {len(sensitive_layers)} sensitive layer(s)…"
        )
        QApplication.processEvents()

        # Pre-build spatial index + reprojected geometry cache per sensitive layer
        layer_index_cache = {}
        for sens_lyr in sensitive_layers:
            needs_transform = trail_lyr.crs() != sens_lyr.crs()
            transform = QgsCoordinateTransform(
                sens_lyr.crs(), trail_lyr.crs(), QgsProject.instance()
            ) if needs_transform else None

            geom_cache = {}
            for sf in sens_lyr.getFeatures():
                geom = sf.geometry()
                if not geom:
                    continue
                if transform:
                    geom = QgsGeometry(geom)
                    geom.transform(transform)
                geom_cache[sf.id()] = geom

            idx = QgsSpatialIndex()
            for fid, geom in geom_cache.items():
                tmp = QgsFeature(fid)
                tmp.setGeometry(geom)
                idx.addFeature(tmp)

            layer_index_cache[sens_lyr.id()] = (idx, geom_cache, sens_lyr.name())

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

            # Buffer geometry for YELLOW proximity check
            buffered_geom = trail_geom.buffer(buffer_native, 8) if buffer_native > 0 else None

            triage = "GREEN"
            red_layers = []
            yellow_layers = []

            for sens_lyr in sensitive_layers:
                idx, geom_cache, lyr_name = layer_index_cache[sens_lyr.id()]

                # Check candidates from spatial index against buffered bbox
                check_bbox = (
                    buffered_geom.boundingBox() if buffered_geom
                    else trail_geom.boundingBox()
                )
                for fid in idx.intersects(check_bbox):
                    sg = geom_cache.get(fid)
                    if not sg:
                        continue

                    if trail_geom.intersects(sg):
                        if lyr_name not in red_layers:
                            red_layers.append(lyr_name)
                        triage = "RED"
                        break  # no need to check more features in this layer
                    elif buffered_geom and buffered_geom.intersects(sg):
                        if lyr_name not in yellow_layers:
                            yellow_layers.append(lyr_name)
                        if triage == "GREEN":
                            triage = "YELLOW"

            results.append({
                "trail": t_name,
                "triage": triage,
                "miles": trail_miles,
                "red_layers": red_layers,
                "yellow_layers": yellow_layers,
                "geom": trail_geom,
                "fid": trail_feat.id(),
            })

        # Sort: RED first, then YELLOW, then GREEN; alphabetical within each
        _order = {"RED": 0, "YELLOW": 1, "GREEN": 2}
        results.sort(key=lambda r: (_order[r["triage"]], r["trail"]))

        self._habitat_data = results
        self._add_habitat_layer_to_map(results, trail_lyr)
        self._display_habitat_results(results, sensitive_layers, buffer_ft)
        self.exportHabitatButton.setEnabled(bool(results))

    def _display_habitat_results(self, results, sensitive_layers, buffer_ft):
        from collections import defaultdict

        if not results:
            self.habitatResultsText.setPlainText("No results.")
            return

        counts = defaultdict(int)
        miles_by_triage = defaultdict(float)
        for r in results:
            counts[r["triage"]] += 1
            miles_by_triage[r["triage"]] += r["miles"]

        total_miles = sum(r["miles"] for r in results)

        lines = [
            f"Sensitive layers analysed : {', '.join(l.name() for l in sensitive_layers)}",
            f"Proximity buffer          : {buffer_ft} ft",
            "",
            "═" * 62,
            "TRIAGE SUMMARY",
            "═" * 62,
            f"  🔴 RED    (direct intersection) : "
            f"{counts['RED']:>3} trail(s)   {miles_by_triage['RED']:>6.2f} mi",
            f"  🟡 YELLOW (within {buffer_ft:>3} ft buffer) : "
            f"{counts['YELLOW']:>3} trail(s)   {miles_by_triage['YELLOW']:>6.2f} mi",
            f"  🟢 GREEN  (clear)               : "
            f"{counts['GREEN']:>3} trail(s)   {miles_by_triage['GREEN']:>6.2f} mi",
            f"  {'─'*48}",
            f"  Total                           : "
            f"{len(results):>3} trail(s)   {total_miles:>6.2f} mi",
            "",
            "  GREEN trails are candidates for Categorical Exclusion.",
            "  RED trails require EA-level analysis.",
            "",
        ]

        # Detail by triage level
        for label, color in [("RED", "🔴"), ("YELLOW", "🟡"), ("GREEN", "🟢")]:
            group = [r for r in results if r["triage"] == label]
            if not group:
                continue
            lines.append(f"{'─'*62}")
            lines.append(f"{color} {label} TRAILS")
            lines.append(f"{'─'*62}")
            for r in group:
                lines.append(f"\n  {r['trail']}  ({r['miles']:.2f} mi)")
                if r["red_layers"]:
                    lines.append(f"    Intersects : {', '.join(r['red_layers'])}")
                if r["yellow_layers"]:
                    lines.append(f"    Adjacent   : {', '.join(r['yellow_layers'])}")
                if not r["red_layers"] and not r["yellow_layers"]:
                    lines.append(f"    No sensitive layer conflicts")
            lines.append("")

        lines += [
            "─" * 62,
            "✓ 'Trail Triage - MTB NEPA' layer added to map canvas.",
            "  Use 'Export Triage Shapefile' to save for the NEPA scoping memo.",
        ]

        self.habitatResultsText.setPlainText("\n".join(lines))

    def _add_habitat_layer_to_map(self, results, trail_lyr):
        from qgis.core import (
            QgsCategorizedSymbolRenderer, QgsRendererCategory, QgsLineSymbol
        )

        for lyr in QgsProject.instance().mapLayersByName("Trail Triage - MTB NEPA"):
            QgsProject.instance().removeMapLayer(lyr.id())

        if not results:
            return

        crs_str = trail_lyr.crs().authid() if trail_lyr else "EPSG:4326"
        mem_layer = QgsVectorLayer(
            f"LineString?crs={crs_str}", "Trail Triage - MTB NEPA", "memory"
        )
        provider = mem_layer.dataProvider()
        provider.addAttributes([
            QgsField("Trail",    QVariant.String, len=60),
            QgsField("Triage",   QVariant.String, len=10),
            QgsField("Miles",    QVariant.Double),
            QgsField("RedLyrs",  QVariant.String, len=250),
            QgsField("YlwLyrs",  QVariant.String, len=250),
        ])
        mem_layer.updateFields()

        feats = []
        fields = mem_layer.fields()
        for r in results:
            feat = QgsFeature(fields)
            feat.setGeometry(r["geom"])
            feat.setAttribute("Trail",   r["trail"][:60])
            feat.setAttribute("Triage",  r["triage"])
            feat.setAttribute("Miles",   round(r["miles"], 4))
            feat.setAttribute("RedLyrs", ", ".join(r["red_layers"])[:250])
            feat.setAttribute("YlwLyrs", ", ".join(r["yellow_layers"])[:250])
            feats.append(feat)
        provider.addFeatures(feats)

        # Categorized line style by triage
        triage_styles = {
            "RED":    ("#e74c3c", "1.2"),
            "YELLOW": ("#f39c12", "1.0"),
            "GREEN":  ("#27ae60", "0.8"),
        }
        categories = []
        for triage, (color, width) in triage_styles.items():
            sym = QgsLineSymbol.createSimple({
                "color": color, "width": width,
            })
            categories.append(QgsRendererCategory(triage, sym, triage))

        mem_layer.setRenderer(QgsCategorizedSymbolRenderer("Triage", categories))
        QgsProject.instance().addMapLayer(mem_layer)
        self.iface.mapCanvas().refresh()

    def export_habitat_triage(self):
        if not self._habitat_data:
            QMessageBox.information(self, "Export", "Run the triage analysis first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Trail Triage Shapefile", "", "Shapefile (*.shp)"
        )
        if not path:
            return

        trail_lyr = trail_layer() or self.iface.activeLayer()
        crs = trail_lyr.crs() if trail_lyr else QgsCoordinateReferenceSystem("EPSG:4326")

        fields = QgsFields()
        fields.append(QgsField("Trail",   QVariant.String, len=60))
        fields.append(QgsField("Triage",  QVariant.String, len=10))
        fields.append(QgsField("Miles",   QVariant.Double))
        fields.append(QgsField("RedLyrs", QVariant.String, len=250))
        fields.append(QgsField("YlwLyrs", QVariant.String, len=250))

        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.LineString, crs, "ESRI Shapefile"
        )
        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(
                self, "Export Error", f"Could not create shapefile:\n{writer.errorMessage()}"
            )
            return

        for r in self._habitat_data:
            feat = QgsFeature()
            feat.setGeometry(r["geom"])
            feat.setFields(fields)
            feat.setAttribute("Trail",   r["trail"][:60])
            feat.setAttribute("Triage",  r["triage"])
            feat.setAttribute("Miles",   round(r["miles"], 4))
            feat.setAttribute("RedLyrs", ", ".join(r["red_layers"])[:250])
            feat.setAttribute("YlwLyrs", ", ".join(r["yellow_layers"])[:250])
            writer.addFeature(feat)

        del writer
        red = sum(1 for r in self._habitat_data if r["triage"] == "RED")
        yellow = sum(1 for r in self._habitat_data if r["triage"] == "YELLOW")
        green = sum(1 for r in self._habitat_data if r["triage"] == "GREEN")
        QMessageBox.information(
            self, "Export Complete",
            f"Exported {len(self._habitat_data)} trail(s) to:\n{path}\n\n"
            f"🔴 RED: {red}   🟡 YELLOW: {yellow}   🟢 GREEN: {green}\n\n"
            "Attributes: Trail, Triage, Miles, RedLyrs, YlwLyrs"
        )
