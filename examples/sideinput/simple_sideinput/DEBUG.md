# DEBUG

```bash
kind create cluster 
helm repo add numaflow https://numaproj.io/helm-charts
helm repo update
helm upgrade --install numaflow numaflow/numaflow --version "0.0.3" -n numaflow-system --create-namespace -f manifests/numaflow-system/values.yaml --force #  = numaflow 1.2.1

git switch v0.7.3
TAG=v0.7.3 make image
cd udf; TAG=v0.7.3 make image; cd ..

kind load docker-image quay.io/numaio/numaflow-python/sideinput-example:v0.7.3
kind load docker-image quay.io/numaio/numaflow-python/udf-sideinput-example:v0.7.3

kubectl apply -f pipeline.yaml
```

```bash
$ kubectl get pods my-pipeline-si-myticker-... -o jsonpath='{.spec.containers[*].name}'
numa udsi

$ kubectl get pods my-pipeline-si-log-... -o jsonpath='{.spec.containers[*].name}'
numa udf side-inputs-synchronizer
```
