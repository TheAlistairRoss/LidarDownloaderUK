# SPDX-FileCopyrightText: 2025-2026 Simon Stoate
# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-QGIS exact point-cloud crop checks; also called by the full plugin smoke test."""
import gc
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from unittest import mock


def write_cloud(path, points, scale=.0001):
    """LAS 1.2 format 3, including precise XYZ, GPS time, RGB and other standard attributes."""
    xs, ys, zs = ([p[i] for p in points] for i in range(3))
    offsets = (min(xs), min(ys), 0.)
    header = struct.pack(
        '<4sHHIHH8sBB32s32sHHHIIBHI5I3d3d6d',
        b'LASF', 0, 0, 0, 0, 0, b'\0' * 8, 1, 2, b'crop test', b'crop test',
        1, 2026, 227, 227, 0, 3, 34, len(points), len(points), 0, 0, 0, 0,
        scale, scale, scale, *offsets, max(xs), min(xs), max(ys), min(ys), max(zs), min(zs))
    with open(path, 'wb') as f:
        f.write(header)
        for i, (x, y, z) in enumerate(points):
            f.write(struct.pack('<iiiHBBbBHdHHH', round((x - offsets[0]) / scale),
                                round((y - offsets[1]) / scale), round(z / scale),
                                100 + i, 9, 2 if i % 2 else 6, 0, 7, 42, 12345.5 + i,
                                1000 + i, 2000 + i, 3000 + i))


def check_crops(mod, check, folder, plugin=None):
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                           QgsPointCloudLayer, QgsProcessingContext, QgsProcessingFeedback, QgsProject,
                           QgsVectorLayer, QgsFeature)
    clouds, postprocess = mod.pointclouds, mod.postprocess
    os.makedirs(folder, exist_ok=True)
    source = os.path.join(folder, 'ordinary cloud with spaces.las')
    points = [(395000 + x, 300000 + y, 10 + i * .0123)
              for i, (x, y) in enumerate((x, y) for x in (1.1234, 3.1234, 5.1234, 7.1234, 9.1234)
                                         for y in (1.1234, 3.1234, 5.1234, 7.1234, 9.1234))]
    write_cloud(source, points)
    outside = os.path.join(folder, 'same_tile_but_outside.las')
    write_cloud(outside, [(x + 100, y + 100, z) for x, y, z in points])
    for path in (source, outside):
        raw = path + '.raw.las'
        os.replace(path, raw)
        clouds._run_pipeline([{'type': 'readers.las', 'filename': raw},
                              {'type': 'filters.ferry', 'dimensions': 'Intensity=>Temperature'},
                              {'type': 'writers.las', 'filename': path, 'forward': 'all',
                               'minor_version': 4, 'extra_dims': 'Temperature=int16'}],
                             path, QgsProcessingFeedback())
        os.remove(raw)
    originals = {p: Path(p).read_bytes() for p in (source, outside)}
    bng = QgsCoordinateReferenceSystem('EPSG:27700')
    # Concave polygon with a hole, a second island, and overlapping selected features.
    polygon = QgsGeometry.fromWkt(
        'MULTIPOLYGON(((395000 300000,395008 300000,395008 300006,395006 300006,'
        '395006 300010,395000 300010,395000 300000),'
        '(395002 300002,395002 300004,395004 300004,395004 300002,395002 300002)),'
        '((395008 300008,395010 300008,395010 300010,395008 300010,395008 300008)))')
    overlapping = QgsGeometry.fromWkt(
        'POLYGON((395000 300000,395002 300000,395002 300002,395000 300002,395000 300000))')
    cutline = postprocess.cutline_from_geometries([polygon, overlapping], bng, None)
    output = clouds.crop_path([source, outside], folder, cutline)
    run = clouds._run
    pipeline = clouds._run_pipeline
    calls = []

    def record(algorithm, parameters, *args):
        calls.append((algorithm, parameters['INPUT'].source() if 'INPUT' in parameters else None))
        return run(algorithm, parameters, *args)

    def record_pipeline(stages, output, feedback, stream=False):
        calls.append(('filters.crop' if len(stages) == 3 else stages[-1]['type'], stages[0]['filename']))
        return pipeline(stages, output, feedback, stream)

    with mock.patch.object(clouds, '_run', side_effect=record), \
            mock.patch.object(clouds, '_run_pipeline', side_effect=record_pipeline):
        contributors = clouds.build_cropped_vpc([source, outside], output, cutline)
    check(contributors == [source] and calls[0] == ('filters.crop', source)
          and all(path is None or path != outside for _, path in calls)
          and all(path.endswith('.part.las') for alg, path in calls
                  if alg == 'writers.copc')
          and not os.path.exists(os.path.join(folder, 'pointcloud_indexes',
                                              os.path.basename(clouds.copc_path(source, folder)))),
          'crop: header rejects outside sources, clips ordinary LAS first and indexes ONLY cropped points')

    def copies(path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return [os.path.normpath(os.path.join(os.path.dirname(path), item['assets']['data']['href']))
                for item in data['features']]

    def read_points(path):
        layer = clouds._layer(path)
        extent = layer.extent()
        extent.grow(1)
        result = layer.dataProvider().identify(.000001, QgsGeometry.fromRect(extent), pointsLimit=10000)
        return layer, sorted(result, key=lambda p: (p['X'], p['Y']))

    expected = [(i, p) for i, p in enumerate(points)
                if polygon.contains(QgsGeometry.fromWkt(f'POINT({p[0]} {p[1]})'))]
    copied = copies(output)
    layer, actual = read_points(copied[0])
    expected = sorted(expected, key=lambda item: (item[1][0], item[1][1]))
    check(layer.pointCount() == len(expected) == len(actual) and layer.dataProvider().hasValidIndex()
          and layer.crs().authid() == 'EPSG:27700',
          'crop: exact concave/multipolygon/hole count, no duplicate overlapping features, missing CRS assumes BNG')
    check(all(abs(a['X'] - p[0]) < .00005 and abs(a['Y'] - p[1]) < .00005
              and abs(a['Z'] - p[2]) < .00005 and a['Intensity'] == 100 + i
              and a['Classification'] == (2 if i % 2 else 6)
              and a['ReturnNumber'] == 1 and a['NumberOfReturns'] == 1
              and a['PointSourceId'] == 42 and a['UserData'] == 7
              and a.get('Temperature') == 100 + i
              and a.get('GpsTime (raw)', a['GpsTime']) == 12345.5 + i
              and (a['Red'], a['Green'], a['Blue']) == (1000 + i, 2000 + i, 3000 + i)
              for a, (i, p) in zip(actual, expected)) and bool(actual),
          'crop: original precise XYZ, intensity, classification, returns, GPS time, RGB and Extra Bytes attributes '
          'preserved')
    layer = None
    vpc = QgsPointCloudLayer(output, 'crop', 'vpc')
    check(vpc.isValid() and vpc.pointCount() == len(expected) and vpc.crs().authid() == 'EPSG:27700',
          'crop: resulting small VPC has correct count, CRS and COPC sources')
    vpc = None
    times = [os.stat(p).st_mtime_ns for p in copied + [output]]
    with mock.patch.object(clouds, '_run', side_effect=AssertionError('cache must avoid PDAL')), \
            mock.patch.object(clouds, '_run_pipeline', side_effect=AssertionError('cache must avoid pipelines')):
        check(clouds.build_cropped_vpc([outside, source], output, cutline) == [source]
              and times == [os.stat(p).st_mtime_ns for p in copied + [output]],
              'crop: repeated same polygon/source reuses both crop COPC and VPC without rewriting')
    different = postprocess.cutline_from_geometries([overlapping], bng, None)
    different_output = clouds.crop_path([source, outside], folder, different)
    clouds.build_cropped_vpc([source, outside], different_output, different)
    smaller, smaller_points = read_points(copies(different_output)[0])
    check(different_output != output and copies(different_output) != copied
          and smaller.pointCount() == len(smaller_points) == 1,
          'crop: changing polygon creates a distinct crop and never reuses the full-tile or other polygon result')
    smaller = None
    previous = clouds.crop_path([source], folder, cutline)
    alternate = os.path.join(folder, 'same bytes different source path.las')
    Path(alternate).write_bytes(originals[source])
    source_stat = os.stat(source)
    os.utime(alternate, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    check(clouds.crop_path([alternate], folder, cutline) != previous,
          'crop: source path participates in crop cache identity even when bytes, size and mtime match')
    os.remove(alternate)
    stat = os.stat(source)
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
    check(clouds.crop_path([source], folder, cutline) != previous,
          'crop: source mtime participates in crop cache identity')
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with open(source, 'ab') as f:
        f.write(b'cache-size-test')
    check(clouds.crop_path([source], folder, cutline) != previous,
          'crop: source size participates in crop cache identity')
    Path(source).write_bytes(originals[source])
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    feedback = QgsProcessingFeedback()
    feedback.cancel()
    with mock.patch.object(clouds, '_run', side_effect=AssertionError('cancelled before PDAL')):
        try:
            clouds.build_cropped_vpc([source], output, cutline, feedback)
        except clouds.Cancelled:
            check(True, 'crop: cancellation before bounds/conversion never launches PDAL')
        else:
            check(False, 'crop: pre-cancellation raises Cancelled')

    no_overlap = postprocess.cutline_from_geometries([QgsGeometry.fromWkt(
        'POLYGON((400000 300000,400010 300000,400010 300010,400000 300010,400000 300000))')], bng, None)
    for label, empty_cutline in (
            ('no header overlap', no_overlap),
            ('header overlap but zero points', postprocess.cutline_from_geometries([QgsGeometry.fromWkt(
                'POLYGON((395002 300002,395002.5 300002,395002.5 300002.5,395002 300002.5,395002 300002))')],
                bng, None))):
        path = clouds.crop_path([source], folder, empty_cutline)
        calls.clear()
        with mock.patch.object(clouds, '_run', side_effect=record), \
                mock.patch.object(clouds, '_run_pipeline', side_effect=record_pipeline):
            try:
                clouds.build_cropped_vpc([source], path, empty_cutline)
            except clouds.EmptyCrop as e:
                check(bool(str(e)) and not os.path.exists(path)
                      and all(alg == 'filters.crop' for alg, _ in calls)
                      and (bool(calls) if 'zero points' in label else not calls),
                      f'crop: {label} warns explicitly, with no COPC indexing or whole-cloud fallback')
            else:
                check(False, f'crop: {label} raises EmptyCrop')
        with mock.patch.object(clouds, '_run_pipeline', side_effect=AssertionError('empty cache avoids crop/index')):
            try:
                clouds.build_cropped_vpc([source], path, empty_cutline)
            except clouds.EmptyCrop:
                check(True, f'crop: repeating {label} never converts or indexes an empty result')

    with mock.patch.object(clouds.shutil, 'which', return_value=None):
        try:
            clouds.build_cropped_vpc([source], output, cutline)
        except RuntimeError as e:
            check('PDAL command-line' in str(e),
                  'crop: missing native PDAL gives actionable error, not whole-cloud fallback')
        else:
            check(False, 'crop: missing native PDAL must fail safely')

    failure_output = clouds.crop_path([source], folder, no_overlap + ' ')
    fail_cutline = postprocess.cutline_from_geometries([QgsGeometry.fromWkt(
        'POLYGON((395000 300000,395010 300000,395010 300007,395000 300007,395000 300000))')], bng, None)
    for cancel in (False, True):
        failure_feedback = QgsProcessingFeedback()

        def failed_run(stages, output, feedback, stream=False):
            Path(output).write_bytes(b'incomplete')
            if cancel:
                failure_feedback.cancel()
                clouds._check_cancelled(failure_feedback)
            raise RuntimeError('PDAL crop diagnostic: synthetic failure')

        with mock.patch.object(clouds, '_run_pipeline', side_effect=failed_run):
            try:
                clouds.build_cropped_vpc([source], failure_output, fail_cutline, failure_feedback)
            except (clouds.Cancelled, RuntimeError) as e:
                check(('cancelled' if cancel else 'diagnostic') in str(e).lower()
                      and not os.path.exists(failure_output)
                      and not list(Path(folder).rglob('*.part.las'))
                      and not list(Path(folder).rglob('*.las.geojson')),
                      f'crop: {"mid-crop cancellation" if cancel else "PDAL failure"} cleans partial files '
                      'and retains diagnostics')
            else:
                check(False, 'crop: injected failure must propagate')

    native_output = os.path.join(folder, 'native-cancel.part.las')
    native_feedback = QgsProcessingFeedback()
    timer = threading.Timer(.1, native_feedback.cancel)
    start = time.monotonic()
    timer.start()
    try:
        pipeline([{'type': 'readers.faux', 'count': 1000000000,
                   'bounds': '([0,1],[0,1],[0,1])', 'mode': 'constant'},
                  {'type': 'writers.las', 'filename': native_output}], native_output, native_feedback, stream=True)
    except clouds.Cancelled:
        check(time.monotonic() - start < 10 and not os.path.exists(native_output + '.pipeline.json'),
              'crop: actual native PDAL process is terminated promptly by cancellation, pipeline JSON cleaned')
    else:
        check(False, 'crop: actual native process must honour cancellation')
    finally:
        timer.cancel()
        timer.join()
        if os.path.exists(native_output):
            os.remove(native_output)
    diagnostic_output = os.path.join(folder, 'native-error.part.las')
    try:
        pipeline([{'type': 'readers.nonexistent_crop_test'}], diagnostic_output, QgsProcessingFeedback())
    except RuntimeError as e:
        check('nonexistent_crop_test' in str(e) and not os.path.exists(diagnostic_output + '.pipeline.json'),
              'crop: actual native PDAL failure retains stderr and cleans pipeline JSON')
    else:
        check(False, 'crop: invalid native PDAL stage must fail')

    # LAS -> LAZ and an existing COPC source: neither requires another full-source index to crop.
    laz = os.path.join(folder, 'ordinary.laz')
    copc = os.path.join(folder, 'existing.copc.laz')
    for path, writer in ((laz, 'writers.las'), (copc, 'writers.copc')):
        pipeline([{'type': 'readers.las', 'filename': source},
                  {'type': writer, 'filename': path, 'forward': 'all', 'extra_dims': 'all',
                   'a_srs': 'EPSG:27700'}], path, QgsProcessingFeedback())
    for path in (laz, copc):
        original = Path(path).read_bytes()
        dest = clouds.crop_path([path], folder, cutline)
        calls.clear()
        with mock.patch.object(clouds, '_run', side_effect=record), \
                mock.patch.object(clouds, '_run_pipeline', side_effect=record_pipeline):
            clouds.build_cropped_vpc([path], dest, cutline)
        cropped, values = read_points(copies(dest)[0])
        check(cropped.pointCount() == len(expected) == len(values)
              and Path(path).read_bytes() == original
              and all(p.endswith('.part.las') for alg, p in calls
                      if alg == 'writers.copc')
              and all(v.get('Temperature') == 100 + i for v, (i, _) in zip(values, expected)),
              f'crop: {os.path.basename(path)} is clipped without a full-source conversion or modifying original')
        cropped = None

    # The cloud CRS differs from the BNG selection CRS: transform the mask, never the points.
    projected = os.path.join(folder, 'web-mercator-source.las')
    raw_projected = os.path.join(folder, 'web-mercator-raw.las')
    mercator = QgsCoordinateReferenceSystem('EPSG:3857')
    transform = QgsCoordinateTransform(bng, mercator, QgsProject.instance().transformContext())
    projected_points = []
    for x, y, z in points:
        p = transform.transform(x, y)
        projected_points.append((p.x(), p.y(), z))
    write_cloud(raw_projected, projected_points)
    run('pdal:assignprojection', {'INPUT': clouds._layer(raw_projected), 'CRS': mercator, 'OUTPUT': projected})
    projected_original = Path(projected).read_bytes()
    projected_output = clouds.crop_path([projected], folder, cutline)
    clouds.build_cropped_vpc([projected], projected_output, cutline)
    projected_layer, projected_values = read_points(copies(projected_output)[0])
    native_mask = clouds._crop_geometry(cutline, mercator, QgsProject.instance().transformContext())
    native_expected = sorted((p for p in projected_points
                              if native_mask.contains(QgsGeometry.fromWkt(f'POINT({p[0]} {p[1]})'))),
                             key=lambda p: (p[0], p[1]))
    check(projected_layer.crs() == mercator and len(projected_values) == len(native_expected) == len(expected)
          and all(abs(a['X'] - p[0]) < .0001 and abs(a['Y'] - p[1]) < .0001
                  and abs(a['Z'] - p[2]) < .0001 for a, p in zip(projected_values, native_expected))
          and Path(projected).read_bytes() == projected_original,
          'crop: polygon transformed into source CRS; original projected coordinates and CRS preserved')
    projected_layer = None

    from lidar_downloader_uk.processing_provider import DownloadTilesAlgorithm
    algorithm = DownloadTilesAlgorithm(os.path.dirname(mod.__file__))
    algorithm.initAlgorithm()
    context = QgsProcessingContext()
    context.setTransformContext(QgsProject.instance().transformContext())
    area_layer = QgsVectorLayer('MultiPolygon?crs=EPSG:27700', 'crop area', 'memory')
    feature = QgsFeature()
    feature.setGeometry(polygon)
    area_layer.dataProvider().addFeatures([feature])
    toolbox_output = os.path.join(folder, 'toolbox-crop.vpc')
    dataset = mod.api.Dataset('lidar_point_cloud', '2024', 'NaN')
    result = algorithm._crop_clouds({algorithm.INPUT: area_layer, algorithm.OUTPUT_CLOUD: toolbox_output},
                                    context, QgsProcessingFeedback(), [source, outside], dataset)
    check(result == toolbox_output and os.path.exists(result),
          'crop: Processing optional VPC output uses the same exact crop pipeline and preserves raster '
          'output contracts')

    if plugin is not None:
        outcome = mod.LoadOutcome()
        tiles = {'SJ90SE': (folder, [], [source], []), 'SJ90NE': (folder, [], [outside], [])}
        plugin.load_cropped_clouds('Polygon crop regression', dataset, tiles, ('sites', cutline), outcome)
        check(outcome.cropped and outcome.added == 1 and outcome.loaded == {'SJ90SE'}
              and outcome.outside == ['SJ90NE'] and not outcome.problems,
              'crop: modal worker loads one cropped VPC and excludes outside-file tile, not filename-estimated bounds')
        repeated = mod.LoadOutcome()
        plugin.load_cropped_clouds('Polygon crop regression', dataset, tiles, ('sites', cutline), repeated)
        check(repeated.already == 1 and not repeated.added and repeated.loaded == {'SJ90SE'},
              'crop: repeated UI load detects already-loaded crop instead of duplicating it')
        for layer in list(QgsProject.instance().mapLayers().values()):
            if layer.name().startswith('Polygon crop regression'):
                QgsProject.instance().removeMapLayer(layer.id())
        missed = mod.LoadOutcome()
        plugin.load_cropped_clouds('Polygon crop regression', dataset, tiles, ('away', no_overlap), missed)
        check(not missed.added and not missed.loaded and missed.problems
              and 'not substituted' in missed.problems[0],
              'crop: UI no-overlap loads nothing and gives useful warning, never whole clouds')
    check(all(Path(p).read_bytes() == data for p, data in originals.items()),
          'crop: all downloaded source bytes unchanged after crops, reuse, failures and cancellation')


if __name__ == '__main__':
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from qgis.core import QgsApplication
    app = QgsApplication([], False)
    app.initQgis()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.path.append(os.path.join(QgsApplication.pkgDataPath(), 'python', 'plugins'))
    from processing.core.Processing import Processing
    Processing.initialize()
    from lidar_downloader_uk import lidar_downloader as mod
    failures = []

    def check(condition, label):
        print(('PASS ' if condition else 'FAIL ') + label, flush=True)
        if not condition:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix='lidar-polygon-crop-') as folder:
        check_crops(mod, check, folder)
        gc.collect()
    print(f'{len(failures)} failures', flush=True)
    sys.exit(bool(failures))
