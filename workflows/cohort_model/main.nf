nextflow.enable.dsl = 2

include { COHORT_MODEL } from '../subworkflows/cohort_model'

workflow {
    if (!params.samples || !params.intervals || !params.contig_ploidy_priors || !params.outdir) {
        error 'Required params: --samples, --intervals, --contig_ploidy_priors, --outdir'
    }
    if (params.samples.size() < 1) error 'At least one selected cohort sample is required.'
    if (!params.filter_options || !params.ploidy_options || !params.gcnv_options) {
        error 'Required params: --filter_options, --ploidy_options, --gcnv_options'
    }

    def sampleIds = params.samples.collect { it.sample_id as String }
    if (sampleIds.unique().size() != sampleIds.size()) error 'Duplicate sample IDs are not allowed.'
    def counts = params.samples.collect { file(it.counts, checkIfExists: true) }
    def intervals = file(params.intervals, checkIfExists: true)
    def priors = file(params.contig_ploidy_priors, checkIfExists: true)
    def referenceDict = file(params.reference_dict, checkIfExists: true)
    COHORT_MODEL(Channel.value(counts), Channel.value(intervals), Channel.value(priors),
                 Channel.value(sampleIds), Channel.value(referenceDict))
}
