apiVersion: v1
kind: ConfigMap
metadata:
  name: ollama-default-model
  namespace: ${LLM_NAMESPACE}
data:
  MODEL: "${DEFAULT_MODEL}"
