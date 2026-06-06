import os
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .MTBDesignToolsNEPA_dockwidget import MTBDesignToolsNEPADockWidget


class MTBDesignToolsNEPA:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dock = None

    def initGui(self):
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        self.action = QAction(icon, 'MTB Design Tools - NEPA', self.iface.mainWindow())
        self.action.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu('&MTB Design Tools NEPA', self.action)

    def unload(self):
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu('&MTB Design Tools NEPA', self.action)
        if self.dock:
            self.iface.removeDockWidget(self.dock)

    def toggle_dock(self):
        if self.dock is None:
            self.dock = MTBDesignToolsNEPADockWidget(self.iface.mainWindow())
            self.iface.addDockWidget(Qt.BottomDockWidgetArea, self.dock)
            self.dock.show()
        else:
            self.dock.setVisible(not self.dock.isVisible())
