include { EXAMPLE } from './subworkflows/local/example/main'

workflow {
    if (!params.run_id || !params.outdir) {
        error 'Use the Pixi experiment or smoke task to create a recorded run.'
    }
    if (params.workflow_name != 'example') {
        error "Unknown workflow: ${params.workflow_name}"
    }
    if (params.sample_count < 1 || params.sample_count > 10000) {
        error 'sample_count must be between 1 and 10000.'
    }
    def packageSource = Channel.value(file("${projectDir}/bin/research_project", checkIfExists: true))
    EXAMPLE(params.seed as long, params.sample_count as int, packageSource)
}
