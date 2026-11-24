process INDEX_FEATURE_FILE {
    tag { "${enabled ? 'index' : 'skip index'} · ${track.name}" }
    label 'bed_analysis'
    cpus { params.index_feature_file_cpus as int }
    memory { params.index_feature_file_memory }
    time { params.index_feature_file_time }
    maxForks params.index_feature_file_max_forks as int
    container { params.gatk_container }

    input:
    tuple val(enabled), path(track)

    output:
    tuple val(enabled), path(track), path("${track}.idx"), emit: indexed

    script:
    def indexing = enabled ? "gatk IndexFeatureFile -I '${track}' -O '${track}.idx'" : "touch '${track}.idx'"
    """
    set -euo pipefail
    ${indexing}
    """
}
