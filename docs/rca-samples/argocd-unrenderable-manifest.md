# RCA: ArgoAppOutOfSync on fault-argo-badpath

**Report:** rca-fault-argo-badpath-20261026-143010 | **Priority:** P3 | **Alert severity:** ticket | **Analysis mode:** rule_based

## Priority and where the problem is
**P3**: respond next business day. Still ongoing.
- alert severity is 'ticket'
- it is platform infrastructure
- it is still ongoing

**Application level** (high confidence): manifests of fault-argo-badpath in Git
- ComparisonError / manifest generation error from the repo server
- the destination cluster is reachable

## What happened
Argo CD cannot generate or compare the manifests of `fault-argo-badpath` (destination platform/sandbox): ComparisonError: Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = Unknown desc = Manifest generation error (cached): gitops/workloads/this-path-does-not-ex. The live state stays as it was; the last change in Git is not applied. This is a content problem in the repository, not in the cluster.

## Root cause (high confidence)
Category: `gitops_sync_failure`

Argo CD cannot render the application: ComparisonError: Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = Unknown desc = Manifest generation error (cached).

## Impact
Changes to fault-argo-badpath are not rolling out.
SLO: no SLO defined for this signal.

## How to fix it now
- [ ] Reproduce locally: kustomize build or helm template for the app path (gitops/workloads/this-path-does-not-exist)
- [ ] Fix the file named in the error and push; or revert the last commit

## How to prevent it
- [ ] CI already renders manifests (kubeconform): add this app's helm/kustomize render to it

## How to confirm it is fixed
- argocd app get fault-argo-badpath  # no ComparisonError

## Timeline
- 2026-10-26T14:30:10Z: alert ArgoAppOutOfSync started firing
- 2026-10-26T14:30:54Z: investigation completed

## Contributing factors
- none identified

## Detection
The alert `ArgoAppOutOfSync` fired in Grafana alerting and was sent by webhook to the SRE agent, which started this investigation as an Argo Workflow automatically.

## Observability gaps (what to add so the next one is faster)
- The telemetry available was enough for this investigation.

## Evidence
- **argocd**: sync Unknown, health Healthy, destination platform; conditions: ComparisonError: Failed to load target state: failed to generate manifest for source 1 of 1: rpc error: code = Unknown d
  - `application fault-argo-badpath`

_This RCA is blameless and was generated automatically. Review it before acting on it._
