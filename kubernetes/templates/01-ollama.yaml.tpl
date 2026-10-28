apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ollama-data
  namespace: ${LLM_NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: ${OLLAMA_STORAGE_CLASS}
  resources:
    requests:
      storage: ${OLLAMA_STORAGE_SIZE}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: ${LLM_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  strategy:
    type: Recreate
  template:
    metadata:
      labels:
        app: ollama
    spec:
      containers:
        - name: ollama
          image: ${OLLAMA_IMAGE}
          imagePullPolicy: IfNotPresent
          args:
            - serve
          ports:
            - containerPort: 11434
              name: http
          env:
            - name: OLLAMA_KEEP_ALIVE
              value: "24h"
          volumeMounts:
            - name: ollama-data
              mountPath: /root/.ollama
          readinessProbe:
            httpGet:
              path: /
              port: 11434
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /
              port: 11434
            initialDelaySeconds: 25
            periodSeconds: 20
            timeoutSeconds: 5
            failureThreshold: 6
          resources:
            requests:
              cpu: ${OLLAMA_CPU_REQUEST}
              memory: ${OLLAMA_MEMORY_REQUEST}
            limits:
              cpu: ${OLLAMA_CPU_LIMIT}
              memory: ${OLLAMA_MEMORY_LIMIT}
      volumes:
        - name: ollama-data
          persistentVolumeClaim:
            claimName: ollama-data
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: ${LLM_NAMESPACE}
spec:
  selector:
    app: ollama
  ports:
    - name: http
      port: 11434
      targetPort: 11434
  type: ClusterIP
