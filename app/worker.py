import click
import fcntl
import os
import signal
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from app import create_app
from app.models import WorkflowJob
from app.services.queue import recover_interrupted_jobs, worker_loop, _worker_slots


@click.command('work')
@click.option('--poll-seconds', default=2, type=click.IntRange(min=1))
def work_command(poll_seconds):
    """Procesa la cola local respetando los presupuestos agregados de CPU y RAM."""
    app = create_app()
    lock_path = Path(app.instance_path) / 'worker.lock'
    with lock_path.open('a+') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise click.ClickException('Ya hay un worker activo para esta aplicación.') from exc
        stop_event = threading.Event()
        previous_handlers = {}

        def request_shutdown(signum, _frame):
            click.echo(f'\nSeñal {signum} recibida; se dejarán terminar los workflows activos.')
            stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_shutdown)
        worker_pid = Path(app.instance_path) / 'worker.pid'
        worker_pid.write_text(f'{os.getpid()}\n', encoding='ascii')
        try:
            try:
                slots = _worker_slots(app.config)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            recovered = recover_interrupted_jobs(app)
            click.echo(f"Worker local iniciado: hasta {slots} workflow(s) según CPU/RAM; concurrencia máxima configurada: {app.config['MAX_CONCURRENT_WORKFLOWS']}.")
            if recovered:
                click.echo(f'{recovered} job(s) interrumpido(s) recuperado(s); consulta /jobs y sus logs.')
            worker_loop(app, poll_seconds=poll_seconds, stop_event=stop_event)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            try:
                if worker_pid.read_text(encoding='ascii').strip() == str(os.getpid()):
                    worker_pid.unlink()
            except OSError:
                pass


@click.command('cleanup-nextflow-work')
@click.option('--older-than-days', type=click.IntRange(min=0), default=None,
              help='Retención en días (por defecto GATK_GCNV_JOB_WORK_RETENTION_DAYS).')
@click.option('--apply', 'apply_changes', is_flag=True,
              help='Borra los directorios work listados. Sin esta opción solo muestra qué borraría.')
def cleanup_nextflow_work_command(older_than_days, apply_changes):
    """Lista o borra directorios temporales Nextflow de jobs terminados; conserva outputs y logs."""
    app = create_app()
    days = app.config['JOB_WORK_RETENTION_DAYS'] if older_than_days is None else older_than_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    terminal = {'succeeded', 'succeeded_with_parse_warnings', 'failed', 'cancelled'}
    removed = 0
    with app.app_context():
        protected_work_paths = set()
        for candidate in WorkflowJob.query.all():
            eligible_for_cleanup = (candidate.status in terminal and candidate.finished_at is not None
                                    and candidate.finished_at < cutoff)
            if eligible_for_cleanup:
                continue
            try:
                candidate_execution = yaml.safe_load(
                    Path(candidate.execution_path).read_text(encoding='utf-8')) or {}
                candidate_work = Path(candidate_execution.get('work_path', '')).resolve()
                if candidate_work.name == 'work':
                    protected_work_paths.add(candidate_work)
            except (OSError, TypeError, yaml.YAMLError):
                continue
        jobs = WorkflowJob.query.filter(WorkflowJob.status.in_(terminal),
                                        WorkflowJob.finished_at.is_not(None),
                                        WorkflowJob.finished_at < cutoff).order_by(WorkflowJob.finished_at).all()
        for job in jobs:
            try:
                execution = yaml.safe_load(Path(job.execution_path).read_text(encoding='utf-8')) or {}
                work_path = Path(execution.get('work_path', '')).resolve()
                job_root = Path(job.execution_path).parent.resolve()
                if work_path.name != 'work' or work_path.parent != job_root or not work_path.is_dir():
                    continue
                if work_path in protected_work_paths:
                    click.echo(f'Conservando work referenciado por una ejecución reciente/activa: {work_path}')
                    continue
                click.echo(f'{"BORRARÍA" if not apply_changes else "Borrando"}: job {job.id} · {work_path}')
                if apply_changes:
                    import shutil
                    shutil.rmtree(work_path)
                    removed += 1
                    try:
                        with Path(job.log_path).open('a', encoding='utf-8') as handle:
                            handle.write(f'\n[{datetime.now(timezone.utc).isoformat()}] Directorio temporal Nextflow eliminado tras {days} días de retención. Outputs publicados y logs conservados.\n')
                    except OSError:
                        pass
            except (OSError, TypeError, yaml.YAMLError) as exc:
                click.echo(f'No se pudo evaluar el job {job.id}: {exc}', err=True)
    if not apply_changes:
        click.echo('Vista previa únicamente. Añade --apply para eliminar estos directorios.')
    else:
        click.echo(f'Se eliminaron {removed} directorio(s) work.')


def init_app(app):
    app.cli.add_command(work_command)
    app.cli.add_command(cleanup_nextflow_work_command)
