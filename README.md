# MTBDesignTools NEPA

A QGIS 3 plugin for MTB trail design with integrated NEPA environmental compliance analysis, built for projects on USFS lands (Willamette National Forest and beyond).

**Author:** Casey Varnum, Redside Surveying and Mapping, LLC  
**QGIS Minimum Version:** 3.40

---

## Overview

MTBDesignTools NEPA extends the trail design workflow to include the spatial analysis deliverables required for NEPA Environmental Assessments. The goal is to bake NEPA compliance checks into the design process itself — so that when trail plans are submitted, much of the environmental analysis is already done.

---

## Tabs

### Profile & Difficulty
Elevation profile generation with IMBA difficulty classification along trail alignments.

- Samples a DEM at 2m intervals along the selected trail geometry
- Colors the profile by IMBA grade class (Easy / Moderate / Difficult / Extreme)
- Interactive hover and click-to-pin marker synced to the QGIS map canvas
- Summary bar: trail name, type, length, avg climb/descent grade, IMBA difficulty

### Stream Crossings
Identifies all trail–stream intersections for the NEPA hydrology and fisheries analysis.

- Intersects `Trail_Design` (or `Trail_Alignment`) against any loaded stream network layer
- Uses a spatial index for performance on large stream datasets
- Results table shows trail name, stream name, stream class, and coordinates
- Export crossing points as a GIS shapefile for the NEPA project record

**Supports:** NEPA Task 2.4 Hydrology (143+ crossings to document on Deathball project)

### Habitat Overlap *(coming soon)*
Intersects the trail corridor buffer against sensitive species and habitat layers:
- NSO habitat, Critical Habitat, RA32 habitat, LRMP allocations
- Oregon spotted frog, pond turtle, bald eagle, peregrine falcon layers
- Riparian Reserve boundaries

**Supports:** NEPA Task 2.1 Wildlife LAA Pre-reporting shapefiles, Task 2.2 Fisheries

### NEPA Report *(coming soon)*
Packages trail design data into a standardized NEPA submission:
- Trail alignments segmented by habitat type and land management allocation
- Crossing point shapefile with stream class attributes
- Sensitive area overlap summary table
- IMBA difficulty summary
- Miles by allocation category

---

## Required Layer Names

| Layer | Expected Name |
|---|---|
| Trail alignment | `Trail_Design` (or `Trail_Alignment` for legacy projects) |
| Elevation model | Any raster layer (select from DEM dropdown) |
| Stream network | Any line vector layer (select from Streams dropdown) |

---

## Installation

1. Copy (or junction) this folder into your QGIS plugins directory:  
   `C:\Users\<you>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\MTBDesignTools_NEPA\`
2. In QGIS: Plugins → Manage and Install Plugins → Installed → enable **MTBDesignTools NEPA**
3. The toolbar icon appears; click to open the dock widget

---

## Development

Built on the same architecture as [MTBDesignTools](https://github.com/caseyvarnum/MTBDesignTools) (the standalone profile/IMBA tool). These two plugins are intentionally kept separate so the profile tool remains stable while NEPA features are developed.

**Tech stack:** Python 3, PyQt5/6, Matplotlib, NumPy, QGIS 3 API (PyQGIS)
