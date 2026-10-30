"""AI layer for the AAP 2.4 to 2.5 / 2.6 / 2.7 migration toolkit on Red Hat OpenShift AI.

Modules
-------
config     settings read from environment variables (ConfigMap / Secrets)
storage    S3 object storage: the migration workspace, datasets, models
toolkit    runs ``aap-bridge`` commands and reads its state database
llm        OpenAI-compatible client for the model served by OpenShift AI (vLLM)
agent      the toolkit migration agent (plan / execute / collect), used by the pipelines
agents     AAP Odyssey orchestrator and its six specialist agents
releases   Smart Planning agent: AAP patch releases from the Red Hat documentation
smoke      Smoke & regression test agent: platform test suite with brand new objects
dataset    turns a finished migration into a labelled training dataset
train      trains and evaluates the migration risk model
predict    AI-enhanced readiness report for the next AAP 2.4 to migrate
casc       Configuration as Code (infra.aap_configuration) generator + validator
assistant  the tools the agents use (data of a migration, CasC, scans, reports, plans, suites)
progress   the migration journey: stages, owner agent, human-in-the-loop points, next step
webapp     AAP Odyssey web UI (PatternFly)
demo       synthetic migration workspace to try everything without AAP
"""

__version__ = "0.3.15"
