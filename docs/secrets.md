# Secrets

Plain secrets never go into Git. We use Sealed Secrets: encrypt with `kubeseal`,
commit the `SealedSecret`, the in-cluster controller decrypts it.

```bash
kubectl create secret generic anthropic-api-key -n sre-agent \
  --from-literal=ANTHROPIC_API_KEY=... --dry-run=client -o yaml \
  | kubeseal --controller-name sealed-secrets-controller --controller-namespace kube-system -o yaml \
  > gitops/workloads/sre-agent/anthropic-sealedsecret.yaml
```

## Key backup

The controller key is generated on first start. If the cluster is destroyed the
sealed files can no longer be decrypted, so back it up once and keep it off Git:

```bash
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealed-secrets-key.backup.yaml
```

Restore it before the controller starts on a new cluster (`kubectl apply -f` then restart the controller).

In AWS this would be External Secrets Operator reading from Secrets Manager.
