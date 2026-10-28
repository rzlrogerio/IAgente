apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: open-webui
  namespace: ${LLM_NAMESPACE}
spec:
  ingressClassName: ${INGRESS_CLASS}
  rules:
    - host: ${INGRESS_HOST}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: open-webui
                port:
                  number: 8080
