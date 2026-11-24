nextflow.enable.dsl = 2

include { SAMPLE_QC } from '../subworkflows/sample_qc'

workflow {
    if (!params.samples || !params.reference_fasta || !params.reference_fai || !params.reference_dict || !params.intervals || !params.outdir) {
        error 'Required params: --samples, --reference_fasta, --reference_fai, --reference_dict, --intervals, --outdir'
    }

    def reference = file(params.reference_fasta, checkIfExists: true)
    def referenceFai = file(params.reference_fai, checkIfExists: true)
    def sequenceDictionary = file(params.reference_dict, checkIfExists: true)
    def intervals = file(params.intervals, checkIfExists: true)
    def samples = Channel.fromList(params.samples.collect { sample ->
        tuple(sample.sample_id as String,
              file(sample.bam, checkIfExists: true),
              file(sample.bai, checkIfExists: true),
              reference, referenceFai, intervals, sequenceDictionary)
    })
    def baitBed = params.bait_bed ?: ''
    if (baitBed) file(baitBed, checkIfExists: true)
    SAMPLE_QC(samples)
}
