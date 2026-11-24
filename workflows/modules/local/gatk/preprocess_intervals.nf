process PREPROCESS_INTERVALS {
    tag { bed.baseName }
    label 'bed_analysis'
    cpus { params.preprocess_intervals_cpus as int }
    memory { params.preprocess_intervals_memory }
    time { params.preprocess_intervals_time }
    maxForks params.preprocess_intervals_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(bed), path(reference), path(reference_fai), path(reference_dict),
          val(padding), val(bin_length), val(merging_rule)

    output:
    path 'preprocessed.interval_list', emit: intervals

    script:
    """
    set -euo pipefail
    gatk PreprocessIntervals \\
      -R '${reference}' \\
      -L '${bed}' \\
      --interval-merging-rule '${merging_rule}' \\
      --padding ${padding} \\
      --bin-length ${bin_length} \\
      -O preprocessed.interval_list
    """
}
