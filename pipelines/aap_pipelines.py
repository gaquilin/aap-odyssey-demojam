"""AI Pipelines (Kubeflow Pipelines v2) of the AAP migration demo on Red Hat OpenShift AI.

Four pipelines, each step is a container running ``migration-ai <command>``
from the ``aap-migration-ai`` image (toolkit + AI layer):

aap-migration-during   DURING migration: prepare -> agent (plan | execute | collect)
                        -> [if the migration produced results] post-migration reports,
                        dataset, CasC -> [if train_after] train + AI report
aap-migration-train    AFTER: (re)train the risk model on every migration dataset
aap-migration-predict  AFTER: AI readiness report (enhanced-report style) for the next migration
aap-migration-casc     AFTER: Configuration as Code of the target AAP (live or from the migration)

Compile (needs Python 3.11+ and kfp 2.14.3 + kfp-kubernetes 2.14.3, not the toolkit):

    pip install "kfp==2.14.3" "kfp-kubernetes==2.14.3"
    MIGRATION_AI_IMAGE=image-registry.openshift-image-registry.svc:5000/aap-migration-ai/aap-migration-ai:latest \
        python aap_pipelines.py            # writes compiled/*.yaml

Import the YAML files in the OpenShift AI dashboard: Develop & train > Pipelines > Import pipeline.
"""


import os
from pathlib import Path

from kfp import compiler, dsl, kubernetes

NAMESPACE = os.environ.get("MIGRATION_AI_NAMESPACE", "aap-migration-ai")
IMAGE = os.environ.get(
    "MIGRATION_AI_IMAGE",
    f"image-registry.openshift-image-registry.svc:5000/{NAMESPACE}/aap-migration-ai:latest",
)
DATA_CONNECTION = "aap-migration-data"
TOOLKIT_ENV_SECRET = "aap-bridge-env"
CONFIG_MAP = "migration-ai-config"
LLM_SECRET = "migration-ai-llm"

S3_KEYS = {k: k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_ENDPOINT", "AWS_S3_BUCKET",
                          "AWS_DEFAULT_REGION")}
CONFIG_KEYS = {k: k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_EXTRA_BODY",
                              "LLM_VERIFY_TLS", "LLM_TIMEOUT", "MIGRATIONS_PREFIX", "MODELS_PREFIX",
                              "AWS_S3_VERIFY_TLS")}


def configure(task: dsl.PipelineTask, cpu: str = "500m", memory: str = "1Gi", memory_limit: str = "4Gi",
              toolkit_env: bool = True) -> dsl.PipelineTask:
    """Secrets, ConfigMap, scratch volume and resources shared by every step."""
    task.set_caching_options(False)
    task.set_cpu_request(cpu)
    task.set_memory_request(memory)
    task.set_memory_limit(memory_limit)
    kubernetes.set_image_pull_policy(task, "Always")
    kubernetes.use_secret_as_env(task, DATA_CONNECTION, S3_KEYS)
    kubernetes.use_config_map_as_env(task, CONFIG_MAP, CONFIG_KEYS)
    kubernetes.use_secret_as_env(task, LLM_SECRET, {"LLM_API_KEY": "LLM_API_KEY"})
    if toolkit_env:
        kubernetes.use_secret_as_volume(task, TOOLKIT_ENV_SECRET, mount_path="/etc/aap-bridge")
    kubernetes.empty_dir_mount(task, volume_name="work", mount_path="/work")
    return task


@dsl.container_component
def prepare_op(migration_id: str, refresh_export: bool, with_dependencies: bool, readiness: dsl.Output[dsl.HTML]):
    """Export from AAP 2.4 (when needed), pre-migration report and Dependency Analyzer."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "prepare", "--migration-id", migration_id, "--refresh-export", refresh_export,
        "--with-dependencies", with_dependencies, "--out-html", readiness.path])


@dsl.container_component
def agent_op(migration_id: str, mode: str, dry_run: bool, include_credentials: bool, include_iam: bool,
             use_llm: bool, abort_failure_rate: float, report: dsl.Output[dsl.Markdown],
             status: dsl.OutputPath(str), collectable: dsl.OutputPath(str)):
    """The migration agent: plan (ask the human), execute (run the toolkit), collect (human already ran it)."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "agent", "--migration-id", migration_id, "--mode", mode, "--dry-run", dry_run,
        "--include-credentials", include_credentials, "--include-iam", include_iam, "--use-llm", use_llm,
        "--abort-failure-rate", abort_failure_rate, "--out-md", report.path, "--out-status", status,
        "--out-collectable", collectable])


@dsl.container_component
def post_reports_op(migration_id: str, enhanced_report: dsl.Output[dsl.HTML]):
    """Toolkit enhanced-report on the finished migration."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "post-reports", "--migration-id", migration_id, "--out-html", enhanced_report.path])


@dsl.container_component
def dataset_op(migration_id: str):
    """Training dataset: RAW export (features) + state database (labels) -> S3."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=["dataset", "--migration-id", migration_id])


@dsl.container_component
def train_op(migrations: str, threshold: float, model_card: dsl.Output[dsl.Markdown], status: dsl.OutputPath(str)):
    """Train + evaluate the risk model on the datasets in S3, publish it when trained."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "train", "--migrations", migrations, "--threshold", threshold, "--out-md", model_card.path,
        "--out-status", status])


@dsl.container_component
def predict_op(migration_id: str, refresh_export: bool, ai_report: dsl.Output[dsl.HTML]):
    """AI readiness report: pre-migration rules + risk model, enhanced-report style."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "predict", "--migration-id", migration_id, "--refresh-export", refresh_export,
        "--out-html", ai_report.path])


@dsl.container_component
def casc_op(migration_id: str, source: str, organization: str, resource_types: str, include_hosts: bool,
            review: bool, casc_readme: dsl.Output[dsl.Markdown]):
    """infra.aap_configuration CasC for the target AAP (+ optional LLM review)."""
    return dsl.ContainerSpec(image=IMAGE, command=["migration-ai"], args=[
        "casc", "--migration-id", migration_id, "--source", source, "--organization", organization,
        "--resource-types", resource_types, "--include-hosts", include_hosts, "--review", review,
        "--out-md", casc_readme.path])


@dsl.pipeline(name="aap-migration-during",
              description="DURING migration: prepare, migration agent (plan/execute/collect), then collect the "
                          "migration data, generate CasC and (re)train the risk model.")
def during_migration(migration_id: str = "aap24-to-aap26", mode: str = "plan", refresh_export: bool = False,
                     with_dependencies: bool = True, dry_run: bool = False, include_credentials: bool = False, include_iam: bool = False,
                     use_llm: bool = True, abort_failure_rate: float = 0.5, train_after: bool = True,
                     casc_review: bool = True):
    prep = configure(prepare_op(migration_id=migration_id, refresh_export=refresh_export,
                                with_dependencies=with_dependencies).set_display_name(
        "1. Export + pre-migration report"))
    agent = configure(agent_op(migration_id=migration_id, mode=mode, dry_run=dry_run,
                               include_credentials=include_credentials, include_iam=include_iam, use_llm=use_llm,
                               abort_failure_rate=abort_failure_rate).set_display_name("2. Migration agent"),
                      memory_limit="6Gi").after(prep)
    with dsl.If(agent.outputs["collectable"] == "true", name="migration-has-results"):
        post = configure(post_reports_op(migration_id=migration_id).set_display_name("3. Enhanced report"))
        ds = configure(dataset_op(migration_id=migration_id).set_display_name("4. Training dataset")).after(post)
        configure(casc_op(migration_id=migration_id, source="migration", organization="", resource_types="",
                          include_hosts=False, review=casc_review).set_display_name("5. Configuration as Code"),
                  toolkit_env=True).after(ds)
        with dsl.If(train_after == True, name="train-after-migration"):
            configure(train_op(migrations="all", threshold=0.5).set_display_name("6. Train risk model"),
                      cpu="1", memory="2Gi", toolkit_env=False).after(ds)


@dsl.pipeline(name="aap-migration-train",
              description="AFTER migration: train and evaluate the risk model on the datasets of all migrations.")
def train_model(migrations: str = "all", threshold: float = 0.5):
    configure(train_op(migrations=migrations, threshold=threshold).set_display_name("Train risk model"),
              cpu="1", memory="2Gi", toolkit_env=False)


@dsl.pipeline(name="aap-migration-predict",
              description="AFTER migration: AI readiness report (enhanced-report style) for the next AAP 2.4 to migrate.")
def predict(migration_id: str = "next-migration", refresh_export: bool = True):
    configure(predict_op(migration_id=migration_id, refresh_export=refresh_export).set_display_name(
        "AI readiness report"))


@dsl.pipeline(name="aap-migration-casc",
              description="AFTER migration: Configuration as Code of the target AAP in the infra.aap_configuration format.")
def generate_casc(migration_id: str = "aap24-to-aap26", source: str = "live", organization: str = "",
                  resource_types: str = "", include_hosts: bool = False, review: bool = True):
    configure(casc_op(migration_id=migration_id, source=source, organization=organization,
                      resource_types=resource_types, include_hosts=include_hosts, review=review)
              .set_display_name("Configuration as Code"))


PIPELINES = {
    "aap-migration-during.yaml": during_migration,
    "aap-migration-train.yaml": train_model,
    "aap-migration-predict.yaml": predict,
    "aap-migration-casc.yaml": generate_casc,
}

if __name__ == "__main__":
    out = Path(__file__).parent / "compiled"
    out.mkdir(exist_ok=True)
    for name, fn in PIPELINES.items():
        compiler.Compiler().compile(pipeline_func=fn, package_path=str(out / name))
        print(f"compiled {out / name}  (image {IMAGE})")
