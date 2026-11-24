include { GATK_VERSION } from '../modules/local/gatk/gatk_version'
include { FILTER_INTERVALS } from '../modules/local/gatk/filter_intervals'
include { DETERMINE_GERMLINE_CONTIG_PLOIDY } from '../modules/local/gatk/determine_germline_contig_ploidy'
include { GERMLINE_CNV_CALLER } from '../modules/local/gatk/germline_cnv_caller'
include { POSTPROCESS_GERMLINE_CNV_CALLS } from '../modules/local/gatk/postprocess_germline_cnv_calls'

workflow COHORT_MODEL {
    take:
    counts
    initial_intervals
    priors
    sample_ids
    reference_dict

    main:
    GATK_VERSION()

    def selectedIntervals
    def filteredIntervals = Channel.empty()
    if (params.filter_options.enabled) {
        def filterInput = counts.combine(initial_intervals)
            .map { combined ->
                def countFiles = combined[0..<(combined.size() - 1)]
                def intervals = combined[-1]
                tuple(countFiles, intervals, params.filter_options)
            }
        FILTER_INTERVALS(filterInput)
        selectedIntervals = FILTER_INTERVALS.out.intervals.first()
        filteredIntervals = FILTER_INTERVALS.out.intervals
    } else {
        selectedIntervals = initial_intervals
    }

    def countFileCount = params.samples.size()
    def ploidyInput = counts.combine(selectedIntervals).combine(priors)
        .map { combined ->
            def countFiles = combined[0..<countFileCount]
            def intervals = combined[countFileCount]
            def priorsFile = combined[countFileCount + 1]
            tuple(countFiles, intervals, priorsFile, params.ploidy_options)
        }
    DETERMINE_GERMLINE_CONTIG_PLOIDY(ploidyInput)

    def callerInput = DETERMINE_GERMLINE_CONTIG_PLOIDY.out.ploidy
        .combine(selectedIntervals)
        .combine(counts)
        .combine(sample_ids)
        .map { combined ->
            def ploidyDirectory = combined[0]
            def intervals = combined[1]
            def countFiles = combined[2..<(2 + countFileCount)]
            def ids = combined[(2 + countFileCount)..<(2 + 2 * countFileCount)]
            tuple(countFiles, intervals, ploidyDirectory, params.gcnv_options, ids)
        }
    GERMLINE_CNV_CALLER(callerInput)

    def postprocessInput = sample_ids
        .flatMap { ids -> ids.withIndex().collect { sampleId, sampleIndex -> tuple(sampleIndex, sampleId) } }
        .combine(GERMLINE_CNV_CALLER.out.caller)
        .combine(DETERMINE_GERMLINE_CONTIG_PLOIDY.out.ploidy)
        .combine(priors)
        .combine(reference_dict)
        .map { combined ->
            tuple(combined[0], combined[1], combined[2], combined[3], combined[4], combined[5])
        }
    POSTPROCESS_GERMLINE_CNV_CALLS(postprocessInput)

    emit:
    version = GATK_VERSION.out.version
    ploidy = DETERMINE_GERMLINE_CONTIG_PLOIDY.out.ploidy
    caller = GERMLINE_CNV_CALLER.out.caller
    postprocessed = POSTPROCESS_GERMLINE_CNV_CALLS.out.sample_results
    filtered_intervals = filteredIntervals
}
