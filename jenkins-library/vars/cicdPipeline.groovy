def call(Map config = [:], Closure body) {
  cicdWithQaPair(config) { context ->
    cicdPipelineBody(config, context ?: [:], body)
  }
}
