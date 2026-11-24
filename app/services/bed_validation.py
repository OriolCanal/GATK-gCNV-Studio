from collections import Counter
import hashlib
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def parse_sequence_dictionary(path):
    contigs = {}
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            if not line.startswith('@SQ\t'):
                continue
            fields = dict(item.split(':', 1) for item in line.rstrip('\n').split('\t')[1:] if ':' in item)
            if 'SN' in fields and 'LN' in fields:
                try:
                    contigs[fields['SN']] = int(fields['LN'])
                except ValueError:
                    continue
    return contigs


def validate_bed(path, reference, canonical_path=None):
    contigs = parse_sequence_dictionary(reference['dict'])
    aliases = reference.get('aliases') or {}
    errors = []
    warnings = []
    lengths = []
    counts_by_chromosome = Counter()
    total_bases = 0
    previous_by_chromosome = {}
    last_start_by_chromosome = {}
    seen_source_contigs = {}
    reference_rank = {contig: index for index, contig in enumerate(contigs)}
    previous_reference_rank = -1
    seen = set()
    interval_count = 0

    canonical_handle = open(canonical_path, 'w', encoding='utf-8') if canonical_path else None
    try:
      with open(path, encoding='utf-8') as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith('#') or line.startswith('track ') or line.startswith('browser '):
                if canonical_handle:
                    canonical_handle.write(raw)
                continue
            fields = line.split('\t')
            if len(fields) < 3:
                errors.append({'line': line_number, 'message': 'Se requieren al menos 3 columnas BED.'})
                continue
            chrom = fields[0]
            try:
                start, end = int(fields[1]), int(fields[2])
            except ValueError:
                errors.append({'line': line_number, 'message': 'Las coordenadas start/end deben ser enteros.'})
                continue
            if start < 0 or end <= start:
                errors.append({'line': line_number, 'message': 'Coordenadas inválidas: se requiere start >= 0 y end > start.'})
                continue

            resolved_chrom = aliases.get(chrom, chrom)
            if resolved_chrom not in contigs:
                errors.append({'line': line_number, 'message': f'Contig {chrom!r} no está en el diccionario de referencia.'})
                continue
            if end > contigs[resolved_chrom]:
                errors.append({'line': line_number, 'message': f'El intervalo excede la longitud del contig {resolved_chrom}.'})
                continue

            source_contig = seen_source_contigs.get(resolved_chrom)
            if source_contig and source_contig != chrom:
                errors.append({'line': line_number, 'message': f'Colisión de aliases: {source_contig!r} y {chrom!r} resuelven a {resolved_chrom!r}.'})
                continue
            seen_source_contigs[resolved_chrom] = chrom

            contig_rank = reference_rank[resolved_chrom]
            if contig_rank < previous_reference_rank:
                warnings.append({'line': line_number, 'message': 'BED no ordenado según el diccionario de referencia.'})
            previous_reference_rank = max(previous_reference_rank, contig_rank)

            if canonical_handle:
                fields[0] = resolved_chrom
                canonical_handle.write('\t'.join(fields) + '\n')

            key = (resolved_chrom, start, end)
            if key in seen:
                warnings.append({'line': line_number, 'message': 'Intervalo duplicado.'})
            seen.add(key)
            previous = previous_by_chromosome.get(resolved_chrom)
            if previous:
                max_end, max_end_line = previous
                if start < last_start_by_chromosome[resolved_chrom]:
                    warnings.append({'line': line_number, 'message': 'BED no ordenado por posición dentro del cromosoma.'})
                if start < max_end:
                    warnings.append({'line': line_number, 'message': f'Solapamiento con línea {max_end_line}.'})
                if end > max_end:
                    previous_by_chromosome[resolved_chrom] = (end, line_number)
            else:
                previous_by_chromosome[resolved_chrom] = (end, line_number)
            last_start_by_chromosome[resolved_chrom] = start

            interval_length = end - start
            interval_count += 1
            lengths.append(interval_length)
            total_bases += interval_length
            counts_by_chromosome[resolved_chrom] += 1
    finally:
        if canonical_handle:
            canonical_handle.close()

    if interval_count == 0:
        errors.append({'line': None, 'message': 'El BED no contiene intervalos válidos.'})

    ordered = sorted(lengths)
    median = None
    if ordered:
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    metrics = {
        'interval_count': interval_count,
        'total_bases': total_bases,
        'mean_interval_length': (total_bases / interval_count) if interval_count else None,
        'median_interval_length': median,
        'interval_lengths': lengths,
        'intervals_by_chromosome': dict(counts_by_chromosome),
        'overlap_warning_count': sum('Solapamiento' in item['message'] for item in warnings),
        'duplicate_warning_count': sum('duplicado' in item['message'] for item in warnings),
    }
    return {
        'valid': not errors,
        'errors': errors,
        'warnings': warnings,
        'metrics': metrics,
        'chromosome_order': list(contigs),
        'alias_mapping': aliases,
    }
