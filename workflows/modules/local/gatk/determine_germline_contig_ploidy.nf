process DETERMINE_GERMLINE_CONTIG_PLOIDY {
    tag 'DetermineGermlineContigPloidy'
    label 'cohort_model'
    cpus { params.determine_germline_contig_ploidy_cpus as int }
    memory { params.determine_germline_contig_ploidy_memory }
    time { params.determine_germline_contig_ploidy_time }
    maxForks params.determine_germline_contig_ploidy_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple path(counts, stageAs: 'counts/*'), path(intervals), path(priors), val(options)

    output:
    path 'ploidy', emit: ploidy

    script:
    """
    set -euo pipefail
    export HOME="\$PWD/.gatk-home"
    export XDG_CACHE_HOME="\$PWD/.cache"
    export PYTENSOR_FLAGS="base_compiledir=\$PWD/.pytensor"
    export THEANO_FLAGS="base_compiledir=\$PWD/.theano"
    mkdir -p "\$HOME/.cache/arviz" "\$XDG_CACHE_HOME/fontconfig" "\$PWD/.pytensor" "\$PWD/.theano"
    COUNT_ARGS=()
    for count_file in counts/*.hdf5; do COUNT_ARGS+=(--input "\$count_file"); done
    gatk DetermineGermlineContigPloidy \\
      -L '${intervals}' \\
      --interval-merging-rule OVERLAPPING_ONLY \\
      "\${COUNT_ARGS[@]}" \\
      --contig-ploidy-priors '${priors}' \\
      --mean-bias-standard-deviation ${options.mean_bias_standard_deviation} \\
      --global-psi-scale ${options.global_psi_scale} \\
      --sample-psi-scale ${options.sample_psi_scale} \\
      -O ploidy \\
      --output-prefix cohort
    """
}
