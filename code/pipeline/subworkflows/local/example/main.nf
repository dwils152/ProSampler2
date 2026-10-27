include { GENERATE_SAMPLES } from '../../../modules/local/generate_samples/main'
include { SUMMARIZE_SAMPLES } from '../../../modules/local/summarize_samples/main'

workflow EXAMPLE {
    take:
    seed
    sampleCount
    packageSource

    main:
    GENERATE_SAMPLES(seed, sampleCount, packageSource)
    SUMMARIZE_SAMPLES(GENERATE_SAMPLES.out.samples, packageSource)

    emit:
    samples = GENERATE_SAMPLES.out.samples
    samples_parquet = GENERATE_SAMPLES.out.samples_parquet
    summary = SUMMARIZE_SAMPLES.out.summary
}
