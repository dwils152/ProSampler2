process GENERATE_SAMPLES {
    publishDir "${params.outdir}/example", mode: 'copy', overwrite: false

    input:
    val seed
    val sampleCount
    path packageSource

    output:
    path 'samples.csv', emit: samples
    path 'samples.parquet', emit: samples_parquet

    script:
    """
    export PYTHONPATH="\$PWD"
    python -m research_project.example generate --seed ${seed} --count ${sampleCount} --output samples.csv --parquet-output samples.parquet --parquet-threads ${params.parquet_threads} --parquet-memory-limit '${params.parquet_memory_limit}'
    """
}
