nextflow.enable.dsl = 2

include { VALIDATE_SAM_FILE } from '../modules/local/gatk/validate_sam_file'
include { COLLECT_READ_COUNTS } from '../modules/local/gatk/collect_read_counts'
include { DETERMINE_GERMLINE_CONTIG_PLOIDY_CASE } from '../modules/local/gatk/determine_germline_contig_ploidy_case'
include { GERMLINE_CNV_CALLER_CASE } from '../modules/local/gatk/germline_cnv_caller_case'
include { POSTPROCESS_GERMLINE_CNV_CALLS_CASE } from '../modules/local/gatk/postprocess_germline_cnv_calls_case'

workflow {
    if (!params.samples || !params.intervals || !params.cohort_model || !params.ploidy_model ||
        !params.contig_ploidy_priors || !params.reference_fasta || !params.reference_fai ||
        !params.reference_dict || !params.outdir) {
        error 'Required params: samples, intervals, model paths, reference files and outdir'
    }

    def reference = file(params.reference_fasta, checkIfExists: true)
    def referenceFai = file(params.reference_fai, checkIfExists: true)
    def referenceDict = file(params.reference_dict, checkIfExists: true)
    def intervals = file(params.intervals, checkIfExists: true)
    def samples = params.samples.sort { it.sample_id }.collect { sample ->
        tuple(sample.sample_id as String, file(sample.bam, checkIfExists: true),
              file(sample.bai, checkIfExists: true), reference, referenceFai, referenceDict, intervals)
    }
    def sampleInputs = Channel.fromList(samples)
    def byTask = sampleInputs.multiMap { sampleId, bam, bai, ref, refFai, dict, targetIntervals ->
        validation: tuple(sampleId, bam, bai, ref, refFai, dict)
        counts: tuple(sampleId, bam, bai, ref, refFai, dict, targetIntervals)
    }
    VALIDATE_SAM_FILE(byTask.validation)
    def countsInput = byTask.counts.join(VALIDATE_SAM_FILE.out.validation)
        .map { sampleId, bam, bai, ref, refFai, dict, targetIntervals, validation ->
            tuple(sampleId, bam, bai, ref, refFai, dict, targetIntervals, validation)
        }
    COLLECT_READ_COUNTS(countsInput)

    def countPairs = COLLECT_READ_COUNTS.out.counts.collect().map { pairs ->
        [samples: pairs.sort { it[0] }]
    }
    def countsForPloidy = countPairs.map { bundle ->
        tuple(bundle.samples.collect { it[1] }, file(params.ploidy_model, checkIfExists: true))
    }
    DETERMINE_GERMLINE_CONTIG_PLOIDY_CASE(countsForPloidy)

    def callerInput = countPairs.combine(DETERMINE_GERMLINE_CONTIG_PLOIDY_CASE.out.ploidy)
        .map { combined ->
            def pairs = combined[0].samples
            def ploidy = combined[1]
            def sorted = pairs.sort { it[0] }
            tuple(sorted.collect { it[1] }, file(params.cohort_model, checkIfExists: true),
                  ploidy, sorted.collect { it[0] })
        }
    GERMLINE_CNV_CALLER_CASE(callerInput)

    def postprocessInput = countPairs.combine(GERMLINE_CNV_CALLER_CASE.out.caller)
        .combine(DETERMINE_GERMLINE_CONTIG_PLOIDY_CASE.out.ploidy)
        .map { combined ->
            def pairs = combined[0].samples
            def caller = combined[1]
            def ploidy = combined[2]
            def sorted = pairs.sort { it[0] }
            def priors = file(params.contig_ploidy_priors, checkIfExists: true)
            def dict = file(params.reference_dict, checkIfExists: true)
            def model = file(params.cohort_model, checkIfExists: true)
            sorted.withIndex().collect { pair, index ->
                tuple(index, pair[0], caller, model, ploidy, priors, dict)
            }
        }.flatMap { it }
    POSTPROCESS_GERMLINE_CNV_CALLS_CASE(postprocessInput)
}
