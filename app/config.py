import os
from pathlib import Path

_MAX_UPLOAD_MB = max(0, int(os.environ.get('GATK_GCNV_MAX_UPLOAD_MB', '0')))
_MODULE_PROCESS_NAMES = (
    'ANNOTATE_INTERVALS', 'BED_TO_INTERVAL_LIST', 'COLLECT_HS_METRICS',
    'COLLECT_READ_COUNTS', 'DETERMINE_GERMLINE_CONTIG_PLOIDY',
    'FILTER_INTERVALS', 'FLAG_STAT', 'GATK_VERSION', 'GERMLINE_CNV_CALLER',
    'INDEX_FEATURE_FILE', 'POSTPROCESS_GERMLINE_CNV_CALLS',
    'PREPROCESS_INTERVALS', 'VALIDATE_SAM_FILE',
)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'development-key-change-me')
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'DATABASE_URL', 'sqlite:///app.sqlite3'
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 0 means no Flask-level limit. Reverse proxies/web servers may still impose one.
    MAX_UPLOAD_MB = _MAX_UPLOAD_MB
    MAX_CONTENT_LENGTH = _MAX_UPLOAD_MB * 1024 * 1024 if _MAX_UPLOAD_MB else None
    STORAGE_ROOT = Path(os.environ.get(
        'GATK_GCNV_STORAGE_ROOT', '/ngs-results/GATK-gCNV-models/'
    )).expanduser().resolve()
    REFERENCE_CATALOG = Path(os.environ.get(
        'GATK_GCNV_REFERENCE_CATALOG', 'config/references.yaml'
    )).expanduser().resolve()
    NEXTFLOW_BIN = os.environ.get('GATK_GCNV_NEXTFLOW', 'nextflow')
    WORKFLOW_PATH = Path(os.environ.get(
        'GATK_GCNV_BED_WORKFLOW', 'workflows/bed_analysis/main.nf'
    )).resolve()
    SAMPLE_QC_WORKFLOW_PATH = Path(os.environ.get(
        'GATK_GCNV_SAMPLE_QC_WORKFLOW', 'workflows/sample_qc/main.nf'
    )).resolve()
    COHORT_MODEL_WORKFLOW_PATH = Path(os.environ.get(
        'GATK_GCNV_COHORT_MODEL_WORKFLOW', 'workflows/cohort_model/main.nf'
    )).resolve()
    VALIDATION_WORKFLOW_PATH = Path(os.environ.get(
        'GATK_GCNV_VALIDATION_WORKFLOW', 'workflows/validation/main.nf'
    )).resolve()
    MAX_CONCURRENT_WORKFLOWS = max(
        1, int(os.environ.get('GATK_GCNV_MAX_CONCURRENT_WORKFLOWS', '1'))
    )
    PROCESS_CPUS = max(1, int(os.environ.get('GATK_GCNV_PROCESS_CPUS', '2')))
    PROCESS_MEMORY = os.environ.get('GATK_GCNV_PROCESS_MEMORY', '8 GB')
    PROCESS_TIME = os.environ.get('GATK_GCNV_PROCESS_TIME', '24h')
    BED_ANALYSIS_CPUS = max(1, int(os.environ.get('GATK_GCNV_BED_ANALYSIS_CPUS', str(PROCESS_CPUS))))
    BED_ANALYSIS_MEMORY = os.environ.get('GATK_GCNV_BED_ANALYSIS_MEMORY', PROCESS_MEMORY)
    BED_ANALYSIS_TIME = os.environ.get('GATK_GCNV_BED_ANALYSIS_TIME', PROCESS_TIME)
    SAMPLE_QC_CPUS = max(1, int(os.environ.get('GATK_GCNV_SAMPLE_QC_CPUS', str(PROCESS_CPUS))))
    SAMPLE_QC_MEMORY = os.environ.get('GATK_GCNV_SAMPLE_QC_MEMORY', PROCESS_MEMORY)
    SAMPLE_QC_TIME = os.environ.get('GATK_GCNV_SAMPLE_QC_TIME', PROCESS_TIME)
    COHORT_MODEL_CPUS = max(1, int(os.environ.get('GATK_GCNV_COHORT_MODEL_CPUS', str(PROCESS_CPUS))))
    COHORT_MODEL_MEMORY = os.environ.get('GATK_GCNV_COHORT_MODEL_MEMORY', PROCESS_MEMORY)
    COHORT_MODEL_TIME = os.environ.get('GATK_GCNV_COHORT_MODEL_TIME', '72h')
    # Per-task settings are independent of workflow-label defaults.
    MODULE_PROCESSES = {
        name.lower(): {
            'cpus': max(1, int(os.environ.get(f'GATK_GCNV_{name}_CPUS', '1'))),
            'memory': os.environ.get(f'GATK_GCNV_{name}_MEMORY', '2 GB'),
            'time': os.environ.get(f'GATK_GCNV_{name}_TIME', '24h'),
        }
        for name in _MODULE_PROCESS_NAMES
    }
    # Per-workflow local executor budgets cap task fan-out within one Nextflow run.
    WORKFLOW_CPUS = max(1, int(os.environ.get(
        'GATK_GCNV_WORKFLOW_CPUS', str(max(BED_ANALYSIS_CPUS, SAMPLE_QC_CPUS))
    )))
    WORKFLOW_MEMORY = os.environ.get(
        'GATK_GCNV_WORKFLOW_MEMORY', PROCESS_MEMORY
    )
    # Aggregate worker budgets reserve resources across concurrently running workflows.
    WORKER_CPU_BUDGET = max(1, int(os.environ.get(
        'GATK_GCNV_WORKER_CPU_BUDGET', str(WORKFLOW_CPUS)
    )))
    WORKER_MEMORY_BUDGET = os.environ.get(
        'GATK_GCNV_WORKER_MEMORY_BUDGET', WORKFLOW_MEMORY
    )
    JOB_WORK_RETENTION_DAYS = max(0, int(os.environ.get('GATK_GCNV_JOB_WORK_RETENTION_DAYS', '30')))
    APPTAINER_CACHE = Path(os.environ.get(
        'GATK_GCNV_APPTAINER_CACHE', str(Path.home() / '.cache/apptainer')
    )).expanduser().resolve()
    GATK_CONTAINER = os.environ.get(
        'GATK_GCNV_GATK_CONTAINER', 'docker://broadinstitute/gatk:4.7.0.0'
    )
    SAMTOOLS_BIN = os.environ.get('GATK_GCNV_SAMTOOLS', 'samtools')
    BAM_ALLOWED_ROOTS = tuple(
        Path(item).expanduser().resolve()
        for item in os.environ.get('GATK_GCNV_BAM_ALLOWED_ROOTS', '').split(os.pathsep)
        if item.strip()
    )
