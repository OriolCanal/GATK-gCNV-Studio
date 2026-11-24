nextflow.enable.dsl = 2

include { BED_ANALYSIS } from '../subworkflows/bed_analysis'

workflow {
    if (!params.bed || !params.reference_fasta || !params.reference_fai || !params.reference_dict || !params.outdir) {
        error 'Required params: --bed, --reference_fasta, --reference_fai, --reference_dict, --outdir'
    }
    if (params.padding == null || params.bin_length == null) error 'Required params: --padding and --bin_length'

    def bed = file(params.bed, checkIfExists: true)
    def reference = file(params.reference_fasta, checkIfExists: true)
    def referenceFai = file(params.reference_fai, checkIfExists: true)
    def referenceDict = file(params.reference_dict, checkIfExists: true)
    def bedInput = Channel.value(tuple(bed, reference, referenceFai, referenceDict,
                                      params.padding as int, params.bin_length as int,
                                      params.preprocess_interval_merging_rule))
    BED_ANALYSIS(bedInput)
}
