# Kong AI Gateway for Routing llm-d Inference Stack and External APIs

This guide demonstrates how to deploy **Kong AI Gateway** (Helm chart, DB-less mode, Kong Ingress Controller) on Kubernetes to route traffic across an existing **llm-d inference stack** and external LLM provider APIs.

---

## Overview

This document provides a production-ready setup guide for deploying **Kong AI Gateway** (Helm chart, DB-less mode, Kong Ingress Controller) on Kubernetes to route traffic across an existing **llm-d inference stack** and an external API provider.

A single Kong AI Gateway deployment can front both:

1. **In-cluster model endpoints**: An existing llm-d inference stack serving models in your Kubernetes cluster (via the `ai-proxy` plugin pointing an `openai` provider at the llm-d endpoint).
2. **External provider APIs**: Third-party LLM services (via the `ai-proxy` plugin pointing a `gemini` provider at Google AI Studio).

All Kong components (gateway, routes, plugins, secrets) live in a dedicated `kong` namespace, keeping the llm-d inference stack isolated in its own namespace.

End users can:
- Call external provider APIs using API keys managed centrally in Kong (the provider key is stored in Kubernetes Secrets and never exposed to client applications).
- Call self-hosted models served by the llm-d inference stack via unified OpenAI-compatible routes.

---

## Prerequisites

1. **Kubernetes Cluster**: A running Kubernetes cluster with `kubectl` configured and [Gateway API CRDs](https://gateway-api.sigs.k8s.io/) installed.
2. **llm-d Inference Stack**: An active llm-d deployment set up using the [Optimized Baseline](../../well-lit-paths/foundations/optimized-baseline.md) hosting a model (e.g., `Qwen/Qwen3-32B`).
   - **Gateway Mode (Default)**: Reached via the Kubernetes Gateway IP (`http://<gateway-ip>/v1`).
   - **Standalone Mode (Optional)**: Reached directly via the Endpoint Picker (EPP) Service:
     `http://optimized-baseline-epp.llm-d-optimized-baseline.svc.cluster.local:80/v1`.
3. **External Provider API Key**: An API key from Google AI Studio for `gemini-3.5-flash`.
4. **Local Tools**: `kubectl`, `helm`, and `curl`.

Set up the Kong target namespace:

```bash
export NAMESPACE=kong

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
```

---

## Step 1: Create Secret for External Provider Keys

Kong securely retrieves external provider keys from Kubernetes Secrets via `configPatches` references in the plugin Custom Resources (CRDs), ensuring keys are never stored in plain text within plugin specs.

```bash
export GEMINI_API_KEY="<your-gemini-api-key>"

kubectl -n "$NAMESPACE" create secret generic ai-provider-keys \
  --from-literal=gemini-api-key="\"$GEMINI_API_KEY\""   # Note the embedded double quotes
```

> [!IMPORTANT]
> Replace `<your-gemini-api-key>` with your actual Google AI Studio API key. Kong Ingress Controller substitutes `configPatches` values directly as JSON; string values require embedded double quotes (e.g. `"\"$GEMINI_API_KEY\""`), whereas numeric or boolean patch values do not.

---

## Step 2: Install Kong Ingress Controller + Gateway via Helm

Make sure Gateway API is installed on your cluster and verify that the standard CRDs are available:

```bash
kubectl get crd gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io
```

Add the official Kong Helm repository and install the `kong/ingress` chart (deploys Kong Ingress Controller + DB-less Kong Gateway data plane):

```bash
helm repo add kong https://charts.konghq.com
helm repo update

helm install kong kong/ingress -n "$NAMESPACE"
kubectl -n "$NAMESPACE" wait --for=condition=Available deploy/kong-controller deploy/kong-gateway --timeout=300s
```

Inspect the deployed services:

```bash
kubectl get deploy,svc -n "$NAMESPACE"
```

Expected output:

```text
NAME                              READY   UP-TO-DATE   AVAILABLE
deployment.apps/kong-controller   1/1     1            1
deployment.apps/kong-gateway      1/1     1            1

NAME                                         TYPE           CLUSTER-IP     EXTERNAL-IP   PORT(S)
service/kong-controller-metrics              ClusterIP      <cluster-ip>   <none>        10255/TCP,10254/TCP
service/kong-controller-validation-webhook   ClusterIP      <cluster-ip>   <none>        443/TCP
service/kong-gateway-admin                   ClusterIP      None           <none>        8444/TCP
service/kong-gateway-manager                 NodePort       <cluster-ip>   <none>        8002:31071/TCP,8445:31084/TCP
service/kong-gateway-proxy                   LoadBalancer   <cluster-ip>   <proxy-ip>    80:32117/TCP,443:30425/TCP
```

Export the proxy LoadBalancer IP or hostname for verification commands:

```bash
export PROXY_IP=$(kubectl -n "$NAMESPACE" get svc kong-gateway-proxy \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}')
```


---

## Step 3: Create GatewayClass, Gateway, and Placeholder Service

Create a `GatewayClass` and `Gateway` resource for Kong, along with a placeholder Service (`ai-placeholder`) that the `HTTPRoute` resources in Step 4 will reference to satisfy Gateway API schema requirements.

Create `kong-gateway.yaml`:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: kong
  annotations:
    konghq.com/gatewayclass-unmanaged: "true"
spec:
  controllerName: konghq.com/kic-gateway-controller
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: kong
  namespace: kong
spec:
  gatewayClassName: kong
  listeners:
    - name: proxy
      port: 80
      protocol: HTTP
      allowedRoutes:
        namespaces:
          from: Same
---
apiVersion: v1
kind: Service
metadata:
  name: ai-placeholder
  namespace: kong
spec:
  type: ClusterIP
  ports:
    - port: 80
      targetPort: 80
```

Apply the resources:

```bash
kubectl apply -f kong-gateway.yaml
```

---

## Step 4: Configure Routes and `ai-proxy` Plugins

> [!WARNING]
> **Explicit Port Requirement**: Include an explicit port in `upstream_url` (e.g., `:80`). In some Kong Gateway versions, the `openai` provider defaults to port 443 even for `http://` URLs (though this may no longer be true in newer releases, e.g., 3.8 and above), causing connection timeouts and returning `503 The upstream server is currently unavailable`.

Create `models.yaml`:

```yaml
# ─────────────────────────────────────────────────────────────────────
# 1) DEFAULT: llm-d Gateway Mode
# Routes to the llm-d Gateway API endpoint (InferenceGateway).
# ─────────────────────────────────────────────────────────────────────
apiVersion: configuration.konghq.com/v1
kind: KongPlugin
metadata:
  name: ai-proxy-qwen3-32b
  namespace: kong
plugin: ai-proxy
config:
  route_type: llm/v1/chat
  model:
    provider: openai
    name: Qwen/Qwen3-32B      # Must match backend model ID
    options:
      upstream_url: http://<gateway-ip>/v1/chat/completions
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: qwen3-32b
  namespace: kong
  annotations:
    konghq.com/plugins: ai-proxy-qwen3-32b, key-auth, rate-limiting
spec:
  parentRefs:
    - name: kong
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /qwen3-32b
      backendRefs:
        - name: ai-placeholder
          port: 80
---
# ─────────────────────────────────────────────────────────────────────
# 2) OPTIONAL: llm-d Standalone Mode
# Routes directly to the EPP Service endpoint (bypassing Gateway API).
# ─────────────────────────────────────────────────────────────────────
apiVersion: configuration.konghq.com/v1
kind: KongPlugin
metadata:
  name: ai-proxy-qwen3-32b-standalone
  namespace: kong
plugin: ai-proxy
config:
  route_type: llm/v1/chat
  model:
    provider: openai
    name: Qwen/Qwen3-32B
    options:
      upstream_url: http://optimized-baseline-epp.llm-d-optimized-baseline.svc.cluster.local:80/v1/chat/completions
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: qwen3-32b-standalone
  namespace: kong
  annotations:
    konghq.com/plugins: ai-proxy-qwen3-32b-standalone, key-auth, rate-limiting
spec:
  parentRefs:
    - name: kong
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /qwen3-32b-standalone
      backendRefs:
        - name: ai-placeholder
          port: 80
---
# ─────────────────────────────────────────────────────────────────────
# 3) EXTERNAL API: Google Gemini
# Authenticates with Google AI Studio key injected from Secret via configPatches.
# ─────────────────────────────────────────────────────────────────────
apiVersion: configuration.konghq.com/v1
kind: KongPlugin
metadata:
  name: ai-proxy-gemini-flash
  namespace: kong
plugin: ai-proxy
config:
  route_type: llm/v1/chat
  auth:
    param_name: key
    param_location: query
  model:
    provider: gemini
    name: gemini-3.5-flash
configPatches:
  - path: /auth/param_value
    valueFrom:
      secretKeyRef:
        name: ai-provider-keys
        key: gemini-api-key
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: gemini-flash
  namespace: kong
  annotations:
    konghq.com/plugins: ai-proxy-gemini-flash, key-auth, rate-limiting
spec:
  parentRefs:
    - name: kong
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /gemini-flash
      backendRefs:
        - name: ai-placeholder
          port: 80
---
# ─────────────────────────────────────────────────────────────────────
# 4) SECURITY & GOVERNANCE: key-auth and rate-limiting
# Enforces client authentication and request rate limits across all routes.
# ─────────────────────────────────────────────────────────────────────
apiVersion: configuration.konghq.com/v1
kind: KongPlugin
metadata:
  name: key-auth
  namespace: kong
plugin: key-auth
config:
  key_names:
    - apikey
  hide_credentials: true
---
apiVersion: configuration.konghq.com/v1
kind: KongPlugin
metadata:
  name: rate-limiting
  namespace: kong
plugin: rate-limiting
config:
  minute: 60
  policy: local
```

Apply the route definitions and security plugins:

```bash
kubectl apply -f models.yaml
```

> [!NOTE]
> - **Fail-Closed Placeholder Backend**: Gateway API `HTTPRoute` requires a valid `backendRef`. Because `ai-proxy` replaces the upstream request path with the target model's `upstream_url`, the `ai-placeholder` Service is not normally reached. Using a selector-less `ClusterIP` Service ensures that if an `ai-proxy` plugin fails to program or is missing, Kong fails closed with an immediate `503 Service Unavailable` rather than proxying to localhost.
> - **Authentication Header**: Kong's `key-auth` plugin compares the full header value and does not strip a `Bearer ` prefix, so clients pass keys via the `apikey` header (`-H "apikey: $CLIENT_KEY"`). OpenAI SDK clients can send this with `default_headers={"apikey": CLIENT_KEY}`. If a client can only send `Authorization: Bearer <key>`, add a Kong `pre-function` plugin to copy the token into `apikey` and clear the original header before `key-auth` runs.
> - **Model Pinning**: `ai-proxy` pins the model per route. Clients can omit `"model"` in the request body; if provided, it must match `model.name`, otherwise Kong returns HTTP `400 Bad Request`.

Next, provision an authorized client consumer and API key credentials:

```bash
# 1) Generate a client API key and store it in a Kubernetes Secret
CLIENT_KEY="key-$(openssl rand -hex 16)"

kubectl -n "$NAMESPACE" create secret generic client-app-key \
  --from-literal=kongCredType=key-auth \
  --from-literal=key="$CLIENT_KEY"

# 2) Create the KongConsumer referencing the credential Secret
kubectl apply -f - <<EOF
apiVersion: configuration.konghq.com/v1
kind: KongConsumer
metadata:
  name: client-app
  namespace: kong
credentials:
  - client-app-key
EOF
```

---

## Step 5: Verification

### 1. Verify Gateway API Resource Programming

Check that all Gateway API resources, Kong plugins, and consumers are programmed:

```bash
kubectl -n "$NAMESPACE" get gateway,httproute,kongplugin,kongconsumer
```

Expected output:

```text
NAME                                     CLASS   ADDRESS      PROGRAMMED   AGE
gateway.gateway.networking.k8s.io/kong   kong    <proxy-ip>   True         77m

NAME                                                        HOSTNAMES   AGE
httproute.gateway.networking.k8s.io/gemini-flash                        77m
httproute.gateway.networking.k8s.io/qwen3-32b                           77m
httproute.gateway.networking.k8s.io/qwen3-32b-standalone                77m

NAME                                                               PLUGIN-TYPE   AGE   PROGRAMMED
kongplugin.configuration.konghq.com/ai-proxy-gemini-flash         ai-proxy      69m
kongplugin.configuration.konghq.com/ai-proxy-qwen3-32b            ai-proxy      71m
kongplugin.configuration.konghq.com/ai-proxy-qwen3-32b-standalone ai-proxy      71m
kongplugin.configuration.konghq.com/key-auth                      key-auth      71m
kongplugin.configuration.konghq.com/rate-limiting                 rate-limiting 71m

NAME                                          AGE
kongconsumer.configuration.konghq.com/client-app 5m
```

### 2. Verify Authentication Enforcement (Security Check)

Attempt an unauthenticated request to verify that Kong blocks unauthorized access:

```bash
curl -i -s http://$PROXY_IP/gemini-flash \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hi"}]}'
```

Expected response (HTTP 401 Unauthorized):

```text
HTTP/1.1 401 Unauthorized
Content-Type: application/json; charset=utf-8
Connection: keep-alive

{
  "message": "No API key found in request"
}
```

### 3. Call Configured Models with Client Authentication

#### A. Call Self-Hosted Model (`/qwen3-32b` via Gateway Mode)

Pass the client API key via the `apikey` header:

```bash
curl -s -m 45 http://$PROXY_IP/qwen3-32b \
  -H "Content-Type: application/json" \
  -H "apikey: $CLIENT_KEY" \
  -d '{
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16
  }' | jq .
```

Expected response (showing vLLM system fingerprint from llm-d):

```json
{
  "id": "<response-id>",
  "object": "chat.completion",
  "model": "Qwen/Qwen3-32B",
  "system_fingerprint": "vllm-0.23.0-tp2-a536750c",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "<think>\nOkay, the user said \"hi\". That's a greeting. I"
      },
      "finish_reason": "length"
    }
  ],
  "usage": {
    "prompt_tokens": 9,
    "completion_tokens": 16,
    "total_tokens": 25
  }
}
```

#### B. Call Self-Hosted Model (`/qwen3-32b-standalone` via Standalone Mode)

```bash
curl -s -m 45 http://$PROXY_IP/qwen3-32b-standalone \
  -H "Content-Type: application/json" \
  -H "apikey: $CLIENT_KEY" \
  -d '{
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 16
  }' | jq .
```

#### C. Call External Model (`/gemini-flash` via Google AI Studio API)

Kong validates the client's `apikey`, translates the OpenAI request format into Gemini API format, and injects the upstream Gemini API key:

```bash
curl -s -m 45 http://$PROXY_IP/gemini-flash \
  -H "Content-Type: application/json" \
  -H "apikey: $CLIENT_KEY" \
  -d '{
    "messages": [{"role": "user", "content": "Reply with just: GEMINI-OK"}],
    "max_tokens": 16
  }' | jq .
```

Expected response:

```json
{
  "id": "<response-id>",
  "object": "chat.completion",
  "model": "gemini-3.5-flash",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "GEMINI-OK"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 9,
    "completion_tokens": 4,
    "total_tokens": 13
  }
}
```

---

## Cleanup

To remove the Kong AI Gateway deployment and all associated resources:

```bash
kubectl delete -f models.yaml -n "$NAMESPACE"
kubectl delete kongconsumer client-app -n "$NAMESPACE" --ignore-not-found
kubectl delete secret client-app-key -n "$NAMESPACE" --ignore-not-found
kubectl delete -f kong-gateway.yaml -n "$NAMESPACE"
helm uninstall kong -n "$NAMESPACE"
kubectl delete namespace "$NAMESPACE"
```
