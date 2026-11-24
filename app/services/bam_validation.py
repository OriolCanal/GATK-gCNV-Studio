import hashlib
import os
import subprocess
from pathlib import Path

from app.services.bed_validation import parse_sequence_dictionary


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def is_within_allowed_roots(path, roots):
    resolved = Path(path).expanduser().resolve(strict=True)
    return any(resolved.is_relative_to(Path(root).resolve()) for root in roots)


def inspect_bam(bam_path, bai_path, sample_id, reference_dict, interval_list, samtools_bin='samtools'):
    bam = Path(bam_path)
    bai = Path(bai_path)
    if bam.suffix.lower() != '.bam' or not bam.is_file() or not bam.stat().st_size:
        raise ValueError('El BAM no existe, está vacío o no termina en .bam.')
    if not bai.is_file() or not bai.stat().st_size:
        raise ValueError('No se encuentra un índice BAI válido para el BAM.')
    if not os.access(bam, os.R_OK):
        raise ValueError('El BAM no tiene permisos de lectura para la aplicación.')
    if not os.access(bai, os.R_OK):
        raise ValueError('El BAI no tiene permisos de lectura para la aplicación.')

    quickcheck = subprocess.run(
        [samtools_bin, 'quickcheck', '-v', str(bam)], capture_output=True, text=True, timeout=180
    )
    if quickcheck.returncode:
        raise ValueError((quickcheck.stderr or quickcheck.stdout or 'samtools quickcheck rechazó el BAM.').strip())

    header_result = subprocess.run(
        [samtools_bin, 'view', '-H', str(bam)], capture_output=True, text=True, timeout=180
    )
    if header_result.returncode:
        raise ValueError((header_result.stderr or 'No se pudo leer la cabecera SAM del BAM.').strip())

    sort_order = None
    bam_contigs = {}
    read_group_samples = set()
    for line in header_result.stdout.splitlines():
        fields = dict(item.split(':', 1) for item in line.split('\t')[1:] if ':' in item)
        if line.startswith('@HD'):
            sort_order = fields.get('SO')
        elif line.startswith('@SQ') and fields.get('SN') and fields.get('LN'):
            try:
                bam_contigs[fields['SN']] = int(fields['LN'])
            except ValueError:
                raise ValueError(f"Longitud de contig inválida en la cabecera BAM: {fields.get('SN')}.")
        elif line.startswith('@RG') and fields.get('SM'):
            read_group_samples.add(fields['SM'])

    if sort_order != 'coordinate':
        raise ValueError('El BAM debe estar ordenado por coordenadas (cabecera @HD SO:coordinate).')
    if not read_group_samples:
        raise ValueError('El BAM no contiene read groups con etiqueta SM.')
    if len(read_group_samples) != 1:
        raise ValueError('El BAM debe representar una sola muestra: sus read groups contienen varios valores SM.')
    read_group_sm = next(iter(read_group_samples))
    if read_group_sm != sample_id:
        raise ValueError(f"El sample ID debe coincidir con el SM del BAM ({read_group_sm}).")

    unknown = [contig for contig in bam_contigs if contig not in reference_dict]
    if unknown:
        raise ValueError(f"El BAM contiene contigs que no están en la referencia del proyecto: {', '.join(unknown[:5])}.")
    mismatches = [contig for contig, length in bam_contigs.items() if reference_dict[contig] != length]
    if mismatches:
        raise ValueError(f"Las longitudes de contig del BAM no coinciden con la referencia: {', '.join(mismatches[:5])}.")

    target_contigs = set()
    with open(interval_list, encoding='utf-8') as handle:
        for line in handle:
            if line.strip() and not line.startswith('@'):
                target_contigs.add(line.split('\t', 1)[0])
    missing_targets = sorted(target_contigs - set(bam_contigs))
    if missing_targets:
        raise ValueError(f"Faltan contigs usados por el BED en el BAM: {', '.join(missing_targets[:5])}.")

    index_check = subprocess.run(
        [samtools_bin, 'idxstats', str(bam)], capture_output=True, text=True, timeout=300
    )
    if index_check.returncode:
        raise ValueError((index_check.stderr or 'samtools no pudo consultar el índice BAI.').strip())

    return {
        'valid': True,
        'sort_order': sort_order,
        'read_group_sm': read_group_sm,
        'read_group_count': sum(line.startswith('@RG') for line in header_result.stdout.splitlines()),
        'contig_count': len(bam_contigs),
        'indexed': True,
    }


def load_reference_dictionary(path):
    return parse_sequence_dictionary(path)
