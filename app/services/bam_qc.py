import csv
import json
import math
import re
from pathlib import Path

import h5py
import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import squareform


def _field_names(dtype):
    return {name.lower(): name for name in (dtype.names or ())}


def _text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def read_gatk_counts(path):
    """Read GATK CollectReadCounts HDF5, both its matrix and legacy table layouts."""
    candidates = []
    with h5py.File(path, 'r') as handle:
        # Current GATK writes counts as a samples-by-intervals matrix and stores
        # genomic coordinates separately as contig-index/start/end rows.
        if all(name in handle for name in (
                '/counts/values', '/intervals/transposed_index_start_end',
                '/intervals/indexed_contig_names')):
            values = np.atleast_1d(np.asarray(handle['/counts/values'][()]).squeeze())
            coordinates = np.asarray(handle['/intervals/transposed_index_start_end'][()])
            contig_names = [_text(value) for value in handle['/intervals/indexed_contig_names'][()]]
            if values.ndim != 1:
                raise ValueError('El HDF5 debe contener una única fila de recuentos por muestra.')
            if coordinates.ndim == 2 and coordinates.shape[0] == 3:
                contig_indices, starts, ends = coordinates
            elif coordinates.ndim == 2 and coordinates.shape[1] == 3:
                contig_indices, starts, ends = coordinates.T
            else:
                raise ValueError('El HDF5 no contiene coordenadas de intervalo con forma reconocible.')
            if len(values) != len(contig_indices) or len(values) != len(starts) or len(values) != len(ends):
                raise ValueError('El HDF5 tiene distinto número de recuentos e intervalos.')
            if not np.all(np.isfinite(values)) or np.any(values < 0):
                raise ValueError('El HDF5 contiene recuentos no finitos o negativos.')
            if not np.all(np.isfinite(coordinates)):
                raise ValueError('El HDF5 contiene coordenadas no finitas.')
            indices = contig_indices.astype(np.int64)
            if np.any(indices < 0) or np.any(indices >= len(contig_names)):
                raise ValueError('El HDF5 contiene índices de cromosoma fuera de rango.')
            intervals = [(contig_names[index], int(start), int(end))
                         for index, start, end in zip(indices, starts, ends)]
            return intervals, np.rint(values).astype(np.int64)

        handle.visititems(lambda _name, item: candidates.append(item)
                          if isinstance(item, h5py.Dataset) else None)
        candidates.sort(key=lambda item: 0 if item.name == '/counts/values' else 1)

        for dataset in candidates:
            fields = _field_names(dataset.dtype)
            if not {'count', 'contig', 'start', 'end'}.issubset(fields):
                continue
            records = dataset[()]
            intervals = [
                (_text(row[fields['contig']]), int(row[fields['start']]), int(row[fields['end']]))
                for row in records
            ]
            counts = np.asarray([int(row[fields['count']]) for row in records], dtype=np.int64)
            if len(intervals):
                return intervals, counts
    raise ValueError('El HDF5 no contiene una tabla GATK con CONTIG/START/END/COUNT reconocible.')


def summarize_counts(counts, low_count_threshold=10):
    values = np.asarray(counts, dtype=np.float64)
    if values.size == 0:
        raise ValueError('CollectReadCounts no produjo intervalos.')
    histogram, edges = np.histogram(values, bins=min(30, max(1, int(np.sqrt(values.size)))))
    return {
        'interval_count': int(values.size),
        'total_read_counts': int(values.sum()),
        'mean_counts': float(np.mean(values)),
        'median_counts': float(np.median(values)),
        'p10_counts': float(np.percentile(values, 10)),
        'p25_counts': float(np.percentile(values, 25)),
        'p75_counts': float(np.percentile(values, 75)),
        'p90_counts': float(np.percentile(values, 90)),
        'zero_count_intervals': int(np.count_nonzero(values == 0)),
        'zero_count_intervals_pct': float(np.mean(values == 0) * 100),
        'low_count_threshold': int(low_count_threshold),
        'low_count_intervals': int(np.count_nonzero(values < low_count_threshold)),
        'low_count_intervals_pct': float(np.mean(values < low_count_threshold) * 100),
        'histogram': [
            {'start': float(edges[i]), 'end': float(edges[i + 1]), 'count': int(histogram[i])}
            for i in range(len(histogram))
        ],
    }


def parse_hs_metrics(path):
    path = Path(path)
    with path.open(encoding='utf-8') as handle:
        rows = list(csv.reader(handle, delimiter='\t'))
    for index, row in enumerate(rows):
        header = [column.strip().upper() for column in row]
        if 'MEAN_TARGET_COVERAGE' not in header:
            continue
        if index + 1 >= len(rows):
            break
        values = rows[index + 1]
        source = dict(zip(header, values))
        keys = (
            'BAIT_SET', 'TOTAL_READS', 'PF_READS', 'PF_UNIQUE_READS', 'PCT_PF_UQ_READS_ALIGNED',
            'MEAN_TARGET_COVERAGE', 'MEDIAN_TARGET_COVERAGE', 'PCT_TARGET_BASES_10X',
            'PCT_TARGET_BASES_20X', 'PCT_TARGET_BASES_30X', 'PCT_TARGET_BASES_100X', 'ZERO_CVG_TARGETS_PCT',
            'FOLD_80_BASE_PENALTY', 'PCT_EXC_DUPE', 'PCT_EXC_MAPQ', 'PCT_EXC_OFF_TARGET',
            'GC_DROPOUT', 'AT_DROPOUT',
        )
        result = {}
        for key in keys:
            value = source.get(key)
            if value in (None, '', 'null', 'NA'):
                result[key.lower()] = None
            else:
                try:
                    number = float(value)
                    result[key.lower()] = number if math.isfinite(number) else None
                except ValueError:
                    result[key.lower()] = value
        return result
    raise ValueError('No se encontró la fila de métricas de CollectHsMetrics.')


def parse_flagstat(path):
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    patterns = {
        # Accept both GATK FlagStat's "N label" form and samtools' "N + M label" form.
        'total_reads': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+in total\b',
        'secondary_alignments': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+secondary\b',
        'supplementary_alignments': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+supplementary\b',
        'duplicate_reads': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+duplicates\b',
        'mapped_reads': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+mapped\b',
        'properly_paired_reads': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+properly paired\b',
        'qc_failed_reads': r'^\s*([\d,]+)(?:\s+\+\s+[\d,]+)?\s+QC(?:-failed| failure)\b',
    }
    result = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.MULTILINE | re.IGNORECASE)
        result[key] = int(match.group(1).replace(',', '')) if match else None
    total = result['total_reads']
    for key in ('mapped_reads', 'properly_paired_reads', 'duplicate_reads', 'qc_failed_reads'):
        result[f'{key}_pct'] = (100 * result[key] / total) if total and result[key] is not None else None
    result['raw'] = text
    return result


def build_sample_metrics(sample_id, batch_name, counts_path, hs_path, flagstat_path, low_count_threshold):
    intervals, counts = read_gatk_counts(counts_path)
    return {
        'schema_version': '1.0',
        'sample_id': sample_id,
        'batch': batch_name,
        'read_counts': summarize_counts(counts, low_count_threshold),
        'collect_hs_metrics': parse_hs_metrics(hs_path),
        'flagstat': parse_flagstat(flagstat_path),
        'interval_count': len(intervals),
    }, intervals, counts


def build_sample_metrics_partial(sample_id, batch_name, counts_path, hs_path, flagstat_path,
                                low_count_threshold):
    """Keep usable read-count metrics even when a secondary QC output is invalid."""
    intervals, counts = read_gatk_counts(counts_path)
    errors = []
    try:
        hs = parse_hs_metrics(hs_path) if Path(hs_path).is_file() else None
    except (OSError, ValueError, KeyError) as exc:
        hs = None
        errors.append(f'CollectHsMetrics: {exc}')
    try:
        flag = parse_flagstat(flagstat_path) if Path(flagstat_path).is_file() else None
    except (OSError, ValueError, KeyError) as exc:
        flag = None
        errors.append(f'FlagStat: {exc}')
    if hs is None and not Path(hs_path).is_file():
        errors.append('Falta el fichero de CollectHsMetrics.')
    if flag is None and not Path(flagstat_path).is_file():
        errors.append('Falta el fichero de FlagStat.')
    target_coverage = {}
    # CollectHsMetrics reports the fraction of target bases reaching each
    # threshold. Convert that to the requested percentage below the threshold.
    if hs:
        for threshold in (30, 100):
            fraction = hs.get(f'pct_target_bases_{threshold}x')
            if isinstance(fraction, (int, float)) and math.isfinite(fraction):
                target_coverage[f'bases_below_{threshold}x_pct'] = max(0.0, min(100.0, (1.0 - fraction) * 100.0))
    metrics = {
        'schema_version': '1.0',
        'sample_id': sample_id,
        'batch': batch_name,
        'read_counts': summarize_counts(counts, low_count_threshold),
        'collect_hs_metrics': hs,
        'flagstat': flag,
        'target_coverage': target_coverage or None,
        'interval_count': len(intervals),
    }
    return metrics, intervals, counts, errors


def _safe_correlation(matrix):
    if matrix.shape[0] == 1:
        return np.ones((1, 1), dtype=np.float64)
    correlation = np.corrcoef(matrix)
    correlation = np.nan_to_num(correlation, nan=0.0, posinf=0.0, neginf=0.0)
    np.fill_diagonal(correlation, 1.0)
    return correlation


def _is_sex_chromosome(contig):
    name = str(contig).upper()
    if name.startswith('CHR'):
        name = name[3:]
    return name in {'X', 'Y'}


def build_project_qc(sample_rows, low_count_threshold):
    samples = []
    intervals = None
    count_vectors = []
    for sample, batch_name in sample_rows:
        if not sample.counts_path or not Path(sample.counts_path).is_file():
            continue
        current_intervals, counts = read_gatk_counts(sample.counts_path)
        if intervals is None:
            intervals = current_intervals
        elif intervals != current_intervals:
            sample.status = 'qc_failed'
            sample.error_message = 'Los intervalos de read counts no coinciden con los del proyecto.'
            continue
        metrics_path = Path(sample.qc_metrics_path)
        metrics = json_read(metrics_path)
        samples.append({
            'sample_id': sample.sample_id,
            'batch': batch_name,
            'read_counts': metrics['read_counts'],
            'collect_hs_metrics': metrics.get('collect_hs_metrics', {}),
            'flagstat': metrics.get('flagstat', {}),
            'target_coverage': metrics.get('target_coverage', {}),
        })
        count_vectors.append(counts)

    matrix = np.vstack(count_vectors) if count_vectors else np.empty((0, 0), dtype=np.float64)
    ids = [sample['sample_id'] for sample in samples]
    n_samples, n_intervals = matrix.shape if matrix.ndim == 2 else (0, 0)
    correlation = []
    pca_points = []
    pca_interval_count = 0
    excluded_sex_interval_count = 0
    cluster_order = ids[:]
    eligible_count = 0

    if n_samples:
        eligible = np.mean(matrix >= low_count_threshold, axis=0) >= 0.5
        eligible_count = int(np.count_nonzero(eligible))
        if eligible_count == 0:
            eligible = np.ones(n_intervals, dtype=bool)
        normalized = matrix[:, eligible].astype(np.float64)
        totals = normalized.sum(axis=1)
        normalized = np.divide(normalized, totals[:, None], out=np.zeros_like(normalized), where=totals[:, None] > 0) * 1_000_000
        sex_chromosome_mask = np.asarray([_is_sex_chromosome(interval[0]) for interval in (intervals or [])], dtype=bool)
        excluded_sex_interval_count = int(np.count_nonzero(sex_chromosome_mask))
        correlation_eligible = eligible & ~sex_chromosome_mask
        correlation_interval_count = int(np.count_nonzero(correlation_eligible))
        correlation_profiles = matrix[:, correlation_eligible].astype(np.float64)
        correlation_totals = correlation_profiles.sum(axis=1)
        correlation_profiles = np.divide(correlation_profiles, correlation_totals[:, None],
                                         out=np.zeros_like(correlation_profiles),
                                         where=correlation_totals[:, None] > 0) * 1_000_000
        if correlation_interval_count == 0:
            # Keep the cohort summary well formed if a BED has no eligible
            # autosomal intervals; it will produce zero off-diagonal correlations.
            correlation_profiles = np.zeros((n_samples, 1), dtype=np.float64)
        # Compare each sample's library-size-normalized coverage profile directly.
        # The log-ratio-to-cohort-median matrix is still useful for per-sample
        # deviation summaries and PCA, but correlating it removes shared coverage
        # structure and answers a different question.
        correlation_matrix = _safe_correlation(correlation_profiles)
        correlation = [[float(value) for value in row] for row in correlation_matrix]
        interval_medians = np.median(normalized, axis=0)
        log_ratios = np.log2((normalized + 0.1) / (interval_medians[None, :] + 0.1))
        for i, sample in enumerate(samples):
            others = np.delete(correlation_matrix[i], i)
            mean_corr = float(np.mean(others)) if others.size else None
            median_log2 = float(np.median(log_ratios[i]))
            mad = float(np.median(np.abs(log_ratios[i] - median_log2)))
            sample['normalized'] = {
                'median_log2_ratio': median_log2,
                'mad': mad,
                'robust_sd': 1.4826 * mad,
                'extreme_interval_pct': float(np.mean(np.abs(log_ratios[i]) > 1) * 100),
                'mean_correlation': mean_corr,
                'sample_coverage_correlation': mean_corr,
            }

        pca_eligible = eligible & ~sex_chromosome_mask
        pca_interval_count = int(np.count_nonzero(pca_eligible))
        if n_samples >= 2 and pca_interval_count:
            pca_matrix = matrix[:, pca_eligible].astype(np.float64)
            pca_totals = pca_matrix.sum(axis=1)
            pca_normalized = np.divide(pca_matrix, pca_totals[:, None], out=np.zeros_like(pca_matrix),
                                       where=pca_totals[:, None] > 0) * 1_000_000
            pca_interval_medians = np.median(pca_normalized, axis=0)
            pca_log_ratios = np.log2((pca_normalized + 0.1) /
                                     (pca_interval_medians[None, :] + 0.1))
            centered = pca_log_ratios - np.mean(pca_log_ratios, axis=0, keepdims=True)
            u, singular_values, _ = np.linalg.svd(centered, full_matrices=False)
            variance = singular_values ** 2
            explained = variance / variance.sum() if variance.sum() else np.zeros_like(variance)
            component_count = min(3, len(singular_values))
            coordinates = u[:, :component_count] * singular_values[:component_count]
            for i, sample in enumerate(samples):
                pca_points.append({
                    'sample_id': sample['sample_id'], 'batch': sample['batch'],
                    'pc1': float(coordinates[i, 0]) if component_count > 0 else 0.0,
                    'pc2': float(coordinates[i, 1]) if component_count > 1 else None,
                    'pc3': float(coordinates[i, 2]) if component_count > 2 else None,
                })
            if component_count:
                pca_variance = [float(explained[i]) for i in range(component_count)]
            else:
                pca_variance = []
            if n_samples >= 3:
                distance = np.clip(1 - correlation_matrix, 0, 2)
                np.fill_diagonal(distance, 0)
                order = leaves_list(linkage(squareform(distance, checks=False), method='average'))
                cluster_order = [ids[int(i)] for i in order]
        else:
            pca_variance = []
        corr_scores = [sample['normalized']['mean_correlation'] for sample in samples
                       if sample['normalized']['mean_correlation'] is not None]
        if len(corr_scores) >= 3:
            median_corr = float(np.median(corr_scores))
            mad_corr = float(np.median(np.abs(np.asarray(corr_scores) - median_corr)))
            robust_scale = 1.4826 * mad_corr if mad_corr else 0.05
            for sample in samples:
                score = sample['normalized']['mean_correlation']
                outlier_score = max(0.0, (median_corr - score) / robust_scale) if score is not None else None
                sample['normalized']['cohort_outlier_score'] = outlier_score
                sample['normalized']['outlier_review'] = bool(outlier_score is not None and outlier_score >= 3.0)
        else:
            for sample in samples:
                sample['normalized']['cohort_outlier_score'] = None
                sample['normalized']['outlier_review'] = False
        sample_by_id = {sample['sample_id']: sample for sample in samples}
        for point in pca_points:
            normalized_metrics = sample_by_id[point['sample_id']]['normalized']
            point['sample_coverage_correlation'] = normalized_metrics.get('sample_coverage_correlation')
            point['cohort_outlier_score'] = normalized_metrics.get('cohort_outlier_score')
            point['outlier_review'] = normalized_metrics.get('outlier_review', False)
    else:
        pca_variance = []

    return {
        'schema_version': '1.0',
        'coverage_correlation_method': 'pearson_library_size_normalized_autosomal_counts_v2',
        'sample_count': n_samples,
        'interval_count': n_intervals,
        'eligible_interval_count': eligible_count,
        'low_count_threshold': int(low_count_threshold),
        'samples': samples,
        'correlation': {'labels': ids, 'matrix': correlation,
                        'interval_count': correlation_interval_count,
                        'excluded_sex_chromosome_interval_count': excluded_sex_interval_count},
        'pca': {'explained_variance': pca_variance, 'points': pca_points,
                'interval_count': pca_interval_count,
                'excluded_sex_chromosome_interval_count': excluded_sex_interval_count},
        'cluster_order': cluster_order,
    }


def json_read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))
