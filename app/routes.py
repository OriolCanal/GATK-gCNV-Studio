import json
import hashlib
import fcntl
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.models import (BamSample, BedProject, CohortModelRun, ModelValidationRun,
                        SampleBatch, ValidationSample, WorkflowJob, new_id)
from app.services.bam_qc import build_project_qc, build_sample_metrics_partial, read_gatk_counts
from app.services.bam_validation import inspect_bam, is_within_allowed_roots, load_reference_dictionary
from app.services.bed_metrics import gene_label
from app.services.bed_validation import parse_sequence_dictionary, sha256_file, validate_bed
from app.services.cohort_diagnostics import (
    DiagnosticsUnavailable, build_cohort_diagnostics, query_intervals,
    interval_detail, sample_detail, entropy_heatmap, load_interval_diagnostics,
    coverage_deviation_heatmap, _contig_sort_key,
)
from app.services.model_validation import build_validation_report
from app.services.references import get_reference, list_references

bp = Blueprint('main', __name__)


def _execution_resources():
    cfg = current_app.config
    return {
        'cpus': cfg['PROCESS_CPUS'], 'memory': cfg['PROCESS_MEMORY'], 'time': cfg['PROCESS_TIME'],
        'workflow_cpus': cfg['WORKFLOW_CPUS'], 'workflow_memory': cfg['WORKFLOW_MEMORY'],
        'worker_cpu_budget': cfg['WORKER_CPU_BUDGET'],
        'worker_memory_budget': cfg['WORKER_MEMORY_BUDGET'],
        'max_concurrent_workflows': cfg['MAX_CONCURRENT_WORKFLOWS'],
        'processes': {
            'bed_analysis': {'cpus': cfg['BED_ANALYSIS_CPUS'], 'memory': cfg['BED_ANALYSIS_MEMORY'],
                             'time': cfg['BED_ANALYSIS_TIME']},
            'sample_qc': {'cpus': cfg['SAMPLE_QC_CPUS'], 'memory': cfg['SAMPLE_QC_MEMORY'],
                          'time': cfg['SAMPLE_QC_TIME']},
            'cohort_model': {'cpus': cfg['COHORT_MODEL_CPUS'], 'memory': cfg['COHORT_MODEL_MEMORY'],
                             'time': cfg['COHORT_MODEL_TIME']},
        },
        'modules': cfg['MODULE_PROCESSES'],
    }


def _enable_retry_resume(execution, previous_job):
    """Use the original Nextflow launch cache and work directory on retry."""
    execution.pop('runtime_params_sha256', None)
    execution.pop('recovery_attempts', None)
    execution['resume'] = True
    execution['resume_mode'] = 'retry'
    execution['resume_launch_path'] = execution.get('resume_launch_path') or str(
        Path(previous_job.execution_path).parent.resolve())
    return execution


def _execution_snapshot_sha256(execution):
    snapshot = dict(execution)
    for runtime_key in ('resume', 'resume_mode', 'resume_launch_path',
                        'runtime_params_sha256', 'recovery_attempts'):
        snapshot.pop(runtime_key, None)
    canonical = yaml.safe_dump(snapshot, sort_keys=False, allow_unicode=True).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def _nextflow_config_path():
    return str((Path(__file__).resolve().parent.parent / 'nextflow.config').resolve())


def _worker_is_running():
    lock_path = Path(current_app.instance_path) / 'worker.lock'
    try:
        with lock_path.open('a+') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def get_project_or_404(project_id):
    project = db.session.get(BedProject, project_id)
    if project is None:
        abort(404)
    return project


def _cohort_manifest(project):
    return Path(project.storage_path) / 'samples' / 'cohort.yaml'


def _read_cohort_state(project):
    path = _cohort_manifest(project)
    if not path.is_file():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _sample_ids_used_by_models(project_id):
    """Return every sample ID present in a persisted model snapshot, or None if unverifiable."""
    sample_ids = set()
    runs = CohortModelRun.query.filter_by(project_id=project_id).all()
    for run in runs:
        try:
            snapshot = json.loads(run.sample_snapshot_json)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(snapshot, list):
            return None
        for item in snapshot:
            if not isinstance(item, dict) or not item.get('id'):
                return None
            sample_ids.add(item['id'])
    return sample_ids


def _refresh_project_qc_summary(project):
    samples = (BamSample.query.filter(
        BamSample.project_id == project.id,
        BamSample.status.in_(['qc_complete', 'qc_partial']),
        BamSample.counts_path.is_not(None),
        BamSample.qc_metrics_path.is_not(None),
    ).order_by(BamSample.sample_id).all())
    summary_path = Path(project.storage_path) / 'samples' / 'project_qc_summary.json'
    if not samples:
        summary_path.unlink(missing_ok=True)
        return
    batches = {batch.id: batch.name for batch in SampleBatch.query.filter(
        SampleBatch.id.in_({sample.batch_id for sample in samples})).all()}
    summary = build_project_qc([(sample, batches.get(sample.batch_id, '')) for sample in samples],
                               samples[0].batch.low_count_threshold)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')


def _project_phase_navigation(project):
    """Build consistent project-wide navigation and lock unavailable phases."""
    accepted = (Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml').is_file()
    samples = BamSample.query.filter(
        BamSample.project_id == project.id, BamSample.status.in_(['qc_complete', 'qc_partial'])
    ).all()
    eligible_count = sum(bool(sample.counts_path and Path(sample.counts_path).is_file())
                         for sample in samples)
    runs = CohortModelRun.query.filter_by(project_id=project.id).order_by(
        CohortModelRun.created_at.desc()).all()
    assessment_run = next((run for run in runs if run.status in {
        'succeeded', 'succeeded_with_parse_warnings', 'queued', 'running', 'cancel_requested'
    }), runs[0] if runs else None)
    return [
        {'key': 'overview', 'label': 'Resumen',
         'url': url_for('main.project_detail', project_id=project.id), 'available': True},
        {'key': 'bed', 'label': 'Diseño BED',
         'url': url_for('main.project_bed_analysis', project_id=project.id), 'available': True},
        {'key': 'bams', 'label': 'Cohorte BAM',
         'url': url_for('main.project_bams', project_id=project.id) if accepted else None,
         'available': accepted},
        {'key': 'qc', 'label': 'QC de muestras',
         'url': url_for('main.project_sample_qc', project_id=project.id) if accepted else None,
         'available': accepted},
        {'key': 'models', 'label': 'Modelos COHORT',
         'url': url_for('main.project_models_page', project_id=project.id)
         if accepted and (eligible_count >= 2 or runs) else None,
         'available': accepted and (eligible_count >= 2 or bool(runs))},
        {'key': 'assessment', 'label': 'Evaluación',
         'url': url_for('main.project_model_detail', project_id=project.id,
                        model_run_id=assessment_run.id) if assessment_run else None,
         'available': assessment_run is not None},
        {'key': 'validation', 'label': 'Validación',
         'url': url_for('main.project_validation', project_id=project.id)
         if accepted else None,
         'available': accepted},
    ]


@bp.app_context_processor
def inject_project_phase_navigation():
    return {'get_project_phase_navigation': _project_phase_navigation}


def _write_cohort_state(project, state):
    path = _cohort_manifest(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.yaml.tmp')
    temporary.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True), encoding='utf-8')
    temporary.replace(path)


def _cohort_is_locked(project):
    state = _read_cohort_state(project)
    active_job = (WorkflowJob.query.filter_by(project_id=project.id, workflow='sample_qc')
                  .filter(WorkflowJob.status.in_(['queued', 'running', 'cancel_requested']))
                  .first())
    if active_job:
        return True, state
    if state.get('roster_opened_at'):
        return False, state
    if state.get('roster_locked_at') or state.get('qc_job_id'):
        return True, state
    # Old projects did not have the cohort manifest. Preserve their lock if
    # they already ran a per-batch QC job.
    legacy_job = WorkflowJob.query.filter_by(project_id=project.id, workflow='sample_qc').first()
    return bool(legacy_job), state


def _sample_qc_jobs_by_sample(project_id):
    """Map each sample to the output directory of its most recent QC job."""
    sample_jobs = {}
    jobs = WorkflowJob.query.filter_by(project_id=project_id, workflow='sample_qc').order_by(
        WorkflowJob.created_at.asc()).all()
    for job in jobs:
        try:
            params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
        except (OSError, yaml.YAMLError):
            continue
        sample_ids = params.get('sample_ids')
        if sample_ids:
            for sample_id in sample_ids:
                sample_jobs[sample_id] = job
        elif params.get('batch_id'):
            batch_samples = BamSample.query.filter_by(batch_id=params['batch_id']).all()
            for sample in batch_samples:
                sample_jobs[sample.id] = job
    return sample_jobs


def reconcile_finished_jobs():
    """Close jobs whose worker recorded a finish time but left an active status."""
    jobs = WorkflowJob.query.filter(
        WorkflowJob.status.in_(['running', 'cancel_requested']),
        WorkflowJob.finished_at.is_not(None),
    ).all()
    changed = False
    for job in jobs:
        prior_status = job.status
        metadata_path = Path(job.output_path) / 'run_metadata.json'
        metadata_status = None
        if metadata_path.is_file():
            try:
                metadata_status = json.loads(metadata_path.read_text(encoding='utf-8')).get('status')
            except (OSError, json.JSONDecodeError):
                pass
        if prior_status == 'cancel_requested':
            job.status = 'cancelled'
            job.error_message = job.error_message or 'Cancelación completada.'
        elif metadata_status in {'succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'}:
            job.status = metadata_status
        elif job.exit_code == 0:
            job.status = 'succeeded'
        else:
            job.status = 'failed'
            job.error_message = job.error_message or 'El worker terminó el job sin guardar su estado final.'

        if job.workflow == 'sample_qc':
            try:
                params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                batch = db.session.get(SampleBatch, params.get('batch_id'))
                samples = (BamSample.query.filter(BamSample.id.in_(params['sample_ids'])).all()
                           if params.get('sample_ids') else
                           BamSample.query.filter_by(batch_id=params.get('batch_id')).all())
                if batch:
                    completed = sum(sample.status in {'qc_complete', 'qc_partial'} for sample in samples)
                    failed = 0
                    for sample in samples:
                        if sample.status not in {'qc_complete', 'qc_partial', 'qc_failed', 'qc_cancelled'}:
                            sample.status = 'qc_cancelled' if job.status == 'cancelled' else 'qc_failed'
                            sample.error_message = job.error_message or 'El worker finalizó antes de guardar las métricas QC.'
                        failed += sample.status in {'qc_failed', 'qc_cancelled'}
                    if job.status == 'cancelled':
                        batch.status = 'cancelled'
                    elif completed and failed:
                        batch.status = 'partial_failed'
                    elif completed:
                        batch.status = 'succeeded'
                    else:
                        batch.status = 'failed'
                    batch.error_message = job.error_message if failed else batch.error_message
                    if batch.status == 'failed' and job.status in {'succeeded', 'succeeded_with_parse_warnings'}:
                        job.status = 'failed'
                        job.error_message = job.error_message or 'Nextflow terminó, pero el worker no guardó las métricas de las muestras.'
                    elif batch.status == 'partial_failed' and job.status == 'succeeded':
                        job.status = 'succeeded_with_parse_warnings'
                    by_batch = {}
                    for sample in samples:
                        by_batch.setdefault(sample.batch_id, []).append(sample)
                    for batch_id, batch_samples in by_batch.items():
                        upload_batch = db.session.get(SampleBatch, batch_id)
                        if not upload_batch:
                            continue
                        good = sum(sample.status in {'qc_complete', 'qc_partial'} for sample in batch_samples)
                        bad = sum(sample.status in {'qc_failed', 'qc_cancelled'} for sample in batch_samples)
                        upload_batch.status = ('partial_failed' if good and bad else
                                               'succeeded' if good else batch.status)
            except (OSError, KeyError, TypeError, yaml.YAMLError):
                pass
        elif job.workflow == 'cohort_model':
            try:
                params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                model_run = db.session.get(CohortModelRun, params.get('model_run_id'))
                if model_run:
                    model_run.status = job.status
                    model_run.error_message = job.error_message
                    model_run.finished_at = model_run.finished_at or job.finished_at
            except (OSError, TypeError, yaml.YAMLError):
                pass
        elif job.workflow == 'model_validation':
            validation_run = ModelValidationRun.query.filter_by(job_id=job.id).first()
            if validation_run:
                validation_run.status = job.status
                validation_run.error_message = job.error_message
                validation_run.finished_at = validation_run.finished_at or job.finished_at
        else:
            project = db.session.get(BedProject, job.project_id)
            if project:
                accepted = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
                if accepted.is_file():
                    project.status = 'intervals_accepted'
                elif job.status in {'succeeded', 'succeeded_with_parse_warnings'}:
                    project.status = 'analysis_complete'
                elif job.status == 'cancelled':
                    project.status = 'analysis_cancelled'
                else:
                    project.status = 'analysis_failed'
        changed = True
    if changed:
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()


@bp.get('/')
def index():
    projects = BedProject.query.order_by(BedProject.created_at.desc()).all()
    return render_template('index.html', projects=projects, references=list_references())


@bp.get('/jobs')
def jobs_page():
    reconcile_finished_jobs()
    jobs = WorkflowJob.query.order_by(WorkflowJob.created_at.desc()).limit(500).all()
    running = [job for job in jobs if job.status in {'running', 'cancel_requested'}]
    queued = [job for job in jobs if job.status == 'queued']
    finished = [job for job in jobs if job.status in {'succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'}]
    retryable_qc_job_ids = {batch.job_id for batch in SampleBatch.query.filter(
        SampleBatch.job_id.is_not(None)).all()}
    return render_template('jobs.html', running=running, queued=queued, finished=finished,
        retryable_qc_job_ids=retryable_qc_job_ids,
        worker_online=_worker_is_running(), max_concurrent=current_app.config['MAX_CONCURRENT_WORKFLOWS'],
        worker_cpus=current_app.config['WORKER_CPU_BUDGET'],
        worker_memory=current_app.config['WORKER_MEMORY_BUDGET'])


@bp.post('/projects')
def create_project():
    name = (request.form.get('name') or '').strip()
    reference_id = request.form.get('reference_id', '')
    bed_file = request.files.get('bed_file')
    reference = get_reference(reference_id)
    if not name or len(name) > 200:
        flash('Indica un nombre de proyecto de hasta 200 caracteres.', 'error')
        return redirect(url_for('main.index'))
    if name in {'.', '..'} or '/' in name or '\\' in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        flash('El nombre no puede contener separadores de ruta ni caracteres de control.', 'error')
        return redirect(url_for('main.index'))
    if len(name.encode('utf-8')) > 240:
        flash('El nombre es demasiado largo para usarlo como carpeta de proyecto.', 'error')
        return redirect(url_for('main.index'))
    if BedProject.query.filter_by(name=name).first():
        flash('Ya existe un proyecto con ese nombre. Usa un nombre distinto para evitar colisiones de carpeta.', 'error')
        return redirect(url_for('main.index'))
    if not reference:
        flash('La referencia seleccionada no está configurada en el catálogo.', 'error')
        return redirect(url_for('main.index'))
    if not bed_file or not bed_file.filename:
        flash('Selecciona un fichero BED.', 'error')
        return redirect(url_for('main.index'))
    if not bed_file.filename.lower().endswith('.bed'):
        flash('Carga un archivo con extensión .bed. BED comprimido aún no está soportado.', 'error')
        return redirect(url_for('main.index'))

    project_id = new_id()
    project_dir = current_app.config['STORAGE_ROOT'] / name
    if project_dir.exists() or project_dir.is_symlink():
        flash('Ya existe una carpeta con ese nombre en el storage. Elige otro nombre o revisa el proyecto existente.', 'error')
        return redirect(url_for('main.index'))
    bed_dir = project_dir / 'bed'
    try:
        bed_dir.mkdir(parents=True, exist_ok=False)
        original_path = bed_dir / 'original.bed'
        canonical_path = bed_dir / 'validated.bed'
        bed_file.save(original_path)
        report = validate_bed(original_path, reference, canonical_path)
        (bed_dir / 'validation_report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
        )
        if not report['valid']:
            shutil.rmtree(project_dir)
            flash(f'El BED no es válido: {len(report["errors"])} error(es). Revisa el informe mostrado.', 'error')
            return render_template('validation_result.html', report=report, project=None), 422

        project = BedProject(
            id=project_id,
            name=name,
            reference_id=reference_id,
            reference_bundle_version=reference['bundle_version'],
            storage_path=str(project_dir),
            original_bed_filename=Path(bed_file.filename).name[:255],
            original_bed_path=str(original_path),
            canonical_bed_path=str(canonical_path),
            original_bed_sha256=sha256_file(original_path),
            status='ready',
        )
        db.session.add(project)
        db.session.commit()
        flash('Proyecto BED creado y validado.', 'success')
        return redirect(url_for('main.project_detail', project_id=project.id))
    except OSError as exc:
        db.session.rollback()
        flash(f'No se pudo guardar el BED en el storage configurado: {exc}', 'error')
        return redirect(url_for('main.index'))


@bp.get('/projects/<project_id>')
def project_detail(project_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    report_path = Path(project.storage_path) / 'bed' / 'validation_report.json'
    validation = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else None
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    accepted_analysis = yaml.safe_load(accepted_path.read_text(encoding='utf-8')) if accepted_path.is_file() else None
    if accepted_analysis and project.status != 'intervals_accepted':
        project.status = 'intervals_accepted'
        db.session.commit()

    jobs = WorkflowJob.query.filter_by(project_id=project.id).order_by(WorkflowJob.created_at.desc()).all()
    bed_jobs = [job for job in jobs if job.workflow == 'bed_analysis']
    bed_active = any(job.status in {'queued', 'running', 'cancel_requested'} for job in bed_jobs)
    bed_complete = any(job.status in {'succeeded', 'succeeded_with_parse_warnings'} and
                       (Path(job.output_path) / 'bed_metrics.json').is_file() for job in bed_jobs)
    qc_state = _read_cohort_state(project)
    qc_job = db.session.get(WorkflowJob, qc_state.get('qc_job_id')) if qc_state.get('qc_job_id') else None
    qc_active = bool(qc_job and qc_job.status in {'queued', 'running', 'cancel_requested'})
    eligible_samples = [sample for sample in BamSample.query.filter(
        BamSample.project_id == project.id, BamSample.status.in_(['qc_complete', 'qc_partial'])
    ).all() if sample.counts_path and Path(sample.counts_path).is_file()]
    qc_complete = bool(qc_job and qc_job.status in {'succeeded', 'succeeded_with_parse_warnings'} and eligible_samples)
    model_runs = CohortModelRun.query.filter_by(project_id=project.id).order_by(
        CohortModelRun.created_at.desc()).all()
    model_active = any(run.status in {'queued', 'running', 'cancel_requested'} for run in model_runs)
    model_complete = any(run.status in {'succeeded', 'succeeded_with_parse_warnings'} for run in model_runs)
    model_url = url_for('main.project_models_page', project_id=project.id)
    assessment_url = next((url_for('main.project_model_detail', project_id=project.id, model_run_id=run.id)
                           for run in model_runs if run.status in {'succeeded', 'succeeded_with_parse_warnings'}),
                          model_url)
    phases = [
        {'number': 1, 'title': 'Diseño BED', 'description': 'Validar, procesar y revisar los intervalos de captura.',
         'status': 'complete' if accepted_analysis else 'active' if bed_active else 'available' if validation and validation.get('valid') else 'blocked',
         'label': 'Aceptado' if accepted_analysis else 'En curso' if bed_active else 'Listo para analizar' if validation and validation.get('valid') else 'Revisar BED',
         'url': url_for('main.project_bed_analysis', project_id=project.id)},
        {'number': 2, 'title': 'Muestras y QC', 'description': 'Añadir BAMs, ejecutar QC y revisar la cohorte.',
         'status': 'complete' if qc_complete else 'active' if qc_active else 'available' if accepted_analysis else 'locked',
         'label': f'{len(eligible_samples)} muestras con QC' if qc_complete else 'QC en curso' if qc_active else 'Requiere BED aceptado' if not accepted_analysis else 'Añadir muestras',
         'url': url_for('main.project_bams', project_id=project.id) if accepted_analysis else None},
        {'number': 3, 'title': 'Modelos COHORT', 'description': 'Elegir muestras y parámetros para crear modelos gCNV independientes.',
         'status': 'complete' if model_complete else 'active' if model_active else 'available' if (qc_complete and len(eligible_samples) >= 2) or model_runs else 'locked',
         'label': f'{len(model_runs)} modelos creados' if model_runs else 'Crear primer modelo' if qc_complete and len(eligible_samples) >= 2 else 'Requiere QC de al menos 2 muestras',
         'url': model_url if qc_complete or model_runs else None},
        {'number': 4, 'title': 'Calidad del modelo', 'description': 'Valorar ploidía, convergencia, parámetros y llamadas CNV.',
         'status': 'complete' if model_complete else 'active' if model_active else 'locked',
         'label': 'Ver evaluación' if model_complete else 'Modelo en curso' if model_active else 'Requiere un modelo COHORT',
         'url': assessment_url if model_complete or model_active else None},
    ]
    return render_template('project_detail.html', project=project, validation=validation,
                           accepted_analysis=accepted_analysis, phases=phases,
                           model_runs=model_runs, bed_complete=bed_complete)


@bp.get('/projects/<project_id>/bed-analysis')
def project_bed_analysis(project_id):
    project = get_project_or_404(project_id)
    report_path = Path(project.storage_path) / 'bed' / 'validation_report.json'
    validation = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else None
    jobs = WorkflowJob.query.filter_by(project_id=project_id).order_by(WorkflowJob.created_at.desc()).all()
    metrics = None
    metrics_job = None
    active_analysis = any(job.status in {'queued', 'running', 'cancel_requested'} for job in jobs)
    for job in jobs:
        if job.status not in {'succeeded', 'succeeded_with_parse_warnings'}:
            continue
        metrics_path = Path(job.output_path) / 'bed_metrics.json'
        if metrics_path.is_file():
            metrics = json.loads(metrics_path.read_text(encoding='utf-8'))
            for interval in metrics.get('gc', {}).get('intervals', []):
                interval['name'] = gene_label(interval.get('name') or '')
            metrics_job = job
            break
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    accepted_analysis = yaml.safe_load(accepted_path.read_text(encoding='utf-8')) if accepted_path.is_file() else None
    if accepted_analysis is not None and project.status != 'intervals_accepted':
        project.status = 'intervals_accepted'
        db.session.commit()
    analysis_locked = project.status == 'intervals_accepted' or accepted_analysis is not None
    if analysis_locked and accepted_analysis is None:
        parameters = (metrics or {}).get('parameters', {})
        accepted_analysis = {
            'padding': parameters.get('padding', 0),
            'bin_length': parameters.get('bin_length', 0),
        }
    return render_template(
        'project_bed_analysis.html', project=project, validation=validation, jobs=jobs,
        metrics=metrics, metrics_job=metrics_job, active_analysis=active_analysis,
        analysis_locked=analysis_locked, accepted_analysis=accepted_analysis,
    )


@bp.post('/projects/<project_id>/analyze')
def launch_bed_analysis(project_id):
    project = get_project_or_404(project_id)
    if project.status == 'intervals_accepted' or (Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml').is_file():
        flash('El análisis de intervalos ya fue aceptado. Su configuración está bloqueada para este proyecto.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    active_count = WorkflowJob.query.filter(
        WorkflowJob.project_id == project_id,
        WorkflowJob.status.in_(['queued', 'running', 'cancel_requested']),
    ).count()
    if active_count:
        flash('Ya hay un análisis de intervalos en cola o ejecución para este BED.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    reference = get_reference(project.reference_id)
    if not reference:
        flash('El bundle de referencia ya no está disponible en el catálogo.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    try:
        padding = int(request.form.get('padding', '0'))
        bin_length = int(request.form.get('bin_length', '0'))
    except ValueError:
        flash('Padding y tamaño de bin deben ser enteros.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    if padding < 0 or bin_length < 0 or padding > 100000 or bin_length > 1000000:
        flash('Los parámetros están fuera de los límites permitidos.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    original_bed = Path(project.original_bed_path)
    current_hash = sha256_file(original_bed)
    if current_hash != project.original_bed_sha256:
        flash('El BED original cambió desde su validación. Crea un proyecto nuevo.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    job_id = new_id()
    analysis_root = Path(project.storage_path) / 'bed_analysis'
    job_dir = analysis_root / 'jobs' / job_id
    output_dir = analysis_root / 'current'
    job_dir.mkdir(parents=True, exist_ok=False)
    params = {
        'bed': project.canonical_bed_path,
        'bed_sha256': sha256_file(project.canonical_bed_path),
        'reference_fasta': reference['fasta'],
        'reference_fai': reference['fai'],
        'reference_dict': reference['dict'],
        'mappability_track': reference.get('mappability_track') or None,
        'segmental_duplication_track': reference.get('segmental_duplication_track') or None,
        'padding': padding,
        'bin_length': bin_length,
        'preprocess_interval_merging_rule': 'OVERLAPPING_ONLY',
        'annotate_interval_merging_rule': 'OVERLAPPING_ONLY',
        'reference_id': project.reference_id,
        'reference_bundle_version': reference['bundle_version'],
        'gatk_version': '4.7.0.0',
        'pipeline_version': '0.1.0',
    }
    params_path = job_dir / 'parameters.yaml'
    params_path.write_text(yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding='utf-8')
    execution = {
        'executor_profile': 'local',
        'nextflow_config_path': _nextflow_config_path(),
        'nextflow_binary': current_app.config['NEXTFLOW_BIN'],
        'workflow_path': str(current_app.config['WORKFLOW_PATH']),
        'output_path': str(output_dir),
        'work_path': str(job_dir / 'work'),
        'apptainer_cache': str(current_app.config['APPTAINER_CACHE']),
        'gatk_container': current_app.config['GATK_CONTAINER'],
        'resources': _execution_resources(),
    }
    execution_path = job_dir / 'execution.yaml'
    execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
    log_path = job_dir / 'nextflow.log'
    log_path.touch()
    job = WorkflowJob(
        id=job_id,
        project_id=project.id,
        workflow='bed_analysis',
        status='queued',
        parameters_path=str(params_path),
        execution_path=str(execution_path),
        output_path=str(output_dir),
        log_path=str(log_path),
    )
    previous_status = project.status
    reserved = (BedProject.query
                .filter_by(id=project_id, status=previous_status)
                .update({BedProject.status: 'analysis_queued'}, synchronize_session=False))
    if reserved != 1:
        db.session.rollback()
        shutil.rmtree(job_dir, ignore_errors=True)
        flash('El estado del proyecto cambió mientras se solicitaba el análisis. Recarga la página.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    job.output_path = str(output_dir)
    db.session.add(job)
    db.session.commit()
    flash('Análisis BED añadido a la cola local.', 'success')
    return redirect(url_for('main.project_bed_analysis', project_id=project.id))


@bp.post('/projects/<project_id>/accept-interval-analysis')
def accept_interval_analysis(project_id):
    project = get_project_or_404(project_id)
    analysis_root = Path(project.storage_path) / 'bed_analysis'
    accepted_path = analysis_root / 'accepted_analysis.yaml'
    if project.status == 'intervals_accepted' or accepted_path.is_file():
        return redirect(url_for('main.project_bams', project_id=project_id))

    active_count = WorkflowJob.query.filter(
        WorkflowJob.project_id == project_id,
        WorkflowJob.status.in_(['queued', 'running', 'cancel_requested']),
    ).count()
    if active_count:
        flash('Espera a que termine el análisis antes de aceptarlo.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    jobs = WorkflowJob.query.filter_by(project_id=project_id).order_by(WorkflowJob.created_at.desc()).all()
    metrics_job = next((job for job in jobs
                        if job.status in {'succeeded', 'succeeded_with_parse_warnings'}
                        and (Path(job.output_path) / 'bed_metrics.json').is_file()), None)
    if metrics_job is None:
        flash('Primero debe completarse correctamente un análisis de intervalos.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    temporary_path = None
    accepted_path_created = False
    try:
        parameters = yaml.safe_load(Path(metrics_job.parameters_path).read_text(encoding='utf-8')) or {}
        accepted = {
            'schema_version': '1.0',
            'accepted_at': datetime.now(timezone.utc).isoformat(),
            'job_id': metrics_job.id,
            'bed_sha256': parameters.get('bed_sha256'),
            'reference_id': project.reference_id,
            'reference_bundle_version': project.reference_bundle_version,
            'padding': int(parameters['padding']),
            'bin_length': int(parameters['bin_length']),
            'analysis_parameters': parameters,
        }
        analysis_root.mkdir(parents=True, exist_ok=True)
        previous_status = project.status
        reserved = (BedProject.query
                    .filter_by(id=project_id, status=previous_status)
                    .update({BedProject.status: 'intervals_accepted'}, synchronize_session=False))
        if reserved != 1:
            db.session.rollback()
            flash('El estado del proyecto cambió durante la aceptación. Recarga la página.', 'error')
            return redirect(url_for('main.project_detail', project_id=project_id))

        temporary_path = accepted_path.with_name(f'.accepted_analysis.{new_id()}.tmp')
        temporary_path.write_text(yaml.safe_dump(accepted, sort_keys=False), encoding='utf-8')
        temporary_path.replace(accepted_path)
        accepted_path_created = True
        db.session.commit()
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        db.session.rollback()
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        if accepted_path_created:
            accepted_path.unlink(missing_ok=True)
        flash(f'No se pudo guardar la configuración aceptada: {exc}', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))

    flash('Análisis aceptado. La configuración queda bloqueada para esta cohorte.', 'success')
    return redirect(url_for('main.project_bams', project_id=project_id))


@bp.post('/projects/<project_id>/bams/analyze')
def analyze_project_bams(project_id):
    project = _require_accepted_project(project_id)
    locked, state = _cohort_is_locked(project)
    if locked:
        flash('La cohorte ya está cerrada y el QC ya se inició.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    all_samples = BamSample.query.filter_by(project_id=project.id).order_by(BamSample.sample_id).all()
    if not all_samples:
        flash('Añade y valida al menos un par BAM/BAI antes de lanzar el QC.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    analyzed_ids = set(state.get('sample_ids') or [])
    samples = [sample for sample in all_samples if sample.id not in analyzed_ids]
    if not samples:
        flash('No hay muestras nuevas pendientes de QC.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    accepted = yaml.safe_load(accepted_path.read_text(encoding='utf-8')) or {}
    interval_list = Path(project.storage_path) / 'bed_analysis' / 'current' / 'preprocessed.interval_list'
    if accepted.get('job_id'):
        accepted_job = db.session.get(WorkflowJob, accepted['job_id'])
        if accepted_job:
            interval_list = Path(accepted_job.output_path) / 'preprocessed.interval_list'
    reference = get_reference(project.reference_id)
    if not reference or not interval_list.is_file():
        flash('No se encuentra el bundle o el interval_list aceptado del proyecto.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))

    batch_number = SampleBatch.query.filter_by(project_id=project.id).filter(
        SampleBatch.name.like('QC conjunto%')).count() + 1
    qc_batch = SampleBatch(id=new_id(), project_id=project.id,
        name='QC conjunto' if batch_number == 1 else f'QC conjunto {batch_number}',
        storage_mode='cohort', kit_name='', protocol='', duplicates_marked=False,
        low_count_threshold=10, status='queued')
    job_id = new_id()
    root = Path(project.storage_path) / 'samples' / 'cohort_qc' / job_id
    output = Path(project.storage_path) / 'samples' / 'qc' / job_id
    root.mkdir(parents=True, exist_ok=False)
    parameters = {
        'schema_version': '1.0', 'batch_id': qc_batch.id,
        'sample_ids': [sample.id for sample in samples],
        'samples': [{'sample_id': sample.sample_id, 'bam': sample.bam_path, 'bai': sample.bai_path}
                    for sample in samples],
        'reference_fasta': reference['fasta'], 'reference_fai': reference['fai'],
        'reference_dict': reference['dict'], 'intervals': str(interval_list),
        'bait_bed': None, 'bait_mode': 'targets_as_baits_approximation',
        'reference_id': project.reference_id, 'reference_bundle_version': project.reference_bundle_version,
        'low_count_threshold': 10, 'gatk_version': '4.7.0.0', 'pipeline_version': '0.2.1',
    }
    params_path = root / 'parameters.yaml'
    params_path.write_text(yaml.safe_dump(parameters, sort_keys=False, allow_unicode=True), encoding='utf-8')
    execution = {'executor_profile': 'local', 'nextflow_config_path': _nextflow_config_path(),
        'nextflow_binary': current_app.config['NEXTFLOW_BIN'],
        'workflow_path': str(current_app.config['SAMPLE_QC_WORKFLOW_PATH']), 'output_path': str(output),
        'work_path': str(root / 'work'), 'apptainer_cache': str(current_app.config['APPTAINER_CACHE']),
        'gatk_container': current_app.config['GATK_CONTAINER'],
        'resources': _execution_resources()}
    execution_path = root / 'execution.yaml'
    execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
    log_path = root / 'nextflow.log'
    log_path.touch()
    qc_batch.job_id = job_id
    db.session.add(qc_batch)
    for sample in samples:
        sample.status = 'queued'
        sample.error_message = None
    db.session.add(WorkflowJob(id=job_id, project_id=project.id, workflow='sample_qc', status='queued',
        parameters_path=str(params_path), execution_path=str(execution_path), output_path=str(output),
        log_path=str(log_path)))
    try:
        db.session.commit()
        state.update({'schema_version': '1.0', 'roster_locked_at': datetime.now(timezone.utc).isoformat(),
                      'sample_ids': sorted(analyzed_ids | {sample.id for sample in samples}),
                      'last_qc_sample_ids': [sample.id for sample in samples],
                      'qc_batch_id': qc_batch.id, 'qc_job_id': job_id,
                      'excluded_sample_ids': state.get('excluded_sample_ids', [])})
        state.pop('roster_opened_at', None)
        state.pop('model_sample_ids', None)
        _write_cohort_state(project, state)
        flash(f'Cohorte cerrada: QC de {len(samples)} muestra(s) nuevas en cola. Al terminar se recalcularán las métricas del conjunto de {len(all_samples)} muestras.', 'success')
    except (OSError, SQLAlchemyError) as exc:
        db.session.rollback()
        flash(f'No se pudo cerrar la cohorte y encolar el QC: {exc}', 'error')
    return redirect(url_for('main.project_sample_qc', project_id=project_id))


@bp.post('/projects/<project_id>/bams/add-more')
def reopen_project_bams(project_id):
    project = get_project_or_404(project_id)
    locked, state = _cohort_is_locked(project)
    job = db.session.get(WorkflowJob, state.get('qc_job_id')) if state.get('qc_job_id') else None
    if not locked or not job or job.status not in {'succeeded', 'succeeded_with_parse_warnings'}:
        flash('Solo puedes añadir una nueva tanda cuando haya terminado correctamente el QC anterior.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    state['roster_opened_at'] = datetime.now(timezone.utc).isoformat()
    state['last_qc_sample_ids'] = []
    try:
        _write_cohort_state(project, state)
        flash('Puedes añadir muestras nuevas. Las muestras ya analizadas no se modificarán; al cerrar el roster se analizarán solo las nuevas.', 'success')
    except OSError as exc:
        flash(f'No se pudo abrir el roster para nuevas muestras: {exc}', 'error')
    return redirect(url_for('main.project_bams', project_id=project_id))


@bp.post('/projects/<project_id>/bams/cancel-add-more')
def cancel_reopen_project_bams(project_id):
    project = get_project_or_404(project_id)
    locked, state = _cohort_is_locked(project)
    analyzed_ids = set(state.get('sample_ids') or [])
    current_ids = {sample.id for sample in BamSample.query.filter_by(project_id=project.id).all()}
    if locked or not state.get('roster_opened_at') or current_ids != analyzed_ids:
        flash('Quita primero las muestras nuevas o termina el QC antes de cancelar esta tanda.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    state.pop('roster_opened_at', None)
    try:
        _write_cohort_state(project, state)
        flash('Se cerró la tanda vacía. Las métricas y la selección anteriores siguen vigentes.', 'success')
    except OSError as exc:
        flash(f'No se pudo cerrar la tanda: {exc}', 'error')
    return redirect(url_for('main.project_sample_qc', project_id=project_id))


@bp.post('/projects/<project_id>/bams/<sample_id>/delete')
def delete_project_bam(project_id, sample_id):
    project = get_project_or_404(project_id)
    sample = BamSample.query.filter_by(project_id=project.id, id=sample_id).first_or_404()
    active_qc = WorkflowJob.query.filter_by(project_id=project.id, workflow='sample_qc').filter(
        WorkflowJob.status.in_(['queued', 'running', 'cancel_requested'])).first()
    if active_qc:
        flash('No se pueden quitar muestras mientras haya un QC en curso o en cola.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    modeled_ids = _sample_ids_used_by_models(project.id)
    if modeled_ids is None:
        flash('No se pudo verificar la selección guardada de los modelos; no se eliminó la muestra.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    if sample.id in modeled_ids:
        flash('No se puede quitar esta muestra porque está incluida en un modelo COHORT.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))

    sample_name = sample.sample_id
    state = _read_cohort_state(project)
    for key in ('sample_ids', 'last_qc_sample_ids', 'excluded_sample_ids', 'model_sample_ids'):
        if key in state:
            state[key] = [item for item in state[key] if item != sample.id]
    if state.get('qc_job_id') and not state.get('sample_ids'):
        state.pop('model_sample_ids', None)
        state.pop('excluded_sample_ids', None)
    sample_dir = Path(sample.bam_path).parent
    batches_root = (Path(project.storage_path) / 'samples' / 'batches').resolve()
    batch = sample.batch
    remaining_in_batch = (BamSample.query.filter_by(batch_id=sample.batch_id)
                          .filter(BamSample.id != sample.id).count())
    db.session.delete(sample)
    if batch and not remaining_in_batch:
        db.session.delete(batch)
    manifest_path = _cohort_manifest(project)
    previous_manifest = manifest_path.read_text(encoding='utf-8') if manifest_path.is_file() else None
    try:
        _write_cohort_state(project, state)
        db.session.commit()
    except (OSError, ValueError, KeyError, SQLAlchemyError) as exc:
        db.session.rollback()
        if previous_manifest is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_text(previous_manifest, encoding='utf-8')
        flash(f'No se pudo actualizar la cohorte al quitar la muestra: {exc}', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    try:
        if sample_dir.is_dir() and sample_dir.resolve().is_relative_to(batches_root):
            shutil.rmtree(sample_dir)
    except OSError as exc:
        flash(f'Se quitó la muestra {sample_name} de la cohorte, pero no se pudieron borrar sus BAM/BAI: {exc}', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    try:
        _refresh_project_qc_summary(project)
    except (OSError, ValueError, KeyError):
        flash(f'Se quitó la muestra {sample_name}, pero no se pudo recalcular el resumen QC.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    flash(f'Se quitó la muestra {sample_name} de la cohorte.', 'success')
    return redirect(url_for('main.project_bams', project_id=project_id))


@bp.get('/projects/<project_id>/bams')
def project_bams(project_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    if not accepted_path.is_file():
        flash('Acepta primero el análisis de intervalos para continuar.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    if project.status != 'intervals_accepted':
        project.status = 'intervals_accepted'
        db.session.commit()
    accepted_analysis = yaml.safe_load(accepted_path.read_text(encoding='utf-8'))
    cohort_locked, cohort_state = _cohort_is_locked(project)
    batches = (SampleBatch.query.filter_by(project_id=project.id)
               .filter(~SampleBatch.name.like('QC conjunto%'))
               .order_by(SampleBatch.created_at.desc()).all())
    batch_jobs = {job.id: job for job in WorkflowJob.query.filter_by(project_id=project.id, workflow='sample_qc').all()}
    qc_job = db.session.get(WorkflowJob, cohort_state.get('qc_job_id')) if cohort_state.get('qc_job_id') else None
    all_samples = BamSample.query.filter_by(project_id=project.id).order_by(BamSample.sample_id).all()
    analyzed_sample_ids = set(cohort_state.get('sample_ids') or [])
    modeled_sample_ids = _sample_ids_used_by_models(project.id)
    active_qc = WorkflowJob.query.filter_by(project_id=project.id, workflow='sample_qc').filter(
        WorkflowJob.status.in_(['queued', 'running', 'cancel_requested'])).first() is not None
    deletable_sample_ids = {
        sample.id for sample in all_samples
        if modeled_sample_ids is not None and sample.id not in modeled_sample_ids and not active_qc
    }
    pending_sample_count = sum(sample.id not in analyzed_sample_ids for sample in all_samples)
    sample_qc_jobs = _sample_qc_jobs_by_sample(project.id)
    recovered = False
    for sample in all_samples:
        batch = next((item for item in batches if item.id == sample.batch_id), None)
        job = sample_qc_jobs.get(sample.id) or qc_job or (batch_jobs.get(batch.job_id) if batch and batch.job_id else None)
        if not job:
            continue
        output = Path(job.output_path)
        metrics_path = Path(sample.qc_metrics_path) if sample.qc_metrics_path else output / f'{sample.sample_id}.qc_metrics.json'
        if not metrics_path.is_file():
            metrics_path = output / f'{sample.sample_id}.qc_metrics.json'
        if metrics_path.is_file():
            try:
                cached_metrics = json.loads(metrics_path.read_text(encoding='utf-8'))
                cached_target = cached_metrics.get('target_coverage') or {}
                cached_flag = cached_metrics.get('flagstat') or {}
                if (cached_metrics.get('read_counts')
                        and cached_target.get('bases_below_30x_pct') is not None
                        and cached_target.get('bases_below_100x_pct') is not None
                        and cached_flag.get('total_reads') is not None):
                    previous_metrics_path = sample.qc_metrics_path
                    previous_status = sample.status
                    sample.qc_metrics_path = str(metrics_path)
                    cached_counts_path = Path(sample.counts_path) if sample.counts_path else output / f'{sample.sample_id}.counts.hdf5'
                    if cached_counts_path.is_file():
                        sample.counts_path = str(cached_counts_path)
                    if sample.status in {'queued', 'qc_running', 'qc_failed'}:
                        sample.status = ('qc_complete' if cached_metrics.get('collect_hs_metrics')
                                         and cached_metrics.get('flagstat') else 'qc_partial')
                    recovered = recovered or previous_metrics_path != sample.qc_metrics_path or previous_status != sample.status
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        counts_path = Path(sample.counts_path) if sample.counts_path else output / f'{sample.sample_id}.counts.hdf5'
        if not counts_path.is_file():
            counts_path = output / f'{sample.sample_id}.counts.hdf5'
        if not counts_path.is_file():
            continue
        hs_path = output / f'{sample.sample_id}.hs_metrics.txt'
        flag_path = output / f'{sample.sample_id}.flagstat.txt'
        try:
            metrics, _intervals, _counts, errors = build_sample_metrics_partial(
                sample.sample_id, batch.name, counts_path, hs_path, flag_path, batch.low_count_threshold)
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
            sample.counts_path = str(counts_path)
            sample.qc_metrics_path = str(metrics_path)
            sample.status = 'qc_partial' if errors else 'qc_complete'
            sample.error_message = '; '.join(errors) if errors else None
            if errors:
                batch.error_message = f'QC parcial para {sample.sample_id}: ' + '; '.join(errors)
                if job.status == 'succeeded':
                    job.status = 'succeeded_with_parse_warnings'
            recovered = True
        except (OSError, ValueError, KeyError) as exc:
            sample.error_message = f'No se pudieron recuperar las métricas QC: {exc}'
            recovered = True
    if recovered:
        for batch in batches:
            states = [sample.status for sample in batch.samples]
            valid = sum(state in {'qc_complete', 'qc_partial'} for state in states)
            failed = sum(state in {'qc_failed', 'qc_cancelled'} for state in states)
            if valid and failed:
                batch.status = 'partial_failed'
            elif valid:
                batch.status = 'succeeded'
            db.session.commit()
        eligible = [sample for sample in all_samples if sample.status in {'qc_complete', 'qc_partial'}
                    and sample.counts_path and sample.qc_metrics_path]
        if eligible:
            names = {batch.id: batch.name for batch in batches}
            try:
                summary = build_project_qc([(sample, names.get(sample.batch_id, '')) for sample in eligible],
                                           eligible[0].batch.low_count_threshold)
                summary_path = Path(project.storage_path) / 'samples' / 'project_qc_summary.json'
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, ValueError, KeyError):
                pass
    samples_root = Path(project.storage_path) / 'samples'
    summary_path = samples_root / 'project_qc_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.is_file() else None
    cohort_metrics = {
        item.get('sample_id'): item.get('normalized', {})
        for item in (summary or {}).get('samples', [])
    }
    sample_metrics = {}
    for sample in all_samples:
        metrics_path = Path(sample.qc_metrics_path) if sample.qc_metrics_path else None
        if metrics_path and metrics_path.is_file():
            try:
                sample_metrics[sample.id] = json.loads(metrics_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                pass
    return render_template('project_bams.html', project=project, accepted_analysis=accepted_analysis,
                           batches=batches, summary=summary,
                           batch_jobs=batch_jobs,
                           cohort_locked=cohort_locked, cohort_state=cohort_state,
                           qc_job=qc_job, sample_count=len(all_samples),
                           pending_sample_count=pending_sample_count,
                           analyzed_sample_ids=analyzed_sample_ids,
                           deletable_sample_ids=deletable_sample_ids,
                           modeled_sample_ids=modeled_sample_ids or set(),
                           active_qc=active_qc,
                           cohort_metrics=cohort_metrics,
                           outlier_count=sum(bool(item.get('normalized', {}).get('outlier_review'))
                                             for item in (summary or {}).get('samples', [])),
                           sample_metrics=sample_metrics,
                           allowed_roots=current_app.config['BAM_ALLOWED_ROOTS'],
                           max_upload_mb=current_app.config['MAX_UPLOAD_MB'])


@bp.get('/projects/<project_id>/bams/qc')
def project_sample_qc(project_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    if not accepted_path.is_file():
        abort(409, 'Acepta primero el análisis de intervalos.')
    locked, state = _cohort_is_locked(project)
    job = db.session.get(WorkflowJob, state.get('qc_job_id')) if state.get('qc_job_id') else None
    selected_ids = set(state.get('sample_ids') or [])
    samples = (BamSample.query.filter(BamSample.id.in_(selected_ids)).order_by(BamSample.sample_id).all()
               if selected_ids else [])
    sample_qc_jobs = _sample_qc_jobs_by_sample(project.id)
    # Recover outputs here too: users can land directly on the QC tab after a
    # worker restart, before visiting the legacy BAM overview route.
    if job and job.status in {'succeeded', 'succeeded_with_parse_warnings', 'failed'}:
        output = Path(job.output_path)
        qc_batch = db.session.get(SampleBatch, state.get('qc_batch_id'))
        threshold = qc_batch.low_count_threshold if qc_batch else 10
        recovered_any = False
        parse_failures = []
        for sample in samples:
            sample_job = sample_qc_jobs.get(sample.id) or job
            sample_output = Path(sample_job.output_path)
            metrics_path = Path(sample.qc_metrics_path) if sample.qc_metrics_path else sample_output / f'{sample.sample_id}.qc_metrics.json'
            if metrics_path.is_file():
                try:
                    existing = json.loads(metrics_path.read_text(encoding='utf-8'))
                    existing_target = existing.get('target_coverage') or {}
                    existing_flag = existing.get('flagstat') or {}
                    has_base_percentages = (existing_target.get('bases_below_30x_pct') is not None
                                             and existing_target.get('bases_below_100x_pct') is not None)
                    flagstat_parsed = existing_flag.get('total_reads') is not None
                    if existing.get('read_counts') and has_base_percentages and flagstat_parsed:
                        required_metrics = ('collect_hs_metrics', 'flagstat', 'target_coverage')
                        complete_metrics = (all(existing.get(key) for key in required_metrics)
                                            and flagstat_parsed)
                        missing_metrics = [label for key, label in (
                            ('collect_hs_metrics', 'CollectHsMetrics'),
                            ('flagstat', 'FlagStat'),
                            ('target_coverage', 'cobertura por región'))
                            if not existing.get(key) or (key == 'flagstat' and not flagstat_parsed)]
                        if sample.status in {'queued', 'qc_running', 'qc_failed'}:
                            sample.status = 'qc_complete' if complete_metrics else 'qc_partial'
                        sample.qc_metrics_path = str(metrics_path)
                        cached_counts_path = sample_output / f'{sample.sample_id}.counts.hdf5'
                        if cached_counts_path.is_file():
                            sample.counts_path = str(cached_counts_path)
                        sample.error_message = None if complete_metrics else (
                            'Faltan métricas: ' + ', '.join(missing_metrics))
                        recovered_any = True
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            counts_path = sample_output / f'{sample.sample_id}.counts.hdf5'
            if not counts_path.is_file():
                if sample.status in {'queued', 'qc_running'}:
                    sample.status = 'qc_cancelled' if job.status == 'cancelled' else 'qc_failed'
                    sample.error_message = job.error_message or 'Nextflow no generó read counts para esta muestra.'
                    recovered_any = True
                continue
            try:
                metrics, _intervals, _counts, metric_errors = build_sample_metrics_partial(
                    sample.sample_id, sample.batch.name if sample.batch else '', counts_path,
                    sample_output / f'{sample.sample_id}.hs_metrics.txt',
                    sample_output / f'{sample.sample_id}.flagstat.txt', threshold)
                metrics_path.parent.mkdir(parents=True, exist_ok=True)
                metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
                sample.counts_path = str(counts_path)
                sample.qc_metrics_path = str(metrics_path)
                sample.status = 'qc_partial' if metric_errors else 'qc_complete'
                sample.error_message = '; '.join(metric_errors) if metric_errors else None
                if metric_errors:
                    parse_failures.append(sample.sample_id)
                recovered_any = True
            except (OSError, ValueError, KeyError) as exc:
                sample.status = 'qc_failed'
                sample.error_message = f'No se pudieron interpretar las métricas QC: {exc}'
                parse_failures.append(sample.sample_id)
                recovered_any = True
        if recovered_any:
            db.session.commit()
            eligible = [sample for sample in samples if sample.status in {'qc_complete', 'qc_partial'}
                        and sample.counts_path and sample.qc_metrics_path]
            if eligible:
                try:
                    summary = build_project_qc([(sample, sample.batch.name if sample.batch else '')
                                                for sample in eligible], threshold)
                    summary_path = Path(project.storage_path) / 'samples' / 'project_qc_summary.json'
                    summary_path.parent.mkdir(parents=True, exist_ok=True)
                    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
                except (OSError, ValueError, KeyError):
                    pass
            if parse_failures and job.status == 'succeeded':
                job.status = 'succeeded_with_parse_warnings'
                job.error_message = 'No se pudieron interpretar métricas para: ' + ', '.join(parse_failures)
                db.session.commit()
    excluded = set(state.get('excluded_sample_ids') or [])
    metrics_by_sample = {}
    for sample in samples:
        metrics_path = Path(sample.qc_metrics_path) if sample.qc_metrics_path else None
        if metrics_path and metrics_path.is_file():
            try:
                metrics_by_sample[sample.id] = json.loads(metrics_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                pass
    summary_path = Path(project.storage_path) / 'samples' / 'project_qc_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.is_file() else None
    if summary and summary.get('coverage_correlation_method') != 'pearson_library_size_normalized_autosomal_counts_v2':
        eligible = [sample for sample in samples if sample.status in {'qc_complete', 'qc_partial'}
                    and sample.counts_path and sample.qc_metrics_path]
        if eligible:
            try:
                summary = build_project_qc([(sample, sample.batch.name if sample.batch else '')
                                            for sample in eligible], threshold)
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, ValueError, KeyError):
                pass
    cohort_metrics = {item.get('sample_id'): item.get('normalized', {})
                      for item in (summary or {}).get('samples', [])}
    qc_finished = bool(job and job.status in {'succeeded', 'succeeded_with_parse_warnings'})
    model_eligible_count = sum(sample.status in {'qc_complete', 'qc_partial'} and sample.counts_path
                               and Path(sample.counts_path).is_file() for sample in samples)
    return render_template('project_sample_qc.html', project=project, samples=samples,
        locked=locked, cohort_state=state, job=job, qc_finished=qc_finished,
        model_eligible_count=model_eligible_count,
        qc_sample_count=len(state.get('last_qc_sample_ids') or state.get('sample_ids') or []),
        excluded_ids=excluded, metrics_by_sample=metrics_by_sample,
        cohort_metrics=cohort_metrics, summary=summary)


@bp.post('/projects/<project_id>/bams/qc/exclusions')
def save_sample_exclusions(project_id):
    project = get_project_or_404(project_id)
    locked, state = _cohort_is_locked(project)
    job = db.session.get(WorkflowJob, state.get('qc_job_id')) if state.get('qc_job_id') else None
    if not locked or not job or job.status not in {'succeeded', 'succeeded_with_parse_warnings'}:
        flash('La selección de muestras estará disponible cuando finalice el QC conjunto.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    sample_ids = set(state.get('sample_ids') or [])
    included = set(request.form.getlist('included_sample_ids'))
    if not included.issubset(sample_ids):
        abort(400, 'La selección contiene muestras ajenas a esta cohorte.')
    if not included:
        flash('Selecciona al menos una muestra para crear el modelo.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    state['excluded_sample_ids'] = sorted(sample_ids - included)
    state['model_sample_ids'] = sorted(included)
    state['selection_saved_at'] = datetime.now(timezone.utc).isoformat()
    try:
        _write_cohort_state(project, state)
        flash(f'Selección guardada: {len(included)} muestra(s) para el modelo.', 'success')
    except OSError as exc:
        flash(f'No se pudo guardar la selección de muestras: {exc}', 'error')
    return redirect(url_for('main.project_sample_qc', project_id=project_id))


@bp.get('/projects/<project_id>/model/setup')
def project_model_setup(project_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    locked, state = _cohort_is_locked(project)
    job = db.session.get(WorkflowJob, state.get('qc_job_id')) if state.get('qc_job_id') else None
    model_runs = CohortModelRun.query.filter_by(project_id=project.id).order_by(
        CohortModelRun.created_at.desc()).all()
    if not (project.status == 'intervals_accepted' or
            (Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml').is_file()):
        flash('Acepta primero el análisis BED para acceder a los modelos.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    samples = (BamSample.query.filter(BamSample.project_id == project.id,
                                      BamSample.status.in_(['qc_complete', 'qc_partial']))
               .order_by(BamSample.sample_id).all())
    samples = [sample for sample in samples if sample.counts_path and Path(sample.counts_path).is_file()]
    can_create_model = len(samples) >= 2
    if not can_create_model and not model_runs:
        flash('Completa el QC de al menos dos muestras antes de crear modelos COHORT.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    reference = get_reference(project.reference_id)
    if not reference:
        abort(409, 'El bundle de referencia del proyecto ya no está disponible.')
    priors = ['CONTIG_NAME\t' + '\t'.join(f'PLOIDY_PRIOR_{i}' for i in range(5))]
    for contig in parse_sequence_dictionary(reference['dict']):
        sex_contig = re.sub(r'^chr', '', contig, flags=re.IGNORECASE).upper()
        if sex_contig == 'X':
            probabilities = ('0.005', '0.4925', '0.4925', '0.005', '0.005')
        elif sex_contig == 'Y':
            probabilities = ('0.4925', '0.4925', '0.005', '0.005', '0.005')
        else:
            probabilities = ('0.005', '0.005', '0.98', '0.005', '0.005')
        priors.append(f'{contig}\t' + '\t'.join(probabilities))
    revision_id = request.args.get('revision_of', '')
    batches = {batch.id: batch for batch in SampleBatch.query.filter(
        SampleBatch.id.in_({sample.batch_id for sample in samples})).all()}
    known_kits = sorted({batches[s.batch_id].kit_name.strip() for s in samples
                         if s.batch_id in batches and batches[s.batch_id].kit_name.strip()})
    known_protocols = sorted({batches[s.batch_id].protocol.strip() for s in samples
                              if s.batch_id in batches and batches[s.batch_id].protocol.strip()})
    warnings = []
    if len(known_kits) > 1 or len(known_protocols) > 1:
        warnings.append('La selección mezcla kits o protocolos; GATK recomienda formar cohortes técnicamente homogéneas.')
    if not known_kits or not known_protocols:
        warnings.append('Hay metadatos de kit/protocolo sin informar; revisa que las muestras sean comparables.')
    return render_template('project_model_setup.html', project=project, samples=samples,
                           model_state=state, model_runs=model_runs,
                           default_ploidy_priors='\n'.join(priors), model_warnings=warnings,
                           revision_id=revision_id, can_create_model=len(samples) >= 2,
                           selected_sample_ids=set(state.get('model_sample_ids') or [s.id for s in samples]))


@bp.post('/projects/<project_id>/models/<model_run_id>/delete')
def delete_failed_model(project_id, model_run_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    model_run = CohortModelRun.query.filter_by(id=model_run_id, project_id=project.id).first_or_404()
    job = db.session.get(WorkflowJob, model_run.job_id)
    if model_run.status != 'failed' or (job and job.status != 'failed'):
        flash('Solo se pueden eliminar modelos con el job terminado en estado failed.', 'error')
        return redirect(url_for('main.project_models_page', project_id=project.id))

    models_root = (Path(project.storage_path) / 'models').resolve()
    run_root = models_root / model_run.id
    if run_root.parent != models_root:
        abort(409, 'La carpeta del modelo no está dentro del directorio de modelos del proyecto.')
    if run_root.is_symlink():
        abort(409, 'No se eliminará una carpeta de modelo enlazada simbólicamente.')
    model_name = model_run.name

    try:
        # A retry may point to the failed execution as its revision base. Keep
        # surviving runs attached to the deleted run's own parent.
        CohortModelRun.query.filter_by(revision_of=model_run.id).update(
            {CohortModelRun.revision_of: model_run.revision_of}, synchronize_session=False)
        if job:
            db.session.delete(job)
        db.session.delete(model_run)
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        flash(f'No se pudo eliminar el modelo fallido: {exc}', 'error')
        return redirect(url_for('main.project_models_page', project_id=project.id))

    try:
        if run_root.exists():
            shutil.rmtree(run_root)
    except OSError as exc:
        flash(f'Modelo eliminado del listado, pero no se pudieron borrar todos sus archivos: {exc}', 'error')
        return redirect(url_for('main.project_models_page', project_id=project.id))
    flash(f'Modelo fallido {model_name} eliminado.', 'success')
    return redirect(url_for('main.project_models_page', project_id=project.id))


@bp.get('/projects/<project_id>/models')
def project_models_page(project_id):
    return project_model_setup(project_id)


def _parse_ploidy_priors(text, expected_contigs):
    rows = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if len(rows) < 2:
        raise ValueError('La tabla de priors debe incluir cabecera y al menos una fila.')
    header = rows[0].split('\t')
    if header != ['CONTIG_NAME'] + [f'PLOIDY_PRIOR_{i}' for i in range(len(header) - 1)] or len(header) < 4:
        raise ValueError('Usa columnas CONTIG_NAME y PLOIDY_PRIOR_0, PLOIDY_PRIOR_1, ... contiguas.')
    parsed = {}
    for line_number, line in enumerate(rows[1:], start=2):
        fields = line.split('\t')
        if len(fields) != len(header):
            raise ValueError(f'La fila {line_number} no tiene {len(header)} columnas tabuladas.')
        contig = fields[0].strip()
        if not contig or contig in parsed:
            raise ValueError(f'Contig vacío o duplicado en fila {line_number}.')
        try:
            probabilities = [float(value) for value in fields[1:]]
        except ValueError as exc:
            raise ValueError(f'Probabilidad no numérica en fila {line_number}.') from exc
        if any(not 0 <= value <= 1 for value in probabilities) or abs(sum(probabilities) - 1) > 1e-6:
            raise ValueError(f'Las probabilidades de {contig} deben estar entre 0 y 1 y sumar 1.')
        parsed[contig] = probabilities
    if set(parsed) != set(expected_contigs):
        missing, unknown = sorted(set(expected_contigs) - set(parsed)), sorted(set(parsed) - set(expected_contigs))
        details = []
        if missing:
            details.append('faltan ' + ', '.join(missing[:8]))
        if unknown:
            details.append('no existen ' + ', '.join(unknown[:8]))
        raise ValueError('La tabla debe cubrir exactamente los contigs de referencia (' + '; '.join(details) + ').')
    return '\n'.join(rows) + '\n'


def _read_interval_list_coordinates(path):
    rows = []
    with Path(path).open(encoding='utf-8') as handle:
        for line in handle:
            if not line.strip() or line.startswith('@'):
                continue
            fields = line.rstrip('\n').split('\t')
            if len(fields) < 3:
                raise ValueError(f'Interval list inválido: {line[:100]!r}')
            rows.append((fields[0], int(fields[1]), int(fields[2])))
    return rows


@bp.post('/projects/<project_id>/models')
def launch_cohort_model(project_id):
    project = _require_accepted_project(project_id)
    state = _read_cohort_state(project)
    eligible = (BamSample.query.filter(BamSample.project_id == project.id,
                                       BamSample.status.in_(['qc_complete', 'qc_partial']))
                .order_by(BamSample.sample_id).all())
    eligible = [sample for sample in eligible
                if sample.counts_path and Path(sample.counts_path).is_file()]
    eligible_ids = {sample.id for sample in eligible}
    posted_ids = set(request.form.getlist('included_sample_ids'))
    selection_submitted = request.form.get('selection_form') == '1'
    selected_ids = posted_ids if selection_submitted else set(state.get('model_sample_ids') or eligible_ids)
    if not selected_ids or not selected_ids.issubset(eligible_ids):
        flash('Selecciona únicamente muestras con QC completo y read counts disponibles.', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))
    samples = (BamSample.query.filter(BamSample.id.in_(selected_ids),
                                      BamSample.project_id == project.id,
                                      BamSample.status.in_(['qc_complete', 'qc_partial']))
               .order_by(BamSample.sample_id).all())
    if len(samples) != len(selected_ids) or any(not sample.counts_path or not Path(sample.counts_path).is_file() for sample in samples):
        flash('La selección incluye muestras sin read counts QC disponibles.', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))
    if len(samples) < 2:
        flash('DetermineGermlineContigPloidy requiere al menos dos muestras para inferir un modelo en modo COHORT.', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))

    accepted = yaml.safe_load((Path(project.storage_path) / 'bed_analysis' /
                               'accepted_analysis.yaml').read_text(encoding='utf-8')) or {}
    bed_job = db.session.get(WorkflowJob, accepted.get('job_id')) if accepted.get('job_id') else None
    if not bed_job:
        flash('No se encuentra la ejecución de análisis BED aceptada.', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))
    intervals = Path(bed_job.output_path) / 'preprocessed.interval_list'
    if not intervals.is_file():
        flash('Faltan los intervalos de la ejecución BED aceptada.', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))

    try:
        expected_intervals, _ = read_gatk_counts(samples[0].counts_path)
        if expected_intervals != _read_interval_list_coordinates(intervals):
            raise ValueError('Los intervalos de los recuentos no coinciden con el BED aceptado.')
        for sample in samples[1:]:
            found, _ = read_gatk_counts(sample.counts_path)
            if found != expected_intervals:
                raise ValueError(f'Los read counts de {sample.sample_id} usan intervalos diferentes.')
        reference = get_reference(project.reference_id)
        priors = _parse_ploidy_priors(request.form.get('contig_ploidy_priors', ''),
                                      parse_sequence_dictionary(reference['dict']))
        model_name = (request.form.get('name') or '').strip() or datetime.now(timezone.utc).strftime('COHORT %Y-%m-%d %H:%M UTC')
        if len(model_name) > 200:
            raise ValueError('El nombre del modelo no puede superar 200 caracteres.')
        coverage_normalization_scope = request.form.get('coverage_normalization_scope', 'retained')
        if coverage_normalization_scope not in {'all', 'retained'}:
            raise ValueError('El conjunto de referencia de normalización debe ser todos o retenidos.')

        def number(field, default, low, high, integer=False):
            raw = request.form.get(field, str(default)).strip()
            value = int(raw) if integer else float(raw)
            if not low <= value <= high:
                raise ValueError(f'{field} debe estar entre {low} y {high}.')
            return value

        filter_options = {
            'enabled': request.form.get('filter_intervals') == 'on',
            'low_count_filter_count_threshold': number('low_count_filter_count_threshold', 5, 0, 100000, True),
            'low_count_filter_percentage_of_samples': number('low_count_filter_percentage_of_samples', 90, 0, 100),
            'extreme_count_filter_minimum_percentile': number('extreme_count_filter_minimum_percentile', 1, 0, 100),
            'extreme_count_filter_maximum_percentile': number('extreme_count_filter_maximum_percentile', 99, 0, 100),
            'extreme_count_filter_percentage_of_samples': number('extreme_count_filter_percentage_of_samples', 90, 0, 100),
        }
        if filter_options['extreme_count_filter_minimum_percentile'] >= filter_options['extreme_count_filter_maximum_percentile']:
            raise ValueError('El percentil extremo mínimo debe ser menor que el máximo.')
        ploidy_options = {
            'mean_bias_standard_deviation': number('mean_bias_standard_deviation', 0.01, 0, 100),
            'global_psi_scale': number('global_psi_scale', 0.001, 0, 100),
            'sample_psi_scale': number('sample_psi_scale', 0.0001, 0, 100),
        }
        caller_options = {
            'max_copy_number': number('max_copy_number', 5, 1, 20, True),
            'max_bias_factors': number('max_bias_factors', 5, 0, 100, True),
            'max_training_epochs': number('max_training_epochs', 50, 1, 10000, True),
            'min_training_epochs': number('min_training_epochs', 10, 0, 10000, True),
            'max_calling_iters': number('max_calling_iters', 10, 0, 10000, True),
            'p_active': number('p_active', 0.01, 0, 1),
            'p_alt': number('p_alt', 0.000001, 0, 1),
            'convergence_snr_averaging_window': number('convergence_snr_averaging_window', 500, 1, 100000, True),
            'convergence_snr_countdown_window': number('convergence_snr_countdown_window', 10, 0, 100000, True),
            'convergence_snr_trigger_threshold': number('convergence_snr_trigger_threshold', 0.1, 0, 100000),
            'verbosity': request.form.get('verbosity', 'DEBUG').upper(),
        }
        if caller_options['verbosity'] not in {'DEBUG', 'INFO'}:
            raise ValueError('El nivel de detalle debe ser DEBUG o INFO.')
        if caller_options['min_training_epochs'] > caller_options['max_training_epochs']:
            raise ValueError('Las épocas mínimas no pueden superar las máximas.')
        revision_id = request.form.get('revision_of') or None
        if revision_id and not CohortModelRun.query.filter_by(id=revision_id, project_id=project.id).first():
            raise ValueError('La revisión seleccionada no pertenece a este proyecto.')
        snapshot = []
        for sample in samples:
            if not sample.counts_path or not Path(sample.counts_path).is_file():
                raise ValueError(f'No se encuentra el read counts de {sample.sample_id}.')
            batch = sample.batch
            snapshot.append({
                'id': sample.id, 'sample_id': sample.sample_id,
                'counts': str(Path(sample.counts_path).resolve()),
                'counts_sha256': sha256_file(sample.counts_path),
                'qc_status': sample.status, 'batch': batch.name if batch else '',
                'kit_name': batch.kit_name if batch else '', 'protocol': batch.protocol if batch else '',
                'read_group_sm': sample.read_group_sm,
            })
        kits = {item['kit_name'].strip() for item in snapshot if item['kit_name'].strip()}
        protocols = {item['protocol'].strip() for item in snapshot if item['protocol'].strip()}
        if len(kits) > 1 or len(protocols) > 1:
            raise ValueError('La selección mezcla kits o protocolos conocidos; homogeneiza la cohorte antes de encolar.')
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        flash(f'No se pudo validar la configuración del modelo: {exc}', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))

    run_id, job_id = new_id(), new_id()
    run_root = Path(project.storage_path) / 'models' / run_id
    job_root = run_root / 'execution'
    output_path = run_root / 'outputs'
    try:
        job_root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        flash(f'No se pudo crear el directorio del modelo: {exc}', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))
    parameters = {
        'schema_version': '1.0', 'pipeline': 'cohort_model', 'pipeline_version': '0.1.0',
        'gatk_version': '4.7.0.0', 'model_run_id': run_id, 'model_name': model_name,
        'reference_id': project.reference_id, 'reference_bundle_version': project.reference_bundle_version,
        'reference_dict': reference['dict'], 'reference_dict_sha256': sha256_file(reference['dict']),
        'reference_fasta': reference['fasta'], 'reference_fasta_sha256': sha256_file(reference['fasta']),
        'coverage_normalization_scope': coverage_normalization_scope,
        'intervals': str(intervals.resolve()), 'intervals_sha256': sha256_file(intervals),
        'bed_path': str(Path(project.canonical_bed_path).resolve()),
        'contig_ploidy_priors_sha256': hashlib.sha256(priors.encode('utf-8')).hexdigest(),
        'filter_options': filter_options, 'ploidy_options': ploidy_options,
        'gcnv_options': caller_options,
        'contig_ploidy_priors': str((run_root / 'contig_ploidy_priors.tsv').resolve()),
        'samples': [{'sample_id': item['sample_id'], 'counts': item['counts'],
                     'counts_sha256': item['counts_sha256']} for item in snapshot],
        'sample_snapshot': snapshot,
    }
    parameters_path, execution_path, log_path = (job_root / 'parameters.yaml',
                                                   job_root / 'execution.yaml', job_root / 'nextflow.log')
    execution = {
        'executor_profile': 'local', 'nextflow_config_path': _nextflow_config_path(),
        'nextflow_binary': current_app.config['NEXTFLOW_BIN'],
        'workflow_path': str(current_app.config['COHORT_MODEL_WORKFLOW_PATH']),
        'output_path': str(output_path), 'work_path': str(job_root / 'work'),
        'apptainer_cache': str(current_app.config['APPTAINER_CACHE']),
        'gatk_container': current_app.config['GATK_CONTAINER'], 'resources': _execution_resources(),
    }
    try:
        output_path.mkdir(parents=True, exist_ok=False)
        parameters_path.write_text(yaml.safe_dump(parameters, sort_keys=False, allow_unicode=True), encoding='utf-8')
        execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
        parameters_sha256 = sha256_file(parameters_path)
        execution_sha256 = sha256_file(execution_path)
        (run_root / 'contig_ploidy_priors.tsv').write_text(priors, encoding='utf-8')
        (output_path / 'parameters.yaml').write_text(
            yaml.safe_dump(parameters, sort_keys=False, allow_unicode=True), encoding='utf-8')
        (output_path / 'execution.yaml').write_text(
            yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
        (output_path / 'contig_ploidy_priors.tsv').write_text(priors, encoding='utf-8')
        log_path.touch()
        db.session.add(CohortModelRun(
            id=run_id, project_id=project.id, revision_of=revision_id, name=model_name, status='queued',
            sample_snapshot_json=json.dumps(snapshot, ensure_ascii=False), reference_id=project.reference_id,
            reference_bundle_version=project.reference_bundle_version, intervals_path=str(intervals.resolve()),
            parameters_path=str(parameters_path), parameters_sha256=parameters_sha256,
            execution_path=str(execution_path), execution_sha256=execution_sha256,
            output_path=str(output_path), job_id=job_id))
        db.session.add(WorkflowJob(
            id=job_id, project_id=project.id, workflow='cohort_model', status='queued',
            parameters_path=str(parameters_path), execution_path=str(execution_path),
            output_path=str(output_path), log_path=str(log_path)))
        db.session.commit()
    except (OSError, SQLAlchemyError) as exc:
        db.session.rollback()
        shutil.rmtree(run_root, ignore_errors=True)
        flash(f'No se pudo guardar el snapshot del modelo: {exc}', 'error')
        return redirect(url_for('main.project_model_setup', project_id=project_id))
    flash(f'Modelo {model_name} añadido a la cola con {len(snapshot)} muestras.', 'success')
    return redirect(url_for('main.project_model_detail', project_id=project_id, model_run_id=run_id))


@bp.get('/projects/<project_id>/models/<model_run_id>')
def project_model_detail(project_id, model_run_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    model_run = CohortModelRun.query.filter_by(id=model_run_id, project_id=project.id).first_or_404()
    job = db.session.get(WorkflowJob, model_run.job_id)
    metrics_path = Path(model_run.output_path) / 'model_metrics.json'
    metrics = json.loads(metrics_path.read_text(encoding='utf-8')) if metrics_path.is_file() else None
    quality = None
    quality_error = None
    try:
        sample_snapshot = json.loads(model_run.sample_snapshot_json)
    except (TypeError, json.JSONDecodeError):
        sample_snapshot = []
    output = Path(model_run.output_path)
    all_artifacts = [str(path.relative_to(output)) for path in sorted(output.rglob('*')) if path.is_file()] if output.is_dir() else []
    artifacts = all_artifacts[:200]
    diagnostics = None
    diagnostics_error = None
    if job and job.status in {'succeeded', 'succeeded_with_parse_warnings'} and Path(job.parameters_path).is_file():
        try:
            params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
            params.setdefault('model_run_id', model_run.id)
            params.setdefault('reference_id', model_run.reference_id)
            params.setdefault('reference_bundle_version', model_run.reference_bundle_version)
            params.setdefault('bed_path', project.canonical_bed_path)
            params.setdefault('model_created_at', model_run.created_at.isoformat() if model_run.created_at else None)
            if job.execution_path and Path(job.execution_path).is_file():
                execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
                params.setdefault('container_image', execution.get('gatk_container'))
                params.setdefault('container_digest', execution.get('gatk_container_digest'))
            if not params.get('reference_fasta'):
                reference = get_reference(model_run.reference_id)
                if reference:
                    params['reference_fasta'] = reference['fasta']
                    params['reference_fasta_sha256'] = sha256_file(reference['fasta'])
            diagnostics = build_cohort_diagnostics(Path(model_run.output_path), params)
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
            current_app.logger.exception('No se pudieron cargar los diagnósticos del modelo %s', model_run.id)
            diagnostics_error = str(exc)
    model_runs = CohortModelRun.query.filter_by(project_id=project.id).order_by(
        CohortModelRun.created_at.desc()).all()
    return render_template('project_model_dashboard.html', project=project, model_run=model_run,
                           model_runs=model_runs,
                           job=job, metrics=metrics, quality=quality, quality_error=quality_error,
                           diagnostics=diagnostics, diagnostics_error=diagnostics_error,
                           sample_snapshot=sample_snapshot, artifacts=artifacts,
                           artifact_total=len(all_artifacts))


def _accepted_interval_list(project):
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    if not accepted_path.is_file():
        return None
    accepted = yaml.safe_load(accepted_path.read_text(encoding='utf-8')) or {}
    job = db.session.get(WorkflowJob, accepted.get('job_id')) if accepted.get('job_id') else None
    path = Path(job.output_path) / 'preprocessed.interval_list' if job else (
        Path(project.storage_path) / 'bed_analysis' / 'current' / 'preprocessed.interval_list')
    return path if path.is_file() else None


@bp.get('/projects/<project_id>/validation')
def project_validation(project_id):
    reconcile_finished_jobs()
    project = get_project_or_404(project_id)
    interval_list = _accepted_interval_list(project)
    models = CohortModelRun.query.filter(
        CohortModelRun.project_id == project.id,
        CohortModelRun.status.in_(['succeeded', 'succeeded_with_parse_warnings'])
    ).order_by(CohortModelRun.created_at.desc()).all()
    models = [run for run in models
              if (Path(run.output_path) / 'caller' / 'cohort-model').is_dir()
              and (Path(run.output_path) / 'ploidy' / 'cohort-model').is_dir()]
    samples = ValidationSample.query.filter_by(project_id=project.id).order_by(
        ValidationSample.sample_id).all()
    sample_rows = []
    for sample in samples:
        try:
            known_cnvs = json.loads(sample.known_cnvs_json or '[]')
        except json.JSONDecodeError:
            known_cnvs = []
        sample_rows.append({'sample': sample, 'known_cnvs': known_cnvs})
    runs = ModelValidationRun.query.filter_by(project_id=project.id).order_by(
        ModelValidationRun.created_at.desc()).all()
    summaries = {}
    validation_charts = {}
    for run in runs:
        try:
            summaries[run.id] = json.loads(run.summary_json) if run.summary_json else None
        except json.JSONDecodeError:
            summaries[run.id] = None
        if summaries[run.id]:
            try:
                validation_charts[run.id] = json.loads(
                    (Path(run.output_path) / 'validation_charts.json').read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                validation_charts[run.id] = {}
                try:
                    params_path = Path(run.output_path) / 'parameters.yaml'
                    params = yaml.safe_load(params_path.read_text(encoding='utf-8')) or {}
                    if not params.get('annotated_intervals'):
                        model_run = db.session.get(CohortModelRun, run.model_run_id)
                        if model_run:
                            model_params = yaml.safe_load(Path(model_run.parameters_path).read_text(
                                encoding='utf-8')) or {}
                            original_intervals = Path(model_params.get('intervals') or '')
                            candidate = original_intervals.parent / 'annotated_intervals.tsv'
                            if candidate.is_file():
                                params['annotated_intervals'] = str(candidate)
                    report = build_validation_report(run.output_path, params)
                    chart_samples = []
                    for item in report.get('samples', []):
                        points = item.get('gc_coverage_points') or []
                        if len(points) > 5000:
                            step = (len(points) - 1) / 4999
                            points = [points[round(index * step)] for index in range(5000)]
                        chart_row = {key: item.get(key) for key in (
                            'sample_id', 'sample_unexplained_variance_log_psi',
                            'sample_unexplained_variance_exp_log_psi_derived', 'global_read_depth',
                            'bias_factor_loadings')}
                        chart_row['gc_coverage_points'] = points
                        chart_samples.append(chart_row)
                    validation_charts[run.id] = {
                        'samples': chart_samples, 'gc_curve': report.get('gc_curve', []),
                        'gc_curve_available': report.get('gc_curve_available', False),
                        'gc_coverage_available': report.get('gc_coverage_available', False),
                        'autosomal_ploidy_exceptions': report.get('autosomal_ploidy_exceptions', []),
                    }
                    (Path(run.output_path) / 'validation_charts.json').write_text(
                        json.dumps(validation_charts[run.id], ensure_ascii=False), encoding='utf-8')
                except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
                    pass
            charts = validation_charts[run.id]
            summaries[run.id]['autosomal_ploidy_exceptions'] = charts.get('autosomal_ploidy_exceptions', [])
            summaries[run.id]['gc_curve_available'] = charts.get('gc_curve_available', False)
            summaries[run.id]['gc_coverage_available'] = charts.get('gc_coverage_available', False)
            chart_by_id = {item.get('sample_id'): item for item in charts.get('samples', [])}
            for sample_row in summaries[run.id].get('samples', []):
                chart_row = chart_by_id.get(sample_row.get('sample_id'), {})
                for key in ('sample_unexplained_variance_log_psi',
                            'sample_unexplained_variance_exp_log_psi_derived', 'global_read_depth'):
                    sample_row.setdefault(key, chart_row.get(key))
    return render_template('project_validation.html', project=project, models=models,
                           samples=sample_rows, runs=runs, summaries=summaries,
                           validation_charts=validation_charts,
                           can_upload=interval_list is not None)


@bp.post('/projects/<project_id>/validation/samples')
def upload_validation_samples(project_id):
    project = _require_accepted_project(project_id)
    interval_list = _accepted_interval_list(project)
    reference = get_reference(project.reference_id)
    files = [item for item in request.files.getlist('alignment_files') if item and item.filename]
    upload_token = (request.form.get('upload_token') or '').strip()
    if (not files and not upload_token) or not interval_list or not reference:
        flash('Selecciona BAMs con sus BAI y comprueba el BED aceptado y la referencia del proyecto.', 'error')
        return redirect(url_for('main.project_validation', project_id=project.id))
    if files and any(not Path(item.filename).name.lower().endswith(('.bam', '.bai')) for item in files):
        flash('Solo se aceptan ficheros BAM y BAI.', 'error')
        return redirect(url_for('main.project_validation', project_id=project.id))
    reference_dict = load_reference_dictionary(reference['dict'])
    upload_root = Path(project.storage_path) / 'samples' / 'validation'
    batch_root = upload_root / new_id()
    staged = []
    staging = None
    try:
        if upload_token:
            if not re.fullmatch(r'[0-9a-f]{32}', upload_token):
                raise ValueError('Sesión de carga no válida.')
            staging = upload_root / f'.uploading-{upload_token}'
            if not staging.is_dir():
                raise ValueError('La sesión de carga ya no existe; vuelve a seleccionar los BAMs.')
            for sample_dir in sorted(path for path in staging.iterdir() if path.is_dir()):
                sample_id = sample_dir.name
                bam_path, bai_path = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
                if not bam_path.is_file() or not bai_path.is_file():
                    raise ValueError(f'La transferencia de {sample_id} está incompleta.')
                staged.append([sample_id, bam_path, bai_path, None])
        else:
            bams = [item for item in files if Path(item.filename).name.lower().endswith('.bam')]
            bais = [item for item in files if Path(item.filename).name.lower().endswith('.bai')]
            index_by_sample = {}
            for bai in bais:
                name = Path(bai.filename).name
                stem = name[:-8] if name.lower().endswith('.bam.bai') else name[:-4]
                sample_id = stem.split('.', 1)[0]
                if sample_id in index_by_sample:
                    raise ValueError(f'Hay más de un BAI para la muestra {sample_id}.')
                index_by_sample[sample_id] = bai
            staging = upload_root / f'.staging-{new_id()}'
            staging.mkdir(parents=True, exist_ok=False)
            seen = set()
            for bam_file in bams:
                filename = Path(bam_file.filename).name
                sample_id = _sample_id_from_bam_name(filename)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sample_id):
                    raise ValueError(f'Nombre BAM inválido para sample ID: {filename}.')
                if sample_id in seen:
                    raise ValueError(f'ID de muestra duplicado: {sample_id}.')
                seen.add(sample_id)
                bai_file = index_by_sample.get(sample_id)
                if not bai_file:
                    raise ValueError(f'No hay match para la muestra {sample_id}: falta su BAI.')
                sample_dir = staging / sample_id
                sample_dir.mkdir()
                bam_path, bai_path = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
                bam_file.save(bam_path)
                bai_file.save(bai_path)
                staged.append([sample_id, bam_path, bai_path, None])
            if set(index_by_sample) != seen:
                raise ValueError('Hay índices BAI sin BAM correspondiente.')
        existing_ids = {sample_id for (sample_id,) in db.session.query(ValidationSample.sample_id)
                        .filter_by(project_id=project.id).all()}
        seen = set()
        if not staged:
            raise ValueError('No hay pares BAM/BAI transferidos en esta sesión.')
        for item in staged:
            sample_id, bam_path, bai_path, _ = item
            if sample_id in seen or sample_id in existing_ids:
                raise ValueError(f'Ya existe la muestra de validación {sample_id}.')
            seen.add(sample_id)
            report = inspect_bam(bam_path, bai_path, sample_id, reference_dict,
                                 interval_list, current_app.config['SAMTOOLS_BIN'])
            item[3] = report
        batch_root.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(batch_root)
        for item in staged:
            sample_id, _bam_path, _bai_path, report = item
            bam_path = batch_root / sample_id / f'{sample_id}.bam'
            bai_path = batch_root / sample_id / f'{sample_id}.bam.bai'
            db.session.add(ValidationSample(project_id=project.id, sample_id=sample_id,
                bam_path=str(bam_path), bai_path=str(bai_path), known_cnvs_json='[]',
                validation_report=json.dumps(report, ensure_ascii=False)))
        db.session.commit()
        flash(f'{len(staged)} muestra(s) de validación añadidas y validadas.', 'success')
    except (OSError, ValueError, subprocess.SubprocessError, SQLAlchemyError) as exc:
        db.session.rollback()
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(batch_root, ignore_errors=True)
        flash(f'No se pudieron añadir las muestras de validación: {exc}', 'error')
    return redirect(url_for('main.project_validation', project_id=project.id))


@bp.post('/projects/<project_id>/validation/upload-sessions')
def create_validation_upload_session(project_id):
    project = _require_accepted_project(project_id)
    token = new_id()
    upload_dir = Path(project.storage_path) / 'samples' / 'validation' / f'.uploading-{token}'
    upload_dir.mkdir(parents=True, exist_ok=False)
    return jsonify({'token': token,
        'upload_url': url_for('main.upload_validation_pair', project_id=project.id, token=token)})


@bp.post('/projects/<project_id>/validation/upload-sessions/<token>/files')
def upload_validation_pair(project_id, token):
    project = _require_accepted_project(project_id)
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        abort(404)
    upload_dir = Path(project.storage_path) / 'samples' / 'validation' / f'.uploading-{token}'
    if not upload_dir.is_dir():
        abort(404)
    bam_file, bai_file = request.files.get('bam_file'), request.files.get('bai_file')
    if not bam_file or not bai_file or not bam_file.filename or not bai_file.filename:
        return jsonify({'error': 'Debe enviarse un BAM con su BAI.'}), 400
    bam_name, bai_name = Path(bam_file.filename).name, Path(bai_file.filename).name
    sample_id = _sample_id_from_bam_name(bam_name)
    bai_stem = bai_name[:-8] if bai_name.lower().endswith('.bam.bai') else (
        bai_name[:-4] if bai_name.lower().endswith('.bai') else '')
    bai_sample = bai_stem.split('.', 1)[0]
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sample_id)
            or bai_sample != sample_id):
        return jsonify({'error': f'No hay match para la muestra {sample_id or "(ID no válido)"}: '
                        f'el BAM {bam_name} y el BAI {bai_name} no corresponden.'}), 400
    sample_dir = upload_dir / sample_id
    if sample_dir.exists():
        return jsonify({'error': f'Ya se transfirió un archivo para {sample_id} en esta sesión.'}), 409
    sample_dir.mkdir(parents=True, exist_ok=False)
    bam_target, bai_target = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
    try:
        bam_file.save(bam_target)
        bai_file.save(bai_target)
    except OSError as exc:
        shutil.rmtree(sample_dir, ignore_errors=True)
        return jsonify({'error': f'No se pudo guardar {sample_id}: {exc}'}), 500
    return jsonify({'sample_id': sample_id, 'status': 'transferred'})


@bp.delete('/projects/<project_id>/validation/upload-sessions/<token>')
def discard_validation_upload_session(project_id, token):
    project = get_project_or_404(project_id)
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        abort(404)
    upload_dir = Path(project.storage_path) / 'samples' / 'validation' / f'.uploading-{token}'
    if upload_dir.is_dir():
        shutil.rmtree(upload_dir)
    return '', 204


@bp.post('/projects/<project_id>/validation/samples/<sample_id>/cnvs')
def save_validation_cnvs(project_id, sample_id):
    project = get_project_or_404(project_id)
    sample = ValidationSample.query.filter_by(project_id=project.id, id=sample_id).first_or_404()
    reference = get_reference(project.reference_id)
    contigs = load_reference_dictionary(reference['dict']) if reference else {}
    parsed = []
    try:
        for line_number, line in enumerate(request.form.get('known_cnvs', '').splitlines(), 1):
            if not line.strip():
                continue
            fields = re.split(r'\s+', line.strip())
            if len(fields) != 4:
                raise ValueError(f'Línea {line_number}: usa chr, start, end y type separados por tabulador o espacios.')
            chrom, start_text, end_text, kind = fields
            start, end, kind = int(start_text), int(end_text), kind.upper()
            if chrom not in contigs:
                raise ValueError(f'Línea {line_number}: el contig {chrom} no existe en la referencia del proyecto.')
            if start < 1 or end < start or end > contigs[chrom]:
                raise ValueError(f'Línea {line_number}: coordenadas inválidas para {chrom}.')
            if kind not in {'DEL', 'DUP', 'CNV'}:
                raise ValueError(f'Línea {line_number}: type debe ser DEL, DUP o CNV.')
            parsed.append({'chrom': chrom, 'start': start, 'end': end, 'type': kind})
        sample.known_cnvs_json = json.dumps(parsed, ensure_ascii=False)
        db.session.commit()
        flash(f'CNVs conocidas guardadas para {sample.sample_id}.', 'success')
    except (ValueError, TypeError, SQLAlchemyError) as exc:
        db.session.rollback()
        flash(f'No se pudieron guardar las CNVs: {exc}', 'error')
    return redirect(url_for('main.project_validation', project_id=project.id))


@bp.post('/projects/<project_id>/validation/run')
def launch_model_validation(project_id):
    project = _require_accepted_project(project_id)
    model_id = request.form.get('model_run_id', '')
    model = CohortModelRun.query.filter_by(id=model_id, project_id=project.id).first()
    selected_ids = set(request.form.getlist('sample_ids'))
    samples = ValidationSample.query.filter(ValidationSample.project_id == project.id,
                                           ValidationSample.id.in_(selected_ids)).order_by(
                                               ValidationSample.sample_id).all()
    if not model or model.status not in {'succeeded', 'succeeded_with_parse_warnings'}:
        flash('Selecciona un modelo COHORT completado.', 'error')
        return redirect(url_for('main.project_validation', project_id=project.id))
    if not samples or len(samples) != len(selected_ids):
        flash('Selecciona al menos una muestra de validación disponible.', 'error')
        return redirect(url_for('main.project_validation', project_id=project.id))
    run_id, job_id = new_id(), new_id()
    root = Path(project.storage_path) / 'validation_runs' / run_id
    job_root, output = root / 'execution', root / 'outputs'
    model_params_path = Path(model.parameters_path)
    try:
        model_params = yaml.safe_load(model_params_path.read_text(encoding='utf-8')) or {}
        model_output = Path(model.output_path)
        cohort_model_path = model_output / 'caller' / 'cohort-model'
        ploidy_model_path = model_output / 'ploidy' / 'cohort-model'
        if not cohort_model_path.is_dir() or not ploidy_model_path.is_dir():
            raise ValueError('El modelo seleccionado no contiene sus modelos CASE de cobertura y ploidía.')
        interval_path = (model_output / 'filtered.interval_list'
                         if model_params.get('filter_options', {}).get('enabled')
                         else Path(model_params['intervals']))
        if not interval_path.is_file():
            raise ValueError('No se encuentra el interval list usado por el modelo seleccionado.')
        reference = get_reference(project.reference_id)
        if not reference:
            raise ValueError('No se encuentra el bundle de referencia del proyecto.')
        snapshots = []
        for sample in samples:
            bam_path, bai_path = Path(sample.bam_path), Path(sample.bai_path)
            if not bam_path.is_file() or not bai_path.is_file():
                raise ValueError(f'Faltan BAM/BAI de {sample.sample_id}.')
            known = json.loads(sample.known_cnvs_json or '[]')
            snapshots.append({'id': sample.id, 'sample_id': sample.sample_id,
                'bam': str(bam_path.resolve()), 'bai': str(bai_path.resolve()),
                'bam_sha256': sha256_file(bam_path), 'bai_sha256': sha256_file(bai_path),
                'known_cnvs': known})
        training = [{'sample_id': item['sample_id'], 'counts': item['counts']}
                    for item in model_params.get('samples', [])]
        annotated_intervals = Path(model_params['intervals']).parent / 'annotated_intervals.tsv'
        run_root = root
        job_root.mkdir(parents=True, exist_ok=False)
        output.mkdir(parents=True, exist_ok=False)
        params = {'schema_version': '1.0', 'pipeline': 'model_validation', 'pipeline_version': '0.1.0',
            'gatk_version': model_params.get('gatk_version', '4.7.0.0'), 'validation_run_id': run_id,
            'model_run_id': model.id, 'model_name': model.name, 'samples': snapshots,
            'training_samples': training, 'intervals': str(interval_path.resolve()),
            'cohort_model': str(cohort_model_path.resolve()), 'ploidy_model': str(ploidy_model_path.resolve()),
            'contig_ploidy_priors': model_params['contig_ploidy_priors'],
            'annotated_intervals': str(annotated_intervals.resolve()) if annotated_intervals.is_file() else None,
            'reference_fasta': reference['fasta'], 'reference_fai': reference['fai'],
            'reference_dict': reference['dict'], 'reference_id': project.reference_id}
        params_path, execution_path, log_path = (job_root / 'parameters.yaml', job_root / 'execution.yaml',
                                                  job_root / 'nextflow.log')
        params_path.write_text(yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding='utf-8')
        execution = {'executor_profile': 'local', 'nextflow_config_path': _nextflow_config_path(),
            'nextflow_binary': current_app.config['NEXTFLOW_BIN'],
            'workflow_path': str(current_app.config['VALIDATION_WORKFLOW_PATH']),
            'output_path': str(output), 'work_path': str(job_root / 'work'),
            'apptainer_cache': str(current_app.config['APPTAINER_CACHE']),
            'gatk_container': current_app.config['GATK_CONTAINER'], 'resources': _execution_resources()}
        execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
        (output / 'parameters.yaml').write_text(yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding='utf-8')
        (output / 'execution.yaml').write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
        log_path.touch()
        validation_run = ModelValidationRun(id=run_id, project_id=project.id, model_run_id=model.id,
            sample_snapshot_json=json.dumps(snapshots, ensure_ascii=False), status='queued',
            output_path=str(output), job_id=job_id)
        job = WorkflowJob(id=job_id, project_id=project.id, workflow='model_validation', status='queued',
            parameters_path=str(params_path), execution_path=str(execution_path), output_path=str(output),
            log_path=str(log_path))
        db.session.add_all([validation_run, job])
        db.session.commit()
        flash(f'Validación del modelo {model.name} encolada para {len(samples)} muestra(s).', 'success')
        return redirect(url_for('main.project_validation', project_id=project.id))
    except (OSError, ValueError, KeyError, TypeError, SQLAlchemyError, yaml.YAMLError) as exc:
        db.session.rollback()
        shutil.rmtree(root, ignore_errors=True)
        flash(f'No se pudo encolar la validación: {exc}', 'error')
        return redirect(url_for('main.project_validation', project_id=project.id))


@bp.get('/projects/<project_id>/validation/runs/<run_id>/samples/<sample_id>')
def validation_sample_detail(project_id, run_id, sample_id):
    project = get_project_or_404(project_id)
    run = ModelValidationRun.query.filter_by(id=run_id, project_id=project.id).first_or_404()
    try:
        summary = json.loads(run.summary_json or '{}')
    except json.JSONDecodeError:
        summary = {}
    sample_ref = next((item for item in summary.get('samples', []) if item.get('sample_id') == sample_id), None)
    if not sample_ref:
        abort(404)
    report_path = Path(run.output_path) / 'sample_reports' / f'{sample_id}.json'
    try:
        sample = json.loads(report_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        abort(404)
    return render_template('validation_sample_detail.html', project=project, run=run, sample=sample)


def _model_diagnostics_or_404(project_id, model_run_id):
    project = get_project_or_404(project_id)
    model_run = CohortModelRun.query.filter_by(id=model_run_id, project_id=project.id).first_or_404()
    job = db.session.get(WorkflowJob, model_run.job_id)
    if not job or not Path(job.parameters_path).is_file():
        abort(404)
    params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
    params.setdefault('model_run_id', model_run.id)
    params.setdefault('reference_id', model_run.reference_id)
    params.setdefault('reference_bundle_version', model_run.reference_bundle_version)
    params.setdefault('bed_path', project.canonical_bed_path)
    params.setdefault('model_created_at', model_run.created_at.isoformat() if model_run.created_at else None)
    if job.execution_path and Path(job.execution_path).is_file():
        execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
        params.setdefault('container_image', execution.get('gatk_container'))
        params.setdefault('container_digest', execution.get('gatk_container_digest'))
    diagnostics = build_cohort_diagnostics(Path(model_run.output_path), params)
    return model_run, params, diagnostics


@bp.get('/projects/<project_id>/models/compare')
def compare_cohort_models(project_id):
    project = get_project_or_404(project_id)
    model_ids = list(dict.fromkeys(request.args.getlist('model_id')))
    if len(model_ids) < 2:
        return jsonify({'error': 'Selecciona al menos dos modelos para compararlos.'}), 400
    if len(model_ids) > 4:
        return jsonify({'error': 'La comparativa admite hasta cuatro modelos.'}), 400
    runs = CohortModelRun.query.filter(CohortModelRun.project_id == project.id,
                                      CohortModelRun.id.in_(model_ids)).all()
    by_id = {run.id: run for run in runs}
    if len(by_id) != len(model_ids):
        return jsonify({'error': 'Una selección no pertenece a este proyecto.'}), 404
    rows = []
    for model_id in model_ids:
        run = by_id[model_id]
        row = {'model_id': run.id, 'name': run.name, 'status': run.status,
               'created_at': run.created_at.isoformat() if run.created_at else None}
        job = db.session.get(WorkflowJob, run.job_id)
        if job and job.status in {'succeeded', 'succeeded_with_parse_warnings'}:
            if not job.parameters_path or not Path(job.parameters_path).is_file():
                row['unavailable_reason'] = 'No se encuentra el snapshot de parámetros.'
                rows.append(row)
                continue
            try:
                _run, params, diagnostics = _model_diagnostics_or_404(project.id, run.id)
                options = params.get('gcnv_options') or {}
                row.update({'gatk_version': diagnostics['metadata'].get('gatk_version'),
                    'gcnvkernel_version': diagnostics['metadata'].get('gcnvkernel_version'),
                    'sample_count': diagnostics['summary'].get('sample_count'),
                    'sample_ids': [sample.get('sample_id') for sample in params.get('samples', [])],
                    'capture_kits': diagnostics['metadata'].get('capture_kits', []),
                    'protocols': diagnostics['metadata'].get('sequencing_protocols', []),
                    'retained_interval_count': diagnostics['summary'].get('retained_interval_count'),
                    'filtered_interval_count': diagnostics['summary'].get('excluded_interval_count'),
                    'filtered_percentage': diagnostics['summary'].get('excluded_percentage'),
                    'median_global_read_depth': diagnostics['summary'].get('median_global_read_depth'),
                    'median_unexplained_variance': diagnostics['summary'].get('median_sample_unexplained_variance'),
                    'median_posterior_entropy': diagnostics['summary'].get('posterior_entropy_median_normalized'),
                    'max_copy_number': options.get('max_copy_number'),
                    'max_bias_factors': options.get('max_bias_factors'),
                    'p_active': options.get('p_active'), 'p_alt': options.get('p_alt'),
                    'coverage_normalization_scope': diagnostics['metadata'].get('coverage_normalization_scope')})
            except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
                row['unavailable_reason'] = str(exc)
        else:
            row['unavailable_reason'] = 'El workflow no ha finalizado correctamente.'
        rows.append(row)
    return jsonify({'models': rows})


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/intervals')
def model_diagnostic_intervals(project_id, model_run_id):
    model_run, _params, _diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    try:
        result = query_intervals(
            Path(model_run.output_path), page=request.args.get('page', 1, type=int),
            page_size=request.args.get('page_size', 50, type=int), contig=request.args.get('contig', ''),
            status=request.args.get('status', ''), reason=request.args.get('reason', ''),
            search=request.args.get('search', ''), gc_min=request.args.get('gc_min', type=float),
            gc_max=request.args.get('gc_max', type=float), mappability_min=request.args.get('mappability_min', type=float),
            mappability_max=request.args.get('mappability_max', type=float),
            segmental_duplication_min=request.args.get('segmental_duplication_min', type=float),
            segmental_duplication_max=request.args.get('segmental_duplication_max', type=float),
            gene=request.args.get('gene', ''),
            median_coverage_min=request.args.get('median_coverage_min', type=float),
            median_coverage_max=request.args.get('median_coverage_max', type=float),
            coverage_variability_min=request.args.get('coverage_variability_min', type=float),
            coverage_variability_max=request.args.get('coverage_variability_max', type=float),
            metric_min=request.args.get('metric_min', type=float), metric_max=request.args.get('metric_max', type=float),
            metric=request.args.get('metric', 'robust_variability'),
            normalization=request.args.get('normalization', 'library_size'), sort=request.args.get('sort', 'metric'),
            direction=request.args.get('direction', 'desc'))
    except (ValueError, DiagnosticsUnavailable) as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(result)


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/interval-plot')
def model_diagnostic_interval_plot(project_id, model_run_id):
    model_run, _params, _diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    rows = load_interval_diagnostics(Path(model_run.output_path))
    contig = request.args.get('contig', '')
    status = request.args.get('status', '')
    reason = request.args.get('reason', '')
    search = request.args.get('search', '').lower()
    gene_filter = request.args.get('gene', '').strip().lower()
    gc_min, gc_max = request.args.get('gc_min', type=float), request.args.get('gc_max', type=float)
    map_min, map_max = request.args.get('mappability_min', type=float), request.args.get('mappability_max', type=float)
    segdup_min = request.args.get('segmental_duplication_min', type=float)
    segdup_max = request.args.get('segmental_duplication_max', type=float)
    metric = request.args.get('metric', 'robust_variability')
    metric_filter = request.args.get('metric_filter', metric)
    normalization = request.args.get('normalization', 'library_size')
    metric_min, metric_max = request.args.get('metric_min', type=float), request.args.get('metric_max', type=float)
    points = []
    key_map = {'robust_variability': 'median_absolute_deviation', 'sd': 'normalized_count_sd',
               'cv': 'coefficient_variation', 'median_count': 'median_normalized_count'}
    for row in rows:
        if contig and row['contig'] != contig:
            continue
        if status == 'retained' and not row['retained'] or status == 'excluded' and row['retained']:
            continue
        if reason and reason != (row.get('primary_filter_reason') or 'retained'):
            continue
        if search and search not in f"{row['contig']}:{row['start']}-{row['end']} {row.get('gene') or ''}".lower():
            continue
        if gene_filter and gene_filter not in str(row.get('gene') or '').lower():
            continue
        metric_value_for_filter = row.get(metric_filter) if metric_filter in {
            'median_normalized_coverage', 'robust_cv_percent', 'relative_median_coverage',
            'median_coverage_per_base', 'median_denoised_copy_ratio_log2', 'robust_sigma_log2'} else None
        if metric_min is not None and (metric_value_for_filter is None or metric_value_for_filter < metric_min):
            continue
        if metric_max is not None and (metric_value_for_filter is None or metric_value_for_filter > metric_max):
            continue
        if gc_min is not None and (row.get('gc_content') is None or row['gc_content'] < gc_min):
            continue
        if gc_max is not None and (row.get('gc_content') is None or row['gc_content'] > gc_max):
            continue
        if map_min is not None and (row.get('mappability') is None or row['mappability'] < map_min):
            continue
        if map_max is not None and (row.get('mappability') is None or row['mappability'] > map_max):
            continue
        if segdup_min is not None and (row.get('segmental_duplication_content') is None or row['segmental_duplication_content'] < segdup_min):
            continue
        if segdup_max is not None and (row.get('segmental_duplication_content') is None or row['segmental_duplication_content'] > segdup_max):
            continue
        if request.args.get('median_coverage_min', type=float) is not None and (row.get('median_normalized_coverage') is None or row['median_normalized_coverage'] < request.args.get('median_coverage_min', type=float)):
            continue
        if request.args.get('median_coverage_max', type=float) is not None and (row.get('median_normalized_coverage') is None or row['median_normalized_coverage'] > request.args.get('median_coverage_max', type=float)):
            continue
        if request.args.get('coverage_variability_min', type=float) is not None and (row.get('robust_cv_percent') is None or row['robust_cv_percent'] < request.args.get('coverage_variability_min', type=float)):
            continue
        if request.args.get('coverage_variability_max', type=float) is not None and (row.get('robust_cv_percent') is None or row['robust_cv_percent'] > request.args.get('coverage_variability_max', type=float)):
            continue
        direct = {'median_normalized_coverage': row.get('median_normalized_coverage'),
                  'robust_cv_percent': row.get('robust_cv_percent'),
                  'relative_median_coverage': row.get('relative_median_coverage'),
                  'median_coverage_per_base': row.get('median_coverage_per_base'),
                  'median_denoised_copy_ratio_log2': row.get('median_denoised_copy_ratio_log2'),
                  'robust_sigma_log2': row.get('robust_sigma_log2'),
                  'zero_fraction': row.get('zero_count_fraction'),
                  'low_count_fraction': row.get('low_count_fraction'),
                  'entropy': row.get('posterior_entropy_median_normalized'),
                  'cnv_frequency': row.get('nonreference_map_frequency'),
                  'model_variance': row.get('model_unexplained_variance_log_psi_native')}
        value = direct.get(metric)
        if metric in key_map:
            value = (row.get('normalized', {}).get(normalization) or {}).get(key_map[metric])
        points.append({'interval_index': row['interval_index'], 'interval_id': row.get('interval_id'),
                       'start': row['start'], 'end': row['end'], 'contig': row['contig'],
                       'retained': row['retained'], 'value': value, 'gene': row.get('gene'),
                       'transcript_id': row.get('transcript_id'), 'exon_number': row.get('exon_number'),
                       'gc_content': row.get('gc_content'), 'mappability': row.get('mappability')})
    points.sort(key=lambda point: (_contig_sort_key(point['contig']), point['start'], point['end']))
    return jsonify({'points': points, 'count': len(points), 'metric': metric})


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/coverage-deviation-heatmap')
def model_diagnostic_coverage_deviation_heatmap(project_id, model_run_id):
    model_run, _params, diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    try:
        return jsonify(coverage_deviation_heatmap(Path(model_run.output_path), diagnostics))
    except (OSError, ValueError, DiagnosticsUnavailable) as exc:
        return jsonify({'error': str(exc)}), 404


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/intervals.csv')
def model_diagnostic_intervals_csv(project_id, model_run_id):
    model_run, _params, _diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    args = request.args
    result = query_intervals(Path(model_run.output_path), page=1, page_size=50_000, all_rows=True,
                            contig=args.get('contig', ''), status=args.get('status', ''),
                            reason=args.get('reason', ''), search=args.get('search', ''),
                            gc_min=args.get('gc_min', type=float), gc_max=args.get('gc_max', type=float),
                            mappability_min=args.get('mappability_min', type=float),
                            mappability_max=args.get('mappability_max', type=float),
                            segmental_duplication_min=args.get('segmental_duplication_min', type=float),
                            segmental_duplication_max=args.get('segmental_duplication_max', type=float),
                            gene=args.get('gene', ''),
                            median_coverage_min=args.get('median_coverage_min', type=float),
                            median_coverage_max=args.get('median_coverage_max', type=float),
                            coverage_variability_min=args.get('coverage_variability_min', type=float),
                            coverage_variability_max=args.get('coverage_variability_max', type=float),
                            metric_min=args.get('metric_min', type=float), metric_max=args.get('metric_max', type=float),
                            metric=args.get('metric', 'robust_variability'),
                            normalization=args.get('normalization', 'library_size'),
                            sort=args.get('sort', 'metric'), direction=args.get('direction', 'desc'))
    rows = result['rows']
    import csv
    import io
    buffer = io.StringIO()
    fields = ['interval_id', 'contig', 'start', 'end', 'length', 'gene', 'transcript_id', 'exon_number', 'retained', 'primary_filter_reason', 'filter_reason_detail',
              'mean_count_raw_descriptive', 'median_count_raw_descriptive', 'mean_coverage_per_base', 'median_coverage_per_base',
              'median_normalized_coverage', 'relative_median_coverage', 'mad_normalized_coverage',
              'robust_cv_percent', 'zero_coverage_percentage', 'coverage_sample_count',
              'median_denoised_copy_ratio_log2', 'mad_denoised_copy_ratio_log2', 'robust_sigma_log2',
              'denoised_sample_count', 'zero_count_fraction', 'low_count_fraction', 'gc_content', 'mappability',
              'segmental_duplication_content',
              'posterior_entropy_median_normalized', 'model_unexplained_variance_log_psi_native',
              'model_unexplained_variance_exp_log_psi_derived', 'selected_metric_value']
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return current_app.response_class(buffer.getvalue(), mimetype='text/csv',
                                      headers={'Content-Disposition': 'attachment; filename=cohort_intervals.csv'})


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/intervals/<int:interval_index>')
def model_diagnostic_interval_detail(project_id, model_run_id, interval_index):
    model_run, params, diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    try:
        samples = request.args.getlist('sample')
        return jsonify(interval_detail(Path(model_run.output_path), params, diagnostics,
                                       interval_index, selected_samples=samples))
    except (IndexError, ValueError, DiagnosticsUnavailable) as exc:
        return jsonify({'error': str(exc)}), 404


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/samples/<path:sample_id>')
def model_diagnostic_sample_detail(project_id, model_run_id, sample_id):
    model_run, params, diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    try:
        return jsonify(sample_detail(Path(model_run.output_path), params, diagnostics, sample_id))
    except (KeyError, ValueError, DiagnosticsUnavailable) as exc:
        return jsonify({'error': str(exc)}), 404


@bp.get('/projects/<project_id>/models/<model_run_id>/diagnostics/entropy-heatmap')
def model_diagnostic_entropy_heatmap(project_id, model_run_id):
    model_run, _params, diagnostics = _model_diagnostics_or_404(project_id, model_run_id)
    return jsonify(entropy_heatmap(Path(model_run.output_path), diagnostics,
                                   samples=request.args.getlist('sample'),
                                   contig=request.args.get('contig', ''),
                                   top_n=request.args.get('top_n', 50, type=int),
                                   metric=request.args.get('metric', 'entropy'),
                                   aggregation=request.args.get('aggregation', 'median')))


@bp.get('/projects/<project_id>/models/<model_run_id>/artifacts/<path:artifact_path>')
def download_model_artifact(project_id, model_run_id, artifact_path):
    project = get_project_or_404(project_id)
    model_run = CohortModelRun.query.filter_by(id=model_run_id, project_id=project.id).first_or_404()
    root = Path(model_run.output_path).resolve()
    target = (root / artifact_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=True, download_name=target.name)


def _require_accepted_project(project_id):
    project = get_project_or_404(project_id)
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    if not accepted_path.is_file():
        abort(409, 'El proyecto aún no tiene un análisis de intervalos aceptado.')
    if project.status != 'intervals_accepted':
        project.status = 'intervals_accepted'
        db.session.commit()
    return project


@bp.post('/projects/<project_id>/bams/batches/<batch_id>/retry')
def retry_bam_batch_qc(project_id, batch_id):
    project = _require_accepted_project(project_id)
    batch = SampleBatch.query.filter_by(id=batch_id, project_id=project.id).first_or_404()
    is_cohort_qc = batch.storage_mode == 'cohort'
    previous = db.session.get(WorkflowJob, batch.job_id) if batch.job_id else None
    if previous is None:
        flash('No se encuentra el job QC anterior para reintentar este lote.', 'error')
        return redirect(url_for('main.project_sample_qc' if is_cohort_qc else 'main.project_bams', project_id=project_id))
    if previous.status == 'queued' or (
            previous.status in {'running', 'cancel_requested'} and previous.finished_at is None):
        flash('El QC de este lote todavía está activo o cancelándose.', 'error')
        return redirect(url_for('main.project_sample_qc' if is_cohort_qc else 'main.project_bams', project_id=project_id))
    if previous.status == 'running' and previous.finished_at is not None:
        previous.status = 'failed'
        previous.error_message = previous.error_message or 'El proceso Nextflow terminó, pero el worker no guardó el estado final.'
    if previous.status == 'cancel_requested' and previous.finished_at is not None:
        previous.status = 'cancelled'
        previous.error_message = previous.error_message or 'El workflow terminó mientras su cancelación estaba solicitada.'
    try:
        params = yaml.safe_load(Path(previous.parameters_path).read_text(encoding='utf-8')) or {}
        execution = yaml.safe_load(Path(previous.execution_path).read_text(encoding='utf-8')) or {}
        # Older QC jobs did not include the FAI in their parameters. Resolve it
        # from the project's configured bundle so they can be retried in place.
        reference = get_reference(project.reference_id)
        if not reference or not reference.get('fai'):
            raise ValueError('No se encuentra el FAI configurado para la referencia del proyecto.')
        params['reference_fai'] = reference['fai']
        params['pipeline_version'] = '0.2.1'
        for item in params['samples']:
            if not Path(item['bam']).is_file() or not Path(item['bai']).is_file():
                raise ValueError(f"Falta el BAM o BAI de {item['sample_id']} en el storage del proyecto.")
        job_id = new_id()
        if is_cohort_qc:
            job_dir = Path(project.storage_path) / 'samples' / 'cohort_qc' / job_id
            output_dir = Path(project.storage_path) / 'samples' / 'qc' / job_id
        else:
            batch_dir = Path(project.storage_path) / 'samples' / 'batches' / batch.id
            job_dir = batch_dir / 'jobs' / job_id
            output_dir = batch_dir / 'qc' / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        execution.update({'workflow_path': str(current_app.config['SAMPLE_QC_WORKFLOW_PATH']),
                          'output_path': str(output_dir)})
        _enable_retry_resume(execution, previous)
        params_path = job_dir / 'parameters.yaml'
        execution_path = job_dir / 'execution.yaml'
        log_path = job_dir / 'nextflow.log'
        params_path.write_text(yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding='utf-8')
        execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
        log_path.touch()
        db.session.add(WorkflowJob(id=job_id, project_id=project.id, workflow='sample_qc', status='queued',
            parameters_path=str(params_path), execution_path=str(execution_path), output_path=str(output_dir),
            log_path=str(log_path)))
        batch.job_id = job_id
        batch.status = 'queued'
        batch.error_message = None
        retry_samples = (BamSample.query.filter(BamSample.id.in_(params.get('sample_ids') or [])).all()
                         if is_cohort_qc else batch.samples)
        for sample in retry_samples:
            sample.status = 'queued'
            sample.error_message = None
            sample.counts_path = None
            sample.qc_metrics_path = None
        db.session.commit()
        if is_cohort_qc:
            state = _read_cohort_state(project)
            state.update({'qc_batch_id': batch.id, 'qc_job_id': job_id,
                          'last_qc_sample_ids': params.get('sample_ids') or []})
            _write_cohort_state(project, state)
        flash('Lote añadido de nuevo a la cola QC. Los BAMs guardados se reutilizarán.', 'success')
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        db.session.rollback()
        flash(f'No se pudo reintentar el lote: {exc}', 'error')
    return redirect(url_for('main.project_sample_qc' if is_cohort_qc else 'main.project_bams', project_id=project_id))


@bp.post('/projects/<project_id>/bams/upload-sessions')
def create_bam_upload_session(project_id):
    project = _require_accepted_project(project_id)
    if _cohort_is_locked(project)[0]:
        abort(409, 'La cohorte BAM está cerrada.')
    token = new_id()
    upload_dir = Path(project.storage_path) / 'samples' / 'batches' / f'.uploading-{token}'
    upload_dir.mkdir(parents=True, exist_ok=False)
    return jsonify({'token': token, 'upload_url': url_for('main.upload_bam_pair', project_id=project_id, token=token),
                    'finalize_url': url_for('main.create_bam_batch', project_id=project_id)})


@bp.post('/projects/<project_id>/bams/upload-sessions/<token>/files')
def upload_bam_pair(project_id, token):
    project = _require_accepted_project(project_id)
    if _cohort_is_locked(project)[0]:
        abort(409, 'La cohorte BAM está cerrada.')
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        abort(404)
    upload_dir = Path(project.storage_path) / 'samples' / 'batches' / f'.uploading-{token}'
    if not upload_dir.is_dir():
        abort(404)
    bam_file, bai_file = request.files.get('bam_file'), request.files.get('bai_file')
    if not bam_file or not bai_file or not bam_file.filename or not bai_file.filename:
        return jsonify({'error': 'Debe enviarse un BAM con su BAI.'}), 400
    bam_name, bai_name = Path(bam_file.filename).name, Path(bai_file.filename).name
    sample_id = _sample_id_from_bam_name(bam_name)
    bai_stem = bai_name[:-8] if bai_name.lower().endswith('.bam.bai') else (
        bai_name[:-4] if bai_name.lower().endswith('.bai') else '')
    bai_sample = bai_stem.split('.', 1)[0]
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sample_id)
            or bai_sample != sample_id):
        return jsonify({'error': (f'No hay match para la muestra {sample_id or "(ID no válido)"}: '
                                 f'el BAM {bam_name} identifica la muestra {sample_id or "(ID no válido)"} '
                                 f'y el BAI {bai_name} identifica la muestra {bai_sample or "(ID no válido)"}. '
                                 'Sube el índice correspondiente (sample.bai o sample.bam.bai).')}), 400
    sample_dir = upload_dir / sample_id
    if sample_dir.exists():
        return jsonify({'error': f'Ya se transfirió un archivo para {sample_id} en esta sesión.'}), 409
    sample_dir.mkdir(parents=True, exist_ok=False)
    bam_target, bai_target = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
    bam_temp, bai_temp = sample_dir / '.bam.uploading', sample_dir / '.bai.uploading'
    try:
        bam_file.save(bam_temp)
        bai_file.save(bai_temp)
        bam_temp.replace(bam_target)
        bai_temp.replace(bai_target)
    except OSError as exc:
        shutil.rmtree(sample_dir, ignore_errors=True)
        return jsonify({'error': f'No se pudo guardar {sample_id}: {exc}'}), 500
    return jsonify({'sample_id': sample_id, 'bam_bytes': bam_target.stat().st_size,
                    'bai_bytes': bai_target.stat().st_size, 'status': 'transferred'})


@bp.delete('/projects/<project_id>/bams/upload-sessions/<token>')
def discard_bam_upload_session(project_id, token):
    project = get_project_or_404(project_id)
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        abort(404)
    upload_dir = Path(project.storage_path) / 'samples' / 'batches' / f'.uploading-{token}'
    if upload_dir.is_dir():
        shutil.rmtree(upload_dir)
    return '', 204


def _index_for_bam(bam_path, uploaded_indexes=()):
    bam = Path(bam_path)
    candidates = [Path(str(bam) + '.bai'), bam.with_suffix('.bai')]
    candidates.extend(Path(item) for item in uploaded_indexes)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _sample_id_from_bam_name(filename):
    name = Path(filename).name
    if not name.lower().endswith('.bam'):
        return ''
    return name[:-4].split('.', 1)[0]


@bp.post('/projects/<project_id>/bams/batches')
def create_bam_batch(project_id):
    project = get_project_or_404(project_id)
    locked, _cohort = _cohort_is_locked(project)
    if locked:
        flash('El QC ya se inició: la lista de BAMs del proyecto está cerrada.', 'error')
        return redirect(url_for('main.project_sample_qc', project_id=project_id))
    accepted_path = Path(project.storage_path) / 'bed_analysis' / 'accepted_analysis.yaml'
    if not accepted_path.is_file():
        flash('Acepta primero el análisis de intervalos.', 'error')
        return redirect(url_for('main.project_detail', project_id=project_id))
    if project.status != 'intervals_accepted':
        project.status = 'intervals_accepted'
        db.session.commit()
    low_count = 10
    batch_number = SampleBatch.query.filter_by(project_id=project.id).count() + 1
    batch_name = f'Lote {batch_number:03d}'
    while SampleBatch.query.filter_by(project_id=project.id, name=batch_name).first():
        batch_number += 1
        batch_name = f'Lote {batch_number:03d}'

    accepted = yaml.safe_load(accepted_path.read_text(encoding='utf-8')) or {}
    reference = get_reference(project.reference_id)
    interval_list = Path(project.storage_path) / 'bed_analysis' / 'current' / 'preprocessed.interval_list'
    if accepted.get('job_id'):
        accepted_job = db.session.get(WorkflowJob, accepted['job_id'])
        if accepted_job:
            interval_list = Path(accepted_job.output_path) / 'preprocessed.interval_list'
    if not reference or not interval_list.is_file():
        flash('No se encuentra el bundle o el interval_list aceptado del proyecto.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))

    storage_mode = request.form.get('storage_mode', 'copy')
    if storage_mode not in {'copy', 'symlink'}:
        flash('Modo de almacenamiento no válido.', 'error')
        return redirect(url_for('main.project_bams', project_id=project_id))
    inputs = []
    uploaded_bams = [item for item in request.files.getlist('bam_files') if item and item.filename]
    uploaded_bais = [item for item in request.files.getlist('bai_files') if item and item.filename]
    upload_token = (request.form.get('upload_token') or '').strip()
    staging = Path(project.storage_path) / 'samples' / 'batches' / f'.staging-{new_id()}'
    batch_id = new_id()
    batch_dir = Path(project.storage_path) / 'samples' / 'batches' / batch_id
    try:
        if storage_mode == 'copy':
            if upload_token:
                if not re.fullmatch(r'[0-9a-f]{32}', upload_token):
                    raise ValueError('Sesión de carga no válida.')
                staging = Path(project.storage_path) / 'samples' / 'batches' / f'.uploading-{upload_token}'
                if not staging.is_dir():
                    raise ValueError('La sesión de carga ya no existe; vuelve a seleccionar los BAMs.')
                for sample_dir in sorted(path for path in staging.iterdir() if path.is_dir()):
                    sample_id = sample_dir.name
                    bam_path, bai_path = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
                    if not bam_path.is_file() or not bai_path.is_file():
                        raise ValueError(f'La transferencia de {sample_id} está incompleta.')
                    inputs.append({'sample_id': sample_id, 'source_bam': str(bam_path),
                                   'source_bai': str(bai_path), 'bam_path': bam_path, 'bai_path': bai_path})
                if not inputs:
                    raise ValueError('No hay pares BAM/BAI transferidos en esta sesión.')
            else:
                if not uploaded_bams:
                    raise ValueError('Selecciona al menos un fichero BAM.')
                staging.mkdir(parents=True, exist_ok=False)
                uploaded_index_paths = []
                for uploaded in uploaded_bais:
                    name = Path(uploaded.filename).name
                    if not name.lower().endswith('.bai'):
                        raise ValueError(f'El índice {name} no termina en .bai.')
                    index_path = staging / name
                    uploaded.save(index_path)
                    uploaded_index_paths.append(index_path)
                seen_ids = set()
                for uploaded in uploaded_bams:
                    filename = Path(uploaded.filename).name
                    sample_id = _sample_id_from_bam_name(filename)
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sample_id):
                        raise ValueError(f'Nombre BAM inválido para usarlo como sample ID: {filename}.')
                    if sample_id in seen_ids:
                        raise ValueError(f'El lote contiene sample ID duplicado: {sample_id}.')
                    seen_ids.add(sample_id)
                    sample_dir = staging / sample_id
                    sample_dir.mkdir()
                    bam_path = sample_dir / f'{sample_id}.bam'
                    uploaded.save(bam_path)
                    possible = []
                    for index_path in uploaded_index_paths:
                        index_name = index_path.name
                        index_stem = index_name[:-8] if index_name.lower().endswith('.bam.bai') else (
                            index_name[:-4] if index_name.lower().endswith('.bai') else '')
                        if index_stem.split('.', 1)[0] == sample_id:
                            possible.append(index_path)
                    bai_source = _index_for_bam(bam_path, possible)
                    if not bai_source:
                        raise ValueError(f'Falta el BAI para {sample_id}. Sube {sample_id}.bam.bai o {sample_id}.bai.')
                    bai_path = sample_dir / f'{sample_id}.bam.bai'
                    if bai_source != bai_path:
                        shutil.copy2(bai_source, bai_path)
                    inputs.append({'sample_id': sample_id, 'source_bam': str(bam_path),
                                   'source_bai': str(bai_path), 'bam_path': bam_path,
                                   'bai_path': bai_path})
        else:
            if not current_app.config['BAM_ALLOWED_ROOTS']:
                raise ValueError('El operador no ha configurado GATK_GCNV_BAM_ALLOWED_ROOTS.')
            lines = [line.strip() for line in request.form.get('server_bams', '').splitlines() if line.strip()]
            if not lines:
                raise ValueError('Indica al menos una ruta BAM (una por línea; ruta BAI opcional en una segunda columna).')
            staging.mkdir(parents=True, exist_ok=False)
            seen_ids = set()
            for line in lines:
                fields = line.split('\t')
                if len(fields) not in {1, 2, 3}:
                    raise ValueError('Cada línea debe tener ruta BAM y, opcionalmente, ruta BAI separadas por tabuladores.')
                # Keep accepting the old three-column form, but derive the ID
                # from the BAM filename rather than trusting the typed value.
                source_bam = Path(fields[1].strip() if len(fields) == 3 else fields[0].strip()).expanduser()
                sample_id = _sample_id_from_bam_name(source_bam.name)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,199}', sample_id):
                    raise ValueError(f'No se pudo extraer un sample ID válido del BAM: {source_bam.name}.')
                if sample_id in seen_ids:
                    raise ValueError(f'Sample ID duplicado: {sample_id}.')
                seen_ids.add(sample_id)
                if not is_within_allowed_roots(source_bam, current_app.config['BAM_ALLOWED_ROOTS']):
                    raise ValueError(f'La ruta BAM de {sample_id} no está dentro de las raíces permitidas.')
                bai_text = fields[2].strip() if len(fields) == 3 else fields[1].strip() if len(fields) == 2 else ''
                source_bai = Path(bai_text).expanduser() if bai_text else _index_for_bam(source_bam)
                if not source_bai or not is_within_allowed_roots(source_bai, current_app.config['BAM_ALLOWED_ROOTS']):
                    raise ValueError(f'No se encuentra un BAI permitido para {sample_id}.')
                sample_dir = staging / sample_id
                sample_dir.mkdir()
                bam_path, bai_path = sample_dir / f'{sample_id}.bam', sample_dir / f'{sample_id}.bam.bai'
                bam_path.symlink_to(source_bam.resolve(strict=True))
                bai_path.symlink_to(source_bai.resolve(strict=True))
                inputs.append({'sample_id': sample_id, 'source_bam': str(source_bam.resolve()),
                               'source_bai': str(source_bai.resolve()), 'bam_path': bam_path, 'bai_path': bai_path})

        existing_ids = {sample_id for (sample_id,) in db.session.query(BamSample.sample_id)
                        .filter_by(project_id=project.id).all()}
        duplicates = existing_ids.intersection(item['sample_id'] for item in inputs)
        if duplicates:
            raise ValueError('Ya existen estos sample ID en el proyecto: ' + ', '.join(sorted(duplicates)))
        reference_dict = load_reference_dictionary(reference['dict'])
        for item in inputs:
            report = inspect_bam(item['bam_path'], item['bai_path'], item['sample_id'], reference_dict,
                                 interval_list, current_app.config['SAMTOOLS_BIN'])
            item['validation'] = report
            item['size'] = item['bam_path'].stat().st_size

        (staging / 'inputs.json').write_text(json.dumps([
            {'sample_id': item['sample_id'], 'source_bam': item['source_bam'],
             'source_bai': item['source_bai'], 'validation': item['validation']}
            for item in inputs], ensure_ascii=False, indent=2), encoding='utf-8')
        batch_dir.parent.mkdir(parents=True, exist_ok=True)
        staging.replace(batch_dir)
        # Replace temporary paths after the atomic directory move.
        for item in inputs:
            item['bam_path'] = batch_dir / item['sample_id'] / f"{item['sample_id']}.bam"
            item['bai_path'] = batch_dir / item['sample_id'] / f"{item['sample_id']}.bam.bai"
            if storage_mode == 'copy':
                item['source_bam'] = str(item['bam_path'])
                item['source_bai'] = str(item['bai_path'])
        (batch_dir / 'inputs.json').write_text(json.dumps([
            {'sample_id': item['sample_id'], 'source_bam': item['source_bam'],
             'source_bai': item['source_bai'], 'validation': item['validation']}
            for item in inputs], ensure_ascii=False, indent=2), encoding='utf-8')
        batch = SampleBatch(id=batch_id, project_id=project.id, name=batch_name,
                            storage_mode=storage_mode, kit_name='', protocol='',
                            duplicates_marked=False, low_count_threshold=low_count,
                            bait_bed_path=None, job_id=None, status='staged')
        db.session.add(batch)
        for item in inputs:
            report_path = batch_dir / item['sample_id'] / 'validation.json'
            report_path.write_text(json.dumps(item['validation'], ensure_ascii=False, indent=2), encoding='utf-8')
            db.session.add(BamSample(project_id=project.id, batch_id=batch_id,
                sample_id=item['sample_id'], read_group_sm=item['validation']['read_group_sm'],
                bam_path=str(item['bam_path']), bai_path=str(item['bai_path']),
                source_bam_path=item['source_bam'], source_bai_path=item['source_bai'],
                storage_mode=storage_mode, bam_size_bytes=item['size'],
                validation_report=json.dumps(item['validation'], ensure_ascii=False), status='validated'))
        db.session.commit()
        flash(f'{len(inputs)} muestra(s) validadas y añadidas. Puedes añadir más BAMs o lanzar el QC conjunto.', 'success')
    except (OSError, ValueError, subprocess.SubprocessError, SQLAlchemyError) as exc:
        db.session.rollback()
        shutil.rmtree(staging, ignore_errors=True)
        if batch_dir.exists():
            shutil.rmtree(batch_dir, ignore_errors=True)
        flash(f'No se pudo crear el lote: {exc}', 'error')
    return redirect(url_for('main.project_bams', project_id=project_id))


@bp.get('/projects/<project_id>/bams/qc.csv')
def download_project_qc_csv(project_id):
    project = get_project_or_404(project_id)
    summary_path = Path(project.storage_path) / 'samples' / 'project_qc_summary.json'
    if not summary_path.is_file():
        abort(404)
    import csv
    import io
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['sample_id', 'batch', 'target_bases_below_30x_pct', 'target_bases_below_100x_pct',
                     'mean_target_coverage', 'fold_80_base_penalty',
                     'sample_coverage_correlation', 'cohort_outlier_score', 'mapped_reads_pct',
                     'duplicate_reads_pct', 'properly_paired_reads_pct', 'qc_failed_reads_pct'])
    for sample in summary.get('samples', []):
        target = sample.get('target_coverage') or {}
        hs = sample.get('collect_hs_metrics') or {}
        flag = sample.get('flagstat') or {}
        normalized = sample.get('normalized') or {}
        writer.writerow([sample.get('sample_id'), sample.get('batch'),
            target.get('bases_below_30x_pct'), target.get('bases_below_100x_pct'),
            hs.get('mean_target_coverage'), hs.get('fold_80_base_penalty'),
            normalized.get('sample_coverage_correlation'), normalized.get('cohort_outlier_score'),
            flag.get('mapped_reads_pct'), flag.get('duplicate_reads_pct'),
            flag.get('properly_paired_reads_pct'), flag.get('qc_failed_reads_pct')])
    return current_app.response_class(buffer.getvalue(), mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{project.name}-qc.csv"'})


@bp.get('/jobs/<job_id>/status')
def job_status(job_id):
    reconcile_finished_jobs()
    job = db.session.get(WorkflowJob, job_id)
    if job is None:
        abort(404)
    return jsonify({
        'id': job.id,
        'workflow': job.workflow,
        'status': job.status,
        'exit_code': job.exit_code,
        'error_message': job.error_message,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
    })


@bp.post('/jobs/<job_id>/retry')
def retry_job(job_id):
    reconcile_finished_jobs()
    previous = db.session.get(WorkflowJob, job_id)
    if previous is None:
        abort(404)
    terminal_statuses = {'succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'}
    if previous.status not in terminal_statuses:
        flash('Solo se pueden reejecutar jobs finalizados.', 'error')
        return redirect(url_for('main.jobs_page'))

    project = get_project_or_404(previous.project_id)
    try:
        params = yaml.safe_load(Path(previous.parameters_path).read_text(encoding='utf-8')) or {}
        old_execution = yaml.safe_load(Path(previous.execution_path).read_text(encoding='utf-8')) or {}
        if previous.workflow == 'sample_qc':
            batch = SampleBatch.query.filter_by(project_id=project.id, job_id=previous.id).first()
            if batch is None:
                raise ValueError('Este QC ya fue sustituido por una ejecución posterior del lote.')
            return retry_bam_batch_qc(project.id, batch.id)

        job_id = new_id()
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        if previous.workflow == 'bed_analysis':
            active = WorkflowJob.query.filter(
                WorkflowJob.project_id == project.id,
                WorkflowJob.status.in_(['queued', 'running', 'cancel_requested']),
            ).count()
            if active:
                raise ValueError('Espera a que terminen los otros jobs activos del proyecto.')
            bed_path = Path(params['bed'])
            if not bed_path.is_file() or sha256_file(bed_path) != params.get('bed_sha256'):
                raise ValueError('El BED original ya no existe o cambió desde el job fallido.')
            for key in ('reference_fasta', 'reference_fai', 'reference_dict', 'mappability_track',
                        'segmental_duplication_track'):
                value = params.get(key)
                if value and not Path(value).is_file():
                    raise ValueError(f'No se encuentra el recurso de referencia {key}: {value}')
            root = Path(project.storage_path) / 'bed_analysis'
            job_dir = root / 'jobs' / job_id
            output_dir = root / 'runs' / job_id
            job_dir.mkdir(parents=True, exist_ok=False)
            execution = dict(old_execution)
            execution.pop('runtime_params_sha256', None)
            execution.update({'output_path': str(output_dir)})
            _enable_retry_resume(execution, previous)
            params_path, execution_path, log_path = (job_dir / 'parameters.yaml',
                job_dir / 'execution.yaml', job_dir / 'nextflow.log')
            params_path.write_text(yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding='utf-8')
            execution_path.write_text(yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
            output_dir.mkdir(parents=True, exist_ok=False)
            log_path.touch()
            retry = WorkflowJob(id=job_id, project_id=project.id, workflow='bed_analysis', status='queued',
                parameters_path=str(params_path), execution_path=str(execution_path),
                output_path=str(output_dir), log_path=str(log_path))
            project.status = 'analysis_queued'

        elif previous.workflow == 'cohort_model':
            old_run = db.session.get(CohortModelRun, params.get('model_run_id'))
            if old_run is None:
                raise ValueError('No se encuentra el snapshot del modelo asociado a este job.')
            if sha256_file(previous.parameters_path) != old_run.parameters_sha256:
                raise ValueError('El YAML científico del job no coincide con el snapshot guardado.')
            interval_path = Path(params['intervals'])
            if not interval_path.is_file() or sha256_file(interval_path) != params.get('intervals_sha256'):
                raise ValueError('Los intervalos del modelo ya no existen o cambiaron.')
            priors_path = Path(params['contig_ploidy_priors'])
            if not priors_path.is_file() or sha256_file(priors_path) != params.get('contig_ploidy_priors_sha256'):
                raise ValueError('Los priors de ploidía ya no existen o cambiaron.')
            if params.get('reference_dict'):
                dictionary_path = Path(params['reference_dict'])
                if (not dictionary_path.is_file() or
                        sha256_file(dictionary_path) != params.get('reference_dict_sha256')):
                    raise ValueError('El diccionario de referencia ya no existe o cambió.')
                if params.get('reference_fasta'):
                    fasta_path = Path(params['reference_fasta'])
                    if (not fasta_path.is_file() or
                            sha256_file(fasta_path) != params.get('reference_fasta_sha256')):
                        raise ValueError('El reference FASTA ya no existe o cambió.')
            else:
                reference = get_reference(old_run.reference_id)
                if not reference:
                    raise ValueError('El bundle de referencia del modelo ya no está disponible.')
                params['reference_dict'] = reference['dict']
                params['reference_dict_sha256'] = sha256_file(reference['dict'])
                params['reference_fasta'] = reference['fasta']
                params['reference_fasta_sha256'] = sha256_file(reference['fasta'])
            if not params.get('reference_fasta'):
                reference = get_reference(old_run.reference_id)
                if not reference:
                    raise ValueError('El bundle de referencia del modelo ya no está disponible.')
                params['reference_fasta'] = reference['fasta']
                params['reference_fasta_sha256'] = sha256_file(reference['fasta'])
            for sample in params.get('samples', []):
                counts_path = Path(sample['counts'])
                if not counts_path.is_file() or sha256_file(counts_path) != sample.get('counts_sha256'):
                    raise ValueError(f"Los read counts de {sample.get('sample_id', 'una muestra')} ya no existen o cambiaron.")
            execution = dict(old_execution)
            if _execution_snapshot_sha256(execution) != old_run.execution_sha256:
                raise ValueError('El YAML de recursos/backend no coincide con el snapshot del modelo.')
            # A retry is another attempt for the same scientific model. Keep its
            # snapshot, output directory and job identity so the model history
            # does not gain a duplicate cohort run.
            job_id = previous.id
            _enable_retry_resume(execution, previous)
            Path(previous.execution_path).write_text(
                yaml.safe_dump(execution, sort_keys=False, allow_unicode=True), encoding='utf-8')
            previous.status = 'queued'
            previous.error_message = None
            previous.exit_code = None
            previous.started_at = None
            previous.finished_at = None
            old_run.status = 'queued'
            old_run.error_message = None
            old_run.finished_at = None
            retry = previous
        else:
            raise ValueError(f'No hay reintento web implementado para el workflow {previous.workflow}.')

        db.session.add(retry)
        db.session.commit()
        if retry.workflow == 'cohort_model':
            flash(f'Se ha vuelto a poner en cola el modelo {job_id[:10]} con -resume. Se conserva el mismo modelo y su work directory.', 'success')
        else:
            flash(f'Nueva ejecución encolada a partir de {previous.id[:10]}. Se conserva la ejecución anterior y sus artefactos.', 'success')
        if retry.workflow == 'cohort_model':
            return redirect(url_for('main.project_model_detail', project_id=project.id, model_run_id=job_id))
        return redirect(url_for('main.jobs_page'))
    except (OSError, KeyError, TypeError, ValueError, SQLAlchemyError, yaml.YAMLError,
            json.JSONDecodeError) as exc:
        db.session.rollback()
        if 'job_dir' in locals():
            shutil.rmtree(job_dir, ignore_errors=True)
        if 'output_dir' in locals() and previous.workflow == 'bed_analysis':
            shutil.rmtree(output_dir, ignore_errors=True)
        if 'run_root' in locals():
            shutil.rmtree(run_root, ignore_errors=True)
        flash(f'No se pudo reejecutar el job: {exc}', 'error')
        return redirect(url_for('main.jobs_page'))


@bp.post('/jobs/<job_id>/cancel')
def cancel_job(job_id):
    reconcile_finished_jobs()
    job = db.session.get(WorkflowJob, job_id)
    if job is None:
        abort(404)
    if job.status == 'queued':
        job.status = 'cancelled'
        job.finished_at = datetime.now(timezone.utc)
        if job.workflow == 'sample_qc':
            params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8'))
            batch = db.session.get(SampleBatch, params['batch_id'])
            if batch:
                batch.status = 'cancelled'
                batch.error_message = 'Cancelado antes de empezar.'
            sample_query = BamSample.query
            if params.get('sample_ids'):
                sample_query = sample_query.filter(BamSample.id.in_(params['sample_ids']))
            else:
                sample_query = sample_query.filter_by(batch_id=params['batch_id'])
            for sample in sample_query.all():
                sample.status = 'qc_cancelled'
                sample.error_message = 'Cancelado antes de empezar.'
        elif job.workflow == 'cohort_model':
            try:
                params = yaml.safe_load(Path(job.parameters_path).read_text(encoding='utf-8')) or {}
                model_run = db.session.get(CohortModelRun, params.get('model_run_id'))
                if model_run:
                    model_run.status = 'cancelled'
                    model_run.error_message = 'Cancelado antes de empezar.'
                    model_run.finished_at = job.finished_at
                output = Path(job.output_path)
                output.mkdir(parents=True, exist_ok=True)
                (output / 'run_metadata.json').write_text(json.dumps({
                    'schema_version': '1.0', 'model_run_id': params.get('model_run_id'),
                    'job_id': job.id, 'workflow': job.workflow, 'status': 'cancelled',
                    'error': 'Cancelado antes de empezar.', 'finished_at': job.finished_at.isoformat(),
                    'parameters': params,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, TypeError, yaml.YAMLError):
                pass
        elif job.workflow == 'model_validation':
            validation_run = ModelValidationRun.query.filter_by(job_id=job.id).first()
            if validation_run:
                validation_run.status = 'cancelled'
                validation_run.error_message = 'Cancelado antes de empezar.'
                validation_run.finished_at = job.finished_at
    elif job.status == 'running':
        job.status = 'cancel_requested'
    elif job.status == 'cancel_requested':
        flash('La cancelación ya está en curso.', 'info')
    else:
        flash('El job ya ha finalizado y no se puede cancelar.', 'info')
        return redirect(request.referrer or url_for('main.jobs_page'))
    db.session.commit()
    if job.status == 'cancelled':
        active_count = WorkflowJob.query.filter(
            WorkflowJob.project_id == job.project_id,
            WorkflowJob.status.in_(['queued', 'running', 'cancel_requested']),
        ).count()
        if active_count == 0 and job.workflow == 'bed_analysis':
            project = db.session.get(BedProject, job.project_id)
            if project:
                completed_count = WorkflowJob.query.filter(
                    WorkflowJob.project_id == job.project_id,
                    WorkflowJob.status.in_(['succeeded', 'succeeded_with_parse_warnings']),
                ).count()
                project.status = 'analysis_complete' if completed_count else 'ready'
                db.session.commit()
        flash('Job cancelado antes de empezar.', 'success')
    elif job.status == 'cancel_requested':
        flash('Cancelación solicitada. El worker está deteniendo Nextflow.', 'success')
    return redirect(request.referrer or url_for('main.jobs_page'))


@bp.get('/jobs/<job_id>/log')
def job_log(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if job is None:
        abort(404)
    project_root = Path(job.project.storage_path).resolve()
    log_path = Path(job.log_path).resolve()
    if not log_path.is_relative_to(project_root):
        abort(404)

    try:
        offset = max(0, int(request.args.get('offset', '0')))
    except ValueError:
        abort(400)
    if not log_path.is_file():
        return jsonify({'content': '', 'offset': 0, 'total_bytes': 0, 'status': job.status})

    total_bytes = log_path.stat().st_size
    offset = min(offset, total_bytes)
    with log_path.open('rb') as handle:
        handle.seek(offset)
        content = handle.read(64 * 1024)
    next_offset = offset + len(content)
    response = jsonify({
        'content': content.decode('utf-8', errors='replace'),
        'offset': next_offset,
        'total_bytes': total_bytes,
        'status': job.status,
    })
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.get('/jobs/<job_id>/log/download')
def download_job_log(job_id):
    job = db.session.get(WorkflowJob, job_id)
    if job is None:
        abort(404)
    project_root = Path(job.project.storage_path).resolve()
    log_path = Path(job.log_path).resolve()
    if not log_path.is_relative_to(project_root) or not log_path.is_file():
        abort(404)
    response = send_file(
        log_path,
        mimetype='text/plain; charset=utf-8',
        as_attachment=True,
        download_name=f"{job.workflow.replace('_', '-')}-{job.id}.log",
        conditional=True,
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@bp.errorhandler(413)
def too_large(_error):
    message = 'La transferencia supera el límite de tamaño configurado por Flask o el proxy. Aumenta ese límite para admitir este BAM/BAI.'
    if request.accept_mimetypes.best == 'application/json':
        return jsonify({'error': message}), 413
    flash(message, 'error')
    project_id = (request.view_args or {}).get('project_id')
    if project_id:
        return redirect(url_for('main.project_bams', project_id=project_id))
    return redirect(request.referrer or url_for('main.index'))
