from datetime import datetime, timezone
import uuid

from app import db
from sqlalchemy import UniqueConstraint


def new_id():
    return uuid.uuid4().hex


class BedProject(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    name = db.Column(db.String(200), nullable=False)
    reference_id = db.Column(db.String(32), nullable=False)
    reference_bundle_version = db.Column(db.String(100), nullable=False, default='')
    storage_path = db.Column(db.Text, nullable=False)
    original_bed_filename = db.Column(db.String(255), nullable=False)
    original_bed_path = db.Column(db.Text, nullable=False)
    canonical_bed_path = db.Column(db.Text, nullable=False)
    original_bed_sha256 = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(32), nullable=False, default='ready')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))
    jobs = db.relationship('WorkflowJob', back_populates='project', cascade='all, delete-orphan')


class WorkflowJob(db.Model):
    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False)
    workflow = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(32), nullable=False, default='queued', index=True)
    parameters_path = db.Column(db.Text, nullable=False)
    execution_path = db.Column(db.Text, nullable=False)
    output_path = db.Column(db.Text, nullable=False)
    log_path = db.Column(db.Text, nullable=False)
    error_message = db.Column(db.Text)
    exit_code = db.Column(db.Integer)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))
    started_at = db.Column(db.DateTime(timezone=True))
    finished_at = db.Column(db.DateTime(timezone=True))
    project = db.relationship('BedProject', back_populates='jobs')


class CohortModelRun(db.Model):
    """Immutable cohort-model execution snapshot; each parameter revision gets a new row."""
    __tablename__ = 'cohort_model_run'

    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False, index=True)
    revision_of = db.Column(db.String(32), db.ForeignKey('cohort_model_run.id'))
    name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(40), nullable=False, default='queued', index=True)
    sample_snapshot_json = db.Column(db.Text, nullable=False)
    reference_id = db.Column(db.String(32), nullable=False)
    reference_bundle_version = db.Column(db.String(100), nullable=False, default='')
    intervals_path = db.Column(db.Text, nullable=False)
    parameters_path = db.Column(db.Text, nullable=False)
    parameters_sha256 = db.Column(db.String(64), nullable=False)
    execution_path = db.Column(db.Text, nullable=False)
    execution_sha256 = db.Column(db.String(64), nullable=False)
    output_path = db.Column(db.Text, nullable=False)
    job_id = db.Column(db.String(32), nullable=False, index=True)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime(timezone=True))
    project = db.relationship('BedProject')
    parent = db.relationship('CohortModelRun', remote_side=[id], uselist=False)


class ValidationSample(db.Model):
    """Independent case samples and user-supplied CNV truth for model validation."""
    __tablename__ = 'validation_sample'
    __table_args__ = (UniqueConstraint('project_id', 'sample_id', name='uq_validation_sample_project_sample'),)

    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False, index=True)
    sample_id = db.Column(db.String(200), nullable=False)
    bam_path = db.Column(db.Text, nullable=False)
    bai_path = db.Column(db.Text, nullable=False)
    storage_mode = db.Column(db.String(16), nullable=False, default='copy')
    known_cnvs_json = db.Column(db.Text, nullable=False, default='[]')
    validation_report = db.Column(db.Text, nullable=False, default='{}')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))


class ModelValidationRun(db.Model):
    """Execution linking a frozen set of case samples to one trained model."""
    __tablename__ = 'model_validation_run'

    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False, index=True)
    model_run_id = db.Column(db.String(32), db.ForeignKey('cohort_model_run.id'), nullable=False, index=True)
    sample_snapshot_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(40), nullable=False, default='queued', index=True)
    output_path = db.Column(db.Text, nullable=False)
    job_id = db.Column(db.String(32), nullable=False, index=True)
    summary_json = db.Column(db.Text)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime(timezone=True))


class SampleBatch(db.Model):
    __tablename__ = 'sample_batch'
    __table_args__ = (
        UniqueConstraint('project_id', 'name', name='uq_sample_batch_project_name'),
    )

    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    storage_mode = db.Column(db.String(16), nullable=False)
    kit_name = db.Column(db.String(200), nullable=False, default='')
    protocol = db.Column(db.String(200), nullable=False, default='')
    duplicates_marked = db.Column(db.Boolean, nullable=False, default=False)
    low_count_threshold = db.Column(db.Integer, nullable=False, default=10)
    bait_bed_path = db.Column(db.Text)
    job_id = db.Column(db.String(32))
    status = db.Column(db.String(32), nullable=False, default='queued', index=True)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    project = db.relationship('BedProject')
    samples = db.relationship('BamSample', back_populates='batch', cascade='all, delete-orphan',
                              order_by='BamSample.sample_id')


class BamSample(db.Model):
    __tablename__ = 'bam_sample'
    __table_args__ = (
        UniqueConstraint('project_id', 'sample_id', name='uq_bam_sample_project_sample'),
    )

    id = db.Column(db.String(32), primary_key=True, default=new_id)
    project_id = db.Column(db.String(32), db.ForeignKey('bed_project.id'), nullable=False, index=True)
    batch_id = db.Column(db.String(32), db.ForeignKey('sample_batch.id'), nullable=False, index=True)
    sample_id = db.Column(db.String(200), nullable=False)
    read_group_sm = db.Column(db.String(200), nullable=False)
    bam_path = db.Column(db.Text, nullable=False)
    bai_path = db.Column(db.Text, nullable=False)
    source_bam_path = db.Column(db.Text, nullable=False)
    source_bai_path = db.Column(db.Text, nullable=False)
    storage_mode = db.Column(db.String(16), nullable=False)
    bam_size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    bam_sha256 = db.Column(db.String(64))
    counts_path = db.Column(db.Text)
    qc_metrics_path = db.Column(db.Text)
    validation_report = db.Column(db.Text)
    status = db.Column(db.String(32), nullable=False, default='validated', index=True)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    project = db.relationship('BedProject')
    batch = db.relationship('SampleBatch', back_populates='samples')
