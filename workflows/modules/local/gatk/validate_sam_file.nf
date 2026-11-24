process VALIDATE_SAM_FILE {
    tag { "ValidateSamFile · ${sample_id}" }
    label 'sample_qc'
    cpus { params.validate_sam_file_cpus as int }
    memory { params.validate_sam_file_memory }
    time { params.validate_sam_file_time }
    maxForks params.validate_sam_file_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    input:
    tuple val(sample_id), path(bam), path(bai), path(reference), path(reference_fai), path(reference_dict)

    output:
    tuple val(sample_id), path("${sample_id}.validation.txt"), emit: validation

    script:
    """
    set -euo pipefail
    set +e
    gatk ValidateSamFile \\
      -I '${bam}' \\
      -R '${reference}' \\
      --MODE SUMMARY \\
      --INDEX_VALIDATION_STRINGENCY EXHAUSTIVE \\
      -O '${sample_id}.validation.summary.txt' > '${sample_id}.validation.log' 2>&1
    validation_status=\$?
    set -e
    touch '${sample_id}.validation.txt'
    cat '${sample_id}.validation.summary.txt' >> '${sample_id}.validation.txt' 2>/dev/null || true
    cat '${sample_id}.validation.log' >> '${sample_id}.validation.txt'
    if [ "\$validation_status" -gt 4 ]; then
      cat '${sample_id}.validation.txt' >&2
      exit "\$validation_status"
    fi
    validation_errors=\$(grep -E '^[[:space:]]*ERROR:' '${sample_id}.validation.txt' | awk '{print \$1}' | sort -u || true)
    unexpected_validation_errors=\$(printf '%s\\n' "\$validation_errors" | grep -Ev '^ERROR:(INVALID_TAG_NM|MATE_NOT_FOUND)\$' || true)
    if [ -n "\$unexpected_validation_errors" ] || { [ "\$validation_status" -eq 4 ] && [ -z "\$validation_errors" ]; }; then
      cat '${sample_id}.validation.txt' >&2
      exit 1
    fi
    if [ "\$validation_status" -gt 0 ]; then
      printf '\\nValidateSamFile exit code: %s; permitted validation findings are retained above. QC collectors will run; review this report before using the sample.\\n' "\$validation_status" >> '${sample_id}.validation.txt'
    fi
    """
}
