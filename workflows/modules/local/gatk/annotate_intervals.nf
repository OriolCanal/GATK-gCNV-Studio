process ANNOTATE_INTERVALS {
    tag 'AnnotateIntervals'
    label 'bed_analysis'
    cpus { params.annotate_intervals_cpus as int }
    memory { params.annotate_intervals_memory }
    time { params.annotate_intervals_time }
    maxForks params.annotate_intervals_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(intervals), path(reference), path(reference_fai), path(reference_dict),
          val(has_mappability), path(mappability, name: 'mappability/*'), path(mappability_index, name: 'mappability/*'),
          val(has_segmental_duplication), path(segmental_duplication, name: 'segmental_duplication/*'), path(segmental_duplication_index, name: 'segmental_duplication/*'),
          val(merging_rule)

    output:
    path 'annotated_intervals.tsv', emit: annotations

    script:
    // The input paths are already staged under their named directories.
    // Prefixing their names again would produce mappability/mappability/<file>.
    def mapArg = has_mappability ? "--mappability-track '${mappability}'" : ''
    def segdupArg = has_segmental_duplication ? "--segmental-duplication-track '${segmental_duplication}'" : ''
    """
    set -euo pipefail
    gatk AnnotateIntervals \\
      -R '${reference}' \\
      -L '${intervals}' \\
      --interval-merging-rule '${merging_rule}' \\
      ${mapArg} \\
      ${segdupArg} \\
      -O annotated_intervals.tsv
    """
}
