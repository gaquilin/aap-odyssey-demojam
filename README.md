# AAP Odyssey — AI-guided AAP migration on Red Hat OpenShift AI

## Table of Contents

1. [Introduction](#1-introduction)
2. [Lab overview](#2-lab-overview)
   - [2.1 Architecture](#21-architecture)
     - [2.1.1 Design choices: two kinds of AI](#211-design-choices-two-kinds-of-ai)
     - [2.1.2 Data layout](#212-data-layout)
   - [2.2 Project folders content](#22-project-folders-content)
   - [2.3 Tests coverage](#23-tests-coverage)
   - [2.4 Security](#24-security)
3. [Lab installation](#3-lab-installation)
   - [3.1 Order at a glance](#31-order-at-a-glance)
   - [3.2 Download the Migration Toolkit](#32-download-the-migration-toolkit)
   - [3.3 Operators](#33-operators)
     - [3.3.1 Node Feature Discovery](#331-node-feature-discovery)
     - [3.3.2 NVIDIA GPU Operator](#332-nvidia-gpu-operator)
     - [3.3.3 OpenShift Data Foundation operator](#333-openshift-data-foundation-operator-skip-if-oc-get-storagecluster--a-already-shows-one)
     - [3.3.4 ODF storage cluster](#334-odf-storage-cluster-console-wizard-documented-procedure)
   - [3.4 GPU node](#34-gpu-node)
     - [3.4.1 GPU MachineSet](#341-gpu-machineset-openshift-procedure-adding-a-gpu-node-to-an-existing-cluster)
     - [3.4.2 NodeFeatureDiscovery instance](#342-nodefeaturediscovery-instance)
     - [3.4.3 ClusterPolicy](#343-clusterpolicy)
     - [3.4.4 GPU test](#344-gpu-test)
   - [3.5 OpenShift AI](#35-openshift-ai-skip-if-already-installed)
     - [3.5.1 Check](#351-check)
     - [3.5.2 Hardware profile and GPU detection](#352-hardware-profile-and-gpu-detection)
   - [3.6 Project and buckets (ODF)](#36-project-and-buckets-odf)
   - [3.7 Pipeline server](#37-pipeline-server)
   - [3.8 Configuration](#38-configuration)
   - [3.9 The aap-migration-ai image](#39-the-aap-migration-ai-image)
   - [3.10 The model: Qwen3-8B (FP8) on the L4](#310-the-model-qwen3-8b-fp8-on-the-l4)
   - [3.11 Migration Toolkit console](#311-migration-toolkit-console)
   - [3.12 AAP Odyssey (orchestrator + 6 agents)](#312-aap-odyssey-orchestrator--6-agents)
     - [3.12.1 Copy the patch release notes](#3121-copy-the-patch-release-notes-only-if-red-hat-docs-shows-fail-403)
   - [3.13 Import the pipelines](#313-import-the-pipelines)
   - [3.14 End-to-end check of the agents](#314-end-to-end-check-of-the-agents)
4. [Journey walkthrough](#4-journey-walkthrough)
   - [4.1 Pre-migration (stages 1–4)](#41-pre-migration-stages-14)
   - [4.2 Migration (stages 5–8)](#42-migration-stages-58)
   - [4.3 Post-migration (stages 9–10)](#43-post-migration-stages-910)
   - [4.4 Training and prediction (stages 11–12)](#44-training-and-prediction-stages-1112)
   - [4.5 Configuration as Code (stage 13, optional)](#45-configuration-as-code-stage-13-optional)
   - [4.6 Pipelines overview](#46-pipelines-overview)
     - [4.6.1 AAP migration - during migration (plan, run, collect)](#461-aap-migration---during-migration-plan-run-collect)
     - [4.6.2 AAP migration - train risk model](#462-aap-migration---train-risk-model)
     - [4.6.3 AAP migration - predict next migration](#463-aap-migration---predict-next-migration)
     - [4.6.4 AAP migration - Configuration as Code](#464-aap-migration---configuration-as-code)
5. [Post-migration Operations](#5-post-migration-operations)
   - [5.1 Risk model](#51-risk-model)
   - [5.2 Next-migration prediction](#52-next-migration-prediction)
   - [5.3 Configuration as Code](#53-configuration-as-code)
   - [5.4 AAP Odyssey web app](#54-aap-odyssey-web-app)
   - [5.5 Learning loop](#55-learning-loop)
6. [AAP Odyssey Agent Breakdown](#6-aap-odyssey-agent-breakdown)
   - [6.1 Agents and tools](#61-agents-and-tools)
   - [6.2 Request routing](#62-request-routing)
   - [6.3 Smart Planning agent](#63-smart-planning-agent)
   - [6.4 Smoke & regression test agent](#64-smoke--regression-test-agent)
   - [6.5 Reporting agent](#65-reporting-agent)
   - [6.6 Configuration as Code agent](#66-configuration-as-code-agent)
   - [6.7 Journey stages and owners](#67-journey-stages-and-owners)
7. [Appendices](#7-appendices)
   - [A. Rebuilding the pipelines](#a-rebuilding-the-pipelines)
   - [References](#references)

## 1. Introduction

An AI layer on Red Hat OpenShift AI for the migration from Red Hat Ansible Automation Platform 2.4 to a newer release (2.5, 2.6, 2.7), built on the `aap-bridge` Migration Toolkit. It turns a migration into a guided, measurable process: it predicts what will fail before anything is written, runs or follows the migration with guardrails and a human in the loop, proves that the target platform works, and learns from every migration to forecast the next one.

- **AAP Odyssey** — one page for the whole migration: a welcome card with the workflow, the journey (13 stages, the agent that owns each one, the points where a person must act) and a chat with an **orchestrator** that hands every request to one of **six specialist agents** and shows how they worked together.
- **During the migration** — OpenShift AI pipelines in two kinds. Pipelines that write nothing to the target AAP: the plan, in which the migration agent analyses the source and writes the runbook, and the dry run, which runs the runbook up to a simulated import. The migration pipeline, which writes the target AAP: a person approves it and starts it from the dashboard (or runs the migration by hand and the pipeline collects the results). Every object is migrated once.
- **After the migration** — the migration data becomes a dataset that trains a risk model shared by all migrations; the model predicts the next migration. Configuration as Code and platform smoke tests close the loop.

| Agent | Does |
| --- | --- |
| Smart Planning | detects the installed target AAP version (2.5, 2.6, 2.7...), reads the Red Hat patch releases and writes a patch plan; facts are computed by code, the priority note by the LLM |
| Migration | pre-migration analysis, plan, dry run, migration with the Migration Toolkit, failures and their causes |
| Reporting | reports of one or all migrations, grouped by phase, with their source |
| Training & Prediction | trains the risk model, predicts the next migration (Pre-migration Agent Assessment) |
| Configuration as Code | generates the Ansible code of some or all target AAP objects as Configuration as Code (CasC, `infra.aap_configuration` format) |
| Smoke & regression tests | a platform test suite with brand new objects (never migrated job templates), run by a person |

AAP Odyssey never starts a dry run or a migration: every write operation on AAP is left to human control, from the OpenShift AI dashboard. Reliable facts (versions, identifiers, counts) come from the Migration Toolkit data and are computed by code; the language model summarizes and explains them, and a note of the model that cites facts not present in the data is discarded.

## 2. Lab overview

The whole lab was built on **Amazon Web Services**. Chapter 3 gives every step, from the operators and the GPU node on AWS to AAP Odyssey, each with what to apply and how to verify it.

| Item | Value |
| --- | --- |
| Cloud | Amazon Web Services |
| OpenShift | **Red Hat OpenShift Container Platform 4.22.18**, installer-provisioned on AWS |
| Nodes | **3 control plane (master) nodes and 3 worker nodes** `m5a.8xlarge` (32 vCPU, 128 GiB), one per availability zone |
| GPU | 1 GPU node `g6.2xlarge` (1 × NVIDIA L4 24 GB) added with a GPU MachineSet; Node Feature Discovery and NVIDIA GPU Operator |
| OpenShift AI | **Red Hat OpenShift AI 3.5.2**: dashboard, AI Pipelines (Data Science Pipelines), KServe RawDeployment, vLLM NVIDIA GPU runtime |
| Storage | OpenShift Data Foundation on the workers; object storage from the Multicloud Object Gateway |
| Model | **Qwen3-8B** (FP8, Red Hat AI validated on 1 × L4) served by vLLM 0.24 with tool calling; Granite 4.0 H Tiny and gpt-oss-20b are the other validated options in `cluster/08-model/` |
| AAP | AAP 2.4 (source) and AAP 2.6 (target of the lab, AAP 2.5 and 2.7 will be supported the same way) in a second AWS account |
| Application | Image `aap-migration-ai` 0.3.15 (Migration Toolkit + `migration_ai`), built in the cluster |

### 2.1 Architecture

```
        namespace aap-migration-ai (Data Science Project)                 release notes copied
                                                                          from docs.redhat.com
  users ──► AAP Odyssey (route + OAuth) ──────── orchestrator ──┬── Smart Planning ┘
            journey · agents · chat · reports                   ├── Migration ──────────┐
                                                                ├── Reporting           │ read-only runs
                                                                ├── Training & Prediction├──► AI Pipelines (DSPA)
                                                                ├── Configuration as Code│    during · train ·
                                                                └── Smoke & regression   │    predict · casc
                       │ tools only, no agent writes to AAP                              │
                       ▼                                                                 ▼
      aap-migration-data bucket (ODF)  ◄──────────────── pipelines write ───────── toolkit + agent
      migrations/<id>/workspace|agent|casc|planning|smoke|journey, models/...           │
                                                                                         ▼
      LLM: Qwen3-8B FP8 on vLLM (KServe) ── GPU node g6.2xlarge (1 × L4)          AAP 2.4 ──► AAP 2.5-2.7
                                                                                   (second AWS account)
```

| Pipeline | What it does |
| --- | --- |
| AAP migration - during migration (plan, run, collect) | (1) AAP 2.4 export and pre-migration analysis (readiness report, dependency analysis) · (2) migration plan with the runbook · (3) dry run or migration, or collection of a migration run by hand · (4) after an import: enhanced report, training dataset, Configuration as Code, risk model training |
| AAP migration - train risk model | trains a new version of the risk model on every finished migration |
| AAP migration - predict next migration | read-only export of the next AAP 2.4 and Pre-migration Agent Assessment |
| AAP migration - Configuration as Code | `infra.aap_configuration` code of the target AAP |

#### 2.1.1 Design choices: two kinds of AI

- **LLM with tools** (Qwen3-8B on vLLM in this lab): routes requests to the agents, decides the next step of the migration agent and writes summaries and explanations. It is not trained on migration data: it reads them on demand through Python tools (state database, reports, export, risk model). It is optional: without it the agents answer deterministically and the migration agent follows a deterministic policy with the same guardrails.
- **Predictive model** (scikit-learn gradient boosting, CPU, inside the pipeline): trained on the real outcome of every migrated object, it estimates the failure probability and the likely error class of every AAP 2.4 object of the next migration.
- **No LLM fine-tuning**: one migration gives a few thousand rows, the data changes at every migration and tools keep every number exact. The dataset written by the pipeline is the starting point if fine-tuning (OpenShift AI `trainer` component) becomes useful.

#### 2.1.2 Data layout

One S3 bucket (ODF, connection *AAP migration data*) holds every migration; every pipeline step downloads the folders it needs and uploads its results, so pipelines run on any node and storage class.

```
aap-migration-data-<suffix>/
├── migrations/<migration-id>/
│   ├── workspace/
│   │   ├── exports/     RAW AAP 2.4 objects (aap-bridge export)
│   │   ├── xformed/     objects transformed for the target AAP (aap-bridge transform)
│   │   ├── reports/     pre-migration, dependencies, enhanced, validate, IAM
│   │   ├── logs/        toolkit logs
│   │   ├── database/    migration_state.db: outcome of every object (ground truth for training)
│   │   └── schemas/     API schemas of AAP 2.4 and of the target AAP (aap-bridge prep)
│   ├── agent/           plan, run summary, decision log
│   ├── dataset/         training dataset + datacard
│   ├── ai/              Pre-migration Agent Assessment (predicted migrations)
│   ├── casc/            generated Configuration as Code
│   ├── planning/        patch plans
│   ├── smoke/           smoke test suites and uploaded results
│   └── journey/         stages marked as done
├── models/aap-migration-risk/
│   ├── <version>/       model.joblib, metrics.json, model-card.md
│   └── latest.json      version in use
└── release-notes/       copied AAP patch release notes
```

### 2.2 Project folders content

| Folder | Content |
| --- | --- |
| `cluster/` | Resources to apply, numbered in application order (chapter 3): operators, GPU MachineSet and NVIDIA stack, OpenShift AI checks, project and ODF buckets, configuration, image, model, console, AAP Odyssey |
| `pipelines/` | Pipeline definitions (`aap_pipelines.py`) and the compiled YAML to import in the dashboard |
| `image/` | `Containerfile` and `prepare-context.sh` to build the image (Migration Toolkit + AI layer) |
| `migration_ai/` | `migration-ai` Python package: agents, AAP Odyssey web app, planning, smoke tests, dataset, training, predictions, CasC |

`cluster/04-project/02…08` (S4 storage) are kept for clusters without ODF and are not used in this lab.

### 2.3 Tests coverage

The following components were covered during testing on the lab (OpenShift 4.22.18, OpenShift AI 3.5.2, AWS):

- GPU MachineSet, NVIDIA stack and Qwen3-8B on vLLM with tool calling;
- ODF buckets, pipeline server, image build and Migration Toolkit console;
- AAP Odyssey with the six agents;
- The full journey on a real source AAP 2.4 and target AAP 2.6:
  - Pre-migration analysis and migration plan;
  - Migration Dry run;
  - Migration, including deliberately broken test objects;
  - IAM roles and permissions;
  - Post-migration validation;
  - Risk model training and Prediction of the next migration;
  - Generation of the Configuration as Code;
- The Smoke and Regression test suite, run against the target AAP, with its results uploaded to AAP Odyssey;
- Patch plans from the Red Hat release notes of AAP 2.5, 2.6 and 2.7.

Known limits:

- Release Notes from docs.redhat.com for AAP 2.5, 2.6, 2.7: for network reasons the release notes were copied from a workstation (section 3.12.1).

### 2.4 Security

- The only service exposed outside the cluster is AAP Odyssey, behind the OpenShift oauth-proxy: only users with access to the project get in.
- AAP Odyssey can start only read-only scans (readiness plan, prediction); its Role allows nothing else on the pipeline server.
- The model has no Route and no token: only the pods of the project call it.
- The AAP 2.4 token can be read-only (System Auditor); the target AAP token must be able to create objects (only for `mode=execute`).
- The model receives configuration data (names, errors, object fields), never secret values: exported credentials are `$encrypted$` and in the CasC they become `{{ vaulted_* }}` placeholders.

## 3. Lab installation

This section lists the steps to apply and, for each one, the checks to perform. Proceed to the next step only after all the previous steps have been completed successfully. Every command is run from the root folder of this package, with `oc` logged in as a `cluster-admin` user (not `kubeadmin`).

The target lab has the following architecture:

| Item | Value |
| --- | --- |
| Cluster | OpenShift Container Platform on AWS (installer-provisioned), 3 control plane nodes |
| Workers | 3 × `m5a.8xlarge` (32 vCPU, 128 GiB), one per availability zone |
| GPU node | 1 × `g6.2xlarge` (1 × NVIDIA L4 24 GB, 8 vCPU, 32 GiB), added in section 3.4 |
| Storage | OpenShift Data Foundation on the 3 workers (`gp3-csi`), object storage = Multicloud Object Gateway |
| OpenShift AI | already installed (checked in section 3.5) |
| AAP 2.4 and 2.6 | **Prerequisite**: AAP 2.4 (source) and AAP 2.6 (target) already installed and running, in a second lab reachable over the network from the cluster (TCP 443); this guide does not install them. Checked in section 3.12 |

### 3.1 Order at a glance

| Section | Apply | Creates | Verify |
| --- | --- | --- | --- |
| 3.2 | `git clone` of the Migration Toolkit | Migration Toolkit checkout on the workstation | branch `demojam/migration-agent` |
| 3.3.1 | `cluster/01-operators/01-nfd-operator.yaml` | Node Feature Discovery operator | CSV Succeeded |
| 3.3.2 | `cluster/01-operators/02-nvidia-gpu-operator.sh` | NVIDIA GPU Operator | CSV Succeeded |
| 3.3.3–3.3.4 | `cluster/01-operators/05-odf-operator.sh` + console wizard | ODF operator + storage cluster | StorageCluster Ready, 3 storage classes |
| 3.4.1 | `cluster/02-gpu/00-gpu-machineset.sh` | GPU MachineSet → 1 GPU node | machine Running, node Ready |
| 3.4.2–3.4.4 | `cluster/02-gpu/01…03` | NFD instance, ClusterPolicy, GPU test | `nvidia.com/gpu: 1`, `Test PASSED` |
| 3.5.1 | `cluster/03-openshift-ai/00-check-openshift-ai.sh` | — (read only) | all `[ OK ]` |
| 3.5.2 | `cluster/03-openshift-ai/02…03` | hardware profile, GPU detection in the dashboard | "NVIDIA GPU" profile |
| 3.6 | `cluster/04-project/01-namespace.yaml` + `cluster/04-project-odf/` | project, 2 buckets, 2 connections | OBCs Bound |
| 3.7 | `cluster/04-project-odf/generated/dspa.yaml` | pipeline server | DSPA Ready, port 8443 |
| 3.8 | `cluster/06-config/01…03` | Migration Toolkit .env, AI settings, LLM key | objects exist |
| 3.9 | `cluster/07-image/` + build | `aap-migration-ai` image | `migration-ai --version` |
| 3.10 | `cluster/08-model/` | Qwen3-8B on vLLM | READY, tool calling OK |
| 3.11 | `cluster/09-console/` | Migration Toolkit console | `aap-bridge --help` |
| 3.12 | `cluster/10-copilot/` | AAP Odyssey + 6 agents | 2/2 pods, egress checks OK |
| 3.13 | `pipelines/compiled/*.yaml` | 4 pipelines in the dashboard | listed in the project |

**Warning**: cert-manager, the OpenShift AI operator and the DataScienceCluster are not installed by this guide (they were already present in the lab): the check of section 3.5.1 tells you if something is missing. The same applies to the two AAP environments, AAP 2.4 (source) and AAP 2.6 (target), which must already be installed and reachable from the cluster before you start.

---

### 3.2 Download the Migration Toolkit

The image of section 3.9 is built from the `aap-bridge` Migration Toolkit. Download it from [GitHub](https://github.com/arnav300/demojam-migartion-AI-agent/tree/demojam/migration-agent), branch `demojam/migration-agent`, to a local folder of your workstation:

```bash
git clone -b demojam/migration-agent https://github.com/arnav300/demojam-migartion-AI-agent.git /path/to/toolkit-checkout
```

### 3.3 Operators

#### 3.3.1 Node Feature Discovery

```bash
oc apply -f cluster/01-operators/01-nfd-operator.yaml
```

Verify:

```bash
oc get csv -n openshift-nfd                     # nfd.*  Succeeded
```

#### 3.3.2 NVIDIA GPU Operator

```bash
bash cluster/01-operators/02-nvidia-gpu-operator.sh
```

The script picks the default channel of the `certified-operators` catalog and approves the first InstallPlan (later upgrades stay manual). Verify:

```bash
oc get csv -n nvidia-gpu-operator               # gpu-operator-certified.*  Succeeded
```

#### 3.3.3 OpenShift Data Foundation operator (skip if `oc get storagecluster -A` already shows one)

```bash
bash cluster/01-operators/05-odf-operator.sh
```

Verify:

```bash
oc get csv -n openshift-storage                 # odf-operator.* and its dependencies  Succeeded
```

In the console go to *Ecosystem → Installed Operators*. OpenShift Data Foundation should display a green tick with Status Succeeded.

#### 3.3.4 ODF storage cluster (console wizard, documented procedure)

In the Console go to *Storage → Data Foundation → Create StorageSystem*. Provide the following options:

1. **Backing storage:** *Use an existing StorageClass* → `gp3-csi`.
2. **Capacity and nodes:** requested capacity **2 TiB**. Select the **3 workers**, one per zone. Performance profile: *Balanced*.
3. Click on **Create StorageSystem**.

Verify (after \~20 minutes):

```bash
oc get storagecluster -n openshift-storage                 # ocs-storagecluster  Ready
oc get sc | grep -E "ceph-rbd|cephfs|noobaa"               # 3 new storage classes
oc get noobaa -n openshift-storage                         # noobaa  Ready (Multicloud Object Gateway)
```

In the Console, go to *Storage → Data Foundation → Storage Cluster → Storage Cluster* and check that *Data Resiliency* has a green status.

### 3.4 GPU node

#### 3.4.1 GPU MachineSet (OpenShift procedure "Adding a GPU node to an existing cluster")

First check in the AWS console (*EC2 → Instance types*, filter `g6.2xlarge`, *Availability zones*) that the instance type exists in the zone of the worker machine set the script copies. Your AWS account also needs a vCPU quota for G instances ("Running On-Demand G and VT instances" ≥ 8).

```bash
bash cluster/02-gpu/00-gpu-machineset.sh            # writes cluster/02-gpu/gpu-machineset.yaml and shows the diff
oc create -f cluster/02-gpu/gpu-machineset.yaml      # Review and Apply
```

The script copies an existing worker machine set, renames it (`worker` → `gpu`), sets `g6.2xlarge`, a 200 GiB root disk (vLLM and model images are large) and the node label `node-role.kubernetes.io/gpu`. `SOURCE=<machineset>` picks another zone, `INSTANCE_TYPE=...` another type.

Verify (after \~5-10 minutes):

```bash
oc -n openshift-machine-api get machinesets | grep gpu      # DESIRED 1  READY 1
oc -n openshift-machine-api get machines | grep gpu         # Running  g6.2xlarge
oc get nodes -l node-role.kubernetes.io/gpu                 # 1 node Ready
```

#### 3.4.2 NodeFeatureDiscovery instance

```bash
OCP=$(oc get clusterversion version -o jsonpath='{.status.desired.version}' | cut -d. -f1,2)
sed "s/:v4.20/:v$OCP/" cluster/02-gpu/01-nodefeaturediscovery.yaml | oc apply -f -
```

Verify:

```bash
oc get pods -n openshift-nfd                                          # nfd-master and nfd-worker Running
oc get nodes -l feature.node.kubernetes.io/pci-10de.present=true      # the GPU node
```

#### 3.4.3 ClusterPolicy

```bash
bash cluster/02-gpu/02-clusterpolicy.sh             # writes cluster/02-gpu/clusterpolicy.yaml
oc apply -f cluster/02-gpu/clusterpolicy.yaml       # Review and Apply
```

Verify (10–20 minutes: driver build and load):

```bash
oc get clusterpolicy gpu-cluster-policy -o jsonpath='{.status.state}{"\n"}'   # ready
oc get nodes -l nvidia.com/gpu.present=true                                   # the GPU node
oc describe node $(oc get nodes -l nvidia.com/gpu.present=true -o name | head -1) | grep -A1 "nvidia.com/gpu:"
```

`nvidia.com/gpu: 1` under both the *Capacity* and *Allocatable* fields.

#### 3.4.4 GPU test

```bash
oc apply -f cluster/02-gpu/03-gpu-test-pod.yaml
oc logs -n default cuda-vectoradd -f                 # Test PASSED
oc delete -f cluster/02-gpu/03-gpu-test-pod.yaml
```

### 3.5 OpenShift AI (Skip if already installed)

#### 3.5.1 Check

```bash
bash cluster/03-openshift-ai/00-check-openshift-ai.sh
```

It reads the operator, the DataScienceCluster (`dashboard`, `kserve`, `aipipelines` must be `Managed`), cert-manager and the vLLM GPU runtime template, and prints the exact `oc patch` for anything missing. Run it again until every line is `[ OK ]`. Only if there is no DataScienceCluster at all:

```
oc apply -f cluster/03-openshift-ai/01-datasciencecluster.yaml
```

#### 3.5.2 Hardware profile and GPU detection

```bash
oc apply -f cluster/03-openshift-ai/02-hardwareprofile-nvidia-gpu.yaml
bash cluster/03-openshift-ai/03-refresh-gpu-detection.sh
```

The second script follows the OpenShift AI 3.5 procedure after installing the GPU Operator:

- Deletes the `migration-gpu-status` ConfigMap
- Restarts the dashboard.

Verify:

```bash
oc get hardwareprofiles -n redhat-ods-applications      # nvidia-gpu
```

In RHOAI Console, go to the Dashboard → *Settings → Environment setup → Hardware profiles* → **NVIDIA GPU** enabled.

### 3.6 Project and buckets (ODF)

```bash
oc apply -f cluster/04-project/01-namespace.yaml
oc apply -f cluster/04-project-odf/01-obc-aap-migration-data.yaml
oc apply -f cluster/04-project-odf/02-obc-pipeline-artifacts.yaml
oc -n aap-migration-ai get obc                     # both Bound
bash cluster/04-project-odf/03-generate-connections.sh
oc apply -f cluster/04-project-odf/generated/connection-aap-migration-data.yaml
oc apply -f cluster/04-project-odf/generated/connection-pipeline-artifacts.yaml
```

The script reads bucket names, endpoint and keys created by ODF and writes the two connections and the pipeline server definition. Note the `AWS_S3_VERIFY_TLS` value it prints as it will be required in section 3.8.

To verify, in the RHOAI Console go to *Project* → **AAP Migration AI** → *Connections*: *AAP migration data* and *Pipeline artifacts*. The `generated/` folder contains credentials: never commit or share it.

### 3.7 Pipeline server

```bash
oc apply -f cluster/04-project-odf/generated/dspa.yaml
oc -n aap-migration-ai get dspa dspa -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}'
oc -n aap-migration-ai get svc ds-pipeline-dspa -o jsonpath='{.spec.ports[*].port}{"\n"}'
```

Expected configuration:

- `Ready=True`
- `ObjectStoreAvailable=True`
- `DatabaseAvailable=True`,
- Port **8443** in the service (API used by AAP Odyssey).
- `WebhookReady=False` and `ManagedPipelineValid=False` are correct values.

### 3.8 Configuration

```bash
cp cluster/06-config/01-secret-aap-bridge-env.example.yaml cluster/06-config/01-secret-aap-bridge-env.yaml
vi cluster/06-config/01-secret-aap-bridge-env.yaml       # SOURCE__URL/TOKEN (AAP 2.4), TARGET__URL/TOKEN (the target AAP)
oc apply -f cluster/06-config/01-secret-aap-bridge-env.yaml

vi cluster/06-config/02-configmap-migration-ai-config.yaml
#   AWS_S3_VERIFY_TLS: provide the value printed in section 3.6
#   LLM_BASE_URL / LLM_MODEL / LLM_EXTRA_BODY: set for qwen3-8b (section 3.10)
oc apply -f cluster/06-config/02-configmap-migration-ai-config.yaml
oc apply -f cluster/06-config/03-secret-migration-ai-llm.yaml
```

Verify:

```bash
oc -n aap-migration-ai get secret aap-bridge-env migration-ai-llm
oc -n aap-migration-ai get cm migration-ai-config -o jsonpath='{.data.LLM_MODEL}{"\n"}'     # qwen3-8b
```

Until section 3.10 is done the agents answer deterministically.

### 3.9 The `aap-migration-ai` image

```bash
oc apply -f cluster/07-image/01-imagestream-buildconfig.yaml
./image/prepare-context.sh /path/to/toolkit-checkout
oc -n aap-migration-ai start-build aap-migration-ai --from-dir=image/build --follow
```

- `/path/to/toolkit-checkout` is the local folder where the Migration Toolkit was downloaded in section 3.2.

Verify:

```bash
oc -n aap-migration-ai run check --rm -i --restart=Never \
  --image=image-registry.openshift-image-registry.svc:5000/aap-migration-ai/aap-migration-ai:latest -- \
  bash -c "aap-bridge premigration-report --help | head -2; migration-ai --version; migration-ai releases --help | head -2"
```

### 3.10 The model: Qwen3-8B (FP8) on the L4

```bash
bash cluster/08-model/01-create-serving-runtime.sh            # ServingRuntime vllm-cuda-runtime from the OpenShift AI template
oc apply -f cluster/08-model/02-inferenceservice-qwen3-8b.yaml
oc -n aap-migration-ai get isvc qwen3-8b -w                   # READY True after 5-15 minutes
bash cluster/08-model/05-test-tool-calling.sh                 # vLLM version, model, TOOL CALLING OK
```

**Model URL.** The test prints the `LLM_BASE_URL` to put in the ConfigMap (section 3.8). With OpenShift AI 3.5 the predictor Service is *headless* (`oc -n aap-migration-ai get svc <name>-predictor` shows `CLUSTER-IP None`): the name resolves straight to the pod, so the URL needs vLLM's port, `http://<name>-predictor.aap-migration-ai.svc.cluster.local:8080/v1`. Without `:8080` the call fails with *Connection refused* even when the model is Ready.

**Tool calling.** The InferenceService runs vLLM with `--tool-call-parser=hermes` (the parser for Qwen3) and `--max-model-len=16384`. The Qwen3 thinking mode is turned off by `LLM_EXTRA_BODY` = `{"chat_template_kwargs": {"enable_thinking": false}}`, already in the ConfigMap: with thinking on, answers are slow and contain `<think>` text. If the test prints *NO TOOL CALL*, check the parser and run the test again.

Verify in the RHOAI dashboard: Project → *Deployments* → **Qwen3-8B FP8** has Status Ready, hardware profile is set to *NVIDIA GPU*. If AAP Odyssey (section 3.12) is already running, restart its deployment so that it connects to the model:

```bash
oc -n aap-migration-ai rollout restart deployment/migration-copilot
```

### 3.11 Migration Toolkit console

```bash
oc apply -f cluster/09-console/01-toolkit-console.yaml
oc -n aap-migration-ai rollout status deployment/aap-migration-console
oc -n aap-migration-ai rsh deploy/aap-migration-console bash -c 'aap-bridge --help | head -3; migration-ai --help | head -3'
```

**[Optional] Working in the console.** `migration-ai` commands (`ls`, `ws pull/push`, `releases`, `smoke`, `ask`) are ready as soon as you enter the pod: the Deployment loads the bucket connection, the AI ConfigMap and the LLM secret as environment variables. `aap-bridge` (the Migration Toolkit) instead reads the AAP URLs and tokens from the `.env` file of the `aap-bridge-env` secret, and writes its folders in the current directory. Once per shell session:

```bash
oc -n aap-migration-ai rsh deploy/aap-migration-console bash
set -a; . /etc/aap-bridge/.env; set +a                 # AAP 2.4 / 2.6 URLs and tokens for aap-bridge
mkdir -p /work/<id> && cd /work/<id>                   # one folder per migration, before any aap-bridge command
export MIGRATION_STATE_DB_PATH=sqlite:////work/<id>/database/migration_state.db   # absolute path of the state database
```

| Command | Preparation |
| --- | --- |
| `migration-ai ...` | none |
| `aap-bridge ...` | the three lines above, in every new shell |

The full manual-migration procedure is in section 4.6.1.

### 3.12 AAP Odyssey (orchestrator + 6 agents)

```bash
oc -n aap-migration-ai create secret generic migration-copilot-proxy --from-literal=session_secret=$(openssl rand -hex 16)
oc apply -f cluster/10-copilot/01-serviceaccount.yaml
oc apply -f cluster/10-copilot/02-deployment.yaml
oc apply -f cluster/10-copilot/03-service-route.yaml
oc apply -f cluster/10-copilot/04-rbac-pipelines.yaml
oc -n aap-migration-ai rollout status deployment/migration-copilot
oc -n aap-migration-ai get route migration-copilot -o jsonpath='https://{.spec.host}{"\n"}'
```

Verify:

```bash
oc -n aap-migration-ai get pods -l app=migration-copilot      # READY 2/2
oc -n aap-migration-ai auth can-i create \
  datasciencepipelinesapplications.datasciencepipelinesapplications.opendatahub.io/dspa \
  --subresource=api --as=system:serviceaccount:aap-migration-ai:migration-copilot    # yes
bash cluster/10-copilot/05-check-egress.sh
```

The egress script runs inside AAP Odyssey pod and must print three `OK` lines:

| Line | Needed by | If it fails |
| --- | --- | --- |
| Red Hat docs | Smart Planning agent | `FAIL 403` ("Access Denied"): the CDN refuses the cluster egress IP, run `bash cluster/10-copilot/06-copy-release-notes.sh` from your workstation (section 3.12.1). No connection: cluster proxy, uncomment `HTTPS_PROXY`/`NO_PROXY` in `02-deployment.yaml` |
| AAP 2.4 (source) | pre-migration analysis, migration | network to the second lab: VPC peering or public endpoint, security group allowing the cluster egress IPs |
| target AAP | migration, CasC live, installed version for the patch plan | same as above |

The egress IPs of an AWS cluster are the Elastic IPs of its NAT gateways (AWS console → *VPC → NAT gateways* of the cluster VPC): allow them on port 443 in the security groups of both AAP.

#### 3.12.1 Copy the patch release notes (only if *Red Hat docs* shows `FAIL 403`)

docs.redhat.com can refuse the cluster egress IP and also any client that is not a real browser (curl and Python get 403/503 even from a workstation, while the browser works). The script therefore downloads the pages with a real browser driven by Playwright (your Google Chrome if installed). One-time setup on your workstation:

```bash
python3 -m venv ~/.venv-aap-docs
~/.venv-aap-docs/bin/pip install playwright
~/.venv-aap-docs/bin/playwright install chromium      # not needed if Google Chrome is installed
```

Run (a browser window opens for a few seconds per page; `--curl` uses curl instead):

```bash
bash cluster/10-copilot/06-copy-release-notes.sh      # on your workstation: oc, python3, Playwright
```

The script downloads the documentation pages where they open (your workstation), copies them to AAP Odyssey and saves them in the bucket (`release-notes/`).

Expected Behavior: one line per AAP version with the number of releases and the latest date, then *saved in the bucket*. Optional: `AAP_RELEASE_NOTES_SOURCE: "copy"` in the ConfigMap. Run it again when new patch releases are published.

### 3.13 Import the pipelines

In the RHOAI Console, go to *Develop & train → Pipelines → Pipeline definitions* → Select Project *AAP Migration AI* → **Import pipeline**, once per file:

| File | Name |
| --- | --- |
| `pipelines/compiled/aap-migration-during.yaml` | AAP migration - during migration (plan, run, collect) |
| `pipelines/compiled/aap-migration-train.yaml` | AAP migration - train risk model |
| `pipelines/compiled/aap-migration-predict.yaml` | AAP migration - predict next migration |
| `pipelines/compiled/aap-migration-casc.yaml` | AAP migration - Configuration as Code |

AAP Odyssey finds the first three by these names (`PIPELINE_READINESS_NAME`, `PIPELINE_TRAIN_NAME`, `PIPELINE_PREDICT_NAME` in `cluster/10-copilot/02-deployment.yaml`; case, spaces and punctuation are ignored). If you import them with other names, change those values too:

```
oc -n aap-migration-ai set env deployment/migration-copilot PIPELINE_READINESS_NAME="<name>"
```

Verify: the four pipelines are listed in RHOAI Console.

**Note:** after an update of the package that changes `pipelines/compiled/*.yaml`, go to the RHOAI console *Develop & train → Pipelines* → select the pipeline menu → **Upload new version** with the new file, then use that version in the next runs (the runs already done keep their version).

#### Expected result

When the steps above are completed, you can open the route that exposes AAP Odyssey (`oc -n aap-migration-ai get route migration-copilot -o jsonpath='{.spec.host}'`) and log in with your OpenShift user. If the resources were created correctly, the page shows the title **AAP Odyssey** and the header **AAP Migration on Red Hat OpenShift AI · Model: \<model>**; on the first visit the welcome card opens, and the *Agents* tab lists the specialists. To start your Migration Journey, follow chapter 4 (*Journey walkthrough*): it lists, stage by stage, the operations, the questions to ask and the pipelines to run.

### 3.14 End-to-end check of the agents

In AAP Odyssey:

| Ask | Agent | Expected |
| --- | --- | --- |
| *Hey Odyssey, who are you?* | AAP Odyssey | a short introduction (written by the code, no LLM) |
| *What's next?* | AAP Odyssey | stage x of 13, next step, owner agent, human in the loop (if required) |
| *Show the patch releases of the installed AAP* | Smart Planning | last AAP releases, the installed one, items relevant to the migration; a patch plan saved in Reports |
| *Scan AAP 2.4 for migration readiness as "wave1"* | Migration | a run of the *AAP migration - during migration (plan, run, collect)* pipeline with `mode=plan` starts (read-only): export of AAP 2.4, pre-migration report, dependency analysis and migration plan; the run is visible in the dashboard and *Scan status* follows it; the reports appear in **Reports** when it ends |
| *Show the reports of all migrations* | Reporting | table of migrations with stage and reports |
| *Generate the smoke test suite* | Smoke & regression tests | zip file, customization points, Upload results |
| *Generate the CasC of "\<Organization Name>"* | Configuration as Code | zip file; `_global` only with the shared objects Finance uses |
| *Train the risk model* | Training & Prediction | training run started |

Every answer has an **Agent activity** block (AAP Odyssey → agent, tools called, result).

## 4. Journey walkthrough

This chapter follows a migration through the 13 stages of the AAP Odyssey journey, grouped in five phases. For each stage it gives the purpose, what is done, what to ask AAP Odyssey and what that request does, the reports produced and the pipeline that runs, if any. Pipelines are started from the OpenShift AI dashboard (*Pipelines → pipeline → Create run*) unless AAP Odyssey starts them; `<id>` is the migration id, the same for every run of one migration. Stages with a human in the loop are closed with **Mark as done** in the journey, and at any stage *What's next?* returns the current stage, the next step and whether a person must act. Section 4.6 describes the four pipelines, their uses and settings.

### 4.1 Pre-migration (stages 1–4)

The pre-migration phase answers one question before anything is written: what will fail, why, and what to fix on AAP 2.4 first.

#### Stage 1 — Pre-migration analysis

**Purpose.** Know, before touching the target, how ready every object of AAP 2.4 is to migrate.

**What is done.** A read-only export of AAP 2.4, the rule-based readiness report, the dependency analysis and the migration plan written by the Migration agent.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Scan AAP 2.4 for migration readiness as "\<id>"* | The Migration agent analyses AAP 2.4 without changing it: it exports the objects, checks them against the readiness rules and their dependencies, and writes the migration plan. To do so it starts the *AAP migration - during migration (plan, run, collect)* pipeline in plan mode (read-only); the run is named `copilot-readiness-<id>-<time>` |
| *Scan status* | Shows the run and its state (Running, then Succeeded) and reloads the data when it finishes |
| *Summarize the result* | Predicted first-attempt success, objects that will fail, at risk, stale and ready |
| *What will fail and why?* | Root causes (RC codes) with the objects affected and the fix to apply before migrating |
| *Show the migration plan* | Order, phases, blocking items and the runbook |

**Reports (Pre-migration phase)**

- *Pre-migration readiness report* (Migration Toolkit): the predicted outcome of every object (ready, at risk, will fail, stale) with the rule that hit it and the fix.
- *Pre-migration dependency analysis* (Migration Toolkit): the references between objects and the missing ones that would block an import.
- *Pre-migration plan* (Migration agent): what to fix first, the migration order by waves and the runbook with the exact commands.
- *Pre-migration analysis summary* (Migration agent): a short narrative of readiness and main root causes, written by the LLM on the computed numbers.

**Pipeline.** *AAP migration - during migration (plan, run, collect)* with `migration_id=<id>`, `mode=plan`, `refresh_export=true`. In plan mode only the first two steps of the pipeline run (export with pre-migration report, and the migration plan); the steps that need migration results are skipped. Execution details are given in section 4.6.1.

#### Stage 2 — Target patch level (optional)

**Purpose.** Make sure the target AAP is on a current patch release before migrating resources to it.

**What is done.** The Smart Planning agent detects the installed version (2.5, 2.6 or 2.7) and release, reads the Red Hat patch release notes and selects the items relevant to the migration. A person decides and schedules the patching, then clicks **Mark as done**.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Show the patch releases of the installed AAP* | First line computed by code (installed release, up to date or not, newer releases), a short priority note by the LLM, then the table of the last releases |
| *Is my AAP up to date?* | Whether the installed AAP is up to date with the latest patch releases, listing the newer releases if it is not |
| *Which CVEs are fixed in the newer releases?* | The CVE ids per newer release; severity is checked on the Red Hat CVE pages, never guessed |

**Reports**

- *Patch plan* (Smart Planning agent): installed release, newer releases, migration-relevant items and patching priority; one plan per migration, replaced at each request.

**Pipeline.** None. The release notes are read from docs.redhat.com, or from the copy made with `cluster/10-copilot/06-copy-release-notes.sh` (section 3.12.1).

#### Stage 3 — Platform smoke test, before (optional)

**Purpose.** Prove that the target AAP works before the migration, and keep the result as the baseline for the regression check.

**What is done.** The Smoke & regression tests agent builds a suite that creates brand new test objects, runs them and deletes them; it never launches migrated job templates. A person customizes the suite, runs it and uploads the results on AAP Odyssey.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Generate the smoke test suite* | Builds the suite (tests chosen from the estate: survey, schedule, custom credential type only if AAP 2.4 uses them), the customization table and a zip to download; an existing suite is reused, *Regenerate the smoke test suite* builds a new one |
| *Did the smoke test pass?* | Reads the uploaded results: "Latest smoke test (pre): N passed, 0 failed" |

The person runs `ansible-playbook playbooks/smoke.yml -e smoke_phase=pre` and uploads `reports/smoke-results.json` with **Upload results** (details in section 6.4).

**Reports**

- *Smoke & regression test suite* (agent): the generated suite as a zip, with its review.
- *Platform smoke test results — before the migration* (test suite, run by a person): PASSED / FAILED / SKIPPED per test, the baseline for stage 10.

**Pipeline.** None.

#### Stage 4 — Review the plan and fix AAP 2.4 (Optional)

**Purpose.** Remove the causes of failure on the source, so the migration imports as much as possible at the first attempt.

**What is done.** A person reads the reports of stage 1, fixes the blocking objects on AAP 2.4 (missing inventories, projects, playbooks, schedule surveys) or decides to leave stale ones behind, then scans again.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *What will fail and why?* | Root causes and fixes, after the latest scan |
| *Why will job template "\<name>" fail?* | The rules that hit the object, the finding and the fix |
| *Is it safe to proceed today? Explain in three sentences.* | The orchestrator delegates to the agents and answers on the numbers of the migration |

**Reports.** The stage 1 reports, refreshed by the new scan.

**Pipeline.** *AAP migration - during migration (plan, run, collect)* again with `mode=plan`, `refresh_export=true`. The stage is done when a dry run succeeds, or with **Mark as done**.

### 4.2 Migration (stages 5–8)

The migration phase is started by a person from the RHOAI dashboard; AAP Odyssey follows it and explains it, but **never** starts it.

#### Stage 5 — Dry run

**Purpose.** Rehearse the migration without writing to the target AAP.

**What is done.** The Migration agent runs the runbook up to the dependency check and a simulated import.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Run the dry run* | Answers that AAP Odyssey does not start the dry run, with the dashboard steps and parameters; nothing is started |
| *What is the status of the migration?* | It displays the running or recent runs of the migration, the step in progress and the last agent result |
| *What's next?* | After the run: *Review the plan* and *Dry run* done, next *Approve and start the migration* |

**Reports (Migration phase)**

- *Dry-run summary* (Migration agent): the steps run, their outcome and the simulated import result.

**Pipeline.** The dry run is started by a person from the RHOAI dashboard:

1. In the RHOAI console go to *Develop & train → Pipelines → Pipeline definitions*, project *AAP Migration AI*.
2. Open *AAP migration - during migration (plan, run, collect)* and click **Create run**.
3. Set the parameters: `migration_id=<id>` (the same id as the analysis), `mode=execute`, `dry_run=true`, `refresh_export=false`; leave the others at their defaults.
4. Click **Create run** and follow the graph in *Runs*, or ask AAP Odyssey *What is the status of the migration?*.
5. When the run ends, open the *Dry-run summary* from the AAP Odyssey **Reports** button.

#### Stage 6 — Approve and start the migration

**Purpose.** Keep the decision to write to the target AAP with a person.

**What is done.** A person backs up the target AAP and starts the migration run from the dashboard.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Start the migration* | Answers that AAP Odyssey does not start the migration, with the dashboard steps (`dry_run=false`) and the reminder to back up the target AAP first |

**Reports.** None.

**Pipeline.** The migration is started by a person from the RHOAI dashboard, after backing up the target AAP:

1. In the RHOAI console go to *Develop & train → Pipelines → Pipeline definitions*, project *AAP Migration AI*.
2. Open *AAP migration - during migration (plan, run, collect)* and click **Create run**.
3. Set the parameters: `migration_id=<id>`, `mode=execute`, `dry_run=false`, `train_after=true`, `abort_failure_rate=1.0` (the default is 0.5: with 1.0 the agent never stops because of failed objects, so every object is migrated; see section 4.6.1); `include_credentials=true` to migrate the credentials (without secrets), `include_iam=true` to migrate roles and permissions.
4. Click **Create run**: stage 7 follows the run.

#### Stage 7 — Migration

**Purpose.** Move the objects to the target AAP in the Migration Toolkit order, with guardrails.

**What is done.** The migration stays in the hands of the person who approved and started it in stage 6. Inside that run, the Migration agent executes the Migration Toolkit runbook step by step (section 4.6.1): after each import it can only continue or stop, within the order and guardrails enforced by code, and it never starts a migration on its own. A person can also migrate by hand from the Migration Toolkit console; the pipeline then collects the results.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *What is the status of the migration?* | Running steps, then the last agent result and the import totals (completed, failed, skipped) |
| *Summarize the result* | Totals by status and the most frequent errors |
| *Why did job template "\<name>" fail?* | The import error, its explanation and the fix |
| *Why did the migration stop?* | The stop reason, if the agent stopped |

**Reports (Migration phase)**

- *Migration run summary* (Migration agent): steps run with their outcome, import totals, most frequent errors and the LLM summary.
- *Permissions audit (AAP 2.4)* (Migration Toolkit, with `include_iam`): the roles and permissions found on the source.
- *Permissions migration* (Migration Toolkit, with `include_iam`): the roles and permissions applied on the target.

**Pipeline.** When the Migration agent (step 2 of the *AAP migration - during migration (plan, run, collect)* pipeline) has finished the imports, the same pipeline run continues by itself with its remaining steps:

- **Step 3 — Enhanced report**: the Migration Toolkit writes the post-migration enhanced report (what was imported, failed or skipped, and why).
- **Step 4 — Training dataset**: every migrated object becomes one example (its features and real outcome) for the risk model.
- **Step 5 — Configuration as Code**: `infra.aap_configuration` code of the migrated objects, validated and zipped.
- **Step 6 — Train risk model**: a new version of the risk model, trained on every finished migration (with `train_after=true`).

If the person migrated by hand from the Migration Toolkit console instead (no agent run), steps 3–6 are obtained by starting the *AAP migration - during migration (plan, run, collect)* pipeline with `mode=collect`: it reads the results of the manual migration and runs the same four steps, without writing to AAP.

#### Stage 8 — Restore credential secrets

**Purpose.** Make the migrated credentials usable.

**What is done.** The AAP API never returns secret values, so migrated credentials arrive without them. A person enters the secret values manually on the target AAP.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *I have restored the credential secrets* | Marks the stage as done (same as **Mark as done** in the journey) |

**Reports.** None.

**Pipeline.** None.

### 4.3 Post-migration (stages 9–10)

The post-migration phase proves that the target AAP holds what was migrated and still works as a platform.

#### Stage 9 — Post-migration verification

**Purpose.** Compare source and target and explain every difference.

**What is done.** A person reads the enhanced and validation reports and spot-checks the target AAP: an organization, a project (sync), a job template (launch), a schedule.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Show the reports* | The Reporting agent lists the reports grouped by phase, each with its source (Migration Toolkit or agent) |
| *What's next?* | Verification is done when both reports exist; next: *Platform smoke test (after)* |

**Reports (Post-migration phase)**

- *Post-migration enhanced report* (Migration Toolkit): per organization, what was imported, failed or skipped, and why.
- *Post-migration validation report — \<date> · live* (Migration Toolkit): the live comparison of the target AAP against AAP 2.4.

**Pipeline.** No new pipeline is needed: both reports are produced by the migration run of stage 7.

- The *Post-migration enhanced report* is written by step 3 of the *AAP migration - during migration (plan, run, collect)* pipeline.
- The *Post-migration validation report* is written by the last step of the runbook (`validate --live`), run by the Migration agent.

If the Migration agent stopped before the end of the runbook, the validation report is missing. In that case a person:

1. runs `aap-bridge validate --live` from the Migration Toolkit console;
2. starts the *AAP migration - during migration (plan, run, collect)* pipeline with `migration_id=<id>` (the id of the current migration: with another id the pipeline finds no results) and `mode=collect` (it writes nothing to AAP), which builds the missing enhanced report, training dataset, CasC and risk model from the data of the migration.

#### Stage 10 — Platform smoke test, after

**Purpose.** Prove that the platform still works after the migration and find regressions.

**What is done.** A person runs the same suite of stage 3 in the post-migration phase, from the folder of the downloaded suite:

```bash
ansible-playbook playbooks/smoke.yml -e smoke_phase=post --ask-vault-pass
```

then uploads `reports/smoke-results.json` with **Upload results**; AAP Odyssey compares the results with the baseline of stage 3.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Did the smoke test pass?* | "Latest smoke test (post): N passed, 0 failed" |
| *Compare the smoke test results with the baseline* | "No regression against the baseline", or each test that changed status (for example "Job execution: PASSED → FAILED") |

**Reports**

- *Platform smoke test results — after the migration* (test suite, run by a person): the post-migration result, compared with the baseline.

**Pipeline.** None.

### 4.4 Training and prediction (stages 11–12)

Every finished migration teaches the risk model what really fails, so the next migration starts with a better forecast than the rules alone.

#### Stage 11 — Train the risk model

**Purpose.** Turn the real outcome of every migrated object into knowledge for the next migration.

**What is done.** A new version of the shared risk model is trained on every finished migration and evaluated against the rule-based report (details in section 5.1).

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Train the risk model* | The Training & Prediction agent starts the *train* pipeline; nothing is written to AAP |
| *Scan status* | The training run and its state |
| *Show the risk model status* | Model version, migrations used, recall on failures, whether this migration is in the training data |

**Reports (Training phase)**

- *Risk model card (latest model)* (agent): data used, metrics of the model against the rules and failures caught only by the model; the same card in every migration, because the model is shared.

**Pipeline.** The training can happen in two ways:

- **Automatically**: if the migration run of stage 6 was started with `train_after=true` (the default), its step 6 *Train risk model* has already trained the model at the end of the migration. Nothing else to run.
- **On demand**: with the current migration selected in AAP Odyssey, ask *Train the risk model*; or start the pipeline *AAP migration - train risk model* from the RHOAI dashboard with `migrations=all` (train on every finished migration, the current one included) and `threshold=0.5` (probability above which an object is "at risk"). To train on selected migrations only, list their ids in `migrations` (e.g. `<id>,<other-id>`) and include the id of the current migration, otherwise its data is left out. Use it when the migration was run with `train_after=false`, or to retrain on a selected set of migrations.

#### Stage 12 — Predict the next migration

**Purpose.** Forecast the failures of the next AAP 2.4 before anything is changed.

**What is done.** The risk model is applied to a new read-only export of the next AAP 2.4 (details in section 5.2). The migration pipeline does not predict the migration it has just run: its real results are known.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Run an AI risk prediction scan as "\<next-migration-id>"* | The Training & Prediction agent starts the *predict* pipeline on `<next-migration-id>` and closes this stage for the selected migration |
| *Scan status* | The prediction run and its state |
| *Which objects have the highest risk?* (with `<next-migration-id>` selected) | The objects with the highest failure probability and the predicted error |

**Reports (Prediction phase, in `<next-migration-id>`)**

- *Pre-migration agent assessment (AI prediction)* (agent): for every object the probability of failure and the likely error, similar objects that failed before, and the objects at risk that no rule flags.

**Pipeline.** *AAP migration - predict next migration* with `migration_id=<next-migration-id>`, `refresh_export=true`.

### 4.5 Configuration as Code (stage 13, optional)

#### Stage 13 — Configuration as Code

**Purpose.** Keep the migrated platform as code, so it can be rebuilt or managed from Git.

**What is done.** The Configuration as Code agent generates `infra.aap_configuration` code for one organization or the whole migration, validated before it is saved. Objects without an organization go to a `_global` scope applied first (details in section 5.3). A person checks and applies the code: AAP Odyssey never applies it.

| Ask AAP Odyssey | What it does |
| --- | --- |
| *Generate the CasC of the migration* | Files per scope, validated against the collection roles, and a zip to download |
| *Generate the CasC of "\<organization>"* | The same for one organization, with the global objects it uses |

**Reports**

- *Configuration as Code* (agent): each generation with its validation result, file list and zip, and `AI-REVIEW.md` written by the LLM.

**Pipeline.** Step 5 of the migration run (`source=migration`), or *AAP migration - Configuration as Code* with `source=live` to read the target AAP as it is now. The stage is done when a CasC exists, or with **Mark as done**.

### 4.6 Pipelines overview

This section describes the four OpenShift AI pipelines created in section 3.13: their purpose, when to use them and their settings. All run in the `aap-migration-ai` project with the same image; every step runs `migration-ai <command>` and publishes an artifact in the dashboard (*Develop & Train → Pipelines → Artifacts*). Only the *AAP migration - during migration (plan, run, collect)* pipeline writes to AAP, and only with `mode=execute` and `dry_run=false`. If an artifact preview says *Content is not available yet* (the browser cannot reach the internal ODF storage), open the same reports from the AAP Odyssey **Reports** button.

| Pipeline | Short name | Started from | Writes to AAP |
| --- | --- | --- | --- |
| AAP migration - during migration (plan, run, collect) | *during* | RHOAI dashboard; AAP Odyssey starts only `mode=plan` | only `mode=execute`, `dry_run=false` |
| AAP migration - train risk model | *train* | AAP Odyssey or dashboard | no |
| AAP migration - predict next migration | *predict* | AAP Odyssey or dashboard | no |
| AAP migration - Configuration as Code | *casc* | dashboard (AAP Odyssey generates CasC in the chat) | no |

#### 4.6.1 AAP migration - during migration (plan, run, collect)

**Purpose.** Drive the whole migration with one graph: plan it, run it (or collect a migration done by hand), then turn the results into reports, a training dataset (one example per migrated object, with its features and real import outcome, used to train the risk model), Configuration as Code and a new risk model.

```
1. Export + pre-migration report ── 2. Migration agent ──┐
   (prepare)                           plan | execute | collect
                                                          │ only if the migration produced results
                       ┌──────────────────────────────────┘
                       3. Enhanced report ── 4. Training dataset ──┬── 5. Configuration as Code
                                                                    └── 6. Train risk model (if train_after)
```

| Step | Command in the container | Reads | Writes (to S3) |
| --- | --- | --- | --- |
| 1 Export + pre-migration report | `migration-ai prepare` → `aap-bridge export` (only when missing or with `refresh_export`), `premigration-report`, `analyze-dependencies` | AAP 2.4 | `workspace/exports`, `workspace/reports` |
| 2 Migration agent | `migration-ai agent --mode ...` → the Migration Toolkit runbook, driven by the LLM or by the deterministic policy | reports, database, LLM, AAP 2.4 and target AAP | `workspace/*` (with `mode=execute`: also the validation report of `validate --live` and, with `include_iam`, the permissions audit and migration reports), `agent/` |
| 3 Enhanced report | `aap-bridge enhanced-report` (HTML and CSV) | database, export | `workspace/reports` |
| 4 Training dataset | `migration-ai dataset` | `exports/`, `database/` | `dataset/` |
| 5 Configuration as Code | `migration-ai casc --source migration` (+ LLM review) | export, database | `casc/<date>/` and `.zip` |
| 6 Train risk model | `migration-ai train --migrations all` | `dataset/` of every migration | `models/aap-migration-risk/<version>` |

Steps 3–6 run only when the migration imported objects (also when the agent stopped after the imports; never for a dry run). The pipeline does not predict the migration it has just run: the prediction is the *predict* pipeline, for the next AAP 2.4.

**Uses.** The `mode` parameter selects the use:

| Mode | What happens | When to use it | Journey stages |
| --- | --- | --- | --- |
| `plan` (default) | The agent analyses the reports and writes the plan with the runbook; no writes to the target AAP | Always as the first run, and after every fix on AAP 2.4 | 1, 4 |
| `execute` + `dry_run=true` | The runbook runs up to `import --check-dependencies` and `import --dry-run`; no writes | To rehearse the migration safely | 5 |
| `execute` | The agent runs the Migration Toolkit runbook and decides when to stop | When the plan is approved and the target AAP is backed up | 6, 7 |
| `collect` | A person ran the migration from the Migration Toolkit console; the pipeline collects the data and runs steps 3–6 | When you want manual control of every step | 7 |

*Migration run by the agent.* A person starts three runs of the pipeline, one after the other, always with the same `migration_id`:

1. `mode=plan`: analysis and migration plan, nothing is written (journey stage 1).
2. `mode=execute`, `dry_run=true`: rehearsal up to a simulated import, nothing is written (stage 5).
3. Back up the target AAP, then `mode=execute`, `dry_run=false`: the real migration (stages 6–7).

During the third run the Migration agent executes the runbook one step at a time. After every import it reads the most frequent errors and decides whether to continue or stop, always within the guardrails below. Every decision is recorded in `agent/agent-log.jsonl`. Follow the run from the RHOAI dashboard or by asking AAP Odyssey *What is the status of the migration?*. The pipeline runs in the cluster, so the browser can be closed.

*Manual migration.* The person runs the migration with their own hands instead of the agent, always with the same `migration_id`:

1. Start the pipeline with `mode=plan` and read the migration plan.
2. Fix the blocking objects on AAP 2.4, then run `mode=plan` again if needed.
3. In the Migration Toolkit console (section 3.11), download the migration data from the bucket (`ws pull`), run the migration with `aap-bridge`, then upload the results back to the bucket (`ws push`). The commands are below.
4. Start the pipeline with `mode=collect`: it reads the uploaded results and runs steps 3–6.

`ws push` overwrites the migration folders in the bucket with the local ones: always run `ws pull` first, and use the same `--migration-id` in both commands.

```bash
oc -n aap-migration-ai rsh deploy/aap-migration-console bash
$ set -a; . /etc/aap-bridge/.env; set +a        # AAP URLs and tokens for aap-bridge
$ mkdir -p /work/<id> && cd /work/<id>
$ export MIGRATION_STATE_DB_PATH=sqlite:////work/<id>/database/migration_state.db
$ migration-ai --work-dir /work/<id> ws pull --migration-id <id>
$ aap-bridge                      # interactive menu, or the runbook commands of the plan
$ migration-ai --work-dir /work/<id> ws push --migration-id <id>
```

**Settings.**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `migration_id` | `aap24-to-aap26` | Folder name under `migrations/`: use the same value for every run of the same migration |
| `mode` | `plan` | `plan`, `execute` or `collect` |
| `refresh_export` | `false` | `true` to export again from AAP 2.4 even when an export exists |
| `with_dependencies` | `true` | Dependency Analyzer (needs AAP 2.4 reachable) |
| `dry_run` | `false` | With `execute`: no writes to the target AAP |
| `include_credentials` | `false` | Adds `aap-bridge credentials migrate` to the runbook |
| `include_iam` | `false` | Adds `aap-bridge iam audit` and `iam migrate` (permissions) to the runbook |
| `use_llm` | `true` | `true` = the LLM decides after each import whether to continue or stop and writes the summaries; `false` = no LLM: fixed rules in the code decide (stop only above `abort_failure_rate` or on authentication/connectivity errors) and no LLM summaries are written |
| `abort_failure_rate` | `0.5` | Share of failed objects (0–1) above which the agent stops. **Set it to `1.0` in this lab**: 1.0 means 100%, a share the failed objects can never exceed, so the agent never stops because of failed objects. This value was chosen to guarantee that every object is attempted and all import phases are migrated. |
| `train_after` | `true` | Trains the model at the end of the migration (on every migration in the bucket) |
| `casc_review` | `true` | Asks the LLM to review the generated CasC (set `false` without an LLM) |

**Guardrails.** Guardrails are safety rules written in the code of the Migration agent: they limit what the agent can do and the LLM cannot change or bypass them. They are always on, whatever the model decides:

- the agent can run **only** runbook steps, never arbitrary commands, and never `cleanup` or `state reset`;
- steps follow their order and prerequisites;
- no `retry failed`: each object is migrated once, so the reports describe that single migration; failed objects are fixed on AAP 2.4 and migrated in a later migration;
- the LLM cannot stop the migration for a failed optional step when the import went well;
- if after an import the share of failed objects exceeds `abort_failure_rate` the migration stops (with the lab value `1.0` this rule never stops the migration);
- if the model does not answer, the agent continues with the deterministic policy;
- global `settings` are never imported automatically (they overwrite the target AAP configuration).

**Runbook order.** The runbook is the ordered list of Migration Toolkit (`aap-bridge`) commands that perform the migration; the Migration agent writes it in the plan and executes it in `mode=execute`. The order matters because objects depend on each other (a job template needs its project, inventory and credentials to exist first), and it is the same order recommended in the Migration Toolkit README, section *Migration Order and Dependencies*:

1. **Preparation** (`prep`, pre-migration report, `transform`): reads the API schemas of both AAPs, checks readiness and converts the exported objects to the format of the target AAP. Nothing is written yet.
2. **Import phase 1 — foundations**: organizations, labels, users, teams, credential types, credentials, execution environments, inventories, inventory sources, groups, hosts, instance groups, projects. These are the objects everything else depends on.
3. **Credentials** (only with `include_credentials`, command `credentials migrate`): creates the credentials still missing on the target, **without secret values**, which a person re-enters in stage 8.
4. **Phase 2 — project details** (`patch-projects`): completes the projects with their SCM settings (repository URL, branch, SCM credential).
5. **Import phase 3 — automation**: notifications, job templates, workflows, schedules, applications. They come after phase 1 because they reference projects, inventories and credentials.
6. **Roles and permissions** (only with `include_iam`, last because permissions need every object to exist). Commands run:

   ```bash
   aap-bridge iam audit --scan-strategy principal --workers 10 --no-verify-ssl \
     --output-dir ./reports/local_audit/ --checkpoint-dir ./reports/local_audit/
   aap-bridge iam migrate --scan-strategy principal --workers 10 --no-verify-ssl \
     --output-dir ./reports/local_migrate/ --checkpoint-dir ./reports/local_migrate/
   ```
7. **Final reports**: the enhanced report (what was imported, failed or skipped, and why) and `validate --live` report (live comparison of the target AAP with AAP 2.4). With `include_iam`, the reports of step 6 are also available: *Permissions audit (AAP 2.4)* (`reports/local_audit/`, the roles and permissions found on the source) and *Permissions migration* (`reports/local_migrate/`, the roles and permissions applied on the target). All of them are listed in AAP Odyssey **Reports**, Migration and Post-migration phases.

If the agent stops after the imports, the pipeline still runs steps 3–6; run the missing steps (IAM, `validate --live`) from the console.

**Results to read.** Every run of the pipeline leaves these files in the migration folder of the bucket (`migrations/<id>/`). The first two are also listed in AAP Odyssey **Reports**:

- **Migration plan** (`agent/migration-plan.md`), written in `mode=plan`: what to fix on AAP 2.4 first, which objects will fail, and the order in which to migrate them. Read it before the dry run.
- **Run summary** (`agent/agent-summary.md`), written at the end of a dry run or migration: each runbook step with its outcome, import totals (completed, failed, skipped), the most frequent errors and a short summary written by the LLM. Read it to know how the run went.
- **Decision log** (`agent/agent-log.jsonl`): one line for every event of the run, such as a step started or ended, an LLM decision, or a stop by a guardrail. It is the full trace of what the agent did and why; read it when you need to explain a decision.
- **Migration Toolkit logs** (`workspace/logs/agent/NN-<step>-<n>.log`): the complete output of every `aap-bridge` command the agent ran. Read them to troubleshoot a failed step.

#### 4.6.2 AAP migration - train risk model

**Purpose.** Retrain the shared risk model on the datasets of finished migrations, without running a migration again; it is the same training as step 6 of *AAP migration - during migration (plan, run, collect)*.

**Uses.** Normally the model is trained automatically at the end of every migration (step 6 of *AAP migration - during migration (plan, run, collect)*). Start this pipeline when you need to train it separately:

- the migration was run with `train_after=false`, so the model does not include it yet;
- you want to train only on selected migrations, for example to leave test migrations out (`migrations=<id1>,<id2>`);
- you want to retrain on a schedule, as more migrations are finished.

It can be started from the RHOAI dashboard or by asking AAP Odyssey *Train the risk model* (journey stage 11).

**Settings.**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `migrations` | `all` | `all`, or comma-separated migration ids |
| `threshold` | `0.5` | Probability above which an object is "at risk" (0.3 = more warnings) |

**Result.** Each successful run creates a new version of the risk model in the bucket (`models/aap-migration-risk/<version>/`), together with its model card, which AAP Odyssey shows in **Reports**. The new version replaces the one in use for the next predictions.

The model needs enough examples to learn from: at least 30 migrated objects with a known outcome, of which at least 5 failed. With less data the run still ends without errors, but no model is created and the log says `not_enough_data`. Details in section 5.1.

#### 4.6.3 AAP migration - predict next migration

**Purpose.** Apply the risk model in use to the next AAP 2.4 to migrate and produce the *Pre-migration Agent Assessment*; it writes to no AAP.

**Uses.** Before planning a new migration, once at least one migration has trained the model. From AAP Odyssey: *Run an AI risk prediction scan as "\<next-migration-id>"* (journey stage 12).

**Settings.**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `migration_id` | `next-migration` | Name of the next migration, e.g. `next-wave-01` |
| `refresh_export` | `true` | `true` = new export from the AAP in `SOURCE__URL`; `false` = reuse the export already in `migrations/<id>/workspace/exports` |

Details in section 5.2.

#### 4.6.4 AAP migration - Configuration as Code

**Purpose.** Generate the Configuration as Code of the target AAP in the `infra.aap_configuration` format (`dispatch` role), validated before it is saved; it reads the target AAP but does not change it.

**Uses.** To manage the target AAP as code after the migration (`source=live`), or to get the code of the migrated objects only (`source=migration`, also step 5 of *AAP migration - during migration (plan, run, collect)*). From AAP Odyssey: *Generate the CasC of "\<organization>"* (journey stage 13).

**Settings.**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `migration_id` | — | Migration whose folder receives the code |
| `source` | `live` | `live` = the target AAP as it is now; `migration` = from the exports, only objects imported successfully |
| `organization` | empty (all) | One organization only |
| `resource_types` | empty (all) | e.g. `job_templates,projects` |
| `include_hosts` | `false` | Hosts of static inventories |
| `review` | `true` | LLM review written to `AI-REVIEW.md` |

Output, verification and how to apply the code: section 5.3.

## 5. Post-migration Operations

After the migration OpenShift AI gives you four tools:

| Tool | Where | Answers |
| --- | --- | --- |
| **AAP migration - train risk model** pipeline | dashboard | "Retrain the model with every migration done so far" |
| **AAP migration - predict next migration** pipeline | dashboard or AAP Odyssey | "What will fail if I migrate *this* AAP?" (enhanced-report style report) |
| **AAP migration - Configuration as Code** pipeline | dashboard | "Give me the Configuration as Code of the target AAP" |
| **AAP Odyssey** | `migration-copilot` route | Questions about a migration, reports, read-only scans, CasC download |

### 5.1 Risk model

#### What it learns

Every row of the dataset is an AAP 2.4 object (job template, project, inventory, credential, schedule, notification, workflow...). The **features** are generic properties of the object, for example: type, number of survey questions, presence of `limit`, days since the last job, status of the last job, number of workflow nodes, pre-migration rules that hit it and their severity. **Never** names, IDs or organizations: the model must work on an AAP it has never seen.

The **label** is the real import outcome in the Migration Toolkit database (`migration_state.db`): succeeded, failed or skipped. Transient errors (timeouts, 502/503) are left out: they do not depend on the object and would teach noise.

The model is a scikit-learn *gradient boosting* classifier with balanced classes, plus a second model for the likely **error class** and a search for **similar objects** that already failed, used to explain every prediction.

#### Reading the model card

The artifact of the *Train risk model* step (and `models/aap-migration-risk/<version>/model-card.md`, also in AAP Odyssey **Reports** → *Model card*) contains:

| Item | How to read it |
| --- | --- |
| Rows / migrations used | Below 30 labelled rows, or fewer than 5 failures, the model is not trained (`not_enough_data`): more migrations are needed |
| **Recall on failures** | Share of really failed objects the model would have flagged. The most important number: an unexpected failure costs more than a false alarm |
| Precision | Share of correct warnings |
| F1, ROC AUC | Overall quality (1 = perfect, 0.5 AUC = random) |
| **Comparison with the rules** | The same metrics computed with the pre-migration rules only. If the model does not beat the rules, rely on the report |
| Most important features | What drove the predictions. "Strange" features mean the data needs a look |
| Error classes | Which error types the model can recognise (at least 3 examples per class) |

The metrics come from 5-fold *cross-validation*: the dataset is split into 5 parts, the model trains on 4 and predicts the fifth, in rotation, so every object is predicted by a model that did not see it. The objects still come from the same AAP, so the measure is honest but somewhat optimistic; the final measure comes with a second migration (train on the first, predict the second).

#### How to tell whether training succeeded

A green node is not enough: `not_enough_data` also ends without errors. Look at the last line of the step log:

```
RESULT: TRAINED - model v20261021-083017 on 202 objects; recall(failed)=0.702 vs rules 0.426
RESULT: NOT TRAINED (not_enough_data) - Need at least 30 labelled objects ...
```

`RESULT: TRAINED` means a new model was created; `RESULT: NOT TRAINED` means there was not enough data and the model in use did not change. When the model was trained, two more checks confirm that it is the one now in use:

1. Just before the `RESULT` line, the step log shows `model published: s3://…/models/aap-migration-risk/<version> (latest.json updated)`: the new version was saved in the bucket and `latest.json` now points to it, so the next predictions use it.
2. From the Migration Toolkit console, `migration-ai ls models/aap-migration-risk` lists the model versions in the bucket: the new one must be there.

#### When to retrain

The *Train* step of the *AAP migration - during migration (plan, run, collect)* pipeline retrains after every finished migration (`train_after=true`). To do it by hand or on a schedule:

- **Once**: *Develop & train → Pipelines → AAP migration - train risk model → Create run*, `migrations=all` (or a comma-separated list), `threshold=0.5`.
- **Scheduled**: *Create schedule* instead of *Create run*, e.g. every Monday. Useful when several teams migrate into the same bucket.

Every training writes a new version and updates `latest.json` only when it succeeded. To go back to a previous version, edit `latest.json` in the bucket. AAP Odyssey picks up a new model within a minute.

`threshold` sets the probability above which an object is "at risk": lower it (0.3) for more warnings and fewer surprises, raise it (0.7) for a shorter report.

### 5.2 Next-migration prediction

**AAP migration - predict next migration** pipeline:

| Parameter | Meaning |
| --- | --- |
| `migration_id` | Name of the new analysis (e.g. `next-migration`, `dc2-2026-11`) |
| `refresh_export` | `true` (default): export from the AAP in `SOURCE__URL` of the `aap-bridge-env` secret. `false`: reuse an export already in `migrations/<id>/workspace/exports` |

The prediction can be started in two ways:

- **From AAP Odyssey**: ask *Run an AI risk prediction scan as "\<next-migration-id>"*. AAP Odyssey starts the pipeline with a new export of AAP 2.4. Add *without export* at the end of the request to reuse an export already in the bucket (`refresh_export=false`).
- **From the RHOAI dashboard**: start *AAP migration - predict next migration* with the parameters in the table above.

The pipeline exports the AAP 2.4 configured in the `aap-bridge-env` secret (`SOURCE__URL`, `SOURCE__TOKEN`). To predict a **different** AAP 2.4 from the one already migrated, either update these two values in the secret before the run, or upload the export of that AAP to `migrations/<next-migration-id>/workspace/exports` in the bucket and run with `refresh_export=false`.

The report (step artifact, AAP Odyssey **Reports**, and `migrations/<id>/ai/ai-readiness-report.{html,json,csv}`) has the same layout as the Migration Toolkit pre-migration report, plus:

- an **AI** rule for objects whose failure probability is above the threshold, even when no rule flags them;
- for every object: failure probability, most likely error and similar objects that failed in past migrations;
- at the top: model version, metrics and number of objects at risk by type.

Use it like the pre-migration report: fix first the objects with the highest probability, then plan the waves. It does not replace the rules: it includes and extends them. The rules say **why** an object will fail and how to fix it; the model says **how likely** it is, learning from the real failures of your migrations.

### 5.3 Configuration as Code

The CasC uses the **infra.aap\_configuration** collection (`dispatch` role). **AAP migration - Configuration as Code** pipeline:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `migration_id` | `aap24-to-aap26` | Where to store the result (`migrations/<id>/casc/`) |
| `source` | `live` | `live`: reads the target AAP (`TARGET__*` of the `.env`), i.e. the real state after the migration. `migration`: from the exports, only objects imported successfully |
| `organization` | empty | Only this organization (empty = all) |
| `resource_types` | empty | E.g. `job_templates,projects` (empty = all) |
| `include_hosts` | `false` | Include hosts of static inventories |
| `review` | `true` | The LLM reviews the CasC and flags issues (set `false` without an LLM) |

The generated code is saved in the bucket and can be downloaded as a `.zip` from AAP Odyssey **Reports**. It is organised to be committed to a Git repository as it is, but it is a proposal: a person always reviews it (and the LLM notes in `AI-REVIEW.md`), checks it against the target AAP and decides whether to apply it. AAP Odyssey never applies it. The structure is:

```
casc/<date>/
├── configure.yml          playbook: infra.aap_configuration.dispatch
├── requirements.yml       required collections
├── env_variables.yml      secret values to fill in (vaulted_* = CHANGE_ME)
├── orgs/_global/          shared objects (organizations, credential types, EEs, instance groups)
├── orgs/<organization>/   projects, inventories, credentials, job templates, workflows, schedules, teams...
├── casc-summary.json      counts, secrets to fill in, notes and validation result
└── README.md
```

Verify it:

1. `casc-summary.json`: `validation.errors` empty; read the `warnings` and the list of secrets.
2. Compare 2–3 job templates, a project and a schedule with the target AAP UI.
3. Run it in check mode, without changes:

```bash
ansible-galaxy collection install -r requirements.yml
export AAP_HOSTNAME=https://aap26.example.com AAP_TOKEN=<token> AAP_VALIDATE_CERTS=false
ansible-playbook configure.yml -e casc_scope=_global --check
ansible-playbook configure.yml -e casc_scope=<organization-folder> --check
```

With `source=live` you expect all `ok` and no `changed`, except credentials whose secrets are still placeholders.

Apply it (after filling in the real secrets):

```bash
ansible-vault encrypt env_variables.yml
ansible-playbook configure.yml -e casc_scope=_global --ask-vault-pass
ansible-playbook configure.yml -e casc_scope=<organization-folder> --ask-vault-pass
```

Every file is validated against the fields allowed by the collection roles and against references between objects (a job template using a missing project is an error; a workflow node pointing to a template that was not migrated is a warning).

Objects without an organization (credential types, instance groups, execution environments and credentials without organization) go to the `_global` scope. With an `organization` filter, `_global` keeps only the shared objects that organization uses (details in chapter 6).

*Why two sources?* `live` describes what really is on the target AAP (including manual fixes made after the migration): the right baseline to manage the target AAP as code from now on. `migration` is useful when the target AAP is not reachable or to compare.

### 5.4 AAP Odyssey web app

Open the `migration-copilot` route, select the migration at the top and write in English. Without an LLM AAP Odyssey gives automatic answers built from the data; with an LLM it answers freely, using the same tools. The interface uses Red Hat PatternFly 6 (served by AAP Odyssey itself: no Internet access needed).

#### Migration selector

The menu at the top lists every migration in the bucket, **newest first**, with the date of its last activity (agent result, pre-migration report or AI report). The list is reloaded when you open the menu, with **Refresh**, and while a pipeline is running; the data of a migration is synced from the bucket every 30 seconds (files deleted in the bucket disappear from AAP Odyssey too). Changing migration resets the conversation, so answers never mix two migrations.

#### Migration journey, agents and human in the loop

The left panel has two tabs. **Journey** shows the whole process as **13 stages**, each with its state for the selected migration (done, next, running, failed, skipped, to do), the **agent** that owns it and the orange **Human in the loop** label where a person must act. **Agents** lists the six specialist agents with their tools and what they may write. Stages, agents and the routing are described in chapter 6.

The state comes from what exists for the migration (reports, state database, agent result, model, CasC, patch plan, uploaded smoke test results) and from its pipeline runs, so it is the same whoever started them. *Restore credential secrets* stays as a reminder after every real migration: secret values cannot be read from the AAP API. Optional stages (*Target patch level*, *Platform smoke test (before)*, *Configuration as Code*) never block the journey.

The box above the chat says **where we are** (stage *x* of 13 and phase), the **next step** with how to do it, the owner agent and whether a **human action** is required. Every answer ends with the same information and an **Agent activity** block (AAP Odyssey → agent, tools called, result). Ask *What's next?* for the full explanation.

| Ask | Answer |
| --- | --- |
| *What's next?* / *Where are we?* | current stage, next step and how to do it, owner agent, human action required, reminders |
| *Show the patch releases of the installed AAP* (or *Show the AAP 2.7 patch releases*) | Smart Planning agent: last releases, installed version, newer releases, items relevant to the migration, patch plan |
| *Generate the smoke test suite* / *Compare the smoke test results with the baseline* | Smoke & regression test agent: suite to download and run, results and regressions |
| *Show the reports of all migrations* | Reporting agent: every migration with stage, next step and reports |
| *Train the risk model* | Training & Prediction agent: starts the training pipeline |
| *Summarize the result* | predicted readiness, main root causes with their fix, import results |
| *What will fail and why?* (or *… in "\<organization>"*) | root causes + objects that will fail |
| *Show the migration plan* | the agent's plan with the runbook |
| *Why will / did "\<object>" fail?* | rules that hit it and the fix; after the migration also the real error and its explanation |
| *Which objects have the highest risk?* (or *… of "\<organization>"*) | the model's predictions |
| *What is the status of the migration?* | running or recent runs (dashboard runs too), the step in progress, the last agent result, import totals |
| *Generate the casc of "\<organization>"* | CasC from the migration data, zip to download |
| *Write the CasC of a new job template "\<name>", same as "\<existing>" but with limit \<pattern>* | written and validated by the LLM (needs an LLM) |

#### Reports from AAP Odyssey

The **Reports** button asks the **Reporting agent** for the reports of the selected migration, **grouped by phase**. Every report shows its title, who generated it (**Toolkit**, the **agent** that wrote it, or **Test suite (run by a human)**), the generation date and a file name that carries the migration and the phase, e.g. `wave1__post-migration-enhanced-report.html`:

| Phase | Report (file name) | Source |
| --- | --- | --- |
| Pre-migration | `pre-migration-readiness-report`, `pre-migration-dependency-analysis` | Toolkit |
| Pre-migration | `pre-migration-plan`, `pre-migration-analysis-summary` | Migration agent |
| Pre-migration | `pre-migration-patch-plan-aap-26-<date>` | Smart Planning agent |
| Pre-migration | `pre-migration-smoke-test-results-pre-<date>` | Test suite (run by a human) |
| Migration | `migration-dry-run-summary`, `migration-run-summary`, `migration-collect-summary` | Migration agent |
| Migration | `migration-permissions-audit`, `migration-permissions-report` | Toolkit |
| Post-migration | `post-migration-enhanced-report`, `post-migration-validation-report-<date>-<scope>` | Toolkit |
| Post-migration | `post-migration-smoke-test-results-post-<date>` (or `-patch-`) | Test suite (run by a human) |
| Training | `training-risk-model-card` (latest model) | Training & Prediction agent |
| Prediction | `prediction-agent-assessment` (*Pre-migration agent assessment*) | Training & Prediction agent |

Reports open in a new tab. The **Configuration as Code** section lists every generation (pipeline or chat) with **Download zip** (`<migration>__casc__<date>.zip`), the validation result and a preview of each file; **Smoke & regression test suites** lists every generated suite (`<migration>__smoke-tests__<date>.zip`). Reports go through AAP Odyssey login, so they open even when the dashboard artifact preview cannot read the internal storage.

#### Pipeline runs from the chat

The agents can start three pipelines that never write to AAP, even without an LLM:

| Write | What starts | Writes to AAP? |
| --- | --- | --- |
| *Scan AAP 2.4 for migration readiness as "\<id>"* | *AAP migration - during migration (plan, run, collect)* pipeline with `mode=plan`: export from AAP 2.4, pre-migration report, Dependency Analyzer, plan | no |
| *Run an AI risk prediction scan as "\<id>"* | *predict* pipeline: Pre-migration agent assessment of the next AAP 2.4, from the trained model; the *Predict the next migration* stage of the selected migration becomes done | no |
| *Train the risk model* | *train* pipeline on every migration (`migrations=all`) | no |
| *Scan status* | state of the last scan; when it finishes AAP Odyssey reloads the data and summarizes the results | — |

The migration name goes in quotes (lowercase letters, digits and `-`); without it the selected migration is used. The run also appears in the dashboard (`copilot-<kind>-<migration>-<date>`, with the user who started it in the description). If a scan of the same kind is already running on the same migration, AAP Odyssey does not start a second one.

The **migration** never starts from the chat: start it from the dashboard (*AAP migration - during migration (plan, run, collect)* pipeline, `mode=execute`). Scan parameters are fixed in the code, not chosen by the model.

Limits: AAP Odyssey sees only the data in the bucket (export, database, reports, logs) and the predictive model; it cannot write to AAP; answers about "why" are as good as the error messages recorded by the Migration Toolkit.

### 5.5 Learning loop

```
migration N ──► dataset N ──► train (all) ──► model vK ──► predict migration N+1
     ▲                                                          │
     └───────────── fixes, agent plan ◄─────────────────────────┘
```

The more migrations you run, the more the model learns the typical errors of your organization. With many migrations (tens of thousands of rows) the dataset in `migrations/*/dataset/` is also the starting point to evaluate fine-tuning an LLM with the OpenShift AI *trainer* component.

## 6. AAP Odyssey Agent Breakdown

The AAP Odyssey page (journey on the left, chat on the right) is the main page of the demo. On the first visit a **welcome card** shows the journey in five phases (13 stages), the agent that owns each stage and where a person acts; *Start* closes it, **How it works** in the header opens it again. Behind the page **AAP Odyssey is an orchestrator**: it hands every request to the specialist agent that owns it and shows the interaction under the answer (**Agent activity**: AAP Odyssey → agent, tools called, result → AAP Odyssey).

### 6.1 Agents and tools

| Agent | Owns | Tools | May write |
| --- | --- | --- | --- |
| **Smart Planning** | target patch level (pre-migration) | `patch_releases`, `installed_version` | nothing: reads docs.redhat.com and the anonymous ping of the target AAP |
| **Migration** | pre-migration analysis, plan, dry run, migration runs, failures | `start_readiness_scan`, `scan_status`, `migration_status`, `overview`, `readiness`, `migration_plan`, `search`, `explain`, `journey_status` | the target AAP, only through the pipeline *AAP migration - during migration (plan, run, collect)* started by a human from the dashboard |
| **Reporting** | reports of one or all migrations | `list_reports`, `migrations_overview`, `overview` | nothing |
| **Training & Prediction** | risk model, AI readiness report | `model_status`, `start_training`, `start_prediction`, `predict`, `scan_status` | the risk model in the bucket (training pipeline) |
| **Configuration as Code** | `infra.aap_configuration` code of some or all target AAP objects | `generate_casc`, `casc_schema`, `validate_casc` | files (zip in the bucket); a human applies them |
| **Smoke & regression tests** | platform test suite and its results | `generate_smoke_suite`, `smoke_results` | files; the suite is run by a human |

The tool subset of each agent is enforced in the code (`migration_ai/agents.py`), not in the prompt: the Reporting agent cannot start a pipeline, no agent can write to AAP. The *Agents* tab of the page lists the same table, with a *Try* button per agent.

### 6.2 Request routing

1. **Known requests** (reports, patches, smoke tests, CasC, training, scans, status, plan, readiness, "what's next") go to their agent through a deterministic router: fast, and the same with or without an LLM.
2. With an LLM, the specialist answers with **its own** tool loop and prompt; the requests that produce a structured result (report lists, patch plan, test suite, CasC, pipeline starts) stay deterministic, and the LLM only writes the summaries (patch plan, suite review).
3. **Free questions** the router does not recognise go to the LLM orchestrator, which delegates with one tool per agent (`ask_planning`, `ask_migration`, ...) and composes the final answer from what the agents returned.
4. Without an LLM every agent answers deterministically and the trace still shows which agent did what.

#### Questions that work without a migration

Before the first pre-migration analysis (or with no migration selected) the journey shows all 13 stages and AAP Odyssey answers the requests that do not depend on a migration: the patch releases of the installed AAP, the installed AAP version, the reports of all migrations, the risk model status and training, the smoke test suite (generic, without the estate features) and *What's next?*. What they generate stays in AAP Odyssey pod and is not copied to the bucket. Every other question asks to choose a migration or to start one, e.g. *Scan AAP 2.4 for migration readiness as "test-migration-01"*.

### 6.3 Smart Planning agent

Ask *Show the patch releases of the installed AAP*. The agent reads the controller version of the target AAP (anonymous ping) and maps it to the AAP version (controller 4.6 = AAP 2.5, 4.7 = AAP 2.6; newer versions are recognised from the component table of the release notes), so the same question works whether 2.5, 2.6 or 2.7 is installed. To read another version, name it: *Show the AAP 2.7 patch releases*.

| Version | Page read |
| --- | --- |
| 2.7 | `…/red_hat_ansible_automation_platform/2.7/whats_new-async_updates` |
| 2.6 | `…/red_hat_ansible_automation_platform/2.6/whats_new-async_updates` |
| 2.5 | `…/red_hat_ansible_automation_platform/2.5/html/release_notes/patch_releases` |

Rules:

- releases dated **after today** if any; the published pages list released versions only, so normally there are none and the agent shows **the last three releases published up to today**;
- when the version read is the installed one, the agent finds the **installed release** (controller version from `/api/controller/v2/ping/`, no credentials) and lists every **platform release newer than the installed one**;
- from the notes of those releases it selects the items **relevant to the migration** (migration, RBAC and roles, credentials and encryption, authentication and mappings, organizations, users, import and backup) and ignores tech preview components (MCP server, Metrics Service);
- with an LLM it adds a **patch-scheduling summary**: what to apply, priority, preparation (backup, operator update in a maintenance window, smoke test after). Dates, versions, IDs and CVEs come only from the parser; the summary must cite the AAP-xxxxx / CVE ids; CVE severity is not in the release notes and is never guessed.

The plan is saved (`migrations/<id>/planning/patch-plan-<version>-<date>.md`) and appears in **Reports** (pre-migration phase); the journey stage *Target patch level* becomes done. Patching itself is decided and scheduled by a human.

Pages are cached for `AAP_RELEASE_NOTES_CACHE_HOURS` (6 by default); if docs.redhat.com is unreachable the last cached copy is used.

If docs.redhat.com refuses the cluster (HTTP 403 "Access Denied": the CDN can block the egress IP of a cloud cluster), run `bash cluster/10-copilot/06-copy-release-notes.sh` from a workstation where the documentation opens. It downloads, with a real browser (Playwright), the index and release pages, copies them to AAP Odyssey cache and saves them in the bucket (`release-notes/`), where the agent finds them after a restart. The patch plan then says when the pages were copied; run the script again when new patch releases are published. `AAP_RELEASE_NOTES_SOURCE: "copy"` in the ConfigMap skips the requests to docs.redhat.com altogether. From the console: `migration-ai releases --installed 4.7.16` (version detected) or `--version 2.7`.

### 6.4 Smoke & regression test agent

Ask *Generate the smoke test suite*. The agent builds a suite that creates **brand new objects** in a dedicated organization (`<prefix>-org`), runs them, checks the results and deletes them. It **never launches migrated job templates**.

Only an explicit request (*generate*, *create*, *build*, *new*, *regenerate*) produces a suite, and a suite that already exists is reused: every suite is a zip in **Reports**. *Regenerate the smoke test suite* makes a new one. Any other question about smoke tests or regressions (*Did the smoke test pass?*, *Any regression?*, *Run the smoke tests now*) is answered from what exists: the suite, the uploaded results, how a person runs it.

| # | Test | What it proves |
| --- | --- | --- |
| 01 | Platform availability | gateway, controller (hub, EDA) answer; controller version |
| 02 | Authentication | the admin logs in through the gateway |
| 03 | Create test objects | organization, project, inventory, host, job template, workflow, created with `infra.aap_configuration` |
| 04 | Project synchronization | SCM access from the execution plane |
| 05 | Job execution | a job runs in an execution environment and succeeds |
| 05b | Survey job | only if the estate uses surveys |
| 06 | Workflow execution | the workflow engine runs a node |
| 07 | Schedule (disabled) | only if the estate has schedules |
| 08 | Custom credential type | only if the estate has custom credential types |
| 99 | Cleanup | reverse dependency order, with `ansible.controller` modules |

Tests 05b, 07, 08 mirror the features the migrated estate uses (read from its export), with new objects. Every test records PASSED / FAILED / SKIPPED and the run never stops at the first failure.

**Human in the loop:** download the zip, set the customization points the agent lists (AAP host and credentials in a vaulted file, a Git repository reachable from the target AAP, the test playbook, optional execution environment, Automation Hub token for the certified collections), then:

```bash
ansible-galaxy collection install -r requirements.yml -p collections/
ansible-vault encrypt vars/connection.yml
ansible-playbook playbooks/smoke.yml -e smoke_phase=pre --ask-vault-pass      # pre | post | patch
```

Upload `reports/smoke-results.json` with **Upload results** in the agent's answer. The journey stages *Platform smoke test (before)* / *(after)* become done (or failed), the results appear in **Reports**, and *Compare the smoke test results with the baseline* lists every test whose status changed: that is the regression check after a patch (`smoke_phase=patch`).

From the console: `migration-ai smoke --migration-id <id>`.

### 6.5 Reporting agent

*Show the reports* (or the **Reports** button) lists the reports of the selected migration grouped by phase, each with its source: **Toolkit**, an **agent** (Migration, Smart Planning, Training & Prediction, Configuration as Code, Smoke & regression tests) or **Test suite (run by a human)**. *Show the reports of all migrations* lists every migration with its last activity, journey stage, next step and number of reports, with *Open reports* per migration.

### 6.6 Configuration as Code agent

Credential types, instance groups, execution environments without organization, credentials without organization (personal credentials) and users have no organization: they go to the `_global` scope, applied first (`ansible-playbook configure.yml -e casc_scope=_global`). Built-in objects (managed credential types and execution environments, the `controlplane` and `default` instance groups) are never exported.

With an organization filter (*Generate the CasC of "Finance"*), `_global` keeps **only the shared objects that organization uses**: the credential types of its credentials, the execution environments and instance groups of its templates, projects and inventories, and the credentials without organization referenced by its templates. The README of the generated code lists what was left out.

### 6.7 Journey stages and owners

| # | Stage | Agent | Human in the loop |
| --- | --- | --- | --- |
| 1 | Pre-migration analysis | Migration | — |
| 2 | Target patch level (optional) | Smart Planning | decide and schedule the patching |
| 3 | Platform smoke test, before (optional) | Smoke & regression tests | customize, run, upload |
| 4 | Review the plan and fix AAP 2.4 | Migration | review and fix |
| 5 | Dry run | Migration | start the dry run pipeline from the RHOAI dashboard |
| 6 | Approve and start the migration | Migration | approve and start |
| 7 | Migration | Migration | the migration pipeline was started by a person in stage 6; follow the run (it writes the target AAP) |
| 8 | Restore credential secrets | Migration | re-enter the secrets |
| 9 | Post-migration verification | Reporting | review and spot-check |
| 10 | Platform smoke test, after | Smoke & regression tests | run, upload |
| 11 | Train the risk model | Training & Prediction | — |
| 12 | Predict the next migration | Training & Prediction | — |
| 13 | Configuration as Code (optional) | Configuration as Code | check and apply |

Optional stages never block the journey: the next step is the first required one, and the optional stages before it are shown as *Optional before*; an optional stage left behind when a later stage is done is shown as *Skipped*.

## 7. Appendices

### A. Rebuilding the pipelines

`aap_pipelines.py` defines the four pipelines (Kubeflow Pipelines v2); the files in `compiled/` are ready to import in the OpenShift AI dashboard for the `aap-migration-ai` namespace.

Recompile only if you change the namespace, the image or the pipeline code:

```bash
python3.11 -m venv .venv-kfp && . .venv-kfp/bin/activate
pip install "kfp==2.14.3" "kfp-kubernetes==2.14.3"
MIGRATION_AI_NAMESPACE=aap-migration-ai python aap_pipelines.py      # rewrites compiled/*.yaml
```

Use a separate virtualenv: kfp has dependencies that conflict with the Migration Toolkit's.

After recompiling, in the dashboard: *Pipeline definitions → pipeline → Upload new version*.

Every step runs `migration-ai <command>` in the `aap-migration-ai` image and mounts: the `aap-migration-data` connection (S3), the `aap-bridge-env` secret (`/etc/aap-bridge/.env`), the `migration-ai-config` ConfigMap and the `migration-ai-llm` secret. Step caching is disabled: every run reads the data from S3 again.

### References

- [Red Hat OpenShift AI Self-Managed 3.5](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5) — [Enabling NVIDIA GPUs](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/working_with_accelerators/enabling-nvidia-gpus_accelerators) — [Installing and deploying OpenShift AI](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/installing_and_uninstalling_openshift_ai_self-managed/installing-and-deploying-openshift-ai_install)
- [OpenShift 4.20 — Managing compute machines with the Machine API (adding a GPU node on AWS)](https://docs.redhat.com/en/documentation/openshift_container_platform/4.20/html/machine_management/managing-compute-machines-with-the-machine-api)
- [Deploying OpenShift Data Foundation using Amazon Web Services](https://docs.redhat.com/en/documentation/red_hat_openshift_data_foundation/4.18/html-single/deploying_openshift_data_foundation_using_amazon_web_services/index)
- [Red Hat AI validated models — support matrix](https://docs.redhat.com/en/documentation/red_hat_ai/3/html/validated_models/model-support-matrix_validated-models) and [ModelCar images](https://docs.redhat.com/de/documentation/red_hat_ai/3/html/validated_models/redhat-ai-validated-modelcar-images)
- [IBM Granite with vLLM](https://www.ibm.com/granite/docs/run/granite-with-vllm-containerized) and [vLLM tool calling](https://docs.vllm.ai/en/latest/features/tool_calling/)
- AAP patch releases: [2.7](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.7/whats_new-async_updates), [2.6](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.6/whats_new-async_updates), [2.5](https://docs.redhat.com/en/documentation/red_hat_ansible_automation_platform/2.5/html/release_notes/patch_releases)
- [gaquilin/aap26-smoke-tests](https://github.com/gaquilin/aap26-smoke-tests) (model for the smoke test suite)
- [infra.aap\_configuration](https://github.com/redhat-cop/infra.aap_configuration)
- [NVIDIA GPU Operator on Red Hat OpenShift](https://docs.nvidia.com/datacenter/cloud-native/openshift/latest/index.html)
- Migration Toolkit: `demojam-migartion-AI-agent`, with the `premigration-report` command
