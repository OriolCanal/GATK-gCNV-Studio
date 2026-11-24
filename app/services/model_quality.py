"""Best-effort, version-tolerant parsing of published GATK gCNV artifacts."""
import csv
import json
import math
import re
from pathlib import Path


def _key(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value).strip().lower()).strip('_')


def _read_tsv(path):
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    for index, line in enumerate(lines):
        candidate = line.strip()
        if not candidate or candidate.startswith(('#', '@')):
            continue
        fields = next(csv.reader([line], delimiter='\t'))
        if len(fields) > 1 and any(re.search(r'[A-Za-z]', cell) for cell in fields):
            try:
                return [{_key(k): v for k, v in row.items()}
                        for row in csv.DictReader(lines[index:], delimiter='\t')]
            except (csv.Error, TypeError):
                return []
    return []


def _number(value):
    try:
        result = float(str(value).replace(',', '.'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _find(rows, *tokens):
    return next((key for row in rows for key in row
                 if all(token in key for token in tokens)), None)


def _emit_csv(output, name, rows):
    if not rows:
        return None
    path = output / f'model_quality_{name}.csv'
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return path.name


def _sample_name(path):
    name_file = path.parent / 'sample_name.txt'
    if name_file.is_file():
        value = name_file.read_text(encoding='utf-8', errors='replace').strip()
        if value:
            return value
    return path.parent.name


def build_model_quality(output, params, log_path=None):
    """Parse common GATK tables without assuming a single output layout."""
    output = Path(output)
    discovered = []
    tables = []
    for path in sorted(output.rglob('*.tsv')):
        if path.name == 'retained_intervals.tsv' or path.name == 'excluded_intervals.tsv':
            continue
        rows = _read_tsv(path)
        if rows:
            rel = str(path.relative_to(output))
            discovered.append(rel)
            tables.append((path, rel, rows))

    result = {
        'schema_version': '1.0', 'parser_version': '1.0',
        'gatk_version': params.get('gatk_version'),
        'reference': params.get('reference_id'),
        'sample_count': len(params.get('samples', [])),
        'input_interval_count': None, 'retained_interval_count': None,
        'shard_count': 0, 'tables_discovered': discovered,
        'ploidy': [], 'depth': [], 'convergence': [], 'variance': [],
        'copy_ratios': [], 'cn_calls': [], 'warnings': [], 'exports': {},
    }
    for path in (output / 'retained_intervals.tsv',):
        if path.is_file():
            result['retained_interval_count'] = max(0, sum(1 for _ in path.open(encoding='utf-8')) - 1)
    interval_path = Path(params.get('intervals', ''))
    if interval_path.is_file():
        with interval_path.open(encoding='utf-8', errors='replace') as handle:
            result['input_interval_count'] = sum(1 for line in handle if line.strip() and not line.startswith(('@', '#')))
    result['shard_count'] = sum(1 for p in (output / 'caller').iterdir() if p.is_dir() and p.name.endswith('-model')) if (output / 'caller').exists() else 0
    result['convergence_parameters'] = params.get('gcnv_options', {})

    for path, rel, rows in tables:
        name = path.name.lower()
        if 'contig_ploidy' in name:
            sample_col = _find(rows, 'sample')
            contig_col = _find(rows, 'contig')
            ploidy_col = _find(rows, 'ploidy')
            gq_col = _find(rows, 'ploidy_gq') or _find(rows, 'gq')
            if ploidy_col and contig_col:
                result['ploidy'].extend({'sample': row.get(sample_col) if sample_col else _sample_name(path),
                                         'contig': row.get(contig_col), 'ploidy': _number(row.get(ploidy_col)),
                                         'gq': _number(row.get(gq_col)) if gq_col else None}
                                        for row in rows)
                result['exports']['ploidy'] = _emit_csv(output, 'ploidy', result['ploidy'])
        if 'global_read_depth' in name:
            for row in rows:
                sample_col = _find([row], 'sample')
                depth_col = _find([row], 'global_read_depth') or _find([row], 'read_depth')
                avg_col = _find([row], 'average_ploidy')
                if depth_col:
                    result['depth'].append({'sample': row.get(sample_col) if sample_col else _sample_name(path),
                                            'global_read_depth': _number(row.get(depth_col)),
                                            'average_ploidy': _number(row.get(avg_col)) if avg_col else None})
            result['exports']['depth'] = _emit_csv(output, 'global_read_depth', result['depth'])
        if 'tracking' in rel.lower() or any(token in name for token in ('snr', 'epoch', 'converg')):
            for row in rows:
                metric = {key: _number(value) for key, value in row.items()}
                metric['_source'] = rel
                result['convergence'].append(metric)
            result['exports']['convergence'] = _emit_csv(output, 'convergence', result['convergence'])
        if any(token in name for token in ('psi', 'variance', 'unexplained')):
            for row in rows:
                parsed = {key: _number(value) for key, value in row.items()}
                parsed['_source'] = rel
                result['variance'].append(parsed)
            result['exports']['variance'] = _emit_csv(output, 'unexplained_variance', result['variance'])
        if any(token in name for token in ('denoised_copy_ratio', 'copy_ratio')):
            result['copy_ratios'].append({'source': rel, 'rows': len(rows), 'columns': list(rows[0])})
        if any(token in name for token in ('copy_number', 'cnv_call', 'posterior')):
            result['cn_calls'].append({'source': rel, 'rows': len(rows), 'columns': list(rows[0])})

    if not result['ploidy']:
        result['warnings'].append('No se encontró una tabla contig_ploidy interpretable.')
    if not result['depth']:
        result['warnings'].append('No se encontró una tabla global_read_depth interpretable.')
    result['convergence_status'] = 'unknown'
    log = Path(log_path) if log_path else output / 'nextflow.log'
    if log.is_file():
        log_text = log.read_text(encoding='utf-8', errors='replace')
        text = log_text.lower()
        if re.search(r'\b(inference )?diverg(ed|ence)\b', text):
            result['convergence_status'] = 'divergence_reported'
        elif re.search(r'\bconverged\b', text) and not re.search(r'\bnot converged\b', text):
            result['convergence_status'] = 'converged_reported'
        for line in log_text.splitlines():
            snr_match = re.search(r'\bsnr\s*[:=]\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)', line, re.IGNORECASE)
            if not snr_match:
                continue
            step_match = re.search(r'\b(?:epoch|iteration|iter)\s*[:=#]?\s*(\d+)', line, re.IGNORECASE)
            result['convergence'].append({'iteration': int(step_match.group(1)) if step_match else len(result['convergence']) + 1,
                                          'training_snr': float(snr_match.group(1)), '_source': 'DEBUG log'})
        if result['convergence'] and not result.get('exports', {}).get('convergence'):
            result['exports']['convergence'] = _emit_csv(output, 'convergence', result['convergence'])
    has_snr = any(any('snr' in key and _number(value) is not None for key, value in row.items())
                  for row in result['convergence'])
    if not has_snr:
        result['warnings'].append('No se detectó tracking SNR interpretable; revisa los artefactos del caller y el nivel DEBUG.')
    result['interpretation'] = 'La evaluación combina dimensiones; no existe un umbral binario universal de calidad.'
    (output / 'model_quality.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
