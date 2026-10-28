apiVersion: v1
kind: ServiceAccount
metadata:
  name: open-webui-updater
  namespace: ${LLM_NAMESPACE}
  labels:
    app.kubernetes.io/name: open-webui-update
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: open-webui-updater
  namespace: ${LLM_NAMESPACE}
  labels:
    app.kubernetes.io/name: open-webui-update
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    resourceNames: ["open-webui"]
    verbs: ["get", "patch", "list", "watch"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: open-webui-updater
  namespace: ${LLM_NAMESPACE}
  labels:
    app.kubernetes.io/name: open-webui-update
subjects:
  - kind: ServiceAccount
    name: open-webui-updater
    namespace: ${LLM_NAMESPACE}
roleRef:
  kind: Role
  name: open-webui-updater
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: open-webui-update
  namespace: ${LLM_NAMESPACE}
  labels:
    app.kubernetes.io/name: open-webui-update
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
            app.kubernetes.io/name: open-webui-update
        spec:
          serviceAccountName: open-webui-updater
          restartPolicy: OnFailure
          containers:
            - name: webui-updater
              image: ${KUBECTL_IMAGE}
              imagePullPolicy: IfNotPresent
              command:
                - /bin/sh
                - -c
              args:
                - |
                  set -eu
                  echo "Garantindo imagePullPolicy Always no container open-webui..."
                  kubectl patch deployment open-webui --type='strategic' \
                    -p '{"spec":{"template":{"spec":{"containers":[{"name":"open-webui","imagePullPolicy":"Always"}]}}}}'
                  echo "Reiniciando deployment open-webui para obter a imagem mais recente..."
                  kubectl rollout restart deployment/open-webui
                  kubectl rollout status deployment/open-webui --timeout=600s
                  echo "Open WebUI atualizado com sucesso."
              resources:
                requests:
                  cpu: 50m
                  memory: 64Mi
                limits:
                  cpu: 250m
                  memory: 256Mi
