import tempfile
import unittest
import gzip
import json
from pathlib import Path

import numpy as np

from app.services.cohort_diagnostics import (
    DiagnosticsUnavailable, _interval_stats, _read_gatk_rows, _size_factors_median_ratio,
    _filter_interval_membership, _hierarchical_order, _intervals, _normalize_interval_coverage,
    _read_interval_annotations,
    _read_postprocessed_copy_ratios, posterior_entropy, query_intervals, robust_z_scores,
)


class CohortDiagnosticUnitTests(unittest.TestCase):
    def test_entropy_is_stable_and_normalized_by_state_count(self):
        entropy, normalized, maximum, map_state = posterior_entropy(
            [[0.0, -np.inf, -np.inf, -np.inf], [0.0, 0.0, -np.inf, -np.inf]],
            state_counts=[2, 4])
        self.assertAlmostEqual(entropy[0], 0.0)
        self.assertAlmostEqual(normalized[0], 0.0)
        self.assertAlmostEqual(maximum[0], 1.0)
        self.assertEqual(map_state.tolist(), [0, 0])
        self.assertAlmostEqual(normalized[1], 0.5)

    def test_robust_z_preserves_missing_values(self):
        result = robust_z_scores([1, 2, 3, 100, np.nan])
        self.assertTrue(np.isnan(result[-1]))
        self.assertGreater(result[3], 3)
        self.assertEqual(robust_z_scores([4, 4, 4]).tolist(), [0.0, 0.0, 0.0])

    def test_hierarchical_order_handles_missing_samples(self):
        order = _hierarchical_order([[0, 0], [10, 10], [0.1, 0.1], [None, None]],
                                    ['A', 'B', 'C', 'missing'])
        self.assertEqual(order[-1], 'missing')
        self.assertEqual(set(order), {'A', 'B', 'C', 'missing'})

    def test_gatk_tsv_accepts_metadata_and_does_not_drop_missing_cells(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'table.tsv'
            path.write_text('@HD\tVN:1.0\n# comment\nCONTIG\tPLOIDY\tPLOIDY_GQ\nchrX\t2\t\n')
            header, rows = _read_gatk_rows(path)
        self.assertEqual(header, ['CONTIG', 'PLOIDY', 'PLOIDY_GQ'])
        self.assertEqual(rows[0]['CONTIG'], 'chrX')
        self.assertEqual(rows[0]['PLOIDY_GQ'], '')

    def test_annotations_preserve_missing_tracks_as_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'annotated_intervals.tsv'
            path.write_text('CONTIG\tSTART\tEND\tGC_CONTENT\nchr1\t1\t10\t0.42\n')
            rows = _read_interval_annotations(path)
        self.assertEqual(rows[('chr1', 1, 10)]['gc_content'], .42)
        self.assertNotIn('mappability', rows[('chr1', 1, 10)])

    def test_normalized_variability_and_zero_fractions(self):
        counts = np.array([[0, 10, 100], [0, 20, 200], [5, 30, 300]], dtype=np.float32)
        totals = counts.sum(axis=1).astype(np.float64)
        factors = _size_factors_median_ratio(counts)
        zero, low, metrics = _interval_stats(counts, totals, factors, 10)
        self.assertAlmostEqual(zero[0], 2 / 3)
        self.assertAlmostEqual(low[0], 1.0)
        self.assertTrue(np.isfinite(metrics['library_size']['median_absolute_deviation']).all())
        self.assertIn('coefficient_variation', metrics['median_ratio'])

    def test_coverage_per_base_normalization_and_robust_cv_manual_values(self):
        intervals = [('chr1', 1, 10), ('chr1', 21, 40)]
        counts = np.asarray([[10, 20], [20, 20]], dtype=np.float64)
        coverage, normalized, factors, metrics = _normalize_interval_coverage(counts, intervals)
        self.assertEqual(coverage.tolist(), [[1.0, 1.0], [2.0, 1.0]])
        self.assertEqual(factors.tolist(), [1.0, 1.5])
        self.assertAlmostEqual(normalized[0, 0], 1.0)
        self.assertAlmostEqual(normalized[1, 0], 4 / 3)
        self.assertAlmostEqual(metrics[0]['median_normalized_coverage'], 7 / 6)
        self.assertAlmostEqual(metrics[0]['mad_normalized_coverage'], 1 / 6)
        self.assertAlmostEqual(metrics[0]['robust_cv_percent'], 1.4826 * (1 / 6) / (7 / 6) * 100)
        self.assertAlmostEqual(metrics[0]['zero_coverage_percentage'], 0)
        self.assertAlmostEqual(metrics[0]['relative_median_coverage'], 7 / 6)

    def test_retained_reference_scope_and_zero_denominator_are_missing(self):
        intervals = [('chr1', 1, 10), ('chr1', 21, 40)]
        counts = np.asarray([[10, 20], [0, 0]], dtype=np.float64)
        _coverage, normalized, factors, metrics = _normalize_interval_coverage(
            counts, intervals, reference_intervals={intervals[0]})
        self.assertEqual(factors[0], 1.0)
        self.assertEqual(factors[1], 0.0)
        self.assertEqual(metrics[0]['zero_coverage_percentage'], 50.0)
        self.assertEqual(metrics[0]['robust_cv_percent'], 0.0)
        self.assertTrue(np.isnan(normalized[1]).all())

    def test_postprocessed_metrics_require_declared_log2_scale_and_matching_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sample_dir = root / 'postprocess' / 'SAMPLE_0'
            sample_dir.mkdir(parents=True)
            (sample_dir / 'sample_name.txt').write_text('sample-A\n')
            output = sample_dir / 'denoised_copy_ratios.tsv'
            output.write_text('@HD\tVN:1.0\nCONTIG\tSTART\tEND\tLOG2_COPY_RATIO\nchr1\t1\t10\t0.25\n')
            matrix, available, warnings = _read_postprocessed_copy_ratios(
                root, [('chr1', 1, 10), ('chr1', 21, 30)], ['sample-A'], [('chr1', 1, 10)])
            self.assertEqual(available, 1)
            self.assertAlmostEqual(matrix[0, 0], .25)
            self.assertTrue(np.isnan(matrix[0, 1]))
            self.assertEqual(warnings, [])
            output.write_text('CONTIG\tSTART\tEND\tCOPY_RATIO\nchr1\t1\t10\t1.2\n')
            matrix, available, warnings = _read_postprocessed_copy_ratios(
                root, [('chr1', 1, 10)], ['sample-A'])
            self.assertEqual(available, 0)
            self.assertTrue(np.isnan(matrix).all())
            self.assertIn('escala no reconocidos', warnings[0])
            output.write_text('CONTIG\tSTART\tEND\tLOG2_COPY_RATIO\nchr1\t2\t11\t0.2\n')
            _matrix, available, warnings = _read_postprocessed_copy_ratios(
                root, [('chr1', 1, 10)], ['sample-A'])
            self.assertEqual(available, 0)
            self.assertIn('no coinciden exactamente', warnings[0])

    def test_zero_total_samples_preserve_missing_normalized_values(self):
        counts = np.zeros((3, 4), dtype=np.float32)
        totals = np.zeros(3, dtype=np.float64)
        factors = _size_factors_median_ratio(counts)
        zero, low, metrics = _interval_stats(counts, totals, factors, 10)
        self.assertTrue(np.all(zero == 1.0))
        self.assertTrue(np.all(low == 1.0))
        self.assertTrue(np.isnan(metrics['library_size']['median_absolute_deviation']).all())

    def test_performance_shape_100_samples_by_5000_intervals(self):
        rng = np.random.default_rng(7)
        counts = rng.poisson(45, size=(100, 5000)).astype(np.float32)
        counts[0, :120] = 0
        totals = counts.sum(axis=1, dtype=np.float64)
        factors = _size_factors_median_ratio(counts)
        zero, low, methods = _interval_stats(counts, totals, factors, 10)
        self.assertEqual(zero.shape, (5000,))
        self.assertEqual(low.shape, (5000,))
        self.assertEqual(methods['library_size']['median_absolute_deviation'].shape, (5000,))
        self.assertTrue(np.isfinite(methods['median_ratio']['normalized_count_sd']).all())

    def test_invalid_posterior_dimensions_fail_clearly(self):
        with self.assertRaises(ValueError):
            posterior_entropy([[0.0]])

    def test_missing_posterior_rows_remain_missing(self):
        entropy, normalized, maximum, state = posterior_entropy([[0.0, np.nan], [np.nan, np.nan]])
        self.assertTrue(np.isnan(entropy).all())
        self.assertTrue(np.isnan(normalized).all())
        self.assertTrue(np.isnan(maximum).all())
        self.assertEqual(state.tolist(), [-1, -1])

    def test_duplicate_interval_coordinates_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'duplicate.interval_list'
            path.write_text('@HD\tVN:1.0\nchr1\t1\t10\t+\tA\nchr1\t1\t10\t+\tB\n')
            with self.assertRaises(DiagnosticsUnavailable):
                _intervals(path)

    def test_filter_intervals_counts_are_original_minus_retained(self):
        original = [('chr1', 1, 10), ('chr1', 11, 20), ('chr2', 1, 10)]
        retained, summary = _filter_interval_membership(original, [original[0], original[2]])
        self.assertEqual(len(retained), 2)
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['filtered'], 1)
        self.assertAlmostEqual(summary['filtered_percentage'], 100 / 3)
        with self.assertRaises(DiagnosticsUnavailable):
            _filter_interval_membership(original, [('chr9', 1, 10)])

    def test_interval_order_is_preserved_from_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'ordered.interval_list'
            path.write_text('@HD\tVN:1.0\nchr2\t20\t30\t+\tB\nchr1\t1\t10\t+\tA\n')
            self.assertEqual(_intervals(path), [('chr2', 20, 30), ('chr1', 1, 10)])

    def test_interval_filters_pagination_and_sort(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'model' / 'outputs'
            cache = root.parent / 'diagnostics_cache'
            cache.mkdir(parents=True)
            rows = [
                {'interval_index': 0, 'contig': 'chr1', 'start': 1, 'end': 10, 'gene': None,
                 'retained': True, 'primary_filter_reason': None, 'gc_content': .3,
                 'robust_cv_percent': .2, 'median_normalized_coverage': .9,
                 'zero_count_fraction': 0.0, 'normalized': {'library_size': {'median_absolute_deviation': .2}}},
                {'interval_index': 1, 'contig': 'chr1', 'start': 20, 'end': 30, 'gene': None,
                 'retained': False, 'primary_filter_reason': 'filtered_by_gatk_filter_intervals', 'gc_content': .7,
                 'robust_cv_percent': .9, 'median_normalized_coverage': .5,
                 'zero_count_fraction': .5, 'normalized': {'library_size': {'median_absolute_deviation': .9}}},
                {'interval_index': 2, 'contig': 'chr2', 'start': 3, 'end': 9, 'gene': None,
                 'retained': True, 'primary_filter_reason': None, 'gc_content': .5,
                 'robust_cv_percent': .7, 'median_normalized_coverage': 1.2,
                 'zero_count_fraction': .1, 'normalized': {'library_size': {'median_absolute_deviation': .4}}},
            ]
            with gzip.open(cache / 'intervals.jsonl.gz', 'wt', encoding='utf-8') as handle:
                for row in rows:
                    handle.write(json.dumps(row) + '\n')
            page = query_intervals(root, page=1, page_size=1, status='retained', sort='robust_variability')
            self.assertEqual(page['total'], 2)
            self.assertEqual(page['rows'][0]['interval_index'], 2)
            all_rows = query_intervals(root, status='excluded', all_rows=True, page_size=10)
            self.assertEqual([row['interval_index'] for row in all_rows['rows']], [1])
            high_cv = query_intervals(root, metric='robust_cv_percent', metric_min=.5, all_rows=True)
            self.assertEqual({row['interval_index'] for row in high_cv['rows']}, {1, 2})


if __name__ == '__main__':
    unittest.main()
