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

        self._setup_profile_tab()
        self._setup_crossings_tab()

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
        self.refreshStreamsButton.clicked.connect(self.populate_streams_dropdown)
        self.runCrossingsButton.clicked.connect(self.run_crossing_analysis)
        self.exportCrossingsButton.clicked.connect(self.export_crossings)
        self.populate_streams_dropdown()

        # Monospace font for the results table
        from qgis.PyQt.QtGui import QFont
        font = QFont("Courier New", 9)
        self.crossingsResultsText.setFont(font)

    def _on_layers_changed(self, *_args):
        self.populate_trail_dropdown()
        self.populate_dem_dropdown()
        self.populate_streams_dropdown()

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

    def populate_streams_dropdown(self, *_args):
        self.streamsDropdown.clear()
        self.streamsDropdown.addItem("— select streams layer —", userData=None)
        for lyr in QgsProject.instance().mapLayers().values():
            if isinstance(lyr, QgsVectorLayer) and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                self.streamsDropdown.addItem(lyr.name(), lyr.id())

    def run_crossing_analysis(self):
        trail_lyr = trail_layer() or self.iface.activeLayer()
        streams_id = self.streamsDropdown.currentData()
        streams_lyr = QgsProject.instance().mapLayer(streams_id) if streams_id else None

        if not trail_lyr:
            QMessageBox.warning(
                self, "Stream Crossings",
                "No trail layer found.\nLoad a Trail_Design (or Trail_Alignment) layer."
            )
            return
        if not streams_lyr:
            QMessageBox.warning(
                self, "Stream Crossings",
                "Select a streams layer from the dropdown.\n"
                "Load NHD, LiDAR-derived streams, or FS corporate stream data first."
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

        stream_fields = [f.name() for f in streams_lyr.fields()]
        stream_class_field = next(
            (f for f in ["StreamClass", "stream_class", "CLASS", "STRMCLASS",
                         "strm_class", "FType", "ftype", "FTYPE"] if f in stream_fields),
            None
        )
        stream_name_field = next(
            (f for f in ["GNIS_Name", "GNIS_name", "Name", "name",
                         "StreamName", "stream_name", "STREAM_NAM"] if f in stream_fields),
            None
        )

        # Build coordinate transform if CRS differs (streams → trail CRS)
        needs_transform = trail_lyr.crs() != streams_lyr.crs()
        transform = QgsCoordinateTransform(
            streams_lyr.crs(), trail_lyr.crs(), QgsProject.instance()
        ) if needs_transform else None

        crs_note = ""
        if needs_transform:
            crs_note = (
                f"\nNote: stream layer CRS ({streams_lyr.crs().authid()}) differs from "
                f"trail layer CRS ({trail_lyr.crs().authid()}) — "
                "geometries reprojected automatically."
            )

        # Build spatial index over reprojected stream geometries for performance
        self.crossingsResultsText.setPlainText("Running analysis…")
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.processEvents()

        stream_index = QgsSpatialIndex()
        stream_geom_cache = {}
        stream_feature_cache = {}
        for sf in streams_lyr.getFeatures():
            geom = sf.geometry()
            if not geom:
                continue
            if transform:
                geom = QgsGeometry(geom)
                geom.transform(transform)
            stream_index.addFeature(sf) if not transform else None
            stream_geom_cache[sf.id()] = geom
            stream_feature_cache[sf.id()] = sf

        # When reprojecting, the spatial index must be built from transformed geometries
        if transform:
            from qgis.core import QgsFeature as _QgsFeature
            stream_index = QgsSpatialIndex()
            for fid, geom in stream_geom_cache.items():
                tmp = _QgsFeature(fid)
                tmp.setGeometry(geom)
                stream_index.addFeature(tmp)

        crossings = []
        for trail_feat in trail_features:
            trail_geom = trail_feat.geometry()
            if not trail_geom:
                continue

            t_name = (
                str(trail_feat.attribute(trail_name_field))
                if trail_name_field else f"Trail {trail_feat.id()}"
            )

            candidate_ids = stream_index.intersects(trail_geom.boundingBox())
            for sid in candidate_ids:
                sf = stream_feature_cache.get(sid)
                if sf is None:
                    continue
                stream_geom = stream_geom_cache.get(sid)
                if not stream_geom or not trail_geom.intersects(stream_geom):
                    continue

                intersection = trail_geom.intersection(stream_geom)
                pts = _extract_points_from_geom(intersection)

                s_class = str(sf.attribute(stream_class_field)) if stream_class_field else "Unknown"
                s_name = str(sf.attribute(stream_name_field)) if stream_name_field else f"Stream {sf.id()}"
                if s_name in ("NULL", "None", ""):
                    s_name = f"Stream {sf.id()}"

                for pt in pts:
                    crossings.append({
                        "trail": t_name,
                        "stream_id": sf.id(),
                        "stream_name": s_name,
                        "stream_class": s_class,
                        "x": round(pt.x(), 2),
                        "y": round(pt.y(), 2),
                        "point": QgsPointXY(pt.x(), pt.y()),
                    })

        self._crossings_data = crossings
        self._display_crossings_results(crossings, trail_lyr, streams_lyr, crs_note)
        self.exportCrossingsButton.setEnabled(bool(crossings))

    def _display_crossings_results(self, crossings, trail_lyr=None, streams_lyr=None, crs_note=""):
        if not crossings:
            self.crossingsResultsText.setPlainText(
                "No stream crossings found.\n\n"
                "Check that the trail and stream layers overlap spatially\n"
                "and that the stream layer is loaded in the project."
                + (f"\n{crs_note}" if crs_note else "")
            )
            return

        hdr_trail = "Trail"
        hdr_stream = "Stream"
        hdr_class = "Class"
        hdr_x = "Easting"
        hdr_y = "Northing"

        col_trail = max(20, max(len(c["trail"]) for c in crossings) + 2)
        col_stream = max(20, max(len(c["stream_name"]) for c in crossings) + 2)
        col_class = max(10, max(len(c["stream_class"]) for c in crossings) + 2)

        layer_info = []
        if trail_lyr:
            layer_info.append(f"Trail layer : {trail_lyr.name()}")
        if streams_lyr:
            layer_info.append(f"Streams layer: {streams_lyr.name()}")
        if crs_note:
            layer_info.append(crs_note)

        lines = layer_info + ["", f"Total crossings found: {len(crossings)}", ""]
        header = (
            f"{'#':<4} "
            f"{hdr_trail:<{col_trail}} "
            f"{hdr_stream:<{col_stream}} "
            f"{hdr_class:<{col_class}} "
            f"{hdr_x:<16} {hdr_y}"
        )
        lines.append(header)
        lines.append("-" * len(header))

        for i, c in enumerate(crossings, 1):
            lines.append(
                f"{i:<4} "
                f"{c['trail'][:col_trail-1]:<{col_trail}} "
                f"{c['stream_name'][:col_stream-1]:<{col_stream}} "
                f"{c['stream_class'][:col_class-1]:<{col_class}} "
                f"{c['x']:<16} {c['y']}"
            )

        lines += [
            "",
            "─" * 40,
            "Next steps:",
            "  • Click 'Export to Shapefile' to save crossing points for the NEPA record",
            "  • Attribute the stream class (Class 2/3/4) for each crossing",
            "  • Use crossing locations to plan bridge/culvert designs",
        ]

        self.crossingsResultsText.setPlainText("\n".join(lines))

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
        fields.append(QgsField("Trail", QVariant.String, len=60))
        fields.append(QgsField("StreamName", QVariant.String, len=60))
        fields.append(QgsField("StrClass", QVariant.String, len=20))
        fields.append(QgsField("Easting", QVariant.Double))
        fields.append(QgsField("Northing", QVariant.Double))

        writer = QgsVectorFileWriter(
            path, "UTF-8", fields, QgsWkbTypes.Point, crs, "ESRI Shapefile"
        )

        if writer.hasError() != QgsVectorFileWriter.NoError:
            QMessageBox.critical(
                self, "Export Error",
                f"Could not create shapefile:\n{writer.errorMessage()}"
            )
            return

        for c in self._crossings_data:
            feat = QgsFeature()
            feat.setGeometry(QgsGeometry.fromPointXY(c["point"]))
            feat.setFields(fields)
            feat.setAttribute("Trail", c["trail"][:60])
            feat.setAttribute("StreamName", c["stream_name"][:60])
            feat.setAttribute("StrClass", c["stream_class"][:20])
            feat.setAttribute("Easting", c["x"])
            feat.setAttribute("Northing", c["y"])
            writer.addFeature(feat)

        del writer

        QMessageBox.information(
            self, "Export Complete",
            f"Exported {len(self._crossings_data)} crossing point(s) to:\n{path}\n\n"
            "Load the shapefile into QGIS to review locations on the map."
        )
