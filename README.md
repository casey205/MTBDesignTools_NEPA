# MTBDesignTools NEPA

A QGIS 3 plugin for MTB trail design with integrated NEPA environmental compliance analysis, built for projects on USFS lands (Willamette National Forest and beyond).

**Author:** Casey Varnum, Redside Surveying and Mapping, LLC  
**QGIS Minimum Version:** 3.40

---

## Overview

MTBDesignTools NEPA extends the trail design workflow to include the spatial analysis deliverables required for NEPA Environmental Assessments. The goal is to bake NEPA compliance checks into the design process itself — so that when trail plans are submitted, much of the environmental analysis is already done.

The plugin operates on a clear division of labor: the plugin produces structured text data and GIS layers; QGIS Layout Editor produces maps and figures; the analyst assembles the final report in a document editor. Each tool does what it does best.

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
- Filter by QGIS layer group to target specific hydro datasets
- Infers stream class from layer name (e.g. `Class_2`, `Streams_3`)
- Fish-bearing crossings (Class 1 & 2) get a per-crossing annotation table — select crossing type (proposed new, existing bridge, existing culvert, ford) and notes for each
- Class 3 crossings flagged for field verification; Class 4/5 flagged for desktop documentation only
- Exports crossing points as a GIS shapefile with full attribute table
- Analysis timestamp and layer list saved to QGIS project file

**Supports:** NEPA Task 2.4 Hydrology, Task 2.2 Fisheries (ESA Section 7 crossing documentation)

### Habitat Overlap
Intersects trail alignments against sensitive species and habitat polygon layers. Classifies each trail segment as RED (direct conflict), YELLOW (within proximity buffer), or GREEN (clear).

- Supports any polygon layer: NSO habitat, Critical Habitat, RA32 habitat, LRMP allocations, Riparian Reserves, wetlands, species occurrence areas
- Filter layers by QGIS layer group
- Proximity buffer is opt-in and off by default — use only for layers with spatial uncertainty (e.g. undelineated wetland areas). Do not use for authoritative FS corporate layers whose polygon edge IS the regulatory boundary
- Results show:
  - Segment-level triage summary (RED / YELLOW / GREEN trail counts and miles)
  - By-layer summary (trails and miles per sensitive layer, including zero-conflict layers)
  - Per-trail breakdown with conflict layer names and miles
- Exports two shapefiles:
  - **Triage shapefile** — trail segments colored RED/YELLOW/GREEN for NEPA scoping
  - **LAA Pre-Report shapefile** — trail segmented at every polygon boundary, each segment labeled by NSO Habitat / Critical Habitat / RA32 Habitat / LRMP Allocation category. Required for ESA Section 7 LAA pre-reporting (RFP Task 2.1)
- Analysis timestamp and layer list saved to QGIS project file

**Supports:** NEPA Task 2.1 Wildlife LAA Pre-reporting, Task 2.2 Fisheries habitat screening

### NEPA Report
Assembles all analysis results into a structured Environmental Screening Memo and exports as plain text.

The memo has seven sections, automatically populated from the analysis tabs:

| Section | Source |
|---|---|
| Key Findings | Auto-generated from crossing and triage data |
| 1. Proposed Action | User-entered |
| 2. Project Design Features | User-entered bullet list |
| 3. Desktop Screening Methodology / Data Sources | User-entered (layer names, dates, methodology notes) |
| 4. Stream Crossing Analysis | Live data from Stream Crossings tab |
| 5. Sensitive Area Overlap — Trail Triage | Live data from Habitat Overlap tab |
| 6. Data Gaps / Outstanding Questions | User-entered |
| 7. NEPA Recommendation | Auto-generated (CE / Tiered / EA pathway) |

Additional features:
- **Analysis Status panel** — shows timestamp and layer list for the last run of each analysis tab; green when data is loaded, gray when not yet run
- **Full project persistence** — all report fields and analysis data are saved to the QGIS project file (`.qgz`) and restored automatically on project open
- **Export as .txt** — memo exported as plain text for paste-in to Word or InDesign

**Supports:** NEPA EA documentation, Forest Service scoping, grant applications, Checkpoint Meeting deliverables

---

## Workflow

```
1. Load trail layer (Trail_Design or Trail_Alignment)
2. Profile & Difficulty tab → generate elevation profile per trail
3. Stream Crossings tab → run crossing analysis → annotate fish-bearing crossings → export shapefile
4. Habitat Overlap tab → run triage against FS corporate layers → export LAA shapefile
5. NEPA Report tab → fill in project info, methodology, data gaps → Generate Screening Memo → export .txt
6. QGIS Layout Editor → build maps using the exported GIS layers
7. Assemble final report in Word / InDesign using memo text + Layout maps
```

---

## Required Layer Names

| Layer | Expected Name |
|---|---|
| Trail alignment | `Trail_Design` (or `Trail_Alignment` for legacy projects) |
| Elevation model | Any raster layer (select from DEM dropdown in Profile tab) |
| Stream network | Any line vector layer (select from Streams list) |
| Sensitive area layers | Any polygon layer (select from Habitat list) |

Stream class is inferred from the layer name — name layers consistently (e.g. `Streams_Class1`, `NHD_Class_3`) for best results.

---

## NEPA RFP Alignment (Deathball / Willamette NF)

| RFP Task | Plugin Feature |
|---|---|
| Task 2.1 — LAA Pre-reporting shapefiles | Habitat Overlap → Export LAA Shapefile |
| Task 2.2 — Fisheries crossing documentation | Stream Crossings → crossing class table + shapefile |
| Task 2.4 — Hydrology stream crossing surveys | Stream Crossings → crossing count and class breakdown |
| Task 5.2 — GIS Final Project Record | Shapefile exports from all analysis tabs |
| Task 6.1 — Trail-by-trail prescription (Checkpoint #2) | Profile + Crossings + Triage per trail |

---

## Installation

1. Copy (or junction) this folder into your QGIS plugins directory:  
   `C:\Users\<you>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\MTBDesignTools_NEPA\`
2. In QGIS: **Plugins → Manage and Install Plugins → Installed → enable MTBDesignTools NEPA**
3. The toolbar icon appears; click to open the dock widget

A junction (directory symlink) is recommended for development so edits in the repo folder are live in QGIS without copying files.

---

## Development

Built on the same architecture as [MTBDesignTools](https://github.com/caseyvarnum/MTBDesignTools) (the standalone profile/IMBA tool). These two plugins are intentionally kept separate so the profile tool remains stable while NEPA features are developed.

**Tech stack:** Python 3, PyQt5/6, Matplotlib, NumPy, QGIS 3 API (PyQGIS)

**Project:** Deathball Mountain Bike Trail System — McKenzie River Ranger District, Willamette National Forest  
**Prepared by:** Redside Surveying and Mapping, LLC
