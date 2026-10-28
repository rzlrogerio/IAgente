apiVersion: apps/v1
kind: Deployment
metadata:
  name: open-webui
  namespace: ${LLM_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: open-webui
  template:
    metadata:
      labels:
        app: open-webui
    spec:
      containers:
        - name: open-webui
          image: ${OPEN_WEBUI_IMAGE}
          imagePullPolicy: Always

          ports:
            - containerPort: 8080
              name: http
          env:
            - name: OLLAMA_BASE_URL
              value: http://ollama.${LLM_NAMESPACE}.svc.cluster.local:11434
            - name: WEBUI_AUTH
              value: "${WEBUI_AUTH}"
            - name: DEFAULT_MODELS
              valueFrom:
                configMapKeyRef:
                  name: ollama-default-model
                  key: MODEL
          volumeMounts:
            - name: webui-data
              mountPath: /app/backend/data
          startupProbe:
            httpGet:
              path: /
              port: 8080
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 20
            timeoutSeconds: 5
            failureThreshold: 6
          resources:
            requests:
              cpu: ${WEBUI_CPU_REQUEST}
              memory: ${WEBUI_MEMORY_REQUEST}
            limits:
              cpu: ${WEBUI_CPU_LIMIT}
              memory: ${WEBUI_MEMORY_LIMIT}
      volumes:
        - name: webui-data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: open-webui
  namespace: ${LLM_NAMESPACE}
spec:
  selector:
    app: open-webui
  ports:
    - name: http
      port: 8080
      targetPort: 8080
  type: ${WEBUI_SERVICE_TYPE}
