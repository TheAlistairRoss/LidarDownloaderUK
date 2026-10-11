# Contributing to LIDAR Downloader UK

Thank you for helping. Bug reports, ideas and code are all welcome. This guide explains how to take part and what happens to your contribution.

LIDAR Downloader UK is maintained by Simon Stoate, who decides what goes into the plugin and when it's released. Suggestions that don't fit the plugin's direction may be declined, but they'll always get a reply.

## Reporting a bug

Open an [issue](https://github.com/simonstoate/LidarDownloaderUK/issues) and include:

- your QGIS version (**Help → About**) and operating system
- the plugin version (**Plugins → Manage and Install Plugins**)
- what you did, what you expected, and what happened instead
- the tile names and dataset involved (e.g. *SU12NE, England Composite DTM, 2022, 1 m*), if it's about a download
- any messages from **View → Panels → Log Messages → LIDAR Downloader UK**

If a download service seems to have changed (datasets missing, downloads failing everywhere), please say which nation it affects: England (Environment Agency), Wales (DataMapWales) or Scotland (Scottish Remote Sensing Portal).

## Suggesting a feature

Open an issue describing the problem you're trying to solve, not only the solution: *"I need to know which surveys cover my site before I download"* helps more than *"add a survey list"*. The plugin is meant to be useful to everyone who works with this data, so features are designed to be general, with options for specialist needs. It sticks to finding, downloading, organising and loading the data: analysis (styling, terrain and contour exports, catchments) belongs in a separate companion plugin.

## Contributing code

For anything more than a small fix, **open an issue first** so we can agree the approach before you spend time on it.

1. Fork the repository and create a branch from `main`.
2. Make your change, with tests (see below).
3. Check that lint, the unit tests and the QGIS smoke test pass.
4. Open a pull request explaining what it changes and why, and link the issue.

Pull requests are reviewed as time allows. You may be asked for changes. A pull request may also be declined, or its idea implemented differently.

### Setting up

See [Development](README.md#development) in the README for the code layout. In short:

```
pip install pytest flake8 bandit detect-secrets
python -m pytest
flake8 lidar_downloader_uk tests tools
bandit -r lidar_downloader_uk
detect-secrets scan lidar_downloader_uk
```

plugins.qgis.org scans every upload with Bandit and detect-secrets and won't publish a version with findings, so both must stay clean.

The smoke test runs the whole plugin headless inside QGIS, with a faked network:

```
"C:\Program Files\QGIS 4.2.1\bin\python-qgis.bat" "<repo>\tests\smoke_test_qgis.py"
```

GitHub Actions runs all of these on QGIS 3.22, 3.34, 3.40, 3.44 (Qt5) and 4.2 (Qt6) for every pull request.

The point-cloud smoke checks use tiny synthetic files, without downloading data. With PDAL available they
reproduce a valid-looking but unrenderable VPC over ordinary LAS files, then verify COPC conversion, real
point rendering and identification, preservation of the originals, cache reuse/invalidation, Windows path
repair, existing-layer refresh, worker progress/cancellation and diagnostic failures. These checks do not
just assert that a VPC exists or `isValid()` returns true. Point clouds are skipped when PDAL is unavailable.
On QGIS 3.42+, they also check actual point colours at three zoomed-out scales, persisted overview
references, overview cache reuse/missing-file regeneration, legacy VPC upgrades and failure/cancellation
cleanup. Older QGIS versions keep their existing rendering path.

For a Windows manual regression, use QGIS 3.44.13, England point cloud (individual surveys), 2024, tile
SJ90SE. Load the existing downloads with the updated plugin, wait for COPC preparation, and zoom inside
one source file's extent. Check that classified points appear and can be identified, the layer uses
EPSG:27700, and there are no source-open warnings in Log Messages. Remove and load the layer again:
the indexed copies should be reused. Save/reopen the project to check persisted VPC references. Keep
the VPC's `mosaics/pointcloud_indexes/` folder; the original LAZ files are not modified by preparation.
Zoom out to the site and beyond: on QGIS 3.42+ dots should remain visible, not only red file outlines.
Repeat with a polygon crop and keep the companion `*-overview.copc.laz` file alongside the VPC.
Record the plugin installation path as well as its version to distinguish a local fix from a release
with the same version number.

#### Recorded point-cloud validation

| QGIS | Environment | Evidence | Scope |
|------|-------------|----------|-------|
| 3.44.13 | Windows, Qt5 | Local automated tests at commit `5d8fdcc` | Full QGIS smoke suite, including cropping, overview rendering, cache reuse and failure/cancellation checks; real cropped-data rendering and saved-project verification |
| 4.2.3 | Windows, Qt6 | Manual confirmation by TheAlistairRoss on 2026-10-11, local fix build at commit `5d8fdcc` | Point-cloud cropping and rendering reported working |

The QGIS 4.2.3 confirmation does not imply that the full automated suite, cache/cancellation cases
or every plugin feature was tested on that version. These fixes remain unreleased until merged and
included in an upstream release; the local build still identifies itself as plugin version 0.14.6.

Exact polygon point-cloud checks also run in the full QGIS smoke suite. For a focused check on Windows,
run QGIS's `python-qgis-ltr.bat` with the absolute path to `tests\crop_test_qgis.py`. It uses a temporary
directory and removes its synthetic sources/results afterwards. The checks cover
header rejection without full-source indexing, exact holes/multipart/overlapping polygons, point
coordinates and LAS attributes, CRS preservation/BNG fallback, LAS/LAZ/existing COPC inputs, crop cache
identity/reuse, empty results, cancellation/error cleanup, Processing output and untouched originals.
The full smoke suite adds the modal worker and dock loading checks plus existing raster regressions.

### Code guidelines

- **One codebase for QGIS 3 and QGIS 4.** Follow the compatibility rules in the README: import Qt only through `qgis.PyQt`, use fully scoped enums, `exec()` not `exec_()`, and guard APIs newer than QGIS 3.22.
- **Background work.** Code in a task's `run()` runs on a worker thread, so it must not touch widgets or the project. Don't connect lambdas to signals from worker threads (this crashed QGIS 3); connect to real methods.
- **Network.** Use QGIS's network classes (`QgsBlockingNetworkRequest` / `QgsNetworkAccessManager`) so users' proxy and authentication settings apply. Only download from the official services listed in `api.py`. Don't scrape unofficial copies of the data.
- **Files.** Never delete or overwrite files the plugin didn't create. Anything that deletes asks the user first.
- **Keep pure logic testable.** `api.py`, `sources.py`, `storage.py`, `places.py` and `migrate.py` don't import QGIS, so they can be unit-tested with plain pytest. Keep them that way.
- **Wording.** User-facing text is in British English, writes LIDAR in capitals (as the Environment Agency and gov.uk do), and explains problems in plain language.
- **Data and licences.** Don't add bundled data unless its licence allows redistribution. Credit it in `lidar_downloader_uk/data/LICENSE-DATA.txt`.
- **New files** start with the two SPDX licence lines the others have, then a docstring saying what the module is for.
- Match the style of the surrounding code, including its comments: docstrings say what a function gives back or does, comments say *why* where the code can't. Lines are at most 120 characters.

## Licensing of contributions

The plugin is licensed under the [GNU General Public License v2.0 or later](LICENSE).

By submitting a contribution (code, documentation or other material) you confirm that:

1. **You have the right to submit it.** You wrote it yourself, or you have permission to contribute it under these terms, and it doesn't include anyone else's code under an incompatible licence.
2. **You license it under GPL-2.0-or-later**, like the rest of the project.

You keep the copyright in your contribution. Please add a `Signed-off-by: Your Name <email>` line to your commits (`git commit -s`) to confirm the above.

## Behaviour

Be respectful and constructive in issues and pull requests. Assume good intent, keep discussion about the work, and remember that everyone here is giving their own time. Harassment or abuse isn't tolerated, and comments or contributors may be removed.

## Questions

For anything else, open an issue.
