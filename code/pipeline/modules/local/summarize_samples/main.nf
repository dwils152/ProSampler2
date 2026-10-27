process SUMMARIZE_SAMPLES {
    publishDir "${params.outdir}/example", mode: 'copy', overwrite: false

    input:
    path samples
    path packageSource

    output:
    path 'summary.json', emit: summary

    script:
    """
    export PYTHONPATH="\$PWD"
    python -m research_project.example summarize --input '${samples}' --output summary.json
    """
}
