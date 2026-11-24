"""Parsing and summary metrics for CASE-mode gCNV model validation."""
import csv
import gzip
import math
import re
from pathlib import Path

import numpy as np

from app.services.bam_qc import read_gatk_counts


def _key(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value).strip().lower()).strip('_')


def _number(value):
    try:
        result = float(str(value).replace(',', '.'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _read_table(path):
    path = Path(path)
    if not path.is_file():
        return []
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    for index, line in enumerate(lines):
        if not line.strip() or line.startswith(('#', '@')):
            continue
        header = next(csv.reader([line], delimiter='\t'))
        if len(header) < 2 or not any(re.search('[A-Za-z]', value) for value in header):
            continue
        return [{_key(k): v for k, v in row.items()}
                for row in csv.DictReader(lines[index:], delimiter='\t')]
    return []


def _read_vcf(path):
    path = Path(path)
    if not path.is_file():
        return []
    opener = gzip.open if path.suffix == '.gz' else open
    records = []
    with opener(path, 'rt', encoding='utf-8', errors='replace') as handle:
        sample_name = None
        for line in handle:
            if line.startswith('#CHROM'):
                fields = line.rstrip('\n').split('\t')
                sample_name = fields[9] if len(fields) > 9 else None
                continue
            if not line or line.startswith('#'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 8:
                continue
            info = {}
            for item in fields[7].split(';'):
                if '=' in item:
                    key, value = item.split('=', 1)
                    info[key] = value
            fmt = fields[8].split(':') if len(fields) > 8 else []
            sample = fields[9].split(':') if len(fields) > 9 else []
            values = dict(zip(fmt, sample))
            start = int(fields[1])
            end = int(info.get('END', start + max(0, len(fields[3]) - 1)))
            cn = _number(values.get('CN') or info.get('CN'))
            gt = values.get('GT', '')
            # gCNV interval/segment VCFs encode non-reference copy-number calls
            # with a non-reference GT; CN is retained for the detail view.
            is_event = any(allele not in {'0', '.', ''} for allele in re.split(r'[/|]', gt))
            if not gt and cn is not None:
                is_event = cn != 2
            records.append({
                'chrom': fields[0], 'start': start, 'end': end,
                'type': 'DEL' if cn is not None and cn < 2 else 'DUP' if cn is not None and cn > 2 else 'CNV',
                'copy_number': cn, 'genotype': gt or None,
                'quality': _number(fields[5]), 'filter': fields[6],
                'format': values, 'sample_name': sample_name, 'is_event': is_event,
            })
    return records


def _known_overlap(call, known):
    chrom_a = call['chrom'].removeprefix('chr')
    chrom_b = known['chrom'].removeprefix('chr')
    same_type = known['type'] in {'CNV', call['type']} or call['type'] == 'CNV'
    overlap = chrom_a == chrom_b and call['start'] <= known['end'] and known['start'] <= call['end']
    return bool(overlap and same_type)


def _read_ploidy_baseline(output, sample_index):
    sample_dir = Path(output) / 'ploidy' / 'validation-calls' / f'SAMPLE_{sample_index}'
    for path in sample_dir.glob('*.tsv'):
        rows = _read_table(path)
        if not rows:
            continue
        contig_key = next((key for key in rows[0] if 'contig' in key), None)
        ploidy_key = next((key for key in rows[0] if 'ploidy' in key and 'gq' not in key), None)
        if contig_key and ploidy_key:
            return {row[contig_key]: _number(row.get(ploidy_key)) for row in rows
                    if row.get(contig_key) and _number(row.get(ploidy_key)) is not None}
    return {}


def _sample_files(output, stage, sample_index, filename):
    """Find a sample artifact in GATK's SAMPLE_n output directory."""
    root = Path(output) / stage
    if not root.is_dir():
        return []
    return sorted(set(root.glob(f'**/SAMPLE_{sample_index}/**/{filename}')) |
                  set(root.glob(f'**/SAMPLE_{sample_index}/{filename}')))


def _sample_unexplained_variance(output, sample_index):
    for path in _sample_files(output, 'caller', sample_index, 'mu_psi_s_log__.tsv'):
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip() or line.startswith(('#', '@')):
                continue
            fields = line.split('\t')
            for field in fields:
                value = _number(field)
                if value is not None:
                    return value
    return None


def _sample_global_depth(output, sample_index):
    for path in _sample_files(output, 'ploidy', sample_index, 'global_read_depth.tsv'):
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            if not line.strip() or line.startswith(('#', '@')):
                continue
            fields = line.split('\t')
            if len(fields) > 1:
                header = [_key(value) for value in fields]
                value_index = next((i for i, name in enumerate(header) if name == 'global_read_depth'), None)
                if value_index is not None:
                    continue
            else:
                value_index = 0
            value = _number(fields[value_index]) if value_index < len(fields) else None
            if value is not None:
                return value
    return None


def _gc_annotations(path):
    annotations = {}
    for row in _read_table(path) if path else []:
        chrom = row.get('contig') or row.get('chromosome') or row.get('chr')
        start, end = _number(row.get('start')), _number(row.get('end'))
        gc = _number(row.get('gc_content') or row.get('gc'))
        if chrom and start is not None and end is not None and gc is not None:
            annotations[(chrom, int(start), int(end))] = gc
    return annotations


def _learned_gc_curve(model_path, output=None):
    """Read a published GC-bin curve when the GATK model exposes one."""
    roots = [Path(path) for path in (model_path, output) if path and Path(path).is_dir()]
    if not roots:
        return []
    candidates = sorted(path for root in roots for path in root.rglob('*.tsv'))
    curve_path = next((path for path in candidates
                       if re.search(r'(gc|z_sg)', path.name.lower())
                       and ('curve' in path.name.lower() or 'z_sg' in path.name.lower())), None)
    if not curve_path:
        return []
    values = []
    for line in curve_path.read_text(encoding='utf-8', errors='replace').splitlines():
        if not line.strip() or line.startswith(('#', '@')):
            continue
        numeric = [_number(value) for value in line.split('\t')]
        numeric = [value for value in numeric if value is not None]
        if numeric:
            values.extend(numeric)
    return [{'bin': index + 1, 'value': value} for index, value in enumerate(values)]


def _autosomal_ploidy(output, sample_index, sample_id):
    rows = []
    for path in _sample_files(output, 'ploidy', sample_index, 'contig_ploidy.tsv'):
        for row in _read_table(path):
            chrom = row.get('contig') or row.get('contig_name') or row.get('chromosome')
            ploidy = _number(row.get('ploidy'))
            if not chrom or ploidy is None:
                continue
            autosome = chrom.lower().removeprefix('chr')
            if autosome.isdigit() and 1 <= int(autosome) <= 22 and ploidy != 2:
                rows.append({'sample_id': sample_id, 'chrom': chrom, 'ploidy': ploidy,
                             'gq': _number(row.get('ploidy_gq'))})
        if rows:
            break
    return rows


def _correlation(case_counts, training_samples, model_intervals):
    try:
        case_intervals, case_values = read_gatk_counts(case_counts)
        allowed = set(tuple(item) for item in model_intervals)
        case_map = {tuple(interval): float(value) for interval, value in zip(case_intervals, case_values)
                    if not allowed or tuple(interval) in allowed}
        if not case_map:
            return None
        training_profiles = []
        for sample in training_samples:
            intervals, values = read_gatk_counts(sample['counts'])
            profile = {tuple(interval): float(value) for interval, value in zip(intervals, values)}
            training_profiles.append(profile)
        if not training_profiles:
            return None
        shared = sorted(set(case_map).intersection(*(set(profile) for profile in training_profiles)))
        if len(shared) < 2:
            return None
        reference = np.asarray([[profile[interval] for interval in shared] for profile in training_profiles], dtype=float)
        case = np.asarray([case_map[interval] for interval in shared], dtype=float)
        ref_totals = reference.sum(axis=1)
        case_total = case.sum()
        if not case_total or np.any(ref_totals == 0):
            return None
        reference = reference / ref_totals[:, None] * 1e6
        case = case / case_total * 1e6
        model_profile = np.median(reference, axis=0)
        if np.std(model_profile) == 0 or np.std(case) == 0:
            return None
        return float(np.corrcoef(case, model_profile)[0, 1])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def build_validation_report(output, params):
    output = Path(output)
    model_intervals = []
    interval_path = Path(params['intervals'])
    if interval_path.is_file():
        with interval_path.open(encoding='utf-8', errors='replace') as handle:
            model_intervals = [tuple(line.rstrip('\n').split('\t')[:3]) for line in handle
                               if line.strip() and not line.startswith(('@', '#'))]
            model_intervals = [(c, int(s), int(e)) for c, s, e in model_intervals]
    annotation_path = params.get('annotated_intervals')
    annotations = _gc_annotations(annotation_path)
    gc_curve = _learned_gc_curve(params.get('cohort_model'), output / 'caller')
    by_sample = []
    autosomal_ploidy_exceptions = []
    for index, sample in enumerate(params['samples']):
        sample_dir = output / 'postprocess' / f'SAMPLE_{index}'
        calls = _read_vcf(sample_dir / 'genotyped_segments.vcf.gz')
        if not calls:
            calls = _read_vcf(sample_dir / 'genotyped_intervals.vcf.gz')
        baselines = _read_ploidy_baseline(output, index)
        for call in calls:
            baseline = baselines.get(call['chrom'], 2)
            call['baseline_copy_number'] = baseline
            if call['copy_number'] is not None:
                call['type'] = ('DEL' if call['copy_number'] < baseline else
                                'DUP' if call['copy_number'] > baseline else 'CNV')
        calls = [call for call in calls if call['is_event']]
        known = sample.get('known_cnvs', [])
        for call in calls:
            call['matches_known'] = any(_known_overlap(call, item) for item in known)
        ratios = _read_table(sample_dir / 'denoised_copy_ratios.tsv')
        intervals = []
        denoised_by_interval = {}
        for row in ratios:
            chrom = row.get('contig') or row.get('chromosome') or row.get('chr')
            start = _number(row.get('start'))
            end = _number(row.get('end'))
            log2 = _number(row.get('log2_copy_ratio'))
            linear_ratio = _number(row.get('linear_copy_ratio'))
            if log2 is None and linear_ratio is not None and linear_ratio > 0:
                log2 = math.log2(linear_ratio)
            if chrom and start is not None and end is not None and log2 is not None:
                key = (chrom, int(start), int(end))
                denoised_by_interval[key] = log2
                intervals.append({'chrom': chrom, 'start': int(start), 'end': int(end), 'log2_ratio': log2})
        count_path = output / f"{sample['sample_id']}.counts.hdf5"
        gc_points = []
        try:
            count_intervals, count_values = read_gatk_counts(count_path)
            total_counts = float(np.sum(count_values))
            median_count = float(np.median(count_values)) if len(count_values) else 0.0
            for (chrom, start, end), count in zip(count_intervals, count_values):
                gc = annotations.get((chrom, start, end))
                if gc is None:
                    continue
                gc_points.append({'gc': gc, 'count_raw': int(count),
                                  'count_per_million': float(count / total_counts * 1e6) if total_counts else None,
                                  'count_relative_median': float(count / median_count) if median_count else None,
                                  'denoised_log2_ratio': denoised_by_interval.get((chrom, start, end)),
                                  'chrom': chrom, 'start': start, 'end': end})
        except (OSError, ValueError, KeyError, TypeError):
            gc_points = []
        unexplained = _sample_unexplained_variance(output, index)
        unexplained_derived = (math.exp(unexplained)
                               if unexplained is not None and unexplained < 700 else None)
        global_depth = _sample_global_depth(output, index)
        autosomal_ploidy_exceptions.extend(_autosomal_ploidy(output, index, sample['sample_id']))
        bias_loadings = []
        for path in _sample_files(output, 'caller', index, 'mu_z_su.tsv'):
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                if line.strip() and not line.startswith(('#', '@')):
                    for field in line.split('\t'):
                        number = _number(field)
                        if number is not None:
                            bias_loadings.append(number)
            break
        by_sample.append({
            'sample_id': sample['sample_id'], 'cnv_detected_count': len(calls),
            'known_cnv_count': len(known), 'matched_known_cnv_count': sum(c['matches_known'] for c in calls),
            'sample_unexplained_variance_log_psi': unexplained,
            'sample_unexplained_variance_exp_log_psi_derived': unexplained_derived,
            'global_read_depth': global_depth,
            'autosomal_ploidy_exceptions': [row for row in autosomal_ploidy_exceptions
                                            if row['sample_id'] == sample['sample_id']],
            'gc_coverage_points': gc_points,
            'bias_factor_loadings': bias_loadings,
            'model_correlation': _correlation(output / f"{sample['sample_id']}.counts.hdf5",
                                              params.get('training_samples', []), model_intervals),
            'calls': calls, 'known_cnvs': known, 'log2_intervals': intervals,
        })
    summary = {
        'model_name': params.get('model_name'), 'model_run_id': params.get('model_run_id'),
        'sample_count': len(by_sample), 'cnv_detected_count': sum(s['cnv_detected_count'] for s in by_sample),
        'known_cnv_count': sum(s['known_cnv_count'] for s in by_sample),
        'matched_known_cnv_count': sum(s['matched_known_cnv_count'] for s in by_sample),
        'autosomal_ploidy_exceptions': autosomal_ploidy_exceptions,
        'gc_curve': gc_curve,
        'gc_curve_available': bool(gc_curve),
        'gc_coverage_available': any(sample['gc_coverage_points'] for sample in by_sample),
        'samples': by_sample,
    }
    return summary
