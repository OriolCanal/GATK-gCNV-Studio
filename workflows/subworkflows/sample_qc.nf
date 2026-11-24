include { BED_TO_INTERVAL_LIST } from '../modules/local/gatk/bed_to_interval_list'
include { VALIDATE_SAM_FILE } from '../modules/local/gatk/validate_sam_file'
include { COLLECT_READ_COUNTS } from '../modules/local/gatk/collect_read_counts'
include { COLLECT_HS_METRICS } from '../modules/local/gatk/collect_hs_metrics'
include { FLAG_STAT } from '../modules/local/gatk/flagstat'

workflow SAMPLE_QC {
    take:
    sample_inputs

    main:
    def intervals = sample_inputs.map { sample -> sample[5] }.first()
    def sequenceDictionary = sample_inputs.map { sample -> sample[6] }.first()
    def baitIntervalChannel
    if (params.bait_bed) {
        def baitInput = sample_inputs.map { sample -> tuple(file(params.bait_bed, checkIfExists: true), sample[6]) }.first()
        BED_TO_INTERVAL_LIST(baitInput)
        baitIntervalChannel = BED_TO_INTERVAL_LIST.out.intervals.first()
    } else {
        baitIntervalChannel = intervals
    }
    def sampleChannels = sample_inputs.multiMap { sampleId, bam, bai, reference, referenceFai, targetIntervals, dictionary ->
        validation: tuple(sampleId, bam, bai, reference, referenceFai, dictionary)
        counts: tuple(sampleId, bam, bai, reference, referenceFai, dictionary, targetIntervals)
        hsMetrics: tuple(sampleId, bam, reference, referenceFai, dictionary, targetIntervals)
        flagstat: tuple(sampleId, bam, reference, referenceFai, dictionary)
    }

    VALIDATE_SAM_FILE(sampleChannels.validation)
    def countsInput = sampleChannels.counts.join(VALIDATE_SAM_FILE.out.validation)
        .map { sampleId, bam, bai, ref, refFai, dictionary, targetIntervals, validation ->
            tuple(sampleId, bam, bai, ref, refFai, dictionary, targetIntervals, validation)
        }
    COLLECT_READ_COUNTS(countsInput)

    def hsInput = COLLECT_READ_COUNTS.out.counts.join(sampleChannels.hsMetrics)
        .map { sampleId, counts, bam, ref, refFai, dictionary, targetIntervals ->
            tuple(sampleId, bam, ref, refFai, dictionary, targetIntervals, counts)
        }
        .combine(baitIntervalChannel)
        .map { sampleId, bam, ref, refFai, dictionary, targetIntervals, counts, baits ->
            tuple(sampleId, bam, ref, refFai, dictionary, targetIntervals, baits, counts)
        }
    COLLECT_HS_METRICS(hsInput)

    def flagstatInput = COLLECT_READ_COUNTS.out.counts.join(sampleChannels.flagstat)
        .map { sampleId, counts, bam, ref, refFai, dictionary ->
            tuple(sampleId, bam, ref, refFai, dictionary, counts)
        }
    FLAG_STAT(flagstatInput)

    emit:
    validation = VALIDATE_SAM_FILE.out.validation
    counts = COLLECT_READ_COUNTS.out.counts
    hs_metrics = COLLECT_HS_METRICS.out.hs_metrics
    flagstat = FLAG_STAT.out.flagstat
}
