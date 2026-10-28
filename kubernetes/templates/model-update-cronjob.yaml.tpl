apiVersion: batch/v1
kind: CronJob
metadata:
  name: ollama-model-update
  namespace: ${LLM_NAMESPACE}
  labels:
    app.kubernetes.io/name: ollama-model-update
spec:
  schedule: "${UPDATE_SCHEDULE}"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 3600
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2
      template:
        metadata:
          labels:
            app.kubernetes.io/name: ollama-model-update
        spec:
          restartPolicy: OnFailure
          containers:
            - name: model-updater
              image: ${OLLAMA_IMAGE}
              imagePullPolicy: IfNotPresent
              command:
                - /bin/sh
                - -c
              args:
                - |
                  set -eu
                  while IFS= read -r model || [ -n "$model" ]; do
                    [ -n "$model" ] || continue
                    case "$model" in \#*) continue ;; esac
                    echo "Atualizando modelo: $model"
                    ollama pull "$model"
                  done < /models/models.txt
              env:
                - name: OLLAMA_HOST
                  value: "http://ollama.${LLM_NAMESPACE}.svc.cluster.local:11434"
              volumeMounts:
                - name: models
                  mountPath: /models
                  readOnly: true
              resources:
                requests:
                  cpu: 100m
                  memory: 128Mi
                limits:
                  cpu: 1
                  memory: 512Mi
          volumes:
            - name: models
              configMap:
                name: ollama-update-models
