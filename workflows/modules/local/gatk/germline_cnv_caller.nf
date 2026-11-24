process GERMLINE_CNV_CALLER {
    tag 'GermlineCNVCaller COHORT'
    label 'cohort_model'
    cpus { params.germline_cnv_caller_cpus as int }
    memory { params.germline_cnv_caller_memory }
    time { params.germline_cnv_caller_time }
    maxForks params.germline_cnv_caller_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(counts, stageAs: 'counts/*'), path(intervals), path(ploidy_directory), val(options), val(sample_ids)

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
      --run-mode COHORT \\
      -L '${intervals}' \\
      --interval-merging-rule OVERLAPPING_ONLY \\
      --contig-ploidy-calls '${ploidy_directory}/cohort-calls' \\
      ${countArgs} \\
      --max-copy-number ${options.max_copy_number} \\
      --max-bias-factors ${options.max_bias_factors} \\
      --max-training-epochs ${options.max_training_epochs} \\
      --min-training-epochs ${options.min_training_epochs} \\
      --max-calling-iters ${options.max_calling_iters} \\
      --p-active ${options.p_active} \\
      --p-alt ${options.p_alt} \\
      --convergence-snr-averaging-window ${options.convergence_snr_averaging_window} \\
      --convergence-snr-countdown-window ${options.convergence_snr_countdown_window} \\
      --convergence-snr-trigger-threshold ${options.convergence_snr_trigger_threshold} \\
      --verbosity ${options.verbosity} \\
      -O caller \\
      --output-prefix cohort
    printf '%s' '${sampleIndexManifest}' | base64 -d > caller/sample_index.tsv
    while IFS=\$'\\t' read -r sample_index sample_id; do
      [[ "\$sample_index" == "sample_index" ]] && continue
      calls_sample="caller/cohort-calls/SAMPLE_\$sample_index/sample_name.txt"
      if [[ ! -f "\$calls_sample" ]]; then
        echo "Missing caller sample output for index \$sample_index (\$sample_id)" >&2
        exit 1
      fi
      actual_sample_id=\$(cat "\$calls_sample")
      if [[ "\$actual_sample_id" != "\$sample_id" ]]; then
        echo "Caller sample order mismatch at index \$sample_index: expected \$sample_id, observed \$actual_sample_id" >&2
        exit 1
      fi
    done < caller/sample_index.tsv
    """
}
