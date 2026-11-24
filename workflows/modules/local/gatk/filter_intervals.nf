process FILTER_INTERVALS {
    tag 'FilterIntervals'
    label 'cohort_model'
    cpus { params.filter_intervals_cpus as int }
    memory { params.filter_intervals_memory }
    time { params.filter_intervals_time }
    maxForks params.filter_intervals_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(counts, stageAs: 'counts/*'), path(intervals), val(options)

    output:
    path 'filtered.interval_list', emit: intervals

    script:
    """
    set -euo pipefail
    COUNT_ARGS=()
    for count_file in counts/*.hdf5; do COUNT_ARGS+=(--input "\$count_file"); done
    gatk FilterIntervals \\
      -L '${intervals}' \\
      "\${COUNT_ARGS[@]}" \\
      --interval-merging-rule OVERLAPPING_ONLY \\
      --low-count-filter-count-threshold ${options.low_count_filter_count_threshold} \\
      --low-count-filter-percentage-of-samples ${options.low_count_filter_percentage_of_samples} \\
      --extreme-count-filter-minimum-percentile ${options.extreme_count_filter_minimum_percentile} \\
      --extreme-count-filter-maximum-percentile ${options.extreme_count_filter_maximum_percentile} \\
      --extreme-count-filter-percentage-of-samples ${options.extreme_count_filter_percentage_of_samples} \\
      -O filtered.interval_list
    """
}
