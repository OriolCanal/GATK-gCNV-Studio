import json
import math
import ast
from collections import Counter
from pathlib import Path


def _summary(values):
    if not values:
        return {'count': 0, 'mean': None, 'median': None, 'min': None, 'max': None, 'histogram': []}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    low, high = ordered[0], ordered[-1]
    bins = 20
    if low == high:
        histogram = [{'start': low, 'end': high, 'count': len(ordered)}]
    else:
        width = (high - low) / bins
        counts = [0] * bins
        for value in ordered:
            index = min(bins - 1, int((value - low) / width))
            counts[index] += 1
        histogram = [
            {'start': round(low + i * width, 2), 'end': round(low + (i + 1) * width, 2), 'count': count}
            for i, count in enumerate(counts)
        ]
    return {
        'count': len(ordered),
        'mean': sum(ordered) / len(ordered),
        'median': median,
        'min': low,
        'max': high,
        'histogram': histogram,
    }


def parse_interval_list(path):
    intervals = []
    counts_by_chromosome = Counter()
    total_bases = 0
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith('@'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 3:
                continue
            try:
                contig, start, end = fields[0], int(fields[1]), int(fields[2])
            except ValueError:
                continue
            length = end - start + 1
            if length < 1:
                continue
            name = fields[4] if len(fields) > 4 else ''
            intervals.append({'chrom': contig, 'start': start, 'end': end, 'length': length, 'name': name})
            counts_by_chromosome[contig] += 1
            total_bases += length
    lengths = [row['length'] for row in intervals]
    return {
        'interval_count': len(intervals),
        'total_bases': total_bases,
        'mean_interval_length': (total_bases / len(intervals)) if intervals else None,
        'median_interval_length': _summary(lengths)['median'],
        'interval_lengths': lengths,
        'intervals_by_chromosome': dict(counts_by_chromosome),
        'intervals': intervals,
    }


def parse_annotated_intervals(path):
    header = None
    records = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith('@'):
                continue
            fields = line.rstrip('\n').split('\t')
            normalized = [value.strip().upper() for value in fields]
            if header is None and 'GC_CONTENT' in normalized:
                header = normalized
                continue
            if header is None:
                continue
            if len(fields) < len(header):
                continue
            row = dict(zip(header, fields))
            try:
                if 'CONTIG' in row and 'START' in row and 'END' in row:
                    chrom, start, end = row['CONTIG'], int(row['START']), int(row['END'])
                elif 'INTERVAL' in row:
                    chrom, coord = row['INTERVAL'].rsplit(':', 1)
                    start, end = (int(part) for part in coord.split('-', 1))
                else:
                    continue
                gc = float(row['GC_CONTENT'])
                if not math.isfinite(gc):
                    continue
            except (KeyError, ValueError):
                continue
            records.append({
                'chrom': chrom,
                'start': start,
                'end': end,
                'gc': gc,
                'name': gene_label(row.get('NAME') or row.get('GENE') or ''),
                'mappability': _float_or_none(row.get('MAPPABILITY')),
                'segmental_duplication': _float_or_none(row.get('SEGMENTAL_DUPLICATION_CONTENT')),
            })
    def chrom_key(chrom):
        value = chrom.lower().removeprefix('chr')
        if value == 'x':
            return (0, 23, '')
        if value == 'y':
            return (0, 24, '')
        if value in {'m', 'mt'}:
            return (0, 25, '')
        try:
            return (0, int(value), '')
        except ValueError:
            return (1, 0, value)
    return sorted(records, key=lambda row: (chrom_key(row['chrom']), row['start'], row['end']))


def _float_or_none(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _add_bed_names(records, bed_path):
    source_by_chromosome = {}
    with open(bed_path, encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith(('#', 'track ', 'browser ')):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 4 or not fields[3]:
                continue
            try:
                start, end = int(fields[1]), int(fields[2])
            except ValueError:
                continue
            source_by_chromosome.setdefault(fields[0], []).append((start, end, gene_label(fields[3])))

    annotations_by_chromosome = {}
    for record in records:
        annotations_by_chromosome.setdefault(record['chrom'], []).append(record)
    for chrom, annotations in annotations_by_chromosome.items():
        source = sorted(source_by_chromosome.get(chrom, []), key=lambda row: (row[0], row[1]))
        active = []
        source_index = 0
        for record in annotations:
            query_start, query_end = record['start'] - 1, record['end']
            while source_index < len(source) and source[source_index][0] < query_end:
                active.append(source[source_index])
                source_index += 1
            active = [item for item in active if item[1] > query_start]
            labels = list(dict.fromkeys(item[2] for item in active if item[0] < query_end and item[1] > query_start))
            if labels and not record['name']:
                record['name'] = ', '.join(labels[:5])


def gene_label(value):
    """Extract a readable gene label from BED name fields, including GENCODE attrs."""
    try:
        attributes = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        attributes = None
    if isinstance(attributes, dict):
        for key in ('gene_name', 'gene', 'Name', 'gene_id', 'ID'):
            label = attributes.get(key)
            if label:
                return str(label)
    return value.strip()


def build_bed_metrics(validation, preprocessed_path, annotated_path, parameters):
    original = validation['metrics']
    processed = parse_interval_list(preprocessed_path)
    annotations = parse_annotated_intervals(annotated_path)
    _add_bed_names(annotations, parameters['bed'])
    if not annotations:
        raise ValueError('AnnotateIntervals no produjo filas de anotación reconocibles.')
    gc_values = [row['gc'] for row in annotations]
    mappability_values = [row['mappability'] for row in annotations if row['mappability'] is not None]
    segdup_values = [row['segmental_duplication'] for row in annotations if row['segmental_duplication'] is not None]
    return {
        'schema_version': '1.0',
        'original': {
            'interval_count': original['interval_count'],
            'total_bases': original['total_bases'],
            'mean_interval_length': original['mean_interval_length'],
            'median_interval_length': original['median_interval_length'],
            'interval_lengths': original['interval_lengths'],
            'intervals_by_chromosome': original['intervals_by_chromosome'],
            'overlap_warning_count': original['overlap_warning_count'],
            'duplicate_warning_count': original['duplicate_warning_count'],
        },
        'processed': {key: value for key, value in processed.items() if key != 'intervals'},
        'annotation_count': len(annotations),
        'gc': {'summary': _summary(gc_values), 'intervals': annotations},
        'mappability': {'summary': _summary(mappability_values), 'available': bool(mappability_values)},
        'segmental_duplication': {'summary': _summary(segdup_values), 'available': bool(segdup_values)},
        'parameters': parameters,
    }


def write_metrics(path, metrics):
    Path(path).write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
