process FLAG_STAT {
    tag { "FlagStat · ${sample_id}" }
    label 'sample_qc'
    cpus { params.flag_stat_cpus as int }
    memory { params.flag_stat_memory }
    time { params.flag_stat_time }
    maxForks params.flag_stat_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple val(sample_id), path(bam), path(reference), path(reference_fai), path(reference_dict), path(counts)

    output:
    path "${sample_id}.flagstat.txt", emit: flagstat

    script:
    """
    set -euo pipefail
    gatk FlagStat \\
      -I '${bam}' \\
      -R '${reference}' \\
      -O '${sample_id}.flagstat.txt'
    """
}
