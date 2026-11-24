process COLLECT_HS_METRICS {
    tag { "CollectHsMetrics · ${sample_id}" }
    label 'sample_qc'
    cpus { params.collect_hs_metrics_cpus as int }
    memory { params.collect_hs_metrics_memory }
    time { params.collect_hs_metrics_time }
    maxForks params.collect_hs_metrics_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple val(sample_id), path(bam), path(reference), path(reference_fai), path(reference_dict),
          path(target_intervals), path(bait_intervals, name: 'baits.interval_list'), path(counts)

    output:
    path "${sample_id}.hs_metrics.txt", emit: hs_metrics

    script:
    """
    set -euo pipefail
    gatk CollectHsMetrics \\
      -I '${bam}' \\
      -R '${reference}' \\
      --BAIT_INTERVALS '${bait_intervals}' \\
      --TARGET_INTERVALS '${target_intervals}' \\
      -O '${sample_id}.hs_metrics.txt'
    """
}
