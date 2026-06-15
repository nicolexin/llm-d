# Pod Snapshots

Pod Snapshots allow you to capture a snapshot of a running Pod's memory state (including GPU memory), and restore future instances of the Pod from that snapshot. For model servers, this can drastically reduce container startup latency by bypassing the container image downloading, model weight downloading, and model server initialization phases, restoring directly into a ready state.

> [!NOTE]
> Pod Snapshots are a GKE-exclusive feature and require the GKE Sandbox (gVisor) runtime.

## Overview and Concepts

For a detailed conceptual overview of how GKE Pod snapshots work, refer to the official [About GKE Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots) documentation.


## Key Use Cases

1. **Scale Out**: Accelerate horizontal scaling of model servers under load by warm-starting new replicas.
2. **Scale from Zero**: Aggressively scale down to zero when idle and restore instantly upon request. See the [Workload Autoscaling guide](../workload-autoscaling) for configuring scale-to-zero in llm-d.
3. **Spot VM Recovery**: Minimize disruption when [spot VMs](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/spot-vms) are preempted and replaced.


## Requirements and Limitations

Before proceeding, review the official [GKE Pod Snapshots Requirements and Limitations](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots#limitations) for up-to-date details on supported machine types and other requirements.

---

## Prerequisites

### 1. Infrastructure Requirements

To use Pod snapshots, you must have a GKE cluster with:
*   Workload Identity enabled.
*   Pod snapshots enabled.

Additionally, the GKE Sandbox (gVisor) runtime must be enabled on your node pools. For Autopilot clusters, this is enabled automatically. For standard clusters, you must explicitly create a GPU node pool with GKE Sandbox enabled (using the `--sandbox=type=gvisor` flag).

For detailed instructions on enabling Pod snapshots, setting up GKE Sandbox, and configuring GPU node pools, see the official GKE guide on [Enabling Pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#enable).

### 2. Environment Preparation

- Have the [proper client tools installed on your local system](../../helpers/client-setup/README.md) to use this guide.

- Checkout llm-d repo:
  ```bash
  export branch="main" # branch, tag, or commit hash
  git clone https://github.com/llm-d/llm-d.git && cd llm-d && git checkout ${branch}
  ```

- Set target environment variables:
  ```bash
  export ROUTER_CHART_VERSION=v0
  export GUIDE_NAME="pod-snapshot"
  export NAMESPACE="llm-d-${GUIDE_NAME}"
  export REPO_ROOT=$(realpath $(git rev-parse --show-toplevel))
  ```

- Create the target namespace for the installation:
  ```bash
  kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
  ```

- [Create the `llm-d-hf-token` secret in your target namespace with the key `HF_TOKEN` matching a valid HuggingFace token](../../helpers/hf-token.md) to pull models:
<!-- llm-d-cicd:skip start -->
  ```bash
  export HF_TOKEN=<your HuggingFace token>
  kubectl create secret generic llm-d-hf-token \
    --from-literal="HF_TOKEN=${HF_TOKEN}" \
    --namespace "${NAMESPACE}" \
    --dry-run=client -o yaml | kubectl apply -f -
  ```
<!-- llm-d-cicd:skip end -->

---

## Pod Snapshot Storage and Policy Configuration

Setting up GKE Pod Snapshots storage involves three main steps:

### 1. Create a GCS Bucket and Configure IAM Permissions
You must create a Google Cloud Storage bucket with hierarchical namespace enabled and set up Workload Identity permissions so your Pods can read and write to the bucket. For complete, step-by-step instructions, follow the official GKE guide on [Store snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#store-snapshots).

### 2. Deploy the PodSnapshotStorageConfig
Create and deploy a `PodSnapshotStorageConfig` resource named `pod-snapshot-storage-config` pointing to your GCS bucket, following the official GKE guide on [Configuring snapshot storage](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#configure-storage).

### 3. Deploy the PodSnapshotPolicy
Create a `PodSnapshotPolicy` that references your storage configuration and targets the model server pods.

For details on policy configuration options, see [Creating a PodSnapshotPolicy](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#create-policy) in the official GKE documentation.

```yaml
apiVersion: podsnapshot.gke.io/v1
kind: PodSnapshotPolicy
metadata:
  name: pod-snapshot-policy
spec:
  storageConfigName: pod-snapshot-storage-config # This must match storage config name
  selector:
    matchLabels:
      llm-d.ai/guide: pod-snapshot # This must match label in pod definition
  triggerConfig:
    type: workload
    postCheckpoint: resume
```

> [!NOTE]
> * **Namespace Scoping**: `PodSnapshotStorageConfig` is a cluster-scoped resource and is globally accessible. `PodSnapshotPolicy` is a namespace-scoped resource and must be deployed in the same namespace as your model server pods.
> * **Trigger Configuration**: The `triggerConfig.type` must be set to `workload` to allow the container itself to trigger the snapshot programmatically. If it is set to `manual`, GKE will ignore container-initiated writes and expect snapshots to be triggered on-demand by creating `PodSnapshotManualTrigger` custom resources.

Save the above to `snapshot-policy.yaml` and apply it to your target namespace:

```bash
kubectl apply -f snapshot-policy.yaml -n $NAMESPACE
```

## Installation Instructions

### 1. Deploy the llm-d Router

#### Standalone Mode
Deploy the router with a standalone Envoy proxy sidecar:

```bash
helm install ${GUIDE_NAME} \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone-dev \
  -f ${REPO_ROOT}/guides/recipes/router/base.values.yaml \
  -f ${REPO_ROOT}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml \
  -n ${NAMESPACE} --version ${ROUTER_CHART_VERSION}
```

#### Gateway Mode (Inference Gateway)
To use a managed Kubernetes Gateway instead of a standalone Envoy sidecar:

1. **Deploy a Kubernetes Gateway**. Follow the [Gateway guides](../prereq/gateways) to deploy a Gateway named `llm-d-inference-gateway`.
2. **Deploy the llm-d Router & HTTPRoute**:

```bash
export PROVIDER_NAME=gke

helm install ${GUIDE_NAME} \
  oci://ghcr.io/llm-d/charts/llm-d-router-gateway-dev \
  -f ${REPO_ROOT}/guides/recipes/router/base.values.yaml \
  -f ${REPO_ROOT}/guides/recipes/router/features/httproute-flags.yaml \
  -f ${REPO_ROOT}/guides/${GUIDE_NAME}/router/${GUIDE_NAME}.values.yaml \
  --set provider.name=${PROVIDER_NAME} \
  -n ${NAMESPACE} --version ${ROUTER_CHART_VERSION}
```

### 2. Deploy the Model Server
Apply the Kustomize overlay to deploy the model server configured for GKE Sandbox and checkpoint hooks:

```bash
kubectl apply -n ${NAMESPACE} -k ${REPO_ROOT}/guides/${GUIDE_NAME}/modelserver/gpu/vllm/gke/
```

### 3. (Optional) Enable monitoring

> [!NOTE]
> GKE provides [automatic application monitoring](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/configure-automatic-application-monitoring) out of the box. The llm-d [Monitoring stack](../../docs/resources/observability/setup.md) is not required for GKE, but it is available if you prefer to use it.

- Install the [Monitoring stack](../../docs/resources/observability/setup.md).
- Deploy the monitoring resources for this guide.

```bash
kubectl apply -n ${NAMESPACE} -k ${REPO_ROOT}/guides/recipes/modelserver/components/monitoring
```

---

## Triggering the Checkpoint (Warm-up)

When the model server starts for the first time, it performs a "cold start" (downloading the model weights and loading them onto the GPU memory). Once the server is ready, the configured wrapper script executes the checkpoint trigger:

```bash
# Check the logs of the initial pod to watch the startup process
kubectl logs -f -l llm-d.ai/guide=pod-snapshot -n ${NAMESPACE}
```

When the server is ready, the logs will show:

```logs
Model server is ready. Triggering checkpoint...
```

This triggers GKE to snapshot the container runtime and memory, and save it to the configured GCS bucket.

### Verify Snapshot Status
Keep monitoring the `PodSnapshot` custom resource to check the status of the snapshot:

```bash
kubectl get podsnapshots -o yaml -n ${NAMESPACE}
```

---

## Measuring Startup Performance

Once the snapshot is successfully created, we can test scaling up the deployment to measure warm start latency.

### 1. Scale Up the Fleet
Scale the deployment to 2 replicas:

```bash
kubectl scale deployment pod-snapshot-gpu-vllm-decode --replicas=2
```

### 2. Observe Warm Start Events
Watch the events of the new warm-started pod:

```bash
kubectl get events \
  --field-selector involvedObject.name=pod-snapshot-gpu-vllm-decode-<new-pod-suffix> \
  -o custom-columns='NAME:.involvedObject.name,TIME:.metadata.creationTimestamp,REASON:.reason,MESSAGE:.message'
```

Output:
```
NAME                                           TIME                   REASON               MESSAGE
pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv   2026-06-09T17:45:33Z   Scheduled            Successfully assigned default/pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv to gke-snapshot-test-l4-28881afe-4lbm
pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv   2026-06-09T17:45:35Z   Pulled               Container image "vllm/vllm-openai:v0.8.5" already present on machine and can be accessed by the pod
pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv   2026-06-09T17:45:35Z   Created              Container created
pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv   2026-06-09T17:45:39Z   Started              Container started
pod-snapshot-gpu-vllm-decode-8689478d99-gdzvv   2026-06-09T17:47:14Z   GKEPodSnapshotting   Successfully restored the pod from PodSnapshot default/a5622707-38be-431c-ab79-18d1523606c5
```

### Startup Time Improvement Analysis (TBD)

Based on benchmarks, here is the performance comparison:

| Metric | Startup Time |
| :--- | :--- |
| **Cold Start (w/o Image Pull)** | 377) |
| **Warm Start (Restore)** | 101s |
| **Startup Improvement** | **~68.1%** |

*(If including image pulling overhead or comparing against a pre-downloaded baseline, performance gains can range up to 83.6%).*

---

## Cleanup

Uninstall the router and delete the model server components:

```bash
helm uninstall ${GUIDE_NAME} -n ${NAMESPACE}
kubectl delete -n ${NAMESPACE} -k ${REPO_ROOT}/guides/${GUIDE_NAME}/modelserver/gpu/vllm/gke/
```

Delete the GKE Pod Snapshot policy and storage configurations:

```bash
kubectl delete podsnapshotpolicy pod-snapshot-policy -n ${NAMESPACE}
kubectl delete podsnapshotstorageconfig pod-snapshot-storage-config
```

Delete all created `PodSnapshot` custom resources in the namespace to release the associated storage:

```bash
kubectl delete podsnapshots --all -n ${NAMESPACE}
```

> [!NOTE]
> Deleting a `PodSnapshot` custom resource automatically triggers GKE's snapshot controller to asynchronously delete the corresponding binary snapshot files from your GCS bucket.
>
> If the deletion fails or `PodSnapshot` resources remain stuck in a `Terminating` state, it is usually because the GKE Service Agent lacks the required `roles/storage.objectUser` permission on your GCS bucket. Follow the [Grant Controller Permission guide](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#grant-controller-permissions) to grant the GKE Service Agent permission to delete snapshots.
