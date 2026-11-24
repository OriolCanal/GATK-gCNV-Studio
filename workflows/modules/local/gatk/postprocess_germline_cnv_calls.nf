process POSTPROCESS_GERMLINE_CNV_CALLS {
    tag "PostprocessGermlineCNVCalls ${sample_id}"
    label 'cohort_model'
    cpus { params.postprocess_germline_cnv_calls_cpus as int }
    memory { params.postprocess_germline_cnv_calls_memory }
    time { params.postprocess_germline_cnv_calls_time }
    maxForks params.postprocess_germline_cnv_calls_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple val(sample_index), val(sample_id), path(caller_directory), path(ploidy_directory),
          path(ploidy_priors), path(reference_dict)

    output:
    path "postprocess/SAMPLE_${sample_index}", emit: sample_results

    script:
    def sampleIdEncoded = Base64.encoder.encodeToString(sample_id.getBytes('UTF-8'))
    """
    set -euo pipefail
    export HOME="\$PWD/.gatk-home"
    export XDG_CACHE_HOME="\$PWD/.cache"
    export MPLCONFIGDIR="\$PWD/.cache/matplotlib"
    export PYTENSOR_FLAGS="base_compiledir=\$PWD/.pytensor"
    export THEANO_FLAGS="base_compiledir=\$PWD/.theano"
    mkdir -p "\$HOME/.cache/arviz" "\$XDG_CACHE_HOME/fontconfig" "\$MPLCONFIGDIR" "\$PWD/.pytensor" "\$PWD/.theano" "postprocess/SAMPLE_${sample_index}"

    ALLOSOMAL_ARGS=()
    while IFS=\$'\\t' read -r contig _; do
      case "\$contig" in
        CONTIG_NAME|'') continue ;;
        X|Y|chrX|chrY) ALLOSOMAL_ARGS+=(--allosomal-contig "\$contig") ;;
      esac
    done < '${ploidy_priors}'

    gatk PostprocessGermlineCNVCalls \\
      --model-shard-path '${caller_directory}/cohort-model' \\
      --calls-shard-path '${caller_directory}/cohort-calls' \\
      --contig-ploidy-calls '${ploidy_directory}/cohort-calls' \\
      --sample-index ${sample_index} \\
      "\${ALLOSOMAL_ARGS[@]}" \\
      --sequence-dictionary '${reference_dict}' \\
      --output-genotyped-intervals "postprocess/SAMPLE_${sample_index}/genotyped_intervals.vcf.gz" \\
      --output-genotyped-segments "postprocess/SAMPLE_${sample_index}/genotyped_segments.vcf.gz" \\
      --output-denoised-copy-ratios "postprocess/SAMPLE_${sample_index}/denoised_copy_ratios.tsv"

    printf '%s' '${sampleIdEncoded}' | base64 -d > "postprocess/SAMPLE_${sample_index}/sample_name.txt"
    """
}
