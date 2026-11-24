process COLLECT_READ_COUNTS {
    tag { "CollectReadCounts · ${sample_id}" }
    label 'sample_qc'
    cpus { params.collect_read_counts_cpus as int }
    memory { params.collect_read_counts_memory }
    time { params.collect_read_counts_time }
    maxForks params.collect_read_counts_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple val(sample_id), path(bam), path(bai), path(reference), path(reference_fai),
          path(reference_dict), path(intervals), path(validation_report)

    output:
    tuple val(sample_id), path("${sample_id}.counts.hdf5"), emit: counts

    script:
    """
    set -euo pipefail
    gatk CollectReadCounts \\
      -I '${bam}' \\
      -R '${reference}' \\
      -L '${intervals}' \\
      --interval-merging-rule OVERLAPPING_ONLY \\
      --format HDF5 \\
      -O '${sample_id}.counts.hdf5'
    """
}
