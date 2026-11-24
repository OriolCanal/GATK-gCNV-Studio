"""Scalable, provenance-aware diagnostics for GATK GermlineCNVCaller COHORT runs.

Native GATK quantities stay separate from coverage summaries derived from
CollectReadCounts. Large sample-by-interval matrices are cached as NumPy
arrays; interval JSON is line-delimited and compressed for paginated access.
"""
import csv
import ast
import gzip
import hashlib
import itertools
import json
import math
import os
import re
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import pdist

from app.services.bam_qc import read_gatk_counts


DIAGNOSTICS_VERSION = 19
DEFAULT_OUTLIER_Z = 3.0
MAX_INTERVAL_PAGE_SIZE = 200


class DiagnosticsUnavailable(ValueError):
    """Raised when the published artifacts are insufficient for diagnostics."""


def robust_z_scores(values):
    """Median/MAD robust z-scores; missing values remain missing."""
    values = np.asarray(values, dtype=np.float64)
    result = np.full(values.shape, np.nan, dtype=np.float64)
    valid = np.isfinite(values)
    if not np.any(valid):
        return result
    median = float(np.median(values[valid]))
    mad = float(np.median(np.abs(values[valid] - median)))
    if mad > 0:
        result[valid] = (values[valid] - median) / (1.4826 * mad)
    elif np.allclose(values[valid], median):
        result[valid] = 0.0
    return result


def posterior_entropy(log_probabilities, state_counts=None):
    """Return Shannon entropy, normalized entropy, max posterior, and MAP state.

    Input rows are GATK log posterior probabilities. The function normalizes
    each row after a stable log-sum-exp shift and preserves invalid rows as NaN.
    """
    logp = np.asarray(log_probabilities, dtype=np.float64)
    if logp.ndim == 1:
        logp = logp[None, :]
    if logp.ndim != 2 or logp.shape[1] < 2:
        raise ValueError('La matriz de posterior debe tener una dimensión por estado y al menos 2 estados.')
    counts = (np.full(logp.shape[0], logp.shape[1], dtype=np.int64) if state_counts is None
              else np.asarray(state_counts, dtype=np.int64).reshape(-1))
    if counts.size != logp.shape[0] or np.any(counts < 2) or np.any(counts > logp.shape[1]):
        raise ValueError('state_counts debe indicar entre 2 y K estados para cada fila.')
    columns = np.arange(logp.shape[1])[None, :] < counts[:, None]
    masked = np.where(columns, logp, -np.inf)
    valid = (np.any(np.isfinite(masked), axis=1)
             & ~np.any(np.isnan(masked), axis=1)
             & ~np.any(np.isposinf(masked), axis=1))
    entropy = np.full(logp.shape[0], np.nan, dtype=np.float64)
    normalized = np.full(logp.shape[0], np.nan, dtype=np.float64)
    maximum = np.full(logp.shape[0], np.nan, dtype=np.float64)
    map_index = np.full(logp.shape[0], -1, dtype=np.int16)
    if not np.any(valid):
        return entropy, normalized, maximum, map_index
    block = masked[valid]
    maxima = np.max(block, axis=1, keepdims=True)
    weights = np.exp(block - maxima)
    totals = np.sum(weights, axis=1, keepdims=True)
    probs = weights / totals
    safe = np.where(probs > 0, probs, 1.0)
    entropy_valid = -np.sum(np.where(probs > 0, probs * np.log(safe), 0.0), axis=1)
    entropy[valid] = entropy_valid
    normalized[valid] = entropy_valid / np.log(counts[valid])
    maximum[valid] = np.max(probs, axis=1)
    map_index[valid] = np.argmax(probs, axis=1).astype(np.int16)
    return entropy, normalized, maximum, map_index


def _json_number(value):
    # Optional GATK metrics can be absent (None) in otherwise valid outputs.
    # Keep them unavailable in the diagnostics JSON instead of failing the
    # entire cohort diagnostics build with ``float(None)``.
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _intervals(path):
    result = []
    with Path(path).open(encoding='utf-8', errors='replace') as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip() or line.startswith(('@', '#')):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 3:
                raise DiagnosticsUnavailable(f'Interval list inválido en línea {line_number}.')
            result.append((fields[0], int(fields[1]), int(fields[2])))
    if len(result) != len(set(result)):
        raise DiagnosticsUnavailable('El interval list contiene coordenadas duplicadas; no se pueden alinear con seguridad.')
    return result


def _read_gatk_rows(path):
    """Read a GATK TSV after optional @-metadata records."""
    lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
    header_index = None
    header_line = None
    for index, line in enumerate(lines):
        stripped = line.lstrip('\ufeff').strip()
        if not stripped:
            continue
        fields = next(csv.reader([stripped], delimiter='\t'))
        # Some GATK TSVs carry the column labels in a SAM @CO line; ordinary
        # @HD/@SQ metadata must still be skipped.
        if fields[0].startswith('@'):
            candidate = fields[1:] if fields[0] in {'@CO', '@COMMENT'} else []
            if candidate and any(_column_key(value) in {'contig', 'start', 'log2_copy_ratio'}
                                 for value in candidate):
                header_index, header_line = index, '\t'.join(candidate)
                break
            continue
        if stripped.startswith('#'):
            candidate_line = stripped.lstrip('#').strip()
            candidate = next(csv.reader([candidate_line], delimiter='\t'))
            if not any(_column_key(value) in {'contig', 'start', 'log2_copy_ratio'} for value in candidate):
                continue
            header_line = candidate_line
        else:
            header_line = stripped
        header_index = index
        break
    if header_index is None:
        return [], []
    table_lines = [header_line] + [line.lstrip('\ufeff') for line in lines[header_index + 1:]]
    reader = csv.DictReader(table_lines, delimiter='\t')
    rows = []
    for row in reader:
        rows.append({str(key).lstrip('\ufeff').strip(): value for key, value in row.items() if key is not None})
    return list(reader.fieldnames or []), rows


def _column_key(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value).lstrip('\ufeff').strip().lower()).strip('_')


def _read_gatk_vector(path):
    header, rows = _read_gatk_rows(path)
    if not header:
        return np.asarray([], dtype=np.float64)
    column = header[0]
    result = []
    for row in rows:
        value = row.get(column)
        if value is None or not str(value).strip():
            result.append(np.nan)
        else:
            try:
                result.append(float(value))
            except (TypeError, ValueError):
                result.append(np.nan)
    return np.asarray(result, dtype=np.float64)


def _read_sample_table(path):
    _header, rows = _read_gatk_rows(path)
    return rows


def _read_interval_annotations(path):
    if not Path(path).is_file():
        return {}
    header, rows = _read_gatk_rows(path)
    normalized = {re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_'): name for name in header}
    contig_key = normalized.get('contig') or normalized.get('chromosome')
    start_key = normalized.get('start')
    end_key = normalized.get('end')
    interval_key = normalized.get('interval')
    if not ((contig_key and start_key and end_key) or interval_key):
        return {}
    result = {}
    for row in rows:
        try:
            if contig_key and start_key and end_key:
                key = (row[contig_key], int(row[start_key]), int(row[end_key]))
            else:
                contig, coordinates = row[interval_key].rsplit(':', 1)
                start, end = (int(part) for part in coordinates.split('-', 1))
                key = (contig, start, end)
        except (KeyError, TypeError, ValueError):
            continue
        if key in result:
            raise DiagnosticsUnavailable('AnnotateIntervals contiene coordenadas duplicadas.')
        item = {}
        gene_key = normalized.get('name') or normalized.get('gene') or normalized.get('gene_name')
        if gene_key and row.get(gene_key):
            item['gene'] = str(row[gene_key]).strip() or None
        for target, candidates in {
            'gc_content': ('gc_content', 'gc'),
            'mappability': ('mappability', 'mappability_content'),
            'segmental_duplication': ('segmental_duplication_content', 'segmental_duplication'),
        }.items():
            source = next((normalized[name] for name in candidates if name in normalized), None)
            if source:
                try:
                    value = float(row[source])
                    item[target] = _json_number(value)
                except (ValueError, TypeError):
                    item[target] = None
        result[key] = item
    return result


def _read_bed_gene_names(path, intervals):
    """Map BED names to interval_list coordinates using overlap semantics."""
    if not path or not Path(path).is_file():
        return {}
    from app.services.bed_metrics import _add_bed_names
    records = [{'chrom': chrom, 'start': start, 'end': end, 'name': ''}
               for chrom, start, end in intervals]
    _add_bed_names(records, path)
    return {(row['chrom'], row['start'], row['end']): row.get('name') or None for row in records}


def _read_bed_feature_annotations(path, intervals):
    """Read GENCODE-style metadata dictionaries from BED column 4 by overlap."""
    if not path or not Path(path).is_file():
        return {}
    sources = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith(('#', 'track ', 'browser ')):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 4:
                continue
            try:
                start, end = int(fields[1]), int(fields[2])
                attributes = ast.literal_eval(fields[3])
            except (ValueError, SyntaxError):
                continue
            if not isinstance(attributes, dict):
                continue
            transcript = str(attributes.get('transcript_id') or '').strip()
            if transcript:
                transcript = re.sub(r'_1$', '', transcript)
            feature = {
                'gene': str(attributes.get('gene_name') or '').strip() or None,
                'transcript_id': transcript or None,
                'exon_number': str(attributes.get('exon_number') or '').strip() or None,
            }
            sources.setdefault(fields[0], []).append((start, end, feature))
    for records in sources.values():
        records.sort(key=lambda item: (item[0], item[1]))

    result = {}
    by_contig = {}
    for interval in intervals:
        by_contig.setdefault(interval[0], []).append(interval)
    for contig, interval_rows in by_contig.items():
        source = sources.get(contig, ())
        active = []
        source_index = 0
        for _contig, start, end in sorted(interval_rows, key=lambda item: (item[1], item[2])):
            # CollectReadCounts interval coordinates are 1-based inclusive; BED is 0-based half-open.
            query_start, query_end = start - 1, end
            while source_index < len(source) and source[source_index][0] < query_end:
                active.append(source[source_index])
                source_index += 1
            active = [item for item in active if item[1] > query_start]
            features = [item[2] for item in active if item[0] < query_end and item[1] > query_start]
            merged = {}
            for key in ('gene', 'transcript_id', 'exon_number'):
                values = list(dict.fromkeys(item[key] for item in features if item.get(key)))
                merged[key] = ', '.join(values[:5]) or None
            result[(contig, start, end)] = merged
    return result


def _read_postprocessed_copy_ratios(output, expected_intervals, sample_names, expected_postprocess_intervals=None):
    """Read denoised ratios, converting declared linear ratios to log2."""
    output = Path(output)
    sample_index = {sample: index for index, sample in enumerate(sample_names)}
    matrix = np.full((len(sample_names), len(expected_intervals)), np.nan, dtype=np.float32)
    interval_index = {interval: index for index, interval in enumerate(expected_intervals)}
    expected_rows = set(expected_postprocess_intervals or expected_intervals)
    warnings = []
    unrecognized = []
    interval_mismatches = []
    invalid_ratio_outputs = []
    available = 0
    for path in sorted(output.glob('postprocess/SAMPLE_*/denoised_copy_ratios.tsv')):
        sample_id = _sample_name(path.parent)
        if sample_id not in sample_index:
            warnings.append(f'{path.name}: falta un sample_name.txt reconocido; archivo omitido.')
            continue
        header, rows = _read_gatk_rows(path)
        normalized = {_column_key(name): name for name in header}
        contig_key = normalized.get('contig') or normalized.get('chromosome')
        start_key, end_key = normalized.get('start'), normalized.get('end')
        # Trust the declared scale, never infer it from observed values.
        log2_key = normalized.get('log2_copy_ratio')
        linear_key = normalized.get('linear_copy_ratio')
        value_key = log2_key or linear_key
        linear_scale = log2_key is None and linear_key is not None
        if not (contig_key and start_key and end_key and value_key):
            unrecognized.append((sample_id, ', '.join(header) if header else 'sin cabecera'))
            continue
        seen = set()
        sample_values = np.full(len(expected_intervals), np.nan, dtype=np.float32)
        malformed_coordinates = 0
        unmatched_rows = 0
        duplicate_rows = 0
        invalid_ratios = 0
        valid_values = 0
        first_unmatched = None
        first_invalid_ratio = None
        for row in rows:
            try:
                interval = (row[contig_key], int(row[start_key]), int(row[end_key]))
            except (KeyError, TypeError, ValueError):
                malformed_coordinates += 1
                continue
            index = interval_index.get(interval)
            if index is None:
                unmatched_rows += 1
                if first_unmatched is None:
                    first_unmatched = interval
                continue
            if index in seen:
                duplicate_rows += 1
                continue
            seen.add(index)
            try:
                value = float(row[value_key])
                if not math.isfinite(value) or (linear_scale and value <= 0):
                    raise ValueError('ratio no finito o no positivo')
                if linear_scale:
                    value = math.log2(value)
            except (KeyError, TypeError, ValueError):
                invalid_ratios += 1
                if first_invalid_ratio is None:
                    first_invalid_ratio = (interval, row.get(value_key))
                continue
            sample_values[index] = value
            valid_values += 1
        expected_indices = {interval_index[interval] for interval in expected_rows if interval in interval_index}
        missing_count = len(expected_indices - seen)
        if valid_values:
            matrix[sample_index[sample_id]] = sample_values
            available += 1
        if (seen != expected_indices or unmatched_rows or duplicate_rows or malformed_coordinates):
            interval_mismatches.append({
                'sample_id': sample_id, 'matched': len(seen), 'expected': len(expected_indices),
                'missing': missing_count, 'unmatched': unmatched_rows, 'duplicates': duplicate_rows,
                'malformed': malformed_coordinates, 'example': first_unmatched,
            })
        if invalid_ratios:
            invalid_ratio_outputs.append({
                'sample_id': sample_id, 'count': invalid_ratios, 'example': first_invalid_ratio,
            })
    if interval_mismatches:
        examples = '; '.join(
            f"{item['sample_id']} ({item['matched']}/{item['expected']} exactos, "
            f"{item['missing']} ausentes, {item['unmatched']} ajenos, "
            f"{item['duplicates']} duplicados, {item['malformed']} inválidos"
            + (f", ej. {item['example']}" if item['example'] else '') + ')'
            for item in interval_mismatches[:4])
        suffix = f' (y {len(interval_mismatches) - 4} más)' if len(interval_mismatches) > 4 else ''
        warnings.append(
            f'{len(interval_mismatches)} salida(s) denoised tienen diferencias de coordenadas respecto a '
            f'CollectReadCounts; se conservaron únicamente los intervalos con coincidencia exacta: '
            f'{examples}{suffix}.')
    if invalid_ratio_outputs:
        examples = '; '.join(
            f"{item['sample_id']} ({item['count']} ratio(s) inválido(s)"
            + (f", ej. {item['example'][0]}={item['example'][1]}" if item['example'] else '') + ')'
            for item in invalid_ratio_outputs[:4])
        suffix = f' (y {len(invalid_ratio_outputs) - 4} más)' if len(invalid_ratio_outputs) > 4 else ''
        warnings.append(
            f'{len(invalid_ratio_outputs)} salida(s) denoised contienen valores LINEAR_COPY_RATIO/LOG2_COPY_RATIO '
            f'ausentes o no válidos; esas coordenadas se dejan como N/D: {examples}{suffix}.')
    if unrecognized:
        examples = '; '.join(f'{sample} [{header}]' for sample, header in unrecognized[:4])
        suffix = f' (y {len(unrecognized) - 4} más)' if len(unrecognized) > 4 else ''
        warnings.append(f'{len(unrecognized)} salida(s) denoised no reconocieron CONTIG, START, END y una escala LINEAR_COPY_RATIO o LOG2_COPY_RATIO: {examples}{suffix}.')
    return matrix, available, warnings


def _sample_name(sample_dir):
    path = Path(sample_dir) / 'sample_name.txt'
    return path.read_text(encoding='utf-8', errors='replace').strip() if path.is_file() else ''


def _sample_output_dirs(root, file_name):
    result = {}
    for path in sorted(Path(root).rglob(file_name)):
        name = _sample_name(path.parent)
        if name:
            result[name] = path.parent
    return result


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _read_filter_intervals(path):
    if not Path(path).is_file():
        return None
    return _intervals(path)


def _filter_interval_membership(original_intervals, retained_intervals):
    """Validate exact interval membership and summarize FilterIntervals output."""
    original_intervals, retained_intervals = list(original_intervals), list(retained_intervals)
    if len(original_intervals) != len(set(original_intervals)):
        raise DiagnosticsUnavailable('El conjunto original contiene intervalos duplicados.')
    if len(retained_intervals) != len(set(retained_intervals)):
        raise DiagnosticsUnavailable('FilterIntervals contiene intervalos duplicados.')
    original_set, retained_set = set(original_intervals), set(retained_intervals)
    if not retained_set.issubset(original_set):
        raise DiagnosticsUnavailable('Los intervalos retenidos no coinciden con los intervalos originales.')
    total = len(original_intervals)
    retained_count = len(retained_set)
    excluded_count = total - retained_count
    return retained_set, {'total': total, 'retained': retained_count, 'filtered': excluded_count,
                          'retained_percentage': retained_count / total * 100 if total else None,
                          'filtered_percentage': excluded_count / total * 100 if total else None}


def _read_elbo(path, max_points=240):
    values = _read_gatk_vector(path)
    values = values[np.isfinite(values)]
    if not values.size:
        return {'iterations': 0, 'points': []}
    if values.size > max_points:
        indexes = np.linspace(0, values.size - 1, max_points).astype(np.int64)
        selected = values[indexes]
    else:
        indexes = np.arange(values.size)
        selected = values
    return {'iterations': int(values.size),
            'points': [{'iteration': int(index + 1), 'elbo': float(value)}
                       for index, value in zip(indexes, selected)]}


def _read_posterior(path):
    header, rows = _read_gatk_rows(path)
    states = []
    for column in header:
        match = re.fullmatch(r'COPY_NUMBER_(\d+)', column.strip(), re.IGNORECASE)
        if not match:
            states = []
            break
        states.append(int(match.group(1)))
    if len(states) < 2:
        raise DiagnosticsUnavailable(f'No se reconoce el encabezado de posterior en {path.name}.')
    values = np.full((len(rows), len(states)), np.nan, dtype=np.float64)
    for i, row in enumerate(rows):
        for j, column in enumerate(header):
            values[i, j] = _number(row.get(column)) if _number(row.get(column)) is not None else np.nan
    return states, values


def _read_posterior_row(path, interval_index):
    """Read one posterior record without loading every sample-interval state."""
    with Path(path).open(encoding='utf-8', errors='replace') as handle:
        header = None
        for line in handle:
            if line.strip() and not line.startswith(('@', '#')):
                header = line.rstrip('\n').split('\t')
                break
        if not header:
            return [], None
        states = []
        for column in header:
            match = re.fullmatch(r'COPY_NUMBER_(\d+)', column.strip(), re.IGNORECASE)
            if not match:
                return [], None
            states.append(int(match.group(1)))
        row = next(itertools.islice(handle, interval_index, interval_index + 1), None)
        if row is None:
            return states, None
        values = [_number(value) for value in row.rstrip('\n').split('\t')]
        if len(values) != len(states) or any(value is None for value in values):
            return states, None
        return states, np.asarray(values, dtype=np.float64)


def _read_count_at(path, interval_index):
    """Fast random access for the current CollectReadCounts matrix schema."""
    with h5py.File(path, 'r') as handle:
        if '/counts/values' in handle:
            values = handle['/counts/values']
            if values.ndim == 2 and values.shape[0] == 1:
                return float(values[0, interval_index])
            if values.ndim == 1:
                return float(values[interval_index])
    # Legacy structured HDF5 remains supported by the established QC reader.
    _coords, counts = read_gatk_counts(path)
    return float(counts[interval_index])


def _read_all_counts(params):
    snapshots = params.get('sample_snapshot') or []
    if not snapshots:
        snapshots = [{'sample_id': item.get('sample_id'), 'counts': item.get('counts'),
                      'batch': item.get('batch', ''), 'kit_name': item.get('kit_name', ''),
                      'protocol': item.get('protocol', ''), 'read_group_sm': item.get('read_group_sm', '')}
                     for item in params.get('samples', [])]
    if not snapshots:
        raise DiagnosticsUnavailable('El snapshot no contiene muestras.')
    reference_intervals = None
    matrix = None
    for row_index, sample in enumerate(snapshots):
        path = Path(sample['counts'])
        if not path.is_file():
            raise DiagnosticsUnavailable(f"Falta el HDF5 de {sample.get('sample_id', 'una muestra')}.")
        intervals, counts = read_gatk_counts(path)
        if reference_intervals is None:
            reference_intervals = intervals
            matrix = np.empty((len(snapshots), len(intervals)), dtype=np.float32)
        elif intervals != reference_intervals:
            raise DiagnosticsUnavailable(f"Los read counts de {sample.get('sample_id')} no mantienen el orden exacto de intervalos.")
        if counts.size != len(reference_intervals):
            raise DiagnosticsUnavailable(f"El número de recuentos de {sample.get('sample_id')} no coincide con los intervalos.")
        matrix[row_index] = counts
    return snapshots, reference_intervals, matrix


def _size_factors_median_ratio(counts):
    positive = counts > 0
    logged = np.full(counts.shape, np.nan, dtype=np.float32)
    np.log(counts, out=logged, where=positive)
    with warnings.catch_warnings(), np.errstate(invalid='ignore'):
        warnings.simplefilter('ignore', RuntimeWarning)
        geometric = np.exp(np.nanmean(logged, axis=0)).astype(np.float32)
    valid = np.isfinite(geometric) & (geometric > 0)
    factors = np.full(counts.shape[0], np.nan, dtype=np.float64)
    for sample_index in range(counts.shape[0]):
        if not np.any(valid):
            continue
        ratios = counts[sample_index, valid] / geometric[valid]
        ratios = ratios[np.isfinite(ratios) & (ratios > 0)]
        if ratios.size:
            factors[sample_index] = float(np.median(ratios))
    valid_factors = np.isfinite(factors) & (factors > 0)
    if np.any(valid_factors):
        factors[valid_factors] /= float(np.exp(np.mean(np.log(factors[valid_factors]))))
    return factors


def _distribution_metrics(values):
    """Summarize normalized counts and log2-normalized values across samples."""
    logged = values.copy()
    with np.errstate(invalid='ignore', divide='ignore'):
        np.log2(logged + 1.0, out=logged)
    with warnings.catch_warnings(), np.errstate(invalid='ignore', divide='ignore'):
        warnings.simplefilter('ignore', RuntimeWarning)
        median = np.nanmedian(logged, axis=0)
        mean = np.nanmean(values, axis=0)
        std = np.nanstd(values, axis=0)
        q25, q75 = np.nanpercentile(logged, [25, 75], axis=0)
        deviations = np.abs(logged - median[None, :])
        mad = np.nanmedian(deviations, axis=0)
        cv = np.divide(std, mean, out=np.full_like(std, np.nan), where=mean > 0)
        median_normalized = np.nanmedian(values, axis=0)
        log_sd = np.nanstd(logged, axis=0)
    return {'mean_normalized_count': mean, 'median_normalized_count': median_normalized,
            'normalized_count_sd': std,
            'log2_normalized_count_sd': log_sd, 'coefficient_variation': cv,
            'interquartile_range': q75 - q25, 'median_absolute_deviation': mad}


def _interval_stats(counts, totals, size_factors, low_count_threshold):
    valid_samples = totals > 0
    zero_fraction = np.mean(counts == 0, axis=0)
    low_fraction = np.mean(counts < low_count_threshold, axis=0)
    methods = {}
    for method in ('library_size', 'median_ratio'):
        normalized = counts.copy()
        if method == 'library_size':
            scales = np.divide(1_000_000.0, totals, out=np.full_like(totals, np.nan), where=valid_samples)
        else:
            scales = np.divide(1.0, size_factors, out=np.full_like(size_factors, np.nan),
                               where=np.isfinite(size_factors) & (size_factors > 0))
        normalized *= scales[:, None].astype(np.float32)
        methods[method] = _distribution_metrics(normalized)
    return zero_fraction, low_fraction, methods


def _normalize_interval_coverage(counts, intervals, reference_intervals=None):
    """Normalize original read counts by per-sample median coverage per base."""
    counts = np.asarray(counts, dtype=np.float64)
    lengths = np.asarray([end - start + 1 for _contig, start, end in intervals], dtype=np.float64)
    if counts.ndim != 2 or counts.shape[1] != len(intervals):
        raise DiagnosticsUnavailable('La matriz de recuentos no coincide con las coordenadas de intervalos.')
    if np.any(lengths <= 0):
        raise DiagnosticsUnavailable('Hay intervalos con longitud no positiva.')
    coverage = counts / lengths[None, :]
    reference_intervals = set(reference_intervals if reference_intervals is not None else intervals)
    if not reference_intervals.issubset(set(intervals)):
        raise DiagnosticsUnavailable('El conjunto de intervalos de referencia no coincide con CollectReadCounts.')
    reference_mask = np.asarray([interval in reference_intervals for interval in intervals], dtype=bool)
    if not np.any(reference_mask):
        raise DiagnosticsUnavailable('El conjunto de intervalos de referencia está vacío o no coincide con CollectReadCounts.')
    factors = np.full(counts.shape[0], np.nan, dtype=np.float64)
    for sample_index, values in enumerate(coverage[:, reference_mask]):
        values = values[np.isfinite(values)]
        if values.size:
            factors[sample_index] = float(np.median(values))
    normalized = np.divide(coverage, factors[:, None], out=np.full_like(coverage, np.nan),
                           where=np.isfinite(factors[:, None]) & (factors[:, None] > 0))
    interval_metrics = []
    for interval_index in range(counts.shape[1]):
        raw_values = counts[:, interval_index]
        valid_raw = raw_values[np.isfinite(raw_values)]
        values = normalized[:, interval_index]
        values = values[np.isfinite(values)]
        median = float(np.median(values)) if values.size else None
        mad = float(np.median(np.abs(values - median))) if values.size and median is not None else None
        interval_metrics.append({
            'median_raw_count': float(np.median(valid_raw)) if valid_raw.size else None,
            'mean_coverage_per_base': float(np.mean(coverage[:, interval_index])),
            'median_coverage_per_base': float(np.median(coverage[:, interval_index])),
            'median_normalized_coverage': median,
            'mad_normalized_coverage': mad,
            'robust_cv_percent': (1.4826 * mad / median * 100.0
                                  if mad is not None and median not in (None, 0) else None),
            'zero_coverage_percentage': (float(np.sum(valid_raw == 0) / valid_raw.size * 100.0)
                                         if valid_raw.size else None),
            'sample_count': int(valid_raw.size),
        })
    cohort_medians = [row['median_normalized_coverage'] for row in interval_metrics
                      if row['median_normalized_coverage'] is not None]
    cohort_median = float(np.median(cohort_medians)) if cohort_medians else None
    for row in interval_metrics:
        row['relative_median_coverage'] = (row['median_normalized_coverage'] / cohort_median
                                            if cohort_median not in (None, 0)
                                            and row['median_normalized_coverage'] is not None else None)
    return coverage, normalized, factors, interval_metrics


def _json_safe(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_safe(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return _json_number(value)
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    return value


def _source_signature(output, params):
    paths = [Path(params.get('intervals', ''))]
    if params.get('bed_path'):
        paths.append(Path(params['bed_path']))
    if params.get('annotated_intervals'):
        paths.append(Path(params['annotated_intervals']))
    if params.get('intervals'):
        paths.append(Path(params['intervals']).parent / 'annotated_intervals.tsv')
    paths += list(Path(output).rglob('*.tsv'))
    paths += list(Path(output).rglob('*.json'))
    fingerprint = [(str(path), path.stat().st_size, path.stat().st_mtime_ns)
                   for path in paths if path.is_file()]
    raw = json.dumps({'version': DIAGNOSTICS_VERSION, 'samples': params.get('samples'),
                      'coverage_normalization_scope': params.get('coverage_normalization_scope'),
                      'intervals_sha256': params.get('intervals_sha256'), 'files': fingerprint},
                     sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(_json_safe(data), ensure_ascii=False, separators=(',', ':')),
                         encoding='utf-8')
    temporary.replace(path)


def _contig_sort_key(contig):
    value = re.sub(r'^chr', '', str(contig), flags=re.IGNORECASE)
    if value.isdigit():
        return (0, int(value))
    special = {'X': 23, 'Y': 24, 'M': 25, 'MT': 25}
    if value.upper() in special:
        return (0, special[value.upper()])
    return (1, value)


def _hierarchical_order(matrix, labels):
    """Average-linkage leaf order; rows lacking all measurements stay at the end."""
    values = np.asarray(matrix, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] != len(labels):
        return list(labels)
    complete = np.any(np.isfinite(values), axis=1)
    indexes = np.flatnonzero(complete)
    if indexes.size < 2:
        return [labels[i] for i in indexes] + [labels[i] for i in np.flatnonzero(~complete)]
    block = values[indexes].copy()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        medians = np.nanmedian(block, axis=0)
    medians[~np.isfinite(medians)] = 0.0
    missing = ~np.isfinite(block)
    block[missing] = np.broadcast_to(medians, block.shape)[missing]
    distances = pdist(block, metric='euclidean')
    if not np.all(np.isfinite(distances)) or not np.any(distances):
        order = np.arange(indexes.size)
    else:
        order = leaves_list(linkage(distances, method='average'))
    clustered = [labels[indexes[i]] for i in order]
    clustered.extend(labels[i] for i in np.flatnonzero(~complete))
    return clustered


def _read_configuration(output):
    result = {}
    for name in ('denoising_config.json', 'calling_config.json'):
        path = Path(output) / 'caller' / 'cohort-model' / name
        if path.is_file():
            try:
                result[name.removesuffix('_config.json')] = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                result[name.removesuffix('_config.json')] = None
    version_path = Path(output) / 'caller' / 'cohort-model' / 'gcnvkernel_version.json'
    if version_path.is_file():
        try:
            result['gcnvkernel_version'] = json.loads(version_path.read_text(encoding='utf-8')).get('version')
        except (OSError, json.JSONDecodeError):
            result['gcnvkernel_version'] = None
    return result


def _caller_execution_parameters(params, output):
    """Describe flags actually assembled by the cohort caller module."""
    options = params.get('gcnv_options') or {}
    selected_intervals = (Path(output) / 'filtered.interval_list'
                          if (params.get('filter_options') or {}).get('enabled')
                          else params.get('intervals'))
    descriptions = {
        '--run-mode': 'Entrena un modelo conjunto para todas las muestras de la cohorte.',
        '--input': 'Read counts HDF5 de una muestra; el orden fija su sample-index.',
        '-L': 'Intervalos procesados por GermlineCNVCaller.',
        '--interval-merging-rule': 'Regla para combinar intervalos solapados.',
        '--contig-ploidy-calls': 'Llamadas de ploidía producidas por DetermineGermlineContigPloidy.',
        '-O': 'Directorio de salida del caller.', '--output-prefix': 'Prefijo de los artefactos del caller.',
        '--max-copy-number': 'Estado máximo de número de copias modelado.',
        '--max-bias-factors': 'Número máximo de factores latentes para modelar sesgos técnicos.',
        '--max-training-epochs': 'Límite superior de épocas de entrenamiento.',
        '--min-training-epochs': 'Mínimo de épocas de entrenamiento.',
        '--max-calling-iters': 'Límite de iteraciones de inferencia de llamadas.',
        '--p-active': 'Prior de probabilidad de que un intervalo esté activo para una CNV.',
        '--p-alt': 'Prior de probabilidad para estados de copia alternativos.',
        '--convergence-snr-averaging-window': 'Ventana de promediado para el criterio SNR.',
        '--convergence-snr-countdown-window': 'Ventana de cuenta atrás del criterio SNR.',
        '--convergence-snr-trigger-threshold': 'Umbral que activa el criterio de convergencia SNR.',
        '--verbosity': 'Nivel de detalle del log GATK.',
    }
    rows = [
        {'parameter': '--run-mode', 'value': 'COHORT', 'default': 'No consultado',
         'description': descriptions['--run-mode'], 'source': 'Comando del workflow'},
        *[{'parameter': '--input', 'value': f"counts/{Path(sample.get('counts', '')).name}", 'default': 'No consultado',
           'description': descriptions['--input'], 'source': 'Argumento staged por Nextflow; archivo del snapshot'}
          for sample in params.get('samples', [])],
        {'parameter': '-L', 'value': ('filtered.interval_list' if (params.get('filter_options') or {}).get('enabled')
                                      else Path(str(selected_intervals)).name if selected_intervals else None),
         'default': 'No consultado', 'description': descriptions['-L'], 'source': 'Workflow / FilterIntervals'},
        {'parameter': '--interval-merging-rule', 'value': 'OVERLAPPING_ONLY', 'default': 'No consultado',
         'description': descriptions['--interval-merging-rule'], 'source': 'Comando del workflow'},
        {'parameter': '--contig-ploidy-calls', 'value': 'ploidy/cohort-calls', 'default': 'No consultado',
         'description': descriptions['--contig-ploidy-calls'], 'source': 'Output de ploidía'},
        {'parameter': '-O', 'value': 'caller', 'default': 'No consultado',
         'description': descriptions['-O'], 'source': 'Comando del workflow'},
        {'parameter': '--output-prefix', 'value': 'cohort', 'default': 'No consultado',
         'description': descriptions['--output-prefix'], 'source': 'Comando del workflow'},
    ]
    flag_names = {
        'max_copy_number': '--max-copy-number', 'max_bias_factors': '--max-bias-factors',
        'max_training_epochs': '--max-training-epochs', 'min_training_epochs': '--min-training-epochs',
        'max_calling_iters': '--max-calling-iters', 'p_active': '--p-active', 'p_alt': '--p-alt',
        'convergence_snr_averaging_window': '--convergence-snr-averaging-window',
        'convergence_snr_countdown_window': '--convergence-snr-countdown-window',
        'convergence_snr_trigger_threshold': '--convergence-snr-trigger-threshold', 'verbosity': '--verbosity',
    }
    for key, flag in flag_names.items():
        rows.append({'parameter': flag, 'value': options.get(key), 'default': 'No consultado',
                     'description': descriptions[flag], 'source': 'Parámetros guardados en el snapshot'})
    return rows


def _histogram(values, bins=24):
    numeric_values = []
    for value in values:
        number = _number(value)
        if number is not None:
            numeric_values.append(number)
    values = np.asarray(numeric_values, dtype=np.float64)
    if not values.size:
        return []
    counts, edges = np.histogram(values, bins=min(bins, max(1, int(np.sqrt(values.size)))))
    return [{'start': float(edges[i]), 'end': float(edges[i + 1]), 'count': int(counts[i])}
            for i in range(len(counts))]


def _write_memmap(path, data, dtype):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    arr = np.lib.format.open_memmap(temp, mode='w+', dtype=dtype, shape=data.shape)
    arr[:] = data
    arr.flush()
    del arr
    os.replace(temp, path)


def build_cohort_diagnostics(output, params, *, parameters_sha256=None):
    """Build or load cached diagnostics from exact GATK and CollectReadCounts artifacts."""
    output = Path(output)
    cache = output.parent / 'diagnostics_cache'
    cache.mkdir(parents=True, exist_ok=True)
    signature = _source_signature(output, params)
    summary_path = cache / 'summary.json'
    interval_path = cache / 'intervals.jsonl.gz'
    entropy_path = cache / 'posterior_entropy.npy'
    states_path = cache / 'map_copy_number.npy'
    try:
        current = json.loads(summary_path.read_text(encoding='utf-8'))
        if (current.get('diagnostics_version') == DIAGNOSTICS_VERSION
                and current.get('source_signature') == signature
                and interval_path.is_file() and entropy_path.is_file() and states_path.is_file()
                and (cache / 'max_posterior.npy').is_file()
                and (cache / 'baseline_copy_number.npy').is_file()
                and (cache / 'normalized_coverage.npy').is_file()
                and (cache / 'coverage_per_base.npy').is_file()
                and (cache / 'denoised_log2_copy_ratio.npy').is_file()):
            return current
    except (OSError, json.JSONDecodeError):
        pass

    snapshots, original_intervals, counts = _read_all_counts(params)
    if not original_intervals:
        raise DiagnosticsUnavailable('No hay intervalos en los HDF5 de read counts.')
    source_interval_path = Path(params.get('intervals') or '')
    original_interval_source_available = source_interval_path.is_file()
    if original_interval_source_available:
        declared_original = _intervals(source_interval_path)
        if declared_original != original_intervals:
            raise DiagnosticsUnavailable('El interval list original no coincide en coordenadas y orden con CollectReadCounts; no se unirán métricas.')
    original_index = {interval: index for index, interval in enumerate(original_intervals)}
    filtered = _read_filter_intervals(output / 'filtered.interval_list')
    if filtered is None:
        filtered = _read_filter_intervals(output / 'caller' / 'cohort-model' / 'interval_list.tsv')
    filter_enabled = bool((params.get('filter_options') or {}).get('enabled'))
    if filtered is None:
        if filter_enabled:
            raise DiagnosticsUnavailable('FilterIntervals está habilitado, pero falta filtered.interval_list.')
        filtered = list(original_intervals)
    retained_set, filter_summary = _filter_interval_membership(original_intervals, filtered)
    filtered_index = {interval: index for index, interval in enumerate(filtered)}
    annotation_candidates = [Path(params['annotated_intervals']) if params.get('annotated_intervals') else None,
                             Path(params.get('intervals', '')).parent / 'annotated_intervals.tsv',
                             *sorted(output.rglob('*annotated*interval*.tsv'))]
    annotation_path = next((path for path in annotation_candidates if path and path.is_file()), None)
    annotations = _read_interval_annotations(annotation_path) if annotation_path else {}
    bed_features = _read_bed_feature_annotations(params.get('bed_path'), original_intervals)
    gene_names = _read_bed_gene_names(params.get('bed_path'), original_intervals)
    if annotations and set(annotations) != set(original_index):
        raise DiagnosticsUnavailable('AnnotateIntervals y CollectReadCounts no cubren el mismo conjunto de coordenadas; no se unirán las anotaciones.')
    normalization_scope = (params.get('coverage_normalization_scope') or
                           ('retained' if filter_enabled else 'all'))
    normalization_intervals = retained_set if normalization_scope == 'retained' else set(original_intervals)
    coverage_per_base, normalized_coverage, sample_coverage_factors, coverage_metrics = (
        _normalize_interval_coverage(counts, original_intervals, normalization_intervals))
    denoised_log2, denoised_samples_available, denoised_warnings = _read_postprocessed_copy_ratios(
        output, original_intervals, [str(row.get('sample_id')) for row in snapshots], filtered)
    warnings = list(denoised_warnings)
    if not original_interval_source_available:
        warnings.append('Falta el interval list original previo a FilterIntervals; no se informa el porcentaje retenido/filtrado.')
    totals = np.sum(counts, axis=1, dtype=np.float64)
    size_factors = _size_factors_median_ratio(counts)
    filter_options = params.get('filter_options') or {}
    low_count_threshold = int(filter_options.get('low_count_filter_count_threshold', 10))
    zero_fraction, low_fraction, interval_metrics = _interval_stats(
        counts, totals, size_factors, low_count_threshold)

    caller_dirs = _sample_output_dirs(output / 'caller', 'log_q_c_tc.tsv')
    model_variance_path = output / 'caller' / 'cohort-model' / 'mu_psi_t_log__.tsv'
    model_variance_log = _read_gatk_vector(model_variance_path) if model_variance_path.is_file() else np.asarray([])
    ploidy_dirs = _sample_output_dirs(output / 'ploidy', 'contig_ploidy.tsv')
    snapshots_by_id = {str(row.get('sample_id')): row for row in snapshots}
    sample_names = [str(row.get('sample_id')) for row in snapshots]
    model_count = len(filtered)
    entropy_matrix = np.full((len(sample_names), model_count), np.nan, dtype=np.float32)
    map_matrix = np.full((len(sample_names), model_count), 255, dtype=np.uint8)
    baseline_matrix = np.full((len(sample_names), model_count), 255, dtype=np.uint8)
    max_posterior_matrix = np.full((len(sample_names), model_count), np.nan, dtype=np.float32)
    ploidy_by_sample = {}
    depth_by_sample = {}
    unexplained_log = {}
    unexplained_std_log = {}
    bias_by_sample = {}
    posterior_state_labels = None
    warnings = list(denoised_warnings)
    model_variance_valid = model_variance_log.size == model_count and model_count > 0
    if model_variance_path.is_file() and not model_variance_valid:
        warnings.append(f'{model_variance_path.name}: {model_variance_log.size} filas; se esperaban {model_count}; métrica omitida.')
        model_variance_log = np.asarray([])
    for sample_index, sample_id in enumerate(sample_names):
        caller_dir = caller_dirs.get(sample_id)
        if caller_dir:
            log_q_path = caller_dir / 'log_q_c_tc.tsv'
            states, log_probabilities = _read_posterior(log_q_path)
            if len(log_probabilities) != model_count:
                warnings.append(f'{sample_id}: log_q_c_tc.tsv tiene {len(log_probabilities)} filas; se esperaban {model_count}.')
            else:
                ent, norm_ent, max_post, map_index = posterior_entropy(log_probabilities)
                entropy_matrix[sample_index] = norm_ent.astype(np.float32)
                max_posterior_matrix[sample_index] = max_post.astype(np.float32)
                posterior_state_labels = states
                baseline_path = caller_dir / 'baseline_copy_number_t.tsv'
                baseline = _read_gatk_vector(baseline_path) if baseline_path.is_file() else np.full(model_count, np.nan)
                if baseline.size == model_count:
                    valid_baseline = np.isfinite(baseline) & (baseline >= 0) & (baseline <= 254)
                    baseline_matrix[sample_index, valid_baseline] = baseline[valid_baseline].astype(np.uint8)
                map_values = np.asarray([states[index] if index >= 0 else 255 for index in map_index], dtype=np.int16)
                valid_map = (map_values >= 0) & (map_values <= 254)
                map_matrix[sample_index, valid_map] = map_values[valid_map].astype(np.uint8)
            psi_path = caller_dir / 'mu_psi_s_log__.tsv'
            if psi_path.is_file():
                psi = _read_gatk_vector(psi_path)
                unexplained_log[sample_id] = float(psi[0]) if psi.size and np.isfinite(psi[0]) else None
            std_path = caller_dir / 'std_psi_s_log__.tsv'
            if std_path.is_file():
                std = _read_gatk_vector(std_path)
                unexplained_std_log[sample_id] = float(std[0]) if std.size and np.isfinite(std[0]) else None
            factor_path = caller_dir / 'mu_z_su.tsv'
            if factor_path.is_file():
                bias_by_sample[sample_id] = _read_gatk_vector(factor_path).tolist()
        ploidy_dir = ploidy_dirs.get(sample_id)
        if ploidy_dir:
            ploidy_rows = _read_sample_table(ploidy_dir / 'contig_ploidy.tsv')
            normalized_ploidy = {}
            for row in ploidy_rows:
                contig = row.get('CONTIG')
                if contig:
                    normalized_ploidy[contig] = {'ploidy': _number(row.get('PLOIDY')),
                                                 'gq': _number(row.get('PLOIDY_GQ'))}
            ploidy_by_sample[sample_id] = normalized_ploidy
            depth_rows = _read_sample_table(ploidy_dir / 'global_read_depth.tsv')
            if depth_rows:
                depth_by_sample[sample_id] = {
                    'global_read_depth': _number(depth_rows[0].get('GLOBAL_READ_DEPTH')),
                    'average_ploidy': _number(depth_rows[0].get('AVERAGE_PLOIDY')),
                }

    _write_memmap(entropy_path, entropy_matrix, np.float32)
    _write_memmap(states_path, map_matrix, np.uint8)
    _write_memmap(cache / 'baseline_copy_number.npy', baseline_matrix, np.uint8)
    _write_memmap(cache / 'max_posterior.npy', max_posterior_matrix, np.float32)
    _write_memmap(cache / 'normalized_coverage.npy', normalized_coverage.astype(np.float32), np.float32)
    _write_memmap(cache / 'coverage_per_base.npy', coverage_per_base.astype(np.float32), np.float32)
    _write_memmap(cache / 'denoised_log2_copy_ratio.npy', denoised_log2, np.float32)

    valid_variance_log = np.asarray([unexplained_log.get(sample_id)
                                     if unexplained_log.get(sample_id) is not None else np.nan
                                     for sample_id in sample_names], dtype=np.float64)
    variance_z = robust_z_scores(valid_variance_log)
    sample_rows = []
    correlations = {}
    qc_path = Path(output).parents[1] / 'samples' / 'project_qc_summary.json'
    if qc_path.is_file():
        try:
            qc = json.loads(qc_path.read_text(encoding='utf-8'))
            correlations = {row.get('sample_id'): row.get('normalized', {}).get('sample_coverage_correlation')
                            for row in qc.get('samples', [])}
        except (OSError, json.JSONDecodeError):
            pass
    contig_set = set()
    for row in ploidy_by_sample.values():
        contig_set.update(row)
    contigs = sorted(contig_set, key=_contig_sort_key)
    low_fraction_by_sample = np.mean(counts < low_count_threshold, axis=1)
    zero_fraction_by_sample = np.mean(counts == 0, axis=1)
    for i, sample_id in enumerate(sample_names):
        snapshot = snapshots_by_id.get(sample_id, {})
        log_variance = unexplained_log.get(sample_id)
        variance = math.exp(log_variance) if log_variance is not None and log_variance < 700 else None
        sample_entropy = entropy_matrix[i]
        finite_entropy = sample_entropy[np.isfinite(sample_entropy)]
        sample_gq = [ploidy_by_sample.get(sample_id, {}).get(contig, {}).get('gq') for contig in contigs]
        sample_gq = [value for value in sample_gq if value is not None]
        sample_rows.append({
            'sample_id': sample_id, 'batch': snapshot.get('batch') or 'Sin lote',
            'kit_name': snapshot.get('kit_name') or None,
            'protocol': snapshot.get('protocol') or None,
            'platform': snapshot.get('platform') or None,
            'library_size_counts': int(totals[i]),
            'global_read_depth': (depth_by_sample.get(sample_id) or {}).get('global_read_depth'),
            'average_ploidy': (depth_by_sample.get(sample_id) or {}).get('average_ploidy'),
            'median_ploidy_gq': float(np.median(sample_gq)) if sample_gq else None,
            'unexplained_variance_log_psi_native': log_variance,
            'unexplained_variance_exp_log_psi_derived': variance,
            'unexplained_variance_log_psi_sd_native': unexplained_std_log.get(sample_id),
            'unexplained_variance_robust_z': _json_number(variance_z[i]),
            'potential_outlier': bool(np.isfinite(variance_z[i]) and variance_z[i] >= DEFAULT_OUTLIER_Z),
            'outlier_threshold': DEFAULT_OUTLIER_Z,
            'zero_count_fraction': float(zero_fraction_by_sample[i]),
            'low_count_fraction': float(low_fraction_by_sample[i]),
            'median_posterior_entropy_normalized': float(np.median(finite_entropy)) if finite_entropy.size else None,
            'posterior_entropy_interval_count': int(finite_entropy.size),
            'sample_to_cohort_correlation_qc': _number(correlations.get(sample_id)),
            'bias_factor_loadings': [_json_number(value) for value in bias_by_sample.get(sample_id, [])],
        })

    model_intervals = [None] * len(filtered)
    for interval, index in filtered_index.items():
        model_intervals[index] = interval
    entropy_by_original = np.full(len(original_intervals), np.nan, dtype=np.float32)
    entropy_q90_by_original = np.full(len(original_intervals), np.nan, dtype=np.float32)
    entropy_sample_count_by_original = np.zeros(len(original_intervals), dtype=np.uint32)
    max_posterior_by_original = np.full(len(original_intervals), np.nan, dtype=np.float32)
    mean_ploidy_quality_by_original = np.full(len(original_intervals), np.nan, dtype=np.float32)
    ploidy_quality_sample_count_by_original = np.zeros(len(original_intervals), dtype=np.uint32)
    event_frequency_by_original = np.full(len(original_intervals), np.nan, dtype=np.float32)
    if model_count:
        for model_index, interval in enumerate(model_intervals):
            original_i = original_index[interval]
            values = entropy_matrix[:, model_index]
            values = values[np.isfinite(values)]
            if values.size:
                entropy_by_original[original_i] = float(np.median(values))
                entropy_q90_by_original[original_i] = float(np.percentile(values, 90))
                entropy_sample_count_by_original[original_i] = values.size
                max_values = max_posterior_matrix[:, model_index]
                max_values = max_values[np.isfinite(max_values)]
                max_posterior_by_original[original_i] = float(np.median(max_values)) if max_values.size else np.nan
                if max_values.size:
                    mean_ploidy_quality_by_original[original_i] = float(np.mean(max_values))
                    ploidy_quality_sample_count_by_original[original_i] = max_values.size
                baseline_values = baseline_matrix[:, model_index]
                map_values = map_matrix[:, model_index]
                valid = (baseline_values != 255) & (map_values != 255)
                if np.any(valid):
                    event_frequency_by_original[original_i] = float(np.mean(map_values[valid] != baseline_values[valid]))

    retained_variability = np.asarray([
        interval_metrics['library_size']['median_absolute_deviation'][original_index[interval]]
        for interval in filtered
    ], dtype=np.float64)
    retained_variability_z = robust_z_scores(retained_variability)
    variability_z_by_original = np.full(len(original_intervals), np.nan, dtype=np.float64)
    for model_index, interval in enumerate(filtered):
        variability_z_by_original[original_index[interval]] = retained_variability_z[model_index]

    rows_for_file = []
    gc_values = []
    interval_metrics_by_method = {}
    for method, columns in interval_metrics.items():
        interval_metrics_by_method[method] = columns
    for i, interval in enumerate(original_intervals):
        contig, start, end = interval
        annotation = annotations.get(interval, {})
        gc = annotation.get('gc_content')
        if gc is not None:
            gc_values.append(gc)
        retained = interval in retained_set
        model_index = filtered_index.get(interval)
        coverage_metric = coverage_metrics[i]
        normalized_median = coverage_metric['median_normalized_coverage']
        normalized_mad = coverage_metric['mad_normalized_coverage']
        robust_cv = coverage_metric['robust_cv_percent']
        denoised_values = denoised_log2[:, i]
        denoised_values = denoised_values[np.isfinite(denoised_values)]
        denoised_median = float(np.median(denoised_values)) if denoised_values.size else None
        denoised_mad = (float(np.median(np.abs(denoised_values - denoised_median)))
                        if denoised_values.size and denoised_median is not None else None)
        raw_values = counts[:, i]
        raw_values = raw_values[np.isfinite(raw_values)]
        interval_variance_log = (float(model_variance_log[model_index])
                                 if model_index is not None and model_index < model_variance_log.size
                                 and np.isfinite(model_variance_log[model_index]) else None)
        row = {
            'interval_index': i, 'model_index': model_index,
            'interval_id': f'{contig}:{start}-{end}',
            'contig': contig, 'start': start, 'end': end, 'length': end - start + 1,
            'gc_content': gc, 'mappability': annotation.get('mappability'),
            'segmental_duplication_content': annotation.get('segmental_duplication'),
            'gene': annotation.get('gene') or bed_features.get(interval, {}).get('gene') or gene_names.get(interval),
            'transcript_id': bed_features.get(interval, {}).get('transcript_id'),
            'exon_number': bed_features.get(interval, {}).get('exon_number'), 'retained': retained,
            'primary_filter_reason': None if retained else 'filtered_by_gatk_filter_intervals',
            'filter_reasons': [] if retained else ['filtered_by_gatk_filter_intervals'],
            'filter_reason_detail': None if retained else 'unknown_not_emitted_by_gatk',
            'mean_count_raw_descriptive': _json_number(np.mean(raw_values)) if raw_values.size else None,
            'median_count_raw_descriptive': _json_number(coverage_metric['median_raw_count']),
            'median_coverage_per_base': _json_number(coverage_metric['median_coverage_per_base']),
            'mean_coverage_per_base': _json_number(coverage_metric['mean_coverage_per_base']),
            'median_normalized_coverage': _json_number(normalized_median),
            'relative_median_coverage': None,
            'mad_normalized_coverage': _json_number(normalized_mad),
            'robust_cv_percent': _json_number(robust_cv),
            'zero_coverage_percentage': coverage_metric['zero_coverage_percentage'],
            'coverage_sample_count': coverage_metric['sample_count'],
            'median_denoised_copy_ratio_log2': _json_number(denoised_median),
            'mad_denoised_copy_ratio_log2': _json_number(denoised_mad),
            'robust_sigma_log2': _json_number(1.4826 * denoised_mad if denoised_mad is not None else None),
            'denoised_sample_count': int(denoised_values.size),
            'zero_count_fraction': float(zero_fraction[i]),
            'low_count_fraction': float(low_fraction[i]),
            'posterior_entropy_median_normalized': _json_number(entropy_by_original[i]),
            'posterior_entropy_q90_normalized': _json_number(entropy_q90_by_original[i]),
            'posterior_sample_count': int(entropy_sample_count_by_original[i]),
            'model_unexplained_variance_log_psi_native': interval_variance_log,
            'model_unexplained_variance_exp_log_psi_derived': (
                math.exp(interval_variance_log) if interval_variance_log is not None and interval_variance_log < 700 else None),
            'maximum_posterior_median': _json_number(max_posterior_by_original[i]),
            'mean_ploidy_quality': _json_number(mean_ploidy_quality_by_original[i]),
            'ploidy_quality_sample_count': int(ploidy_quality_sample_count_by_original[i]),
            'nonreference_map_frequency': _json_number(event_frequency_by_original[i]),
            'normalized_variability_robust_z': _json_number(variability_z_by_original[i]),
            'potential_interval_outlier': bool(np.isfinite(variability_z_by_original[i])
                                                and variability_z_by_original[i] >= DEFAULT_OUTLIER_Z),
        }
        row['normalized'] = {}
        for method, columns in interval_metrics_by_method.items():
            row['normalized'][method] = {key: _json_number(values[i]) for key, values in columns.items()}
        rows_for_file.append(row)

        row['relative_median_coverage'] = coverage_metric['relative_median_coverage']

    with gzip.open(interval_path, 'wt', encoding='utf-8', compresslevel=5) as handle:
        for row in rows_for_file:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')

    excluded_count = filter_summary['filtered']
    reported_original_count = len(original_intervals) if original_interval_source_available else None
    reported_excluded_count = excluded_count if original_interval_source_available else None
    reason_summary = ([{'code': 'filtered_by_gatk_filter_intervals', 'label': 'FilterIntervals · criterio individual no disponible',
                        'count': excluded_count, 'specific_reason_available': False}] if excluded_count else [])
    variance_values = [row['unexplained_variance_exp_log_psi_derived'] for row in sample_rows
                       if row['unexplained_variance_exp_log_psi_derived'] is not None]
    entropy_values = entropy_matrix[np.isfinite(entropy_matrix)]
    samples_flagged = sum(1 for row in sample_rows if row['potential_outlier'])
    interval_candidates = sorted(
        (row for row in rows_for_file if row['retained']),
        key=lambda row: row['normalized']['library_size'].get('median_absolute_deviation') or -1,
        reverse=True)
    entropy_candidates = sorted(
        (row for row in rows_for_file if row['retained'] and row['posterior_entropy_median_normalized'] is not None),
        key=lambda row: row['posterior_entropy_median_normalized'], reverse=True)
    top_samples = sorted(sample_rows, key=lambda row: row['unexplained_variance_robust_z']
                         if row['unexplained_variance_robust_z'] is not None else -math.inf, reverse=True)[:20]
    config = _read_configuration(output)
    gatk_version_text = (output / 'gatk_version.txt').read_text(encoding='utf-8', errors='replace') \
        if (output / 'gatk_version.txt').is_file() else ''
    version_match = re.search(r'\b(?:GATK\s+)?v?(\d+\.\d+(?:\.\d+){0,2})\b', gatk_version_text, re.IGNORECASE)
    effective_gatk_version = version_match.group(1) if version_match else params.get('gatk_version')
    model_shards = sorted(path for path in (output / 'caller').glob('**/*model') if path.is_dir())
    model_shard_count = len(model_shards) or (1 if (output / 'caller' / 'cohort-model').is_dir() else 0)
    sample_labels = sample_names
    ploidy_matrix = [[ploidy_by_sample.get(sample, {}).get(contig, {}).get('gq') for contig in contigs]
                     for sample in sample_labels]
    copy_number_matrix = [[ploidy_by_sample.get(sample, {}).get(contig, {}).get('ploidy') for contig in contigs]
                          for sample in sample_labels]
    factor_count = max((len(bias_by_sample.get(sample, [])) for sample in sample_names), default=0)
    factor_labels = [f'Factor {i + 1}' for i in range(factor_count)]
    bias_matrix = [[_json_number(value) for value in bias_by_sample.get(sample, [])[:factor_count]]
                   + [None] * max(0, factor_count - len(bias_by_sample.get(sample, [])))
                   for sample in sample_names]
    bias_cluster_order = _hierarchical_order(bias_matrix, sample_names)
    factor_cluster_order = _hierarchical_order(np.asarray(bias_matrix, dtype=object).T.tolist(), factor_labels) if factor_count else []
    ploidy_cluster_order = _hierarchical_order(ploidy_matrix, sample_labels)
    tracking_root = output / 'caller' / 'cohort-tracking'
    warmup = _read_elbo(tracking_root / 'warm_up_elbo_history.tsv')
    main_elbo = _read_elbo(tracking_root / 'main_elbo_history.tsv')
    summary = {
        'diagnostics_version': DIAGNOSTICS_VERSION, 'source_signature': signature,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'provenance': {
            'model_run_id': params.get('model_run_id'), 'gatk_version': effective_gatk_version,
            'reference_id': params.get('reference_id'), 'reference_bundle_version': params.get('reference_bundle_version'),
            'input_interval_file': str(params.get('intervals')), 'input_sample_count': len(sample_names),
            'reference_fasta': params.get('reference_fasta'), 'reference_fasta_sha256': params.get('reference_fasta_sha256'),
            'intervals_sha256': params.get('intervals_sha256'),
            'container_image': params.get('container_image'), 'container_digest': params.get('container_digest'),
            'pipeline_version': params.get('pipeline_version'), 'pipeline_git_commit': params.get('pipeline_git_commit'),
            'created_at': params.get('model_created_at'), 'modified_at': params.get('model_modified_at'),
            'coverage_normalization_scope': normalization_scope,
            'coverage_normalization_method': ('Por muestra: mediana de read counts / longitud; factor = mediana de cobertura por base en '
                                              + ('intervalos retenidos' if normalization_scope == 'retained' else 'todos los intervalos')
                                              + '; cobertura normalizada = cobertura por base / factor.'),
            'calculation_method': 'Las métricas de cobertura cruda usan CollectReadCounts. Cobertura por base = recuento/longitud; el factor por muestra es la mediana de cobertura por base en el conjunto de referencia configurado. La dispersión entre muestras usa mediana y MAD; se mantiene separada de los outputs denoised GATK.',
            'native_metrics': ['GLOBAL_READ_DEPTH', 'AVERAGE_PLOIDY', 'PLOIDY_GQ', 'sample mu_psi_s_log__', 'interval mu_psi_t_log__', 'log_q_c_tc copy-number posterior', 'mu_z_su bias factor loadings'],
            'derived_metrics': ['Cobertura por base y normalización mediana por muestra', 'mediana relativa, MAD y CV robusto de cobertura normalizada', 'entropía posterior', 'frecuencia MAP no basal', 'exp(log psi) de varianza por intervalo'],
            'low_count_threshold': low_count_threshold,
            'filter_reason_note': 'GATK output supplies retained interval membership but no interval-level reason in this run; the UI reports the general filter and marks the specific reason unavailable.',
            'outlier_threshold': DEFAULT_OUTLIER_Z, 'outlier_threshold_note': 'Exploratory robust z threshold; configurable in the dashboard, not a validated exclusion threshold.',
        },
        'metadata': {
            'gatk_version': effective_gatk_version, 'germline_cnv_caller_version': effective_gatk_version,
            'gcnvkernel_version': config.get('gcnvkernel_version'),
            'reference_id': params.get('reference_id'), 'reference_bundle_version': params.get('reference_bundle_version'),
            'model_mode': (params.get('gcnv_options') or {}).get('run_mode', 'COHORT'),
            'sample_count': len(sample_names), 'original_interval_count': reported_original_count,
            'model_shard_count': model_shard_count,
            'model_created_at': params.get('model_created_at'), 'model_modified_at': params.get('model_modified_at'),
            'python_version': config.get('python_version'),
            'container_image': params.get('container_image'), 'container_digest': params.get('container_digest'),
            'reference_fasta': params.get('reference_fasta'), 'reference_fasta_sha256': params.get('reference_fasta_sha256'),
            'intervals_sha256': params.get('intervals_sha256'), 'pipeline_version': params.get('pipeline_version'),
            'pipeline_git_commit': params.get('pipeline_git_commit'),
            'retained_interval_count': len(filtered), 'excluded_interval_count': reported_excluded_count,
            'retained_percentage': (len(filtered) / len(original_intervals) * 100) if original_interval_source_available and original_intervals else None,
            'excluded_percentage': (excluded_count / len(original_intervals) * 100) if original_interval_source_available and original_intervals else None,
            'capture_kits': sorted({row.get('kit_name') for row in snapshots if row.get('kit_name')}),
            'sequencing_protocols': sorted({row.get('protocol') for row in snapshots if row.get('protocol')}),
            'sequencing_platforms': sorted({row.get('platform') for row in snapshots if row.get('platform')}),
            'filter_intervals_enabled': filter_enabled,
            'annotation_available': bool(annotations), 'gc_available': any('gc_content' in x for x in annotations.values()),
            'mappability_available': any('mappability' in x for x in annotations.values()),
            'segmental_duplication_available': any('segmental_duplication' in x for x in annotations.values()),
            'posterior_available': bool(caller_dirs), 'bias_factors_available': bool(factor_count),
            'sample_unexplained_variance_available': bool(unexplained_log),
            'interval_unexplained_variance_available': model_variance_valid,
            'tracking_available': bool(warmup['iterations'] or main_elbo['iterations']),
            'annotated_intervals_available': bool(annotations),
            'denoised_copy_ratios_available': bool(denoised_samples_available),
            'denoised_sample_count': denoised_samples_available,
            'coverage_normalization_scope': normalization_scope,
            'training_epochs': None, 'optimization_iterations': main_elbo['iterations'] or None,
            'convergence_status': 'No se informa explícitamente; revisar la curva ELBO' if main_elbo['iterations'] else 'No disponible',
            'configuration': config, 'execution_parameters': _caller_execution_parameters(params, output),
        },
        'summary': {
            'sample_count': len(sample_names), 'original_interval_count': reported_original_count,
            'retained_interval_count': len(filtered), 'excluded_interval_count': reported_excluded_count,
            'retained_percentage': (len(filtered) / len(original_intervals) * 100) if original_interval_source_available and original_intervals else None,
            'excluded_percentage': (excluded_count / len(original_intervals) * 100) if original_interval_source_available and original_intervals else None,
            'potential_outlier_sample_count': samples_flagged,
            'median_sample_unexplained_variance': float(np.nanmedian(variance_values)) if any(v is not None for v in variance_values) else None,
            'posterior_entropy_median_normalized': float(np.median(entropy_values)) if entropy_values.size else None,
            'potential_interval_outlier_count': sum(1 for row in rows_for_file if row['potential_interval_outlier']),
            'median_global_read_depth': float(np.nanmedian([row['global_read_depth'] for row in sample_rows
                if row['global_read_depth'] is not None])) if any(row['global_read_depth'] is not None for row in sample_rows) else None,
            'median_ploidy_gq': float(np.nanmedian([row['median_ploidy_gq'] for row in sample_rows
                if row['median_ploidy_gq'] is not None])) if any(row['median_ploidy_gq'] is not None for row in sample_rows) else None,
            'interval_filter_reasons': reason_summary,
            'unexplained_variance_histogram': _histogram(variance_values),
            'global_read_depth_histogram': _histogram([row['global_read_depth'] for row in sample_rows]),
            'posterior_entropy_histogram': _histogram(entropy_values),
            'top_intervals': interval_candidates[:20], 'top_samples': top_samples,
            'top_entropy_intervals': entropy_candidates[:20],
        },
        'samples': sample_rows,
        'contigs': list(dict.fromkeys(row['contig'] for row in rows_for_file)),
        'cluster_orders': {'ploidy_samples': ploidy_cluster_order,
                           'bias_samples': bias_cluster_order,
                           'bias_factors': factor_cluster_order},
        'ploidy_heatmap': {'samples': sample_labels, 'contigs': contigs, 'gq': ploidy_matrix,
                            'ploidy': copy_number_matrix, 'scale': 'PLOIDY_GQ continuo; ausencia de celda = NA'},
        'interval_ploidy_quality': [
            {'interval_index': row['interval_index'], 'contig': row['contig'], 'start': row['start'],
             'end': row['end'], 'gene': row['gene'], 'transcript_id': row['transcript_id'],
             'exon_number': row['exon_number'], 'retained': row['retained'], 'value': row['mean_ploidy_quality'],
             'sample_count': row['ploidy_quality_sample_count']}
            for row in rows_for_file
        ],
        'interval_depth_mad': [
            {'interval_index': row['interval_index'], 'contig': row['contig'], 'start': row['start'],
             'end': row['end'], 'retained': row['retained'],
             'gene': row['gene'], 'transcript_id': row['transcript_id'], 'exon_number': row['exon_number'],
             'gc_content': row['gc_content'], 'mappability': row['mappability'],
             'median_coverage': row['median_normalized_coverage'], 'variability': row['robust_cv_percent'],
             'mean_depth': row['mean_coverage_per_base'], 'mad': row['mad_normalized_coverage']}
            for row in rows_for_file
        ],
        'bias_factor_heatmap': {'samples': sample_labels, 'factors': factor_labels, 'values': bias_matrix,
                                'scale': 'Divergente, centrada en cero; loadings nativos mu_z_su.tsv'},
        'performance': {
            'convergence_status': 'unknown' if main_elbo['iterations'] else 'unavailable',
            'warm_up': warmup, 'main_elbo': main_elbo,
            'posterior_state_labels': posterior_state_labels or [],
            'posterior_note': 'Entropy measures posterior uncertainty, not the probability that a biological CNV is real. Non-reference MAP frequency is a modeled state summary, not a clinical event rate.',
        },
        'availability': {
            'sample_unexplained_variance': bool(unexplained_log), 'interval_annotations': bool(annotations),
            'posterior_probabilities': bool(caller_dirs), 'training_elbo': bool(warmup['iterations'] or main_elbo['iterations']),
            'segmental_duplication': any('segmental_duplication' in x for x in annotations.values()),
            'denoised_copy_ratios': bool(denoised_samples_available),
            'copy_ratio_residuals': False,
            'copy_ratio_residuals_note': 'No se encontró un output nativo que identifique explícitamente residuales por intervalo.',
            'cnv_postprocessing_vcf': any(output.glob('**/*.vcf*')),
        },
        'warnings': warnings,
    }
    _atomic_json(summary_path, summary)
    return summary


def load_interval_diagnostics(output):
    path = Path(output).parent / 'diagnostics_cache' / 'intervals.jsonl.gz'
    if not path.is_file():
        raise DiagnosticsUnavailable('Las métricas de intervalos aún no están disponibles.')
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        return [json.loads(line) for line in handle if line.strip()]


def coverage_deviation_heatmap(output, diagnostics):
    """Return robust per-interval standardized coverage deviations by sample."""
    rows = load_interval_diagnostics(output)
    cache = Path(output).parent / 'diagnostics_cache'
    matrix = np.load(cache / 'normalized_coverage.npy', mmap_mode='r')
    if matrix.shape[1] != len(rows):
        raise DiagnosticsUnavailable('La matriz de cobertura no coincide con la tabla de intervalos.')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        expected = np.nanmedian(matrix, axis=0)
    differences = np.asarray(matrix, dtype=np.float32) - expected[None, :]
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        mad = np.nanmedian(np.abs(differences), axis=0)
        standard_deviation = np.nanstd(differences, axis=0)
    scale = 1.4826 * mad
    fallback = (~np.isfinite(scale) | (scale <= 0)) & np.isfinite(standard_deviation) & (standard_deviation > 0)
    scale[fallback] = standard_deviation[fallback]
    valid_scale = np.isfinite(scale) & (scale > 0)
    deviations = np.zeros_like(differences, dtype=np.float32)
    np.divide(differences, scale[None, :], out=deviations, where=valid_scale[None, :])
    deviations[~np.isfinite(differences)] = np.nan
    np.clip(deviations, -3, 3, out=deviations)
    return {
        'samples': [row['sample_id'] for row in diagnostics.get('samples', [])],
        'intervals': [{'interval_index': row['interval_index'], 'contig': row['contig'],
                       'start': row['start'], 'end': row['end'], 'gene': row.get('gene'),
                       'transcript_id': row.get('transcript_id'), 'exon_number': row.get('exon_number')}
                      for row in rows],
        'values': _json_safe(deviations),
        'metric': 'per-interval robust standardized deviation (MAD z-score), clipped to [-3, 3]',
    }


def query_intervals(output, *, page=1, page_size=50, contig='', status='', reason='',
                    search='', gc_min=None, gc_max=None, mappability_min=None, mappability_max=None,
                    segmental_duplication_min=None, segmental_duplication_max=None,
                    gene='', median_coverage_min=None, median_coverage_max=None,
                    coverage_variability_min=None, coverage_variability_max=None,
                    metric_min=None, metric_max=None,
                    metric='robust_variability',
                    normalization='library_size', sort='metric', direction='desc', all_rows=False):
    rows = load_interval_diagnostics(output)
    page = max(1, int(page))
    page_size = max(1, int(page_size)) if all_rows else min(MAX_INTERVAL_PAGE_SIZE, max(1, int(page_size)))
    search = str(search or '').strip().lower()
    metric_map = {
        'median_count': 'median_normalized_count', 'mean_count': 'mean_normalized_count',
        'robust_variability': 'median_absolute_deviation', 'sd': 'normalized_count_sd',
        'cv': 'coefficient_variation', 'iqr': 'interquartile_range',
    }
    metric = metric if metric in {'entropy', 'zero_fraction', 'low_count_fraction',
                                   'cnv_frequency', 'max_posterior', 'model_variance'} else metric
    direct_metrics = {'median_count_raw_descriptive', 'median_coverage_per_base',
                      'coverage_sample_count', 'median_normalized_coverage', 'relative_median_coverage',
                      'mad_normalized_coverage', 'robust_cv_percent',
                      'zero_coverage_percentage', 'median_denoised_copy_ratio_log2',
                      'mad_denoised_copy_ratio_log2', 'robust_sigma_log2'}
    def metric_value(row):
        if metric in direct_metrics:
            return row.get(metric)
        direct = {'entropy': 'posterior_entropy_median_normalized', 'zero_fraction': 'zero_count_fraction',
                  'low_count_fraction': 'low_count_fraction', 'cnv_frequency': 'nonreference_map_frequency',
                  'max_posterior': 'maximum_posterior_median',
                  'model_variance': 'model_unexplained_variance_log_psi_native'}
        if metric in direct:
            return row.get(direct[metric])
        key = metric_map.get(metric, metric_map['robust_variability'])
        return (row.get('normalized', {}).get(normalization) or {}).get(key)
    filtered = []
    for row in rows:
        if contig and row['contig'] != contig:
            continue
        if status == 'retained' and not row['retained']:
            continue
        if status == 'excluded' and row['retained']:
            continue
        if gene and gene.lower() not in str(row.get('gene') or '').lower():
            continue
        if reason and reason != (row.get('primary_filter_reason') or 'retained'):
            continue
        gc = row.get('gc_content')
        if gc_min is not None and (gc is None or gc < gc_min):
            continue
        if gc_max is not None and (gc is None or gc > gc_max):
            continue
        mappability = row.get('mappability')
        if mappability_min is not None and (mappability is None or mappability < mappability_min):
            continue
        if mappability_max is not None and (mappability is None or mappability > mappability_max):
            continue
        median_coverage = row.get('median_normalized_coverage')
        if median_coverage_min is not None and (median_coverage is None or median_coverage < median_coverage_min):
            continue
        if median_coverage_max is not None and (median_coverage is None or median_coverage > median_coverage_max):
            continue
        variability = row.get('robust_cv_percent')
        if coverage_variability_min is not None and (variability is None or variability < coverage_variability_min):
            continue
        if coverage_variability_max is not None and (variability is None or variability > coverage_variability_max):
            continue
        segdup = row.get('segmental_duplication_content')
        if segmental_duplication_min is not None and (segdup is None or segdup < segmental_duplication_min):
            continue
        if segmental_duplication_max is not None and (segdup is None or segdup > segmental_duplication_max):
            continue
        if search and search not in f"{row['contig']}:{row['start']}-{row['end']} {row.get('gene') or ''} {row.get('transcript_id') or ''} {row.get('exon_number') or ''}".lower():
            continue
        row = dict(row)
        row['selected_metric_value'] = metric_value(row)
        if metric_min is not None and (row['selected_metric_value'] is None or row['selected_metric_value'] < metric_min):
            continue
        if metric_max is not None and (row['selected_metric_value'] is None or row['selected_metric_value'] > metric_max):
            continue
        filtered.append(row)
    if sort == 'contig':
        filtered.sort(key=lambda row: (_contig_sort_key(row['contig']), row['start']),
                      reverse=direction == 'desc')
    elif sort == 'gene':
        filtered.sort(key=lambda row: str(row.get('gene') or '').casefold(), reverse=direction == 'desc')
    elif sort == 'retained':
        filtered.sort(key=lambda row: bool(row['retained']), reverse=direction == 'desc')
    elif sort in {'start', 'end', 'length', 'gc_content', 'mappability',
                  'segmental_duplication_content', 'coverage_sample_count', 'zero_count_fraction',
                  'low_count_fraction', 'posterior_entropy_median_normalized',
                  'nonreference_map_frequency', 'model_unexplained_variance_log_psi_native',
                  'median_count_raw_descriptive', 'median_coverage_per_base',
                  'median_normalized_coverage', 'relative_median_coverage',
                  'mad_normalized_coverage', 'robust_cv_percent', 'zero_coverage_percentage',
                  'median_denoised_copy_ratio_log2', 'robust_sigma_log2',
                  'median_count', 'robust_variability', 'cv', 'median_normalized_coverage',
                  'relative_median_coverage', 'mad_normalized_coverage', 'robust_cv_percent',
                  'zero_coverage_percentage', 'median_denoised_copy_ratio_log2',
                  'mad_denoised_copy_ratio_log2', 'robust_sigma_log2'}:
        nested = {'median_count': 'median_normalized_count', 'robust_variability': 'median_absolute_deviation',
                  'cv': 'coefficient_variation'}
        def sort_value(row):
            value = ((row.get('normalized', {}).get(normalization) or {}).get(nested[sort])
                     if sort in nested else row.get(sort))
            return (value is not None, value if value is not None else 0)
        filtered.sort(key=sort_value, reverse=direction == 'desc')
    else:
        filtered.sort(key=lambda row: row['selected_metric_value'] is not None and row['selected_metric_value'],
                      reverse=direction == 'desc')
    total = len(filtered)
    start = (page - 1) * page_size
    return {'page': page, 'page_size': page_size, 'total': total,
            'pages': max(1, math.ceil(total / page_size)), 'metric': metric,
            'normalization': normalization, 'rows': filtered[start:start + page_size]}


def interval_detail(output, params, diagnostics, interval_index, selected_samples=None, limit=30):
    rows = load_interval_diagnostics(output)
    if interval_index < 0 or interval_index >= len(rows):
        raise IndexError('Interval index fuera de rango.')
    interval = rows[interval_index]
    selected_samples = set(selected_samples or [])
    all_samples = diagnostics.get('samples', [])
    samples = [row for row in all_samples if not selected_samples or row['sample_id'] in selected_samples]
    cache = Path(output).parent / 'diagnostics_cache'
    entropy = np.load(cache / 'posterior_entropy.npy', mmap_mode='r')
    states = np.load(cache / 'map_copy_number.npy', mmap_mode='r')
    maxima = np.load(cache / 'max_posterior.npy', mmap_mode='r')
    baselines = np.load(cache / 'baseline_copy_number.npy', mmap_mode='r')
    normalized_coverage = np.load(cache / 'normalized_coverage.npy', mmap_mode='r')
    coverage_per_base = np.load(cache / 'coverage_per_base.npy', mmap_mode='r')
    denoised_log2 = np.load(cache / 'denoised_log2_copy_ratio.npy', mmap_mode='r')
    interval_counts = []
    posterior = []
    all_samples = diagnostics.get('samples', [])
    sample_position = {row['sample_id']: i for i, row in enumerate(all_samples)}
    target_values = np.asarray([normalized_coverage[sample_position[row['sample_id']], interval['interval_index']]
                                for row in samples if row['sample_id'] in sample_position], dtype=np.float64)
    target_median = float(np.nanmedian(target_values)) if np.any(np.isfinite(target_values)) else None
    if target_median is not None:
        samples.sort(key=lambda row: abs(float(normalized_coverage[sample_position[row['sample_id']], interval['interval_index']])
                                         - target_median)
                     if np.isfinite(normalized_coverage[sample_position[row['sample_id']], interval['interval_index']])
                     else -1, reverse=True)
    total_measurement_samples = len(samples)
    samples = samples[:max(1, min(int(limit), 50))]
    params_samples = params.get('sample_snapshot') or params.get('samples', [])
    counts_path_by_name = {row.get('sample_id'): row.get('counts') for row in params_samples}
    for sample in samples:
        sample_id = sample['sample_id']
        model_index = interval.get('model_index')
        raw_count = None
        path = counts_path_by_name.get(sample_id)
        if path and model_index is not None:
            # HDF5 coordinates are the original interval list; use its index.
            raw_count = _read_count_at(path, interval['interval_index'])
        total = sample.get('library_size_counts') or 0
        cpm = (raw_count / total * 1_000_000.0) if raw_count is not None and total else None
        position = sample_position.get(sample_id)
        per_base = (_json_number(coverage_per_base[position, interval['interval_index']])
                    if position is not None else None)
        normalized = (_json_number(normalized_coverage[position, interval['interval_index']])
                      if position is not None else None)
        denoised = (_json_number(denoised_log2[position, interval['interval_index']])
                    if position is not None else None)
        caller_dir = _sample_output_dirs(Path(output) / 'caller', 'log_q_c_tc.tsv').get(sample_id)
        posterior_row = None
        if caller_dir and model_index is not None:
            state_labels, logp = _read_posterior_row(caller_dir / 'log_q_c_tc.tsv', model_index)
            if logp is not None:
                _h, _nh, _mx, _map = posterior_entropy(logp)
                maximum = float(maxima[position, model_index])
                probabilities = np.exp(logp - np.max(logp))
                probabilities /= probabilities.sum()
                baseline = int(baselines[position, model_index])
                posterior_row = {
                    'states': [{'copy_number': state, 'probability': float(probability)}
                               for state, probability in zip(state_labels, probabilities)],
                    'entropy_normalized': _json_number(entropy[position, model_index]),
                    'maximum_posterior': _json_number(maximum), 'baseline_copy_number': baseline if baseline != 255 else None,
                    'map_copy_number': int(states[position, model_index]),
                }
        interval_counts.append({'sample_id': sample_id, 'batch': sample.get('batch'),
                                'count_raw_observed': raw_count, 'coverage_per_base': per_base,
                                'normalized_coverage': normalized, 'count_cpm_derived': cpm,
                                'denoised_copy_ratio_log2': denoised,
                                'outlier_robust_z': sample.get('unexplained_variance_robust_z'),
                                'posterior': posterior_row})
    return {'interval': interval, 'sample_measurements': interval_counts,
            'sample_measurements_total': total_measurement_samples,
            'sample_measurements_truncated': total_measurement_samples > len(interval_counts),
            'calculation_note': diagnostics['provenance']['calculation_method']}


def sample_detail(output, params, diagnostics, sample_id):
    summary = next((row for row in diagnostics.get('samples', []) if row['sample_id'] == sample_id), None)
    if summary is None:
        raise KeyError('Muestra no incluida en el snapshot de este modelo.')
    snapshots = params.get('sample_snapshot') or params.get('samples', [])
    snapshot = next((row for row in snapshots if row.get('sample_id') == sample_id), None)
    if not snapshot:
        raise KeyError('Falta el snapshot de read counts para la muestra.')
    intervals, counts = read_gatk_counts(snapshot['counts'])
    if counts.size == 0:
        raise DiagnosticsUnavailable('El HDF5 no contiene recuentos.')
    total = float(np.sum(counts))
    cpm = counts.astype(np.float64) / total * 1_000_000.0 if total else np.full(counts.size, np.nan)
    log_cpm = np.log2(cpm + 1.0)
    finite = log_cpm[np.isfinite(log_cpm)]
    histogram = _histogram(finite, bins=40)
    entropy = np.load(Path(output).parent / 'diagnostics_cache' / 'posterior_entropy.npy', mmap_mode='r')
    samples = diagnostics['samples']
    sample_index = next(i for i, row in enumerate(samples) if row['sample_id'] == sample_id)
    entropy_values = np.asarray(entropy[sample_index], dtype=np.float32)
    valid_entropy = entropy_values[np.isfinite(entropy_values)]
    top_indices = np.argsort(np.nan_to_num(entropy_values, nan=-1.0))[-20:][::-1]
    interval_rows = load_interval_diagnostics(output)
    row_by_model_index = {row.get('model_index'): row for row in interval_rows if row.get('model_index') is not None}
    top_entropy_intervals = []
    for model_index in top_indices:
        matching = row_by_model_index.get(int(model_index))
        if matching:
            top_entropy_intervals.append({'interval_index': matching['interval_index'], 'contig': matching['contig'],
                                          'start': matching['start'], 'end': matching['end'],
                                          'entropy_normalized': _json_number(entropy_values[model_index])})
    return {
        'sample': summary, 'coverage_histogram_log2_cpm': histogram,
        'coverage_normalization': 'log2(CPM + 1), CPM = counts / total sample counts * 1e6',
        'zero_interval_count': int(np.count_nonzero(counts == 0)),
        'low_count_threshold': diagnostics['provenance']['low_count_threshold'],
        'ploidy': diagnostics['ploidy_heatmap']['contigs'],
        'ploidy_gq': diagnostics['ploidy_heatmap']['gq'][sample_index],
        'ploidy_state': diagnostics['ploidy_heatmap']['ploidy'][sample_index],
        'bias_factors': dict(zip(diagnostics['bias_factor_heatmap']['factors'], summary['bias_factor_loadings'])),
        'posterior_entropy_histogram': _histogram(valid_entropy),
        'top_entropy_intervals': top_entropy_intervals,
    }


def entropy_heatmap(output, diagnostics, *, samples=None, contig='', top_n=50, metric='entropy', aggregation='median'):
    rows = load_interval_diagnostics(output)
    sample_rows = diagnostics.get('samples', [])
    if samples:
        selected = set(samples)
        sample_rows = [row for row in sample_rows if row['sample_id'] in selected]
    sample_rows = sample_rows[:100]
    if not sample_rows:
        return {'intervals': [], 'samples': [row['sample_id'] for row in sample_rows], 'values': [], 'metric': metric}
    cache = Path(output).parent / 'diagnostics_cache'
    matrix_path = cache / ('posterior_entropy.npy' if metric == 'entropy' else 'map_copy_number.npy')
    matrix = np.load(matrix_path, mmap_mode='r')
    all_samples = diagnostics.get('samples', [])
    sample_indexes = [i for i, row in enumerate(all_samples) if row['sample_id'] in
                      {sample['sample_id'] for sample in sample_rows}]
    selected_ids = [all_samples[i]['sample_id'] for i in sample_indexes]
    candidates = [row for row in rows if row.get('retained') and (not contig or row['contig'] == contig)]
    max_items = max(1, min(int(top_n), 200))
    if metric == 'entropy':
        # Aggregate all retained intervals into fixed 1 Mb genomic bins before ranking.
        groups = {}
        for item in candidates:
            start = ((item['start'] - 1) // 1_000_000) * 1_000_000 + 1
            key = (item['contig'], start)
            groups.setdefault(key, []).append(item)
        method = aggregation if aggregation in {'mean', 'median', 'q90'} else 'median'
        reducers = {'mean': np.nanmean, 'median': np.nanmedian,
                    'q90': lambda values, axis: np.nanpercentile(values, 90, axis=axis)}
        reduced = []
        for (chrom, start), intervals in groups.items():
            indexes = [item['model_index'] for item in intervals if item.get('model_index') is not None]
            if not indexes:
                continue
            block = np.asarray(matrix[np.ix_(sample_indexes, indexes)], dtype=np.float32)
            with np.errstate(invalid='ignore'):
                per_sample = reducers[method](block, axis=1)
            cohort_values = per_sample[np.isfinite(per_sample)]
            rank = float(np.median(cohort_values)) if cohort_values.size else -1
            reduced.append({'contig': chrom, 'start': start, 'end': max(x['end'] for x in intervals),
                            'rank': rank, 'interval_count': len(indexes),
                            'first_interval_index': intervals[0]['interval_index'],
                            'values': [_json_number(value) for value in per_sample]})
        selected_bins = sorted(reduced, key=lambda item: item['rank'], reverse=True)[:max_items]
        return {'intervals': [{'interval_index': row['first_interval_index'], 'contig': row['contig'], 'start': row['start'],
                               'end': row['end'], 'interval_count': row['interval_count']}
                              for row in selected_bins],
                'samples': selected_ids, 'values': [row['values'] for row in selected_bins],
                'metric': metric, 'aggregation': method,
                'observations_represented': 'Intervalos retenidos agregados por bins genómicos de 1 Mb; valor por muestra.',
                'note': diagnostics['performance']['posterior_note']}
    # Copy-number states stay at interval resolution because averaging categories is invalid.
    metric_key = 'nonreference_map_frequency'
    candidates.sort(key=lambda row: row.get(metric_key) if row.get(metric_key) is not None else -1, reverse=True)
    candidates = candidates[:max_items]
    values = []
    for interval in candidates:
        model_index = interval.get('model_index')
        row = [matrix[sample_index, model_index] for sample_index in sample_indexes] if model_index is not None else [None] * len(sample_indexes)
        values.append([_json_safe(value) if value != 255 else None for value in row])
    return {'intervals': [{'interval_index': row['interval_index'], 'contig': row['contig'],
                           'start': row['start'], 'end': row['end']}
                          for row in candidates],
            'samples': selected_ids, 'values': values,
            'metric': metric, 'aggregation': aggregation,
            'observations_represented': '1 sample × 1 retained interval per cell',
            'note': 'Copy-number state MAP; this view is distinct from posterior uncertainty.'}
