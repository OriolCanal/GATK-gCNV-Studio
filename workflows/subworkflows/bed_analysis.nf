include { PREPROCESS_INTERVALS } from '../modules/local/gatk/preprocess_intervals'
include { INDEX_FEATURE_FILE as INDEX_MAPPABILITY } from '../modules/local/gatk/index_feature_file'
include { INDEX_FEATURE_FILE as INDEX_SEGMENTAL_DUPLICATION } from '../modules/local/gatk/index_feature_file'
include { ANNOTATE_INTERVALS } from '../modules/local/gatk/annotate_intervals'

workflow BED_ANALYSIS {
    take:
    bed_input

    main:
    def bed = file(params.bed, checkIfExists: true)
    def reference = file(params.reference_fasta, checkIfExists: true)
    def referenceFai = file(params.reference_fai, checkIfExists: true)
    def referenceDict = file(params.reference_dict, checkIfExists: true)
    def mappabilityPath = params.mappability_track ? file(params.mappability_track, checkIfExists: true) : bed
    def segmentalDuplicationPath = params.segmental_duplication_track ? file(params.segmental_duplication_track, checkIfExists: true) : bed
    def mappabilityEnabled = params.mappability_track ? true : false
    def segmentalDuplicationEnabled = params.segmental_duplication_track ? true : false

    PREPROCESS_INTERVALS(bed_input)
    INDEX_MAPPABILITY(Channel.value(tuple(mappabilityEnabled, mappabilityPath)))
    INDEX_SEGMENTAL_DUPLICATION(Channel.value(tuple(segmentalDuplicationEnabled, segmentalDuplicationPath)))

    def annotationInput = PREPROCESS_INTERVALS.out.intervals
        .combine(INDEX_MAPPABILITY.out.indexed)
        .combine(INDEX_SEGMENTAL_DUPLICATION.out.indexed)
        .map { intervals, hasMap, mapTrack, mapIndex, hasSegdup, segdupTrack, segdupIndex ->
            tuple(intervals, reference, referenceFai, referenceDict, hasMap, mapTrack, mapIndex,
                  hasSegdup, segdupTrack, segdupIndex, params.annotate_interval_merging_rule)
        }
    ANNOTATE_INTERVALS(annotationInput)

    emit:
    preprocessed = PREPROCESS_INTERVALS.out.intervals
    annotated = ANNOTATE_INTERVALS.out.annotations
}
