process GATK_VERSION {
    tag 'GATK version'
    label 'cohort_model'
    cpus { params.gatk_version_cpus as int }
    memory { params.gatk_version_memory }
    time { params.gatk_version_time }
    maxForks params.gatk_version_max_forks as int
    container { params.gatk_container }
    publishDir params.outdir, mode: 'copy', overwrite: true

    output:
    path 'gatk_version.txt', emit: version

    script:
    """
    set -euo pipefail
    gatk --version 2>&1 | tee gatk_version.txt
    """
}
