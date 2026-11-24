process GERMLINE_CNV_CALLER_CASE {
    tag 'GermlineCNVCaller CASE'
    label 'cohort_model'
    cpus { params.germline_cnv_caller_cpus as int }
    memory { params.germline_cnv_caller_memory }
    time { params.germline_cnv_caller_time }
    maxForks params.germline_cnv_caller_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(counts, stageAs: 'counts/*'), path(cohort_model), path(ploidy_directory), val(sample_ids)

    output:
    path 'caller', emit: caller

    script:
    def countArgs = counts.collect { "--input '${it}'" }.join(' \\\n      ')
    def sampleIndexTsv = (['sample_index\tsample_id'] + sample_ids.withIndex().collect { sampleId, sampleIndex -> "${sampleIndex}\t${sampleId}" }).join('\n') + '\n'
    def sampleIndexManifest = Base64.encoder.encodeToString(sampleIndexTsv.getBytes('UTF-8'))
    """
    set -euo pipefail
    export HOME="\$PWD/.gatk-home"
    export XDG_CACHE_HOME="\$PWD/.cache"
    export MPLCONFIGDIR="\$PWD/.cache/matplotlib"
    export PYTENSOR_FLAGS="base_compiledir=\$PWD/.pytensor"
    export THEANO_FLAGS="base_compiledir=\$PWD/.theano"
    mkdir -p "\$HOME/.cache/arviz" "\$XDG_CACHE_HOME/fontconfig" "\$MPLCONFIGDIR" "\$PWD/.pytensor" "\$PWD/.theano"
    gatk GermlineCNVCaller \\
      --run-mode CASE \\
      --model '${cohort_model}' \\
      --contig-ploidy-calls '${ploidy_directory}/validation-calls' \\
      ${countArgs} \\
      -O caller \\
      --output-prefix validation
    printf '%s' '${sampleIndexManifest}' | base64 -d > caller/sample_index.tsv
    """
}
