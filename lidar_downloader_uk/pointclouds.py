# SPDX-FileCopyrightText: 2025-2026 Simon Stoate
# SPDX-License-Identifier: GPL-2.0-or-later
"""Point clouds load as downloaded, or as exact polygon crops with cached COPC indexes.
Ordinary LAS/LAZ files need cached COPC copies for QGIS to render their points through a VPC.
The downloaded originals are never changed.

Uses QGIS's PDAL tools (QGIS 3.32+). Environment Agency point clouds often have no coordinate
system recorded, so the VPC is given British National Grid.
"""

import json
import os
import shutil
from collections import deque

from qgis.core import (QgsApplication, QgsBlockingProcess, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsGeometry, QgsJsonUtils, QgsPointXY, QgsPointCloudClassifiedRenderer, QgsPointCloudLayer,
                       QgsProcessingContext,
                       QgsProcessingException, QgsProcessingFeedback, QgsProcessingMultiStepFeedback,
                       QgsProject, QgsRectangle)

from .postprocess import file_key

BNG = 'EPSG:27700'


def available():
    """True if QGIS's PDAL tools are installed (QGIS 3.32 or later with PDAL): the algorithms and the
    pdal_wrench program they run (some Linux packages register the algorithms without it)."""
    if any(QgsApplication.processingRegistry().algorithmById(name) is None
           for name in ('pdal:virtualpointcloud', 'pdal:convertformat', 'pdal:assignprojection')):
        return False
    return any(os.path.exists(os.path.join(folder, name))
               for folder in (QgsApplication.libexecPath(), QgsApplication.applicationDirPath())
               for name in ('pdal_wrench', 'pdal_wrench.exe'))


def _bng():
    return QgsCoordinateReferenceSystem(BNG)


class Cancelled(RuntimeError):
    """Point-cloud preparation was cancelled, not a corrupt download."""


class EmptyCrop(ValueError):
    """The polygon misses the source bounds, or contains no source points."""


class _PdalFeedback(QgsProcessingMultiStepFeedback):
    def __init__(self, feedback):
        super().__init__(1, feedback)
        self.messages = deque(maxlen=20)

    def reportError(self, error, fatalError=False):
        if error:
            self.messages.append(error)
        super().reportError(error, fatalError)

    def pushConsoleInfo(self, info):
        if info:
            self.messages.append(info)
        super().pushConsoleInfo(info)


def _check_cancelled(feedback):
    if feedback.isCanceled():
        raise Cancelled('Point-cloud preparation cancelled. Completed indexes can be reused on the next load.')


def _run(algorithm, parameters, feedback=None, transform_context=None):
    """Run a PDAL tool. Returns its output path; raises RuntimeError if it didn't write one (some QGIS
    builds report success when pdal_wrench refused the command)."""
    import processing  # here, not at the top: the Processing plugin may load after this one
    feedback = feedback if feedback is not None else QgsProcessingFeedback()
    _check_cancelled(feedback)
    context = QgsProcessingContext()
    if transform_context is not None:
        context.setTransformContext(transform_context)
    pdal_feedback = _PdalFeedback(feedback)
    try:
        result = processing.run(algorithm, parameters, context=context, feedback=pdal_feedback)
    except QgsProcessingException as e:
        _check_cancelled(feedback)
        raise RuntimeError(f'{algorithm}: {e}\n' + '\n'.join(pdal_feedback.messages)) from e
    _check_cancelled(feedback)
    output = result.get('OUTPUT')
    if not output or not os.path.exists(output):
        raise RuntimeError(f'{algorithm} did not produce {parameters.get("OUTPUT")}\n'
                           + '\n'.join(pdal_feedback.messages))
    return output


def fix_vpc_crs(path, transform_context=None):
    """Normalise Windows asset paths and give items without a CRS British National Grid and WGS84 bounds.
    Returns True if anything changed. A transform context can be supplied when running in a worker."""
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    bng = _bng()
    # The WKT variant enum became scoped (QgsCoordinateReferenceSystem.WktVariant) in newer bindings
    variants = getattr(QgsCoordinateReferenceSystem, 'WktVariant', QgsCoordinateReferenceSystem)
    variant = variants.WKT2_2019_SIMPLIFIED
    wkt = bng.toWkt(variant)
    context = transform_context if transform_context is not None else QgsProject.instance().transformContext()
    to_wgs84 = QgsCoordinateTransform(bng, QgsCoordinateReferenceSystem('EPSG:4326'), context)
    changed = False
    for item in data.get('features', []):
        properties = item.get('properties', {})
        filename = properties.get('filename')
        if isinstance(filename, str) and '\\' in filename:
            properties['filename'] = filename.replace('\\', '/')
            changed = True
        for asset in item.get('assets', {}).values():
            href = asset.get('href')
            if isinstance(href, str) and '\\' in href:
                asset['href'] = href.replace('\\', '/')
                changed = True
        if properties.get('proj:wkt2') or len(properties.get('proj:bbox') or []) != 6:
            continue
        x0, y0, z0, x1, y1, z1 = properties['proj:bbox']
        box = to_wgs84.transformBoundingBox(QgsRectangle(x0, y0, x1, y1))
        properties['proj:wkt2'] = wkt
        item['bbox'] = [box.xMinimum(), box.yMinimum(), z0, box.xMaximum(), box.yMaximum(), z1]
        ring = [[box.xMinimum(), box.yMinimum(), z0], [box.xMinimum(), box.yMaximum(), z0],
                [box.xMaximum(), box.yMaximum(), z1], [box.xMaximum(), box.yMinimum(), z1],
                [box.xMinimum(), box.yMinimum(), z0]]
        item['geometry'] = {'type': 'Polygon', 'coordinates': [ring]}
        changed = True
    if changed:
        part = path + '.part'
        with open(part, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
        os.replace(part, path)
    return changed


def provider_for_file(path):
    """COPC has its own QGIS provider; ordinary LAS/LAZ files use PDAL's indexing workflow."""
    return 'copc' if path.lower().endswith('.copc.laz') else 'pdal'


def _layer(path):
    options = QgsPointCloudLayer.LayerOptions()
    options.skipIndexGeneration = True
    options.skipStatisticsCalculation = True
    layer = QgsPointCloudLayer(path, os.path.basename(path), provider_for_file(path), options)
    if not layer.isValid():
        raise RuntimeError(f'Cannot open point cloud {path}: {layer.error().summary()}')
    if path.lower().endswith('.copc.laz'):
        layer.dataProvider().loadIndex()
    return layer


def copc_path(source, folder):
    """Cache path tied to the original's path, size and modification time. Existing COPC files need no copy."""
    if source.lower().endswith('.copc.laz'):
        return source
    stat = os.stat(source)
    key = file_key([source], f'|{stat.st_size}|{stat.st_mtime_ns}')
    stem = os.path.splitext(os.path.basename(source))[0]
    return os.path.join(folder, f'{stem}_{key}.copc.laz')


def crop_path(files, folder, cutline):
    """A separate crop identity incorporating polygon geometry and every source's path, size and mtime."""
    sources = sorted({os.path.abspath(f) for f in files})
    stamps = [(os.stat(f).st_size, os.stat(f).st_mtime_ns) for f in sources]
    key = file_key(sources, json.dumps(['exact-crop-2', cutline, stamps], sort_keys=True))
    return os.path.join(folder, f'cropped_cloud_{key}.vpc')


def _crop_geometry(cutline, crs, transform_context):
    data = json.loads(cutline)
    name = data.get('crs', {}).get('properties', {}).get('name', BNG)
    source_crs = QgsCoordinateReferenceSystem.fromOgcWmsCrs(name)
    if not source_crs.isValid():
        raise ValueError('The crop polygon has no readable coordinate system.')
    geometries = [feature.geometry() for feature in QgsJsonUtils.stringToFeatureList(cutline)
                  if feature.hasGeometry()]
    geometry = QgsGeometry.unaryUnion(geometries)
    if geometry.isEmpty() or not geometry.isGeosValid():
        raise ValueError('The crop has no valid polygons.')
    transform = QgsCoordinateTransform(source_crs, crs, transform_context)
    geometry.transform(transform)
    if geometry.isEmpty() or not geometry.isGeosValid():
        raise ValueError('The transformed crop has no valid polygons.')
    return geometry


def _bounds_geometry(bounds):
    # LAS headers can describe a single point or a line, not just a rectangle.
    if bounds.width() == 0 and bounds.height() == 0:
        return QgsGeometry.fromPointXY(QgsPointXY(bounds.xMinimum(), bounds.yMinimum()))
    if bounds.width() == 0 or bounds.height() == 0:
        return QgsGeometry.fromPolylineXY([QgsPointXY(bounds.xMinimum(), bounds.yMinimum()),
                                          QgsPointXY(bounds.xMaximum(), bounds.yMaximum())])
    return QgsGeometry.fromRect(bounds)


def _run_pipeline(stages, output, feedback, stream=False):
    """Native PDAL pipeline: QGIS 3.44's clip/convert wrappers drop LAS Extra Bytes dimensions.

    Explicit forward/extra_dims options preserve them. QgsBlockingProcess is cancellation-aware
    and safe in the existing worker; streaming the polygon crop avoids holding whole ordinary sources.
    """
    executable = shutil.which('pdal')
    if executable is None:
        raise RuntimeError('Exact attribute-preserving point-cloud cropping needs the PDAL command-line '
                           'program (pdal) supplied with QGIS. It was not found on PATH.')
    _check_cancelled(feedback)
    pipeline = output + '.pipeline.json'
    messages = deque(maxlen=20)

    def log(data):
        text = bytes(data).decode('utf-8', errors='replace').strip()
        if text:
            messages.append(text)
            feedback.pushConsoleInfo(text)

    try:
        with open(pipeline, 'w', encoding='utf-8') as f:
            json.dump({'pipeline': stages}, f)
        arguments = ['pipeline', pipeline]
        if stream:
            arguments.append('--stream')
        process = QgsBlockingProcess(executable, arguments)
        process.setStdOutHandler(log)
        process.setStdErrHandler(log)
        code = process.run(feedback)
        _check_cancelled(feedback)
        if code != 0 or not os.path.exists(output):
            raise RuntimeError(f'PDAL pipeline failed ({code}): {output}\n' + '\n'.join(messages))
        return output
    finally:
        if os.path.exists(pipeline):
            os.remove(pipeline)


def _header(source, feedback):
    """Bounds, CRS and count without indexing or reading points.

    Some QGIS 3.44 LAS providers reject valid typed Extra Bytes VLRs. Native PDAL's metadata-only
    inspection reads those headers correctly; COPC still opens in QGIS's independent COPC provider.
    Use native metadata for all sources for consistent header handling.
    """
    executable = shutil.which('pdal')
    if executable is None:
        raise RuntimeError('Point-cloud header inspection needs the PDAL command-line program on PATH.')
    _check_cancelled(feedback)
    output, errors = [], []

    def stdout(data):
        output.append(bytes(data).decode('utf-8', errors='replace'))

    def stderr(data):
        errors.append(bytes(data).decode('utf-8', errors='replace'))

    process = QgsBlockingProcess(executable, ['info', '--metadata', source])
    process.setStdOutHandler(stdout)
    process.setStdErrHandler(stderr)
    code = process.run(feedback)
    _check_cancelled(feedback)
    if code != 0:
        raise RuntimeError(f'Cannot inspect point-cloud header: {source}\n' + ''.join(errors))
    try:
        metadata = json.loads(''.join(output))['metadata']
        bounds = QgsRectangle(metadata['minx'], metadata['miny'], metadata['maxx'], metadata['maxy'])
        wkt = metadata.get('srs', {}).get('compoundwkt') or metadata.get('comp_spatialreference', '')
        return bounds, QgsCoordinateReferenceSystem.fromWkt(wkt), int(metadata['count'])
    except (ValueError, KeyError, TypeError) as e:
        raise RuntimeError(f'Cannot read PDAL header metadata: {source}') from e


def _index_crop(source, output, crs, count, feedback):
    part = output[:-len('.copc.laz')] + '.part.copc.laz'
    try:
        if os.path.exists(part):
            os.remove(part)
        feedback.setProgressText(f'Indexing {count:,} cropped points (original unchanged)')
        _run_pipeline([{'type': 'readers.las', 'filename': source, 'use_eb_vlr': True},
                       {'type': 'writers.copc', 'filename': part, 'forward': 'all',
                        'extra_dims': 'all', 'a_srs': crs.toWkt()}], part, feedback)
        layer = _layer(part)
        valid = layer.pointCount() == count and layer.dataProvider().hasValidIndex() and layer.crs() == crs
        layer = None
        if not valid:
            raise RuntimeError(f'COPC crop did not preserve CRS, a readable index and {count:,} points: {source}')
        _check_cancelled(feedback)
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)


def build_cropped_vpc(files, output, cutline, feedback=None, transform_context=None):
    """Header-reject outside files; clip ordinary LAS/LAZ first, then index ONLY retained points.

    Returns the original sources contributing points. Holes and multipart/overlapping polygons are
    respected in each source's CRS. Missing source CRS means BNG. Original files remain untouched.
    Dock cache outputs use crop_path(); Processing may supply its own VPC destination.
    Empty crops never fall back to whole clouds.
    """
    if not available():
        raise RuntimeError('Polygon point-cloud cropping needs QGIS PDAL tools.')
    feedback = feedback if feedback is not None else QgsProcessingFeedback()
    context = transform_context if transform_context is not None else QgsProject.instance().transformContext()
    _check_cancelled(feedback)
    folder = os.path.join(os.path.dirname(output), 'pointcloud_crops')
    os.makedirs(folder, exist_ok=True)
    retained, contributors = [], []
    steps = QgsProcessingMultiStepFeedback(len(files) + 1, feedback)
    intersected, geometries = False, {}
    for i, source in enumerate(sorted(set(files))):
        steps.setCurrentStep(i)
        _check_cancelled(feedback)
        feedback.setProgressText(f'Checking bounds: {os.path.basename(source)}')
        bounds, crs, source_count = _header(source, steps)
        crs = crs if crs.isValid() else _bng()
        crs_key = crs.toWkt()
        if crs_key not in geometries:
            geometries[crs_key] = _crop_geometry(cutline, crs, context)
        geometry = geometries[crs_key]
        if not source_count or not geometry.intersects(_bounds_geometry(bounds)):
            feedback.pushInfo(f'Skipping outside polygon: {source}')
            continue
        intersected = True
        cached = crop_path([source], folder, cutline)[:-4] + '.copc.laz'
        empty = cached + '.empty'
        if os.path.exists(empty):
            continue
        if os.path.exists(cached):
            try:
                layer = _layer(cached)
                valid = layer.pointCount() > 0 and layer.dataProvider().hasValidIndex() and layer.crs() == crs
            except RuntimeError as e:
                feedback.pushInfo(f'Rebuilding unreadable cropped index: {cached}\n{e}')
                valid = False
            layer = None
            if valid:
                retained.append(cached)
                contributors.append(source)
                continue
        clipped = cached[:-len('.copc.laz')] + '.part.las'
        try:
            if os.path.exists(clipped):
                os.remove(clipped)
            feedback.setProgressText(f'Cropping {os.path.basename(source)} to polygon (original unchanged)')
            reader = {'type': 'readers.las', 'filename': source, 'override_srs': crs.toWkt(),
                      'use_eb_vlr': True}
            if source.lower().endswith('.copc.laz'):
                box = geometry.boundingBox()
                reader = {'type': 'readers.copc', 'filename': source,
                          'bounds': f'([{box.xMinimum()},{box.xMaximum()}],'
                                    f'[{box.yMinimum()},{box.yMaximum()}])'}
            _run_pipeline([reader, {'type': 'filters.crop', 'polygon': geometry.asWkt()},
                           {'type': 'writers.las', 'filename': clipped, 'forward': 'all',
                            'extra_dims': 'all', 'a_srs': crs.toWkt()}], clipped, steps, stream=True)
            _, _, count = _header(clipped, steps)
            if count == 0:
                _check_cancelled(feedback)
                with open(empty, 'w', encoding='utf-8') as f:
                    f.write('No points inside this polygon.\n')
                continue
            _index_crop(clipped, cached, crs, count, steps)
            retained.append(cached)
            contributors.append(source)
        finally:
            if os.path.exists(clipped):
                os.remove(clipped)
    if not retained:
        raise EmptyCrop('No points inside the crop polygon.' if intersected
                        else 'The crop polygon does not overlap any point-cloud file bounds.')
    steps.setCurrentStep(len(files))
    ensure_vpc(retained, output, steps, context)
    feedback.setProgress(100)
    return contributors


def _ensure_copc(source, output, feedback, transform_context):
    original = _layer(source)
    count, crs = original.pointCount(), original.crs()
    if os.path.exists(output):
        cached = _layer(output)
        if cached.dataProvider().hasValidIndex() and cached.pointCount() == count:
            return output
        cached = None
        feedback.pushInfo(f'Rebuilding invalid point-cloud index: {output}')
    if source == output:
        raise RuntimeError(f'{source} is named as COPC but has no readable COPC index.')
    part = output[:-len('.copc.laz')] + '.part.copc.laz'
    try:
        if os.path.exists(part):
            os.remove(part)
        feedback.setProgressText(f'Indexing {os.path.basename(source)} (original unchanged)')
        feedback.pushInfo(f'Creating COPC index: {source} -> {output}')
        parameters = {'INPUT': original, 'OUTPUT': part}
        algorithm = 'pdal:convertformat'
        if not crs.isValid():
            algorithm = 'pdal:assignprojection'
            parameters['CRS'] = BNG
        _run(algorithm, parameters, feedback, transform_context)
        indexed = _layer(part)
        valid = indexed.dataProvider().hasValidIndex() and indexed.pointCount() == count
        indexed = None
        if not valid:
            raise RuntimeError(f'COPC conversion did not preserve a readable index and {count:,} points: {source}')
        _check_cancelled(feedback)
        os.replace(part, output)
        return output
    finally:
        if os.path.exists(part):
            os.remove(part)


def ensure_vpc(files, output, feedback=None, transform_context=None):
    """Ensure a renderable VPC and reusable COPC copies exist. Returns True if the VPC changed.
    Original files are untouched. Conversion and PDAL calls may run in a worker with a transform context."""
    if not files:
        raise ValueError('No point-cloud files were supplied.')
    feedback = feedback if feedback is not None else QgsProcessingFeedback()
    _check_cancelled(feedback)
    folder = os.path.join(os.path.dirname(output), 'pointcloud_indexes')
    os.makedirs(folder, exist_ok=True)
    steps = QgsProcessingMultiStepFeedback(len(files) + 1, feedback)
    indexed = []
    for i, source in enumerate(sorted(files)):
        steps.setCurrentStep(i)
        _check_cancelled(feedback)
        indexed.append(_ensure_copc(source, copc_path(source, folder), steps, transform_context))
    steps.setCurrentStep(len(files))
    feedback.setProgressText('Building virtual point cloud...')
    if os.path.exists(output):
        with open(output, encoding='utf-8') as f:
            data = json.load(f)
        references = [item.get('assets', {}).get('data', {}).get('href', '')
                      for item in data.get('features', [])]
        resolved = sorted(os.path.normcase(os.path.abspath(os.path.join(
            os.path.dirname(output), ref.replace('\\', '/')))) for ref in references)
        if resolved == sorted(os.path.normcase(os.path.abspath(path)) for path in indexed):
            changed = fix_vpc_crs(output, transform_context)
            feedback.setProgress(100)
            return changed
        feedback.pushInfo(f'Rebuilding VPC over indexed point clouds: {output}')
    part = output[:-4] + '_part.vpc'
    layers = [_layer(path) for path in indexed]
    try:
        if os.path.exists(part):
            os.remove(part)
        parameters = {'LAYERS': layers, 'BOUNDARY': False, 'STATISTICS': False,
                      'OVERVIEW': False, 'OUTPUT': part}
        _run('pdal:virtualpointcloud', parameters, steps, transform_context)
        fix_vpc_crs(part, transform_context)
        _check_cancelled(feedback)
        os.replace(part, output)
    finally:
        if os.path.exists(part):
            os.remove(part)
    feedback.setProgress(100)
    return True


def build_vpc(files, output, feedback=None, transform_context=None):
    """A renderable virtual point cloud at output, preparing cached COPC copies as needed. Returns output."""
    ensure_vpc(files, output, feedback, transform_context)
    return output


def style_classified(layer):
    """Colour points by their classification (ground, vegetation, buildings, water...)."""
    names = [a.name() for a in layer.attributes().attributes()] if layer.attributes() else []
    if 'Classification' not in names:
        return
    renderer = QgsPointCloudClassifiedRenderer('Classification', QgsPointCloudClassifiedRenderer.defaultCategories())
    renderer.setPointSize(2)
    layer.setRenderer(renderer)
    layer.triggerRepaint()


def ensure_crs(layer):
    """Point clouds without a coordinate system are British National Grid."""
    if not layer.crs().isValid():
        layer.setCrs(_bng())
