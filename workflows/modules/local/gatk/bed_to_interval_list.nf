process BED_TO_INTERVAL_LIST {
    tag 'BedToIntervalList'
    label 'sample_qc'
    cpus { params.bed_to_interval_list_cpus as int }
    memory { params.bed_to_interval_list_memory }
    time { params.bed_to_interval_list_time }
    maxForks params.bed_to_interval_list_max_forks as int
    container { params.gatk_container }

    input:
    tuple path(bed), path(sequence_dictionary)

    output:
    path 'bait_intervals.interval_list', emit: intervals

    script:
    """
    set -euo pipefail
    gatk BedToIntervalList \\
      -I '${bed}' \\
      -O bait_intervals.interval_list \\
      -SD '${sequence_dictionary}'
    """
}
