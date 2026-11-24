import json
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml
from flask import current_app

from app import db
from app.models import (BamSample, BedProject, CohortModelRun, ModelValidationRun,
                        SampleBatch, WorkflowJob)
from app.services.bam_qc import build_project_qc, build_sample_metrics_partial
from app.services.bam_validation import sha256_file
from app.services.bed_metrics import build_bed_metrics, write_metrics
from app.services.cohort_diagnostics import build_cohort_diagnostics
from app.services.model_validation import build_validation_report


def _memory_bytes(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*\.?\s*([kmgt]?i?b?)?\s*', str(value), re.IGNORECASE)
    if not match:
        raise ValueError(f'Memoria no válida: {value!r}; usa, por ejemplo, "8 GB" o "4096 MB".')
    amount = float(match.group(1))
    unit = (match.group(2) or 'B').upper()
    unit = {'K': 'KB', 'KI': 'KIB', 'M': 'MB', 'MI': 'MIB', 'G': 'GB', 'GI': 'GIB',
            'T': 'TB', 'TI': 'TIB'}.get(unit, unit)
    factor = {'B': 1, 'KB': 1024, 'KIB': 1024, 'MB': 1024**2, 'MIB': 1024**2,
              'GB': 1024**3, 'GIB': 1024**3, 'TB': 1024**4, 'TIB': 1024**4}.get(unit)
    if factor is None:
        raise ValueError(f'Unidad de memoria no reconocida: {unit}.')
    return int(amount * factor)


def _worker_slots(config):
    workflow_cpus = int(config['WORKFLOW_CPUS'])
    workflow_memory = _memory_bytes(config['WORKFLOW_MEMORY'])
    process_cpu_max = max(config['BED_ANALYSIS_CPUS'], config['SAMPLE_QC_CPUS'])
    process_cpu_max = max(process_cpu_max, config['COHORT_MODEL_CPUS'])
    process_memory_max = max(_memory_bytes(config['BED_ANALYSIS_MEMORY']),
                             _memory_bytes(config['SAMPLE_QC_MEMORY']))
    process_memory_max = max(process_memory_max, _memory_bytes(config['COHORT_MODEL_MEMORY']))
    module_resources = config.get('MODULE_PROCESSES', {}).values()
    for process in module_resources:
        process_cpu_max = max(process_cpu_max, int(process['cpus']))
        process_memory_max = max(process_memory_max, _memory_bytes(process['memory']))
    if process_cpu_max > workflow_cpus or process_memory_max > workflow_memory:
        raise ValueError('El presupuesto por workflow debe cubrir los recursos del proceso más exigente.')
    cpu_slots = int(config['WORKER_CPU_BUDGET']) // workflow_cpus
    memory_slots = _memory_bytes(config['WORKER_MEMORY_BUDGET']) // workflow_memory
    slots = min(int(config['MAX_CONCURRENT_WORKFLOWS']), cpu_slots, memory_slots)
    if slots < 1:
        raise ValueError('Los presupuestos agregados del worker no alcanzan para ejecutar un workflow.')
    return slots


def _process_forks(resources, profile):
    workflow_cpus = int(resources.get('workflow_cpus', resources.get('cpus', 1)))
    workflow_memory = _memory_bytes(resources.get('workflow_memory', resources.get('memory', '1 GB')))
    process = resources.get('processes', {}).get(profile, {})
    process_cpus = max(1, int(process.get('cpus', resources.get('cpus', 1))))
    process_memory = _memory_bytes(process.get('memory', resources.get('memory', '1 GB')))
    if process_cpus > workflow_cpus or process_memory > workflow_memory:
        raise ValueError(f'Los recursos de {profile} superan el presupuesto local del workflow.')
    return max(1, min(workflow_cpus // process_cpus, workflow_memory // process_memory))


def _module_forks(resources, name):
    workflow_cpus = int(resources.get('workflow_cpus', resources.get('cpus', 1)))
    workflow_memory = _memory_bytes(resources.get('workflow_memory', resources.get('memory', '1 GB')))
    process = resources.get('modules', {}).get(name, {})
    process_cpus = max(1, int(process.get('cpus', 1)))
    process_memory = _memory_bytes(process.get('memory', '1 GB'))
    if process_cpus > workflow_cpus or process_memory > workflow_memory:
        raise ValueError(f'Los recursos de {name} superan el presupuesto local del workflow.')
    return max(1, min(workflow_cpus // process_cpus, workflow_memory // process_memory))


def _job_reservation(job):
    execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
    resources = execution.get('resources', {})
    cpus = max(1, int(resources.get('workflow_cpus', resources.get('cpus', 1))))
    memory = _memory_bytes(resources.get('workflow_memory', resources.get('memory', '1 GB')))
    return cpus, memory


def _append_job_log(job, message):
    try:
        path = Path(job.log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f'\n[{datetime.now(timezone.utc).isoformat()}] {message}\n')
    except OSError:
        pass


def claim_next_job(cpu_available=None, memory_available=None, cpu_budget=None, memory_budget=None):
    job = (WorkflowJob.query.filter_by(status='queued')
           .order_by(WorkflowJob.created_at.asc())
           .with_for_update(skip_locked=True).first())
    if job is None:
        return None
    cpus, memory = _job_reservation(job)
    if cpu_budget is not None and (cpus > cpu_budget or memory > memory_budget):
        message = (f'El job solicita {cpus} CPU y {memory / 1024**3:.2f} GiB, por encima del presupuesto '
                   f'configurado para el worker ({cpu_budget} CPU y {memory_budget / 1024**3:.2f} GiB).')
        job.status = 'running'
        job.error_message = message
        job.exit_code = 1
        job.finished_at = datetime.now(timezone.utc)
        db.session.commit()
        _append_job_log(job, message)
        from app.routes import reconcile_finished_jobs
        reconcile_finished_jobs()
        return ('rejected', 0, 0)
    if cpu_available is not None and (cpus > cpu_available or memory > memory_available):
        return None
    job.status = 'running'
    job.started_at = datetime.now(timezone.utc)
    db.session.commit()
    return job.id, cpus, memory


def _versions(execution):
    versions = {'nextflow': 'unavailable', 'apptainer': 'unavailable'}
    for key, command in (('nextflow', [execution['nextflow_binary'], '-version']),
                         ('apptainer', ['apptainer', '--version'])):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            versions[key] = (result.stdout or result.stderr).strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return versions


def _process_group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ensure_process_group_stopped(pgid, log):
    """Do not release a worker reservation while task descendants remain alive."""
    if not _process_group_exists(pgid):
        return True
    log.write('\nNextflow terminó, pero quedan procesos en su grupo; enviando SIGTERM.\n')
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not _process_group_exists(pgid):
            return True
        time.sleep(0.25)
    log.write('El grupo sigue activo; enviando SIGKILL.\n')
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _process_group_exists(pgid):
            return True
        time.sleep(0.25)
    log.write('No se pudo confirmar la terminación completa del grupo Nextflow.\n')
    return False


def _execute_nextflow(job, params, execution):
    job_dir = Path(job.execution_path).parent
    output_dir = Path(job.output_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    job_dir.mkdir(parents=True, exist_ok=True)
    runtime = {**params, 'outdir': execution['output_path'],
               'process_cpus': execution['resources']['cpus'],
               'process_memory': execution['resources']['memory'],
               'process_time': execution['resources']['time'],
               'gatk_container': execution['gatk_container']}
    resources = execution.get('resources', {})
    for profile in ('bed_analysis', 'sample_qc', 'cohort_model'):
        process = resources.get('processes', {}).get(profile, {})
        runtime[f'{profile}_cpus'] = int(process.get('cpus', resources.get('cpus', 1)))
        runtime[f'{profile}_memory'] = process.get('memory', resources.get('memory', '1 GB'))
        runtime[f'{profile}_time'] = process.get('time', resources.get('time', '24h'))
        runtime[f'{profile}_max_forks'] = _process_forks(resources, profile)
    runtime['workflow_cpus'] = int(resources.get('workflow_cpus', resources.get('cpus', 1)))
    runtime['workflow_memory'] = resources.get('workflow_memory', resources.get('memory', '1 GB'))
    for name, process in resources.get('modules', {}).items():
        runtime[f'{name}_cpus'] = int(process['cpus'])
        runtime[f'{name}_memory'] = process['memory']
        runtime[f'{name}_time'] = process['time']
        runtime[f'{name}_max_forks'] = _module_forks(resources, name)
    runtime_path = job_dir / 'nextflow_params.yaml'
    runtime_text = yaml.safe_dump(runtime, sort_keys=False, allow_unicode=True)
    runtime_digest = hashlib.sha256(runtime_text.encode('utf-8')).hexdigest()
    if (execution.get('resume') and execution.get('resume_mode') != 'retry'
            and execution.get('runtime_params_sha256') != runtime_digest):
        raise RuntimeError('Los parámetros efectivos no coinciden con el snapshot de la ejecución; no se reanudará.')
    runtime_path.write_text(runtime_text, encoding='utf-8')
    if not execution.get('resume') or execution.get('resume_mode') == 'retry':
        execution['runtime_params_sha256'] = runtime_digest
        Path(job.execution_path).write_text(
            yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
    command = [execution['nextflow_binary'], 'run', execution['workflow_path'],
               '-c', execution.get('nextflow_config_path', str(Path.cwd() / 'nextflow.config')),
               '-profile', execution['executor_profile'], '-params-file', str(runtime_path),
               '-work-dir', execution['work_path'], '-with-report', str(job_dir / 'nextflow_report.html'),
               '-with-timeline', str(job_dir / 'nextflow_timeline.html'),
               '-with-trace', str(job_dir / 'nextflow_trace.txt')]
    if execution.get('resume'):
        command.append('-resume')
    env = os.environ.copy()
    env['GATK_GCNV_APPTAINER_CACHE'] = execution['apptainer_cache']
    env['APPTAINER_CACHEDIR'] = execution['apptainer_cache']
    env['NXF_ANSI_LOG'] = 'false'
    versions = _versions(execution)
    log_path = Path(job.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code = -1
    resume = bool(execution.get('resume'))
    with log_path.open('a' if resume else 'w', encoding='utf-8') as log:
        if resume:
            log.write('\n--- Reanudando la sesión Nextflow previa con -resume ---\n')
        log.write('Command: ' + json.dumps(command) + '\n\n')
        log.flush()
        try:
            launch_path = Path(execution.get('resume_launch_path') or job_dir).resolve()
            if not launch_path.is_dir():
                launch_path = job_dir
            process = subprocess.Popen(command, cwd=str(launch_path), env=env,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            pid_path = job_dir / 'nextflow.pid'
            try:
                pid_path.write_text(f'{process.pid}\n', encoding='ascii')
            except OSError as exc:
                log.write(f'No se pudo guardar el PID Nextflow: {exc}; deteniendo el grupo.\n')
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                _ensure_process_group_stopped(process.pid, log)
                raise
            sent_term_at = None
            while process.poll() is None:
                db.session.expire_all()
                fresh = db.session.get(WorkflowJob, job.id)
                cancel = fresh is not None and fresh.status == 'cancel_requested'
                db.session.rollback()
                if cancel:
                    if sent_term_at is None:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        sent_term_at = time.monotonic()
                    elif time.monotonic() - sent_term_at > 15:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                time.sleep(0.5)
            exit_code = process.wait()
            if not _ensure_process_group_stopped(process.pid, log):
                exit_code = exit_code or 1
            pid_path.unlink(missing_ok=True)
        except OSError as exc:
            log.write(f'\nError iniciando Nextflow: {exc}\n')
    return exit_code, versions


def _set_job_result(job, exit_code):
    db.session.expire_all()
    job = db.session.get(WorkflowJob, job.id)
    job.exit_code = exit_code
    if job.status == 'cancel_requested':
        job.status = 'cancelled'
        job.error_message = 'Cancelado por el usuario.'
    elif exit_code == 0:
        job.status = 'succeeded'
        job.error_message = None
    else:
        job.status = 'failed'
        job.error_message = f'Nextflow finalizó con código {exit_code}.'
    return job


def _wait_for_qc_publication(output, samples, timeout_seconds=180):
    """Wait until Nextflow's asynchronous publishDir copies are complete."""
    expected = [output / f'{sample.sample_id}.{suffix}'
                for sample in samples
                for suffix in ('counts.hdf5', 'hs_metrics.txt', 'flagstat.txt')]
    deadline = time.monotonic() + timeout_seconds
    previous = None
    stable_checks = 0
    while time.monotonic() < deadline:
        if all(path.is_file() for path in expected):
            current = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in expected)
            if current == previous:
                stable_checks += 1
                if stable_checks >= 3:
                    return True
            else:
                previous = current
                stable_checks = 0
        time.sleep(1)
    return False


def _read_interval_list(path):
    intervals = []
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith('@'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 3:
                raise ValueError(f'Fila no reconocible en interval_list: {line[:120]!r}')
            intervals.append((fields[0], int(fields[1]), int(fields[2])))
    return intervals


def _write_model_interval_report(parameters, output):
    original = _read_interval_list(parameters['intervals'])
    filter_enabled = bool(parameters['filter_options']['enabled'])
    filtered_path = output / 'filtered.interval_list'
    retained = _read_interval_list(filtered_path) if filter_enabled else original
    original_set, retained_set = set(original), set(retained)
    excluded = [interval for interval in original if interval not in retained_set]
    retained_rows = ['CONTIG\tSTART\tEND\n'] + [f'{c}\t{s}\t{e}\n' for c, s, e in retained]
    excluded_rows = ['CONTIG\tSTART\tEND\tREASON\n'] + [
        f'{c}\t{s}\t{e}\tfiltered_by_gatk_filter_intervals\n' for c, s, e in excluded]
    (output / 'retained_intervals.tsv').write_text(''.join(retained_rows), encoding='utf-8')
    (output / 'excluded_intervals.tsv').write_text(''.join(excluded_rows), encoding='utf-8')
    metrics = {
        'schema_version': '1.0', 'parser_version': '1.0',
        'gatk_version': parameters.get('gatk_version'),
        'filter_intervals_enabled': filter_enabled,
        'input_interval_count': len(original),
        'retained_interval_count': len(retained),
        'excluded_interval_count': len(excluded),
        'intervals_missing_from_filter_output': len(original_set - retained_set),
        'retained_intervals_path': 'retained_intervals.tsv',
        'excluded_intervals_path': 'excluded_intervals.tsv',
    }
    (output / 'model_metrics.json').write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
    return metrics


def _wait_for_model_publication(output, filter_enabled, sample_ids, timeout_seconds=180):
    expected = [output / 'ploidy', output / 'caller', output / 'gatk_version.txt']
    if filter_enabled:
        expected.append(output / 'filtered.interval_list')
    for index, _ in enumerate(sample_ids):
        sample_dir = output / 'postprocess' / f'SAMPLE_{index}'
        expected.extend(sample_dir / name for name in (
            'sample_name.txt', 'genotyped_intervals.vcf.gz', 'genotyped_segments.vcf.gz',
            'denoised_copy_ratios.tsv'))
    deadline = time.monotonic() + timeout_seconds
    previous = None
    stable_checks = 0
    while time.monotonic() < deadline:
        if all(path.is_file() or (path.is_dir() and any(item.is_file() for item in path.rglob('*')))
               for path in expected):
            current = tuple((path.stat().st_size, path.stat().st_mtime_ns) if path.is_file() else
                            (sum(item.stat().st_size for item in path.rglob('*') if item.is_file()),
                             max((item.stat().st_mtime_ns for item in path.rglob('*') if item.is_file()), default=0))
                            for path in expected)
            if current == previous:
                stable_checks += 1
                if stable_checks >= 2:
                    return True
            else:
                previous = current
                stable_checks = 0
        time.sleep(1)
    return False


def _wait_for_validation_publication(output, samples, timeout_seconds=180):
    expected = [output / f"{sample['sample_id']}.counts.hdf5" for sample in samples]
    for index, _sample in enumerate(samples):
        sample_dir = output / 'postprocess' / f'SAMPLE_{index}'
        expected.extend(sample_dir / name for name in (
            'sample_name.txt', 'genotyped_intervals.vcf.gz',
            'genotyped_segments.vcf.gz', 'denoised_copy_ratios.tsv'))
    deadline = time.monotonic() + timeout_seconds
    previous = None
    stable_checks = 0
    while time.monotonic() < deadline:
        if all(path.is_file() and path.stat().st_size > 0 for path in expected):
            current = tuple((path.stat().st_size, path.stat().st_mtime_ns) for path in expected)
            if current == previous:
                stable_checks += 1
                if stable_checks >= 2:
                    return True
            else:
                previous = current
                stable_checks = 0
        time.sleep(1)
    return False


def run_cohort_model_job(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if not job:
        return
    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
    execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
    model_run = db.session.get(CohortModelRun, params['model_run_id'])
    if not model_run:
        raise ValueError('No existe el snapshot persistido del modelo de cohorte.')
    if sha256_file(job.parameters_path) != model_run.parameters_sha256:
        raise ValueError('El YAML científico cambió desde que se encoló; se cancela para proteger el snapshot.')
    original_execution_path = Path(job.output_path) / 'execution.yaml'
    if not original_execution_path.is_file():
        raise ValueError('Falta el snapshot de recursos/backend de la ejecución.')
    original_execution = yaml.safe_load(original_execution_path.read_text(encoding='utf-8')) or {}
    for key, value in original_execution.items():
        if execution.get(key) != value:
            raise ValueError(f'El snapshot de ejecución cambió desde que se encoló ({key}).')
    if not execution.get('resume') and sha256_file(job.execution_path) != model_run.execution_sha256:
        raise ValueError('El YAML de ejecución cambió antes de iniciar el modelo.')
    immutable_inputs = [(params['intervals'], params['intervals_sha256']),
                        (params['contig_ploidy_priors'], params['contig_ploidy_priors_sha256'])]
    if params.get('reference_dict'):
        immutable_inputs.append((params['reference_dict'], params.get('reference_dict_sha256')))
    if params.get('reference_fasta') and params.get('reference_fasta_sha256'):
        immutable_inputs.append((params['reference_fasta'], params['reference_fasta_sha256']))
    immutable_inputs.extend((sample['counts'], sample['counts_sha256']) for sample in params['samples'])
    for input_path, expected_sha256 in immutable_inputs:
        if not Path(input_path).is_file() or sha256_file(input_path) != expected_sha256:
            raise ValueError(f'El input del snapshot no existe o cambió desde que se encoló: {input_path}')
    model_run.status = 'running'
    db.session.commit()

    exit_code, versions = _execute_nextflow(job, params, execution)
    job = _set_job_result(job, exit_code)
    parse_warnings = []
    diagnostics = None
    if job.status == 'succeeded':
        if not _wait_for_model_publication(Path(job.output_path), params['filter_options']['enabled'],
                                           [sample['sample_id'] for sample in params['samples']]):
            parse_warnings.append('No se pudieron confirmar todos los outputs publicados por Nextflow.')
        version_path = Path(job.output_path) / 'gatk_version.txt'
        if not version_path.is_file():
            parse_warnings.append('No se publicó la versión efectiva de GATK.')
        elif params['gatk_version'] not in version_path.read_text(encoding='utf-8', errors='replace'):
            parse_warnings.append('La versión GATK del contenedor difiere de la versión fijada en los parámetros.')
        try:
            metrics = _write_model_interval_report(params, Path(job.output_path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parse_warnings.append(f'No se pudo comparar intervalos: {exc}')
            metrics = None
        try:
            diagnostics = build_cohort_diagnostics(Path(job.output_path), params)
            parse_warnings.extend(diagnostics.get('warnings', []))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            current_app.logger.exception('No se pudieron precalcular diagnósticos del cohorte para job %s', job.id)
            parse_warnings.append(f'No se pudieron precalcular diagnósticos del cohorte: {exc}')
        expected = (Path(job.output_path) / 'ploidy', Path(job.output_path) / 'caller')
        if not all(path.is_dir() and any(item.is_file() for item in path.rglob('*')) for path in expected):
            parse_warnings.append('Falta el directorio de modelo o llamadas en las salidas de GATK.')
        if parse_warnings:
            job.status = 'succeeded_with_parse_warnings'
            job.error_message = 'GATK terminó correctamente; ' + ' '.join(parse_warnings)
    job.finished_at = datetime.now(timezone.utc)
    model_run.status = job.status
    model_run.error_message = job.error_message
    model_run.finished_at = job.finished_at
    metadata = {
        'schema_version': '1.0', 'model_run_id': model_run.id, 'job_id': job.id,
        'workflow': job.workflow, 'workflow_version': params['pipeline_version'],
        'gatk_version': params['gatk_version'], 'nextflow_version': versions['nextflow'],
        'gatk_version_reported': ((Path(job.output_path) / 'gatk_version.txt').read_text(encoding='utf-8', errors='replace').strip()
                                  if (Path(job.output_path) / 'gatk_version.txt').is_file() else None),
        'apptainer_version': versions['apptainer'], 'parameters': params,
        'execution': execution, 'exit_code': exit_code, 'status': job.status,
        'metrics': metrics if job.status in {'succeeded', 'succeeded_with_parse_warnings'} else None,
        'quality_parse_status': ('partial' if parse_warnings else 'available') if job.status in {'succeeded', 'succeeded_with_parse_warnings'} else 'unavailable',
        'quality_summary': ({'convergence_status': diagnostics['metadata']['convergence_status'],
                            'samples': diagnostics['summary']['sample_count'],
                            'original_intervals': diagnostics['summary']['original_interval_count'],
                            'retained_intervals': diagnostics['summary']['retained_interval_count'],
                            'warnings': diagnostics['warnings']} if diagnostics else None),
        'finished_at': job.finished_at.isoformat(),
    }
    output = Path(job.output_path)
    output.mkdir(parents=True, exist_ok=True)
    job_dir = Path(job.execution_path).parent
    for filename in ('parameters.yaml', 'execution.yaml', 'nextflow_params.yaml',
                     'nextflow_report.html', 'nextflow_timeline.html', 'nextflow_trace.txt'):
        source = job_dir / filename
        if source.is_file():
            shutil.copy2(source, output / filename)
    (output / 'run_metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    db.session.commit()


def run_model_validation_job(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if not job:
        return
    run = ModelValidationRun.query.filter_by(job_id=job.id).first()
    if not run:
        raise ValueError('No existe el registro de validación asociado al job.')
    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
    execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
    model_run = db.session.get(CohortModelRun, run.model_run_id)
    if not model_run or model_run.status not in {'succeeded', 'succeeded_with_parse_warnings'}:
        raise ValueError('El modelo asociado ya no está disponible para validación.')
    immutable = [(sample['bam'], sample['bam_sha256']) for sample in params['samples']]
    immutable.extend((sample['bai'], sample['bai_sha256']) for sample in params['samples'])
    for path, expected in immutable:
        if not Path(path).is_file() or sha256_file(path) != expected:
            raise ValueError(f'El input de validación ya no existe o cambió: {path}')
    run.status = 'running'
    db.session.commit()

    exit_code, versions = _execute_nextflow(job, params, execution)
    job = _set_job_result(job, exit_code)
    warnings = []
    summary = None
    if job.status == 'succeeded':
        if not _wait_for_validation_publication(Path(job.output_path), params['samples']):
            warnings.append('No se pudieron confirmar todos los outputs publicados por Nextflow.')
        try:
            summary = build_validation_report(Path(job.output_path), params)
            for item in summary.get('samples', []):
                if not (Path(job.output_path) / 'postprocess' / f"SAMPLE_{summary['samples'].index(item)}" /
                        'genotyped_intervals.vcf.gz').is_file():
                    warnings.append(f"No se publicó el VCF de intervalos para {item['sample_id']}.")
            if warnings:
                job.status = 'succeeded_with_parse_warnings'
                job.error_message = 'GATK terminó correctamente; ' + ' '.join(warnings)
                summary['warnings'] = warnings
            details_dir = Path(job.output_path) / 'sample_reports'
            details_dir.mkdir(parents=True, exist_ok=True)
            def chart_points(rows, limit=5000):
                if len(rows) <= limit:
                    return rows
                step = (len(rows) - 1) / (limit - 1)
                return [rows[round(index * step)] for index in range(limit)]

            def chart_sample(item):
                result = {key: item.get(key) for key in (
                    'sample_id', 'sample_unexplained_variance_log_psi',
                    'sample_unexplained_variance_exp_log_psi_derived', 'global_read_depth',
                    'bias_factor_loadings')}
                result['gc_coverage_points'] = chart_points(item.get('gc_coverage_points') or [])
                return result

            validation_charts = {
                'samples': [chart_sample(item) for item in summary.get('samples', [])],
                'gc_curve': summary.get('gc_curve', []),
                'gc_curve_available': summary.get('gc_curve_available', False),
                'gc_coverage_available': summary.get('gc_coverage_available', False),
                'autosomal_ploidy_exceptions': summary.get('autosomal_ploidy_exceptions', []),
            }
            (Path(job.output_path) / 'validation_charts.json').write_text(
                json.dumps(validation_charts, ensure_ascii=False), encoding='utf-8')
            compact_samples = []
            for item in summary.get('samples', []):
                report_path = details_dir / f"{item['sample_id']}.json"
                report = dict(item)
                report.pop('gc_coverage_points', None)
                report_path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
                compact_samples.append({key: item[key] for key in (
                    'sample_id', 'cnv_detected_count', 'known_cnv_count',
                    'matched_known_cnv_count', 'model_correlation',
                    'sample_unexplained_variance_log_psi',
                    'sample_unexplained_variance_exp_log_psi_derived', 'global_read_depth')})
            summary['samples'] = compact_samples
            (Path(job.output_path) / 'validation_summary.json').write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            run.summary_json = json.dumps(summary, ensure_ascii=False)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f'No se pudo resumir la validación: {exc}')
            job.status = 'succeeded_with_parse_warnings'
            job.error_message = 'GATK terminó correctamente; ' + ' '.join(warnings)
    job.finished_at = datetime.now(timezone.utc)
    run.status = job.status
    run.error_message = job.error_message
    run.finished_at = job.finished_at
    output = Path(job.output_path)
    output.mkdir(parents=True, exist_ok=True)
    metadata = {'schema_version': '1.0', 'workflow': job.workflow, 'job_id': job.id,
        'validation_run_id': run.id, 'model_run_id': run.model_run_id,
        'nextflow_version': versions.get('nextflow'), 'apptainer_version': versions.get('apptainer'),
        'exit_code': exit_code, 'status': job.status, 'error': job.error_message,
        'finished_at': job.finished_at.isoformat()}
    (output / 'run_metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    db.session.commit()


def run_bed_job(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if not job:
        return
    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8'))
    execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8'))
    output = Path(job.output_path)
    project = db.session.get(BedProject, job.project_id)
    analysis_root = Path(project.storage_path) / 'bed_analysis'
    resolved_root = analysis_root.resolve()
    for old in WorkflowJob.query.filter_by(project_id=job.project_id).all():
        if old.id == job.id or old.workflow != 'bed_analysis':
            continue
        old_output = Path(old.output_path).resolve()
        old_run = Path(old.execution_path).resolve().parent
        old_log = Path(old.log_path).resolve()
        if old_log.is_file() and old_log.is_relative_to(old_run):
            archive = analysis_root / 'logs' / f'{old.id}.log'
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old_log, archive)
            old.log_path = str(archive)
            db.session.commit()
        if (old.status != 'failed' and old_run.is_dir()
                and old_run.parent == (analysis_root / 'jobs').resolve()):
            shutil.rmtree(old_run, ignore_errors=True)
        if old_output.is_dir() and old_output.parent == resolved_root and old_output != output.resolve():
            shutil.rmtree(old_output, ignore_errors=True)
    if output.exists() and not execution.get('resume'):
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    exit_code, versions = _execute_nextflow(job, params, execution)
    job = _set_job_result(job, exit_code)
    if job.status == 'succeeded':
        try:
            validation_path = Path(project.storage_path) / 'bed' / 'validation_report.json'
            metrics = build_bed_metrics(json.loads(validation_path.read_text(encoding='utf-8')),
                                        output / 'preprocessed.interval_list',
                                        output / 'annotated_intervals.tsv', params)
            write_metrics(output / 'bed_metrics.json', metrics)
        except (OSError, ValueError, KeyError) as exc:
            job.status = 'succeeded_with_parse_warnings'
            job.error_message = f'GATK terminó correctamente; no se pudieron parsear todos los outputs: {exc}'
    job.finished_at = datetime.now(timezone.utc)
    metadata = {'schema_version': '1.0', 'job_id': job.id, 'workflow': job.workflow,
                'workflow_version': params.get('pipeline_version', '0.1.0'),
                'gatk_version': params.get('gatk_version'), 'nextflow_version': versions['nextflow'],
                'apptainer_version': versions['apptainer'], 'parameters': params,
                'execution': execution, 'exit_code': exit_code, 'status': job.status}
    (output / 'run_metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    active = WorkflowJob.query.filter(WorkflowJob.project_id == project.id,
                                      WorkflowJob.status.in_(['queued', 'running', 'cancel_requested'])).count()
    if not active:
        project.status = 'analysis_complete' if job.status in {'succeeded', 'succeeded_with_parse_warnings'} else (
            'analysis_cancelled' if job.status == 'cancelled' else 'analysis_failed')
    db.session.commit()


def run_sample_qc_job(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if not job:
        return
    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8'))
    execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8'))
    output = Path(job.output_path)
    batch = db.session.get(SampleBatch, params['batch_id'])
    if params.get('sample_ids'):
        samples = BamSample.query.filter(BamSample.id.in_(params['sample_ids'])).order_by(BamSample.sample_id).all()
    else:
        samples = BamSample.query.filter_by(batch_id=params['batch_id']).order_by(BamSample.sample_id).all()
    upload_batches = {sample.batch_id: sample.batch for sample in samples}
    batch.status = 'running'
    batch.error_message = None
    for upload_batch in upload_batches.values():
        upload_batch.status = 'running'
        upload_batch.error_message = None
    for sample in samples:
        sample.status = 'qc_running'
        sample.error_message = None
    db.session.commit()
    for sample in samples:
        if not sample.bam_sha256:
            sample.bam_sha256 = sha256_file(sample.bam_path)
    db.session.commit()
    exit_code, versions = _execute_nextflow(job, params, execution)
    job = _set_job_result(job, exit_code)
    publication_complete = True
    if job.status == 'succeeded':
        publication_complete = _wait_for_qc_publication(output, samples)
    parse_warnings = []
    for sample in samples:
        counts_path = output / f'{sample.sample_id}.counts.hdf5'
        hs_path = output / f'{sample.sample_id}.hs_metrics.txt'
        flag_path = output / f'{sample.sample_id}.flagstat.txt'
        validation_path = output / f'{sample.sample_id}.validation.txt'
        sample.counts_path = str(counts_path) if counts_path.is_file() else None
        if validation_path.is_file():
            sample.validation_report = validation_path.read_text(encoding='utf-8', errors='replace')
        if not counts_path.is_file():
            sample.status = 'qc_cancelled' if job.status == 'cancelled' else 'qc_failed'
            sample.error_message = job.error_message or 'Nextflow no generó read counts para esta muestra.'
            continue
        try:
            metrics, _intervals, _counts, metric_errors = build_sample_metrics_partial(
                sample.sample_id, sample.batch.name if sample.batch else batch.name,
                counts_path, hs_path, flag_path, batch.low_count_threshold)
            metrics_path = output / f'{sample.sample_id}.qc_metrics.json'
            metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
            sample.qc_metrics_path = str(metrics_path)
            sample.status = 'qc_complete' if not metric_errors else 'qc_partial'
            sample.error_message = '; '.join(metric_errors) if metric_errors else None
            if sample.status == 'qc_partial':
                parse_warnings.append(sample.sample_id)
        except (OSError, ValueError, KeyError) as exc:
            sample.status = 'qc_partial'
            sample.error_message = f'No se pudieron interpretar todas las métricas: {exc}'
            parse_warnings.append(sample.sample_id)
    complete = sum(s.status in {'qc_complete', 'qc_partial'} for s in samples)
    failed = sum(s.status == 'qc_failed' for s in samples)
    if job.status == 'cancelled':
        batch.status = 'cancelled'
    elif complete and failed:
        batch.status = 'partial_failed'
    elif complete:
        batch.status = 'succeeded'
    else:
        batch.status = 'failed'
    if parse_warnings:
        batch.error_message = 'QC parcial para: ' + ', '.join(parse_warnings)
        if job.status == 'succeeded':
            job.status = 'succeeded_with_parse_warnings'
    elif failed:
        batch.error_message = f'{failed} muestra(s) no completaron QC.'
    elif batch.status == 'failed':
        batch.error_message = job.error_message or 'Ninguna muestra produjo salidas QC.'
        if job.status == 'succeeded':
            job.status = 'failed'
            job.error_message = batch.error_message
    if not publication_complete:
        warning = 'Algunas salidas de Nextflow no aparecieron o seguían copiándose al storage tras 180 segundos.'
        batch.error_message = f'{batch.error_message}; {warning}' if batch.error_message else warning
        if job.status == 'succeeded':
            job.status = 'succeeded_with_parse_warnings'
            job.error_message = warning
    for upload_batch in upload_batches.values():
        upload_samples = [sample for sample in samples if sample.batch_id == upload_batch.id]
        upload_complete = sum(sample.status in {'qc_complete', 'qc_partial'} for sample in upload_samples)
        upload_failed = sum(sample.status in {'qc_failed', 'qc_cancelled'} for sample in upload_samples)
        upload_batch.status = ('partial_failed' if upload_complete and upload_failed else
                               'succeeded' if upload_complete else batch.status)
        upload_batch.error_message = batch.error_message
    job.finished_at = datetime.now(timezone.utc)
    if complete:
        try:
            all_samples = BamSample.query.filter(BamSample.project_id == job.project_id,
                                                 BamSample.status.in_(['qc_complete', 'qc_partial'])).all()
            names = {b.id: b.name for b in SampleBatch.query.filter(
                SampleBatch.id.in_({s.batch_id for s in all_samples})).all()} if all_samples else {}
            summary = build_project_qc([(s, names.get(s.batch_id, '')) for s in all_samples], batch.low_count_threshold)
            summary_path = Path(job.project.storage_path) / 'samples' / 'project_qc_summary.json'
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        except (OSError, ValueError, KeyError, np.linalg.LinAlgError) as exc:
            warning = f'QC individual completado, pero falló el resumen de cohorte: {exc}'
            batch.error_message = f'{batch.error_message}; {warning}' if batch.error_message else warning
            if job.status == 'succeeded':
                job.status = 'succeeded_with_parse_warnings'
                job.error_message = warning
    metadata = {'schema_version': '1.0', 'job_id': job.id, 'workflow': job.workflow,
                'gatk_version': params.get('gatk_version'), 'nextflow_version': versions['nextflow'],
                'apptainer_version': versions['apptainer'], 'parameters': params,
                'execution': execution, 'exit_code': exit_code, 'status': job.status,
                'batch_id': batch.id, 'sample_count': len(samples)}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'run_metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    db.session.commit()


def _run_claimed_job(app, job_id):
    with app.app_context():
        try:
            job = db.session.get(WorkflowJob, job_id)
            if job and job.workflow == 'cohort_model':
                run_cohort_model_job(job_id)
            elif job and job.workflow == 'model_validation':
                run_model_validation_job(job_id)
            elif job and job.workflow == 'sample_qc':
                run_sample_qc_job(job_id)
            else:
                run_bed_job(job_id)
        except Exception as exc:
            db.session.rollback()
            job = db.session.get(WorkflowJob, job_id)
            if job:
                job.status = 'failed'
                job.error_message = str(exc)
                job.finished_at = datetime.now(timezone.utc)
                _append_job_log(job, f'Error interno del worker: {type(exc).__name__}: {exc}')
                try:
                    output = Path(job.output_path)
                    output.mkdir(parents=True, exist_ok=True)
                    metadata = {'schema_version': '1.0', 'job_id': job.id,
                                'workflow': job.workflow, 'status': 'failed',
                                'exit_code': job.exit_code,
                                'error': f'{type(exc).__name__}: {exc}',
                                'finished_at': job.finished_at.isoformat()}
                    (output / 'run_metadata.json').write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
                except OSError:
                    pass
                if job.workflow == 'sample_qc':
                    try:
                        params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8'))
                        batch = db.session.get(SampleBatch, params['batch_id'])
                        if batch:
                            batch.status, batch.error_message = 'failed', str(exc)
                        samples = (BamSample.query.filter(BamSample.id.in_(params['sample_ids'])).all()
                                   if params.get('sample_ids') else
                                   BamSample.query.filter_by(batch_id=params['batch_id']).all())
                        for sample in samples:
                            if sample.status not in {'qc_complete', 'qc_partial'}:
                                sample.status, sample.error_message = 'qc_failed', str(exc)
                            if sample.batch:
                                sample.batch.status = 'failed'
                                sample.batch.error_message = str(exc)
                    except Exception:
                        pass
                elif job.workflow == 'cohort_model':
                    try:
                        params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                        model_run = db.session.get(CohortModelRun, params.get('model_run_id'))
                        if model_run:
                            model_run.status = 'failed'
                            model_run.error_message = str(exc)
                            model_run.finished_at = job.finished_at
                    except (OSError, yaml.YAMLError):
                        pass
                elif job.workflow == 'model_validation':
                    run = ModelValidationRun.query.filter_by(job_id=job.id).first()
                    if run:
                        run.status = 'failed'
                        run.error_message = str(exc)
                        run.finished_at = job.finished_at
                else:
                    project = db.session.get(BedProject, job.project_id)
                    if project:
                        project.status = 'analysis_failed'
                db.session.commit()


def _stop_orphaned_nextflow(job):
    pid_path = Path(job.execution_path).parent / 'nextflow.pid'
    try:
        pid = int(pid_path.read_text(encoding='ascii').strip())
    except FileNotFoundError:
        # A hard kill can land in the small window between Popen and writing
        # nextflow.pid. Search for this job's unique work directory before
        # concluding that there is no process to stop.
        try:
            execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
            workflow = str(execution.get('workflow_path') or '')
            raw_work_path = execution.get('work_path')
            if not workflow or not raw_work_path:
                return False, 'No se pudo identificar la ejecución Nextflow sin su PID; no se reanudará automáticamente.'
            work_path = str(Path(raw_work_path).resolve())
            matches = []
            for proc in Path('/proc').glob('[0-9]*/cmdline'):
                try:
                    args = proc.read_bytes().replace(b'\\0', b' ').decode(errors='replace')
                except OSError:
                    continue
                if workflow in args and work_path in args:
                    matches.append(int(proc.parent.name))
            if not matches:
                return True, None
            if len(matches) != 1:
                return False, 'Se encontraron varios procesos Nextflow candidatos; no se reanudará automáticamente.'
            pid = matches[0]
        except (OSError, ValueError, TypeError, yaml.YAMLError):
            return False, 'No se pudo buscar de forma segura un Nextflow sin PID guardado.'
    except (OSError, ValueError):
        return False, 'No se pudo leer el PID guardado; no se reanudará automáticamente.'
    try:
        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except FileNotFoundError:
        return True, 'El proceso Nextflow anterior ya no existe.'
    except OSError:
        return False, 'No se pudo inspeccionar el proceso guardado; no se reanudará automáticamente.'
    try:
        execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
        expected_workflow = str(execution.get('workflow_path') or '')
        raw_work_path = execution.get('work_path')
        expected_work_path = str(Path(raw_work_path).resolve()) if raw_work_path else ''
        if (not cmdline or not expected_workflow or expected_workflow not in cmdline
                or not expected_work_path or expected_work_path not in cmdline):
            return False, 'No se pudo verificar de forma segura el PID de Nextflow; comprueba manualmente si queda un proceso huérfano.'
        if os.getpgid(pid) != pid:
            return False, 'El PID guardado ya no es líder de su grupo de procesos; no se reanudará automáticamente.'
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True, 'Se detuvo el proceso Nextflow huérfano.'
            time.sleep(0.25)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True, 'Se detuvo el proceso Nextflow huérfano.'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True, 'Se detuvo el proceso Nextflow huérfano con SIGKILL.'
            time.sleep(0.25)
        return False, 'El grupo Nextflow sigue activo tras SIGKILL; no se reanudará automáticamente.'
    except (OSError, ValueError, yaml.YAMLError):
        return False, 'No se pudo detener/verificar el proceso Nextflow; no se reanudará automáticamente.'


def _resume_artifacts_available(job):
    job_dir = Path(job.execution_path).parent
    try:
        execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
        work = Path(execution['work_path'])
        launch_dir = Path(execution.get('resume_launch_path') or job_dir)
        nextflow_state = launch_dir / '.nextflow'
        params_path = Path(job.parameters_path)
        runtime_path = job_dir / 'nextflow_params.yaml'
        runtime_digest = hashlib.sha256(runtime_path.read_bytes()).hexdigest() if runtime_path.is_file() else None
        return (params_path.is_file() and runtime_path.is_file()
                and runtime_digest == execution.get('runtime_params_sha256')
                and work.is_dir() and (nextflow_state / 'history').is_file()
                and (nextflow_state / 'cache').is_dir())
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return False


def recover_interrupted_jobs(app):
    """Safely resume one interrupted Nextflow session; close unverified runs with traceability."""
    with app.app_context():
        jobs = WorkflowJob.query.filter(
            WorkflowJob.status.in_(['running', 'cancel_requested']),
            WorkflowJob.finished_at.is_(None),
        ).all()
        if not jobs:
            return 0
        for job in jobs:
            process_stopped, process_note = _stop_orphaned_nextflow(job)
            metadata_path = Path(job.output_path) / 'run_metadata.json'
            metadata = {}
            if metadata_path.is_file():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                except (OSError, json.JSONDecodeError):
                    pass
            if process_note:
                _append_job_log(job, f'Recuperación al arrancar el worker: {process_note}')
            execution_path = Path(job.execution_path)
            execution = {}
            try:
                execution = yaml.safe_load(execution_path.read_text(encoding='utf-8')) or {}
            except (OSError, yaml.YAMLError):
                pass
            recovery_attempts = int(execution.get('recovery_attempts', 0))
            if not process_stopped:
                job.status = 'failed'
                job.error_message = process_note or 'No se pudo verificar que el proceso Nextflow anterior haya terminado.'
                job.finished_at = datetime.now(timezone.utc)
                job.exit_code = metadata.get('exit_code', job.exit_code)
                _append_job_log(job, f'Recuperación al arrancar el worker: {job.error_message}')
                if job.workflow == 'model_validation':
                    validation_run = ModelValidationRun.query.filter_by(job_id=job.id).first()
                    if validation_run:
                        validation_run.status = 'failed'
                        validation_run.error_message = job.error_message
                        validation_run.finished_at = job.finished_at
                continue
            if (job.status == 'running' and not metadata and process_stopped
                    and recovery_attempts < 1 and _resume_artifacts_available(job)):
                execution['resume'] = True
                execution['resume_mode'] = 'recovery'
                execution['recovery_attempts'] = recovery_attempts + 1
                execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True),
                                          encoding='utf-8')
                job.status = 'queued'
                job.started_at = None
                job.finished_at = None
                job.error_message = None
                _append_job_log(job, 'Recuperación al arrancar el worker: se verificaron la sesión, caché y work directory Nextflow; el job volverá a la cola y usará -resume una vez.')
                if job.workflow == 'sample_qc':
                    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                    samples = (BamSample.query.filter(BamSample.id.in_(params['sample_ids'])).all()
                               if params.get('sample_ids') else
                               BamSample.query.filter_by(batch_id=params.get('batch_id')).all())
                    for sample in samples:
                        if sample.status not in {'qc_complete', 'qc_partial'}:
                            sample.status, sample.error_message = 'queued', None
                    batch = db.session.get(SampleBatch, params.get('batch_id'))
                    if batch:
                        batch.status, batch.error_message = 'queued', None
                    for batch_id in {sample.batch_id for sample in samples}:
                        upload_batch = db.session.get(SampleBatch, batch_id)
                        if upload_batch:
                            upload_batch.status, upload_batch.error_message = 'queued', None
                else:
                    if job.workflow == 'cohort_model':
                        try:
                            params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                            model_run = db.session.get(CohortModelRun, params.get('model_run_id'))
                            if model_run:
                                model_run.status = 'queued'
                                model_run.error_message = None
                        except (OSError, TypeError, yaml.YAMLError):
                            pass
                    elif job.workflow == 'model_validation':
                        validation_run = ModelValidationRun.query.filter_by(job_id=job.id).first()
                        if validation_run:
                            validation_run.status = 'queued'
                            validation_run.error_message = None
                    else:
                        project = db.session.get(BedProject, job.project_id)
                        if project:
                            project.status = 'analysis_queued'
                continue
            if job.status == 'running' and not metadata:
                if not process_stopped:
                    job.error_message = process_note or 'No se pudo verificar el proceso Nextflow; comprueba el servidor antes de reintentar.'
                elif recovery_attempts >= 1:
                    job.error_message = 'El worker volvió a interrumpirse durante una reanudación; se detiene el reintento automático. Revisa el log.'
                else:
                    job.error_message = ('No hay una sesión/work cache Nextflow verificable para reanudar. El job se cierra '
                                         'como fallido y conserva sus logs y work directory para revisión.')
                _append_job_log(job, f'Recuperación al arrancar el worker: {job.error_message}')
            job.exit_code = metadata.get('exit_code', job.exit_code)
            if metadata.get('status') == 'failed':
                job.error_message = job.error_message or f"Nextflow terminó con código {metadata.get('exit_code', 'desconocido')} antes de que el worker guardara el estado final."
            elif metadata.get('status') == 'succeeded_with_parse_warnings':
                job.error_message = job.error_message or 'Nextflow terminó, pero el worker se interrumpió mientras registraba avisos de parseo.'
            if job.status == 'cancel_requested' and not metadata:
                job.error_message = 'Cancelación completada durante la recuperación del worker.'
            if job.status != 'queued':
                job.finished_at = datetime.now(timezone.utc)
            if job.workflow == 'model_validation':
                validation_run = ModelValidationRun.query.filter_by(job_id=job.id).first()
                if validation_run:
                    validation_run.status = job.status
                    validation_run.error_message = job.error_message
                    validation_run.finished_at = job.finished_at
        db.session.commit()
        # Reuse the web recovery path to finalize project, batch, and sample states.
        from app.routes import reconcile_finished_jobs
        reconcile_finished_jobs()
        return len(jobs)


def worker_loop(app, poll_seconds=2, stop_event=None):
    _worker_slots(app.config)  # fail fast on an inconsistent or unusable resource budget
    max_workers = app.config['MAX_CONCURRENT_WORKFLOWS']
    cpu_budget = app.config['WORKER_CPU_BUDGET']
    memory_budget = _memory_bytes(app.config['WORKER_MEMORY_BUDGET'])
    active = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while stop_event is None or not stop_event.is_set():
            active = {future: reservation for future, reservation in active.items() if not future.done()}
            used_cpus = sum(resources[0] for resources in active.values())
            used_memory = sum(resources[1] for resources in active.values())
            while len(active) < max_workers and used_cpus < cpu_budget and used_memory < memory_budget:
                with app.app_context():
                    claim = claim_next_job(cpu_budget - used_cpus, memory_budget - used_memory,
                                           cpu_budget, memory_budget)
                if claim is None:
                    break
                job_id, cpus, memory = claim
                if job_id == 'rejected':
                    continue
                active[pool.submit(_run_claimed_job, app, job_id)] = (cpus, memory)
                used_cpus += cpus
                used_memory += memory
            if stop_event is None:
                time.sleep(poll_seconds)
            else:
                stop_event.wait(poll_seconds)
