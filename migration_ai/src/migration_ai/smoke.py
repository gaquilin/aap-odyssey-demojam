"""Smoke & regression test agent: a platform test suite for the target AAP, delivered to a human.

The suite creates BRAND NEW objects in a dedicated organization, runs them, checks the
results and deletes everything again. It never launches migrated job templates: the
goal is to prove that the platform works (gateway, controller, project sync, execution
environments, job and workflow execution), before and after the migration and after
every patch.

The agent only GENERATES the suite (and validates the object definitions with the same
validator as the Configuration as Code). A person reviews the customization points,
runs it with ansible-playbook and uploads ``reports/smoke-results.json`` to the
Copilot: that closes the "Platform smoke test" stages of the journey and becomes the
baseline for the next run (regression = same suite, compared with the baseline).

Built the way the example repository gaquilin/aap26-smoke-tests uses the collections:
objects created with infra.aap_configuration (validated content), launches and checks
through the REST API (ansible.builtin.uri), cleanup with ansible.controller modules in
reverse dependency order.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from migration_ai import casc as casc_mod

SUITE_NAME = "aap-migration-smoke"
UNSAFE = "__UNSAFE__"

CUSTOMIZATION = [
    ("vars/connection.yml", "aap_hostname", "platform gateway host name of the target AAP, without https://"),
    ("vars/connection.yml", "aap_username / aap_password", "an administrator of the target AAP; encrypt the file with "
                                                          "ansible-vault"),
    ("vars/connection.yml", "aap_validate_certs", "false only for lab certificates"),
    ("vars/smoke.yml", "smoke_prefix", "prefix of every test object; must not match existing objects"),
    ("vars/smoke.yml", "smoke_scm_url / smoke_scm_branch", "Git repository with the test playbook, reachable "
                                                          "from the target AAP (default: public ansible-tower-samples)"),
    ("vars/smoke.yml", "smoke_scm_credential", "name of an existing SCM credential on the target AAP, only for a "
                                               "private repository"),
    ("vars/smoke.yml", "smoke_playbook", "playbook of that repository; runs on localhost inside the execution "
                                         "environment"),
    ("vars/smoke.yml", "smoke_execution_environment", "optional: an execution environment to test (empty = "
                                                      "the platform default)"),
    ("vars/smoke.yml", "smoke_check_hub / smoke_check_eda", "set false if the component is not installed"),
    ("vars/smoke.yml", "smoke_cleanup", "true: delete every test object at the end (recommended)"),
    ("ansible.cfg", "token", "Automation Hub token, to install the certified collections"),
]


def estate_features(work: Path) -> dict[str, Any]:
    """Which platform features the migrated estate uses (from the RAW export), to mirror them with test objects."""
    feats = {"surveys": 0, "schedules": 0, "workflows": 0, "custom_credential_types": 0, "custom_ees": 0,
             "source": "defaults"}
    try:
        objs = casc_mod.objects_from_migration(work, only_imported=False)
    except Exception:
        return feats
    if not any(objs.values()):
        return feats
    feats["source"] = "export"
    feats["surveys"] = sum(1 for o in objs.get("job_templates", []) if o.get("survey_enabled"))
    feats["schedules"] = len(objs.get("schedules", []))
    feats["workflows"] = len(objs.get("workflow_job_templates", []))
    feats["custom_credential_types"] = sum(1 for o in objs.get("credential_types", []) if not o.get("managed"))
    feats["custom_ees"] = sum(1 for o in objs.get("execution_environments", []) if not o.get("managed"))
    return feats


def test_plan(feats: dict[str, Any]) -> list[dict[str, str]]:
    plan = [
        {"id": "01_availability", "name": "Platform availability", "why": "gateway, controller (and hub, EDA) answer"},
        {"id": "02_authentication", "name": "Authentication", "why": "the admin can log in through the gateway"},
        {"id": "03_create_objects", "name": "Create test objects",
         "why": "organization, project (synced), inventory, host, job template, workflow via infra.aap_configuration"},
        {"id": "04_project_sync", "name": "Project synchronization", "why": "SCM access from the execution plane"},
        {"id": "05_job", "name": "Job execution", "why": "a job runs in an execution environment and succeeds"},
        {"id": "06_workflow", "name": "Workflow execution", "why": "the workflow engine runs a node"},
    ]
    if feats.get("surveys"):
        plan.insert(5, {"id": "05b_survey", "name": "Survey job", "why": f"the estate uses surveys "
                                                                          f"({feats['surveys']} job templates)"})
    if feats.get("schedules"):
        plan.append({"id": "07_schedule", "name": "Schedule (disabled)",
                     "why": f"the estate has {feats['schedules']} schedules: create one, disabled, and read it back"})
    if feats.get("custom_credential_types"):
        plan.append({"id": "08_credential_type", "name": "Custom credential type",
                     "why": f"the estate has {feats['custom_credential_types']} custom credential types"})
    plan.append({"id": "99_cleanup", "name": "Cleanup", "why": "delete every test object (reverse order)"})
    return plan


def objects(feats: dict[str, Any]) -> dict[str, list[dict]]:
    """Dispatch variables of the test objects (names are Jinja expressions resolved at run time)."""
    p = "{{ smoke_prefix }}"
    org, proj, inv = f"{p}-org", f"{p}-project", f"{p}-inventory"
    jt, wf = f"{p}-job", f"{p}-workflow"
    docs: dict[str, list[dict]] = {
        "aap_organizations": [{"name": org, "description": "Platform smoke test (created and deleted by the suite)"}],
        "controller_projects": [{
            "name": proj, "organization": org, "scm_type": "git", "scm_url": "{{ smoke_scm_url }}",
            "scm_branch": "{{ smoke_scm_branch }}", "credential": "{{ smoke_scm_credential }}",
            "update_project": True, "wait": True}],
        "controller_inventories": [{"name": inv, "organization": org}],
        "controller_hosts": [{"name": "localhost", "inventory": inv, "variables": {"ansible_connection": "local"}}],
        "controller_templates": [{
            "name": jt, "organization": org, "project": proj, "inventory": inv, "playbook": "{{ smoke_playbook }}",
            "execution_environment": "{{ smoke_execution_environment }}"}],
        "controller_workflows": [{
            "name": wf, "organization": org,
            "simplified_workflow_nodes": [{"identifier": "smoke-job", "unified_job_template": jt,
                                           "lookup_organization": org}]}],
    }
    if feats.get("surveys"):
        docs["controller_templates"].append({
            "name": f"{p}-survey-job", "organization": org, "project": proj, "inventory": inv,
            "playbook": "{{ smoke_playbook }}", "survey_enabled": True,
            "survey_spec": {"name": "smoke", "description": "smoke test survey", "spec": [{
                "question_name": "Smoke value", "variable": "smoke_value", "type": "text",
                "required": False, "default": "survey-ok"}]}})
    if feats.get("schedules"):
        docs["controller_schedules"] = [{
            "name": f"{p}-schedule", "unified_job_template": jt, "organization": org, "enabled": False,
            "rrule": "DTSTART:20300101T000000Z RRULE:FREQ=DAILY;INTERVAL=1;COUNT=1"}]
    if feats.get("custom_credential_types"):
        docs["controller_credential_types"] = [{
            "name": f"{p}-credential-type", "kind": "cloud",
            "inputs": {"fields": [{"id": "smoke_token", "label": "Smoke token", "type": "string", "secret": True}]},
            "injectors": {"env": {"SMOKE_TOKEN": UNSAFE + "{{ smoke_token }}"}}}]
    return docs


def _unsafe(text: str) -> str:
    """'__UNSAFE__{{ x }}' -> !unsafe '{{ x }}' (Ansible keeps it literal for AAP)."""
    import re

    def sub(m):
        value = m.group(2).replace("'", "''")
        return f"!unsafe '{value}'"
    return re.sub(r"""(['"]?)""" + UNSAFE + r"""(.*?)\1[ \t]*$""", sub, text, flags=re.M)


def validate(docs: dict[str, list[dict]]) -> dict[str, list[str]]:
    known: dict[str, set[str]] = {v: {i["name"] for i in items} for v, items in docs.items()}
    return casc_mod.validate_documents(docs, known)


README = """# Platform smoke test suite for the target AAP

Generated by the **Smoke & regression test agent** of AAP Odyssey for migration
`{migration_id}` on {generated} (estate features from: {feature_source}).

The suite creates **brand new objects** in a dedicated organization (`<smoke_prefix>-org`),
runs a job and a workflow, checks the results and deletes everything again. It never
launches migrated job templates. Nothing runs until **you** run it.

## Tests

| # | Test | Why |
|---|---|---|
{tests}

## 1. Customize (human in the loop)

| File | Variable | What to set |
|---|---|---|
{custom}

## 2. Install the collections

```bash
# put your Automation Hub token in ansible.cfg (console.redhat.com > Automation Hub > Connect to Hub)
ansible-galaxy collection install -r requirements.yml -p collections/
```

## 3. Run

```bash
ansible-vault encrypt vars/connection.yml
ansible-playbook playbooks/smoke.yml -e smoke_phase=post --ask-vault-pass     # pre | post | patch
```

`smoke_phase`: `pre` = the target AAP before the migration, `post` = after the migration,
`patch` = after a patch release (regression against the previous run).

## 4. Give the results to AAP Odyssey

The run writes `reports/smoke-results.json` and `reports/smoke-report.html`.
Upload `smoke-results.json` in AAP Odyssey (answer of the test agent > **Upload results**):
the journey stage "Platform smoke test" becomes done, and the next run is compared with it.

## Files

* `objects/smoke-objects.yml` — test objects as infra.aap_configuration variables
  (validated by AAP Odyssey: {errors} errors, {warnings} warnings)
* `playbooks/smoke.yml` — the suite; every test records PASSED / FAILED / SKIPPED
* `vars/smoke.yml`, `vars/connection.yml` — what you customize
"""

ANSIBLE_CFG = """[defaults]
inventory = inventory.yml
collections_path = collections
gathering = explicit
retry_files_enabled = False
interpreter_python = auto_silent
stdout_callback = ansible.builtin.default
callback_result_format = yaml

[galaxy]
server_list = published_hub, validated_hub, galaxy

[galaxy_server.published_hub]
url = https://console.redhat.com/api/automation-hub/content/published/
auth_url = https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token = CHANGE_ME

[galaxy_server.validated_hub]
url = https://console.redhat.com/api/automation-hub/content/validated/
auth_url = https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token
token = CHANGE_ME

[galaxy_server.galaxy]
url = https://galaxy.ansible.com/
"""

REQUIREMENTS = """---
collections:
  - name: infra.aap_configuration      # validated content: creates the test objects
  - name: ansible.controller           # certified: deletes them (cleanup)
  - name: ansible.platform             # certified: used by infra.aap_configuration for gateway objects
"""

INVENTORY = """---
all:
  hosts:
    localhost:
      ansible_connection: local
      ansible_python_interpreter: "{{ ansible_playbook_python }}"
"""

CONNECTION = """---
# Platform gateway of the target AAP. Encrypt this file: ansible-vault encrypt vars/connection.yml
aap_hostname: CHANGE_ME.example.com      # host name only, no https://
aap_username: admin
aap_password: CHANGE_ME
aap_validate_certs: true
"""

SMOKE_VARS = """---
migration_id: "{migration_id}"
smoke_phase: post                         # pre | post | patch (override with -e smoke_phase=...)
smoke_prefix: smoke-{short}               # every test object starts with this prefix

# Test project: a Git repository reachable from the target AAP with a playbook that runs on localhost
smoke_scm_url: https://github.com/ansible/ansible-tower-samples.git
smoke_scm_branch: master
smoke_scm_credential: ""                  # existing SCM credential name, only for a private repository
smoke_playbook: hello_world.yml
smoke_execution_environment: ""           # empty = platform default execution environment

smoke_check_hub: true                     # set false if automation hub is not installed
smoke_check_eda: true                     # set false if Event-Driven Ansible is not installed
smoke_job_timeout: 600                    # seconds
smoke_cleanup: true
"""

PLAYBOOK = r"""---
# Platform smoke test suite for the target AAP (generated by AAP Odyssey).
#   ansible-playbook playbooks/smoke.yml -e smoke_phase=post --ask-vault-pass
- name: AAP platform smoke test
  hosts: localhost
  connection: local
  gather_facts: false
  vars_files:
    - ../vars/connection.yml
    - ../vars/smoke.yml
  vars:
    api: "https://{{ aap_hostname }}"
    auth: &auth
      user: "{{ aap_username }}"
      password: "{{ aap_password }}"
      force_basic_auth: true
      validate_certs: "{{ aap_validate_certs }}"
    smoke_results: []
    org_name: "{{ smoke_prefix }}-org"
    project_name: "{{ smoke_prefix }}-project"
    inventory_name: "{{ smoke_prefix }}-inventory"
    job_name: "{{ smoke_prefix }}-job"
    workflow_name: "{{ smoke_prefix }}-workflow"
    reports_dir: "{{ playbook_dir }}/../reports"
  tasks:
    - name: Start
      ansible.builtin.set_fact:
        suite_started: "{{ now(utc=true).isoformat() }}"
        objects_ok: false

    # 01 ---------------------------------------------------------------------------------
    - name: 01 Platform availability
      block:
        - name: Ping the platform components
          ansible.builtin.uri:
            url: "{{ api }}{{ item.path }}"
            validate_certs: "{{ aap_validate_certs }}"
            status_code: [200]
          loop: "{{ [{'name': 'Gateway', 'path': '/api/gateway/v1/ping/'},
                     {'name': 'Controller', 'path': '/api/controller/v2/ping/'}]
                    + ([{'name': 'Automation hub', 'path': '/api/galaxy/pulp/api/v3/status/'}] if smoke_check_hub else [])
                    + ([{'name': 'Event-Driven Ansible', 'path': '/api/eda/v1/status/'}] if smoke_check_eda else []) }}"
          loop_control: {label: "{{ item.name }}"}
          register: pings
        - name: Controller version
          ansible.builtin.uri:
            url: "{{ api }}/api/controller/v2/ping/"
            validate_certs: "{{ aap_validate_certs }}"
          register: ping_controller
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '01_availability', 'name': 'Platform availability', 'status': 'PASSED',
                               'details': (pings.results | length) ~ ' components answer; controller ' ~ ping_controller.json.version}] }}"
            controller_version: "{{ ping_controller.json.version }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '01_availability', 'name': 'Platform availability', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('component not reachable'))[:300]}] }}"

    # 02 ---------------------------------------------------------------------------------
    - name: 02 Authentication
      block:
        - name: Log in through the gateway
          ansible.builtin.uri:
            url: "{{ api }}/api/gateway/v1/me/"
            <<: *auth
          register: me
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '02_authentication', 'name': 'Authentication', 'status': 'PASSED',
                               'details': 'logged in as ' ~ (me.json.results[0].username | default(aap_username))}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '02_authentication', 'name': 'Authentication', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('login failed'))[:300]}] }}"

    # 03 ---------------------------------------------------------------------------------
    - name: 03 Create the test objects (infra.aap_configuration)
      block:
        - name: Load the object definitions
          ansible.builtin.include_vars:
            file: "{{ playbook_dir }}/../objects/smoke-objects.yml"
        - name: Drop empty optional values (no SCM credential, default execution environment)
          ansible.builtin.set_fact:
            controller_projects: "{{ controller_projects | map('dict2items') | map('rejectattr', 'value', 'equalto', '')
                                     | map('items2dict') | list }}"
            controller_templates: "{{ controller_templates | map('dict2items') | map('rejectattr', 'value', 'equalto', '')
                                      | map('items2dict') | list }}"
        - name: Create organization, project (synced), inventory, host, job template, workflow
          ansible.builtin.include_role:
            name: infra.aap_configuration.dispatch
        - ansible.builtin.set_fact:
            objects_ok: true
            smoke_results: "{{ smoke_results + [{'id': '03_create_objects', 'name': 'Create test objects', 'status': 'PASSED',
                               'details': 'created in organization ' ~ org_name}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '03_create_objects', 'name': 'Create test objects', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('creation failed'))[:300]}] }}"

    # 04 ---------------------------------------------------------------------------------
    - name: 04 Project synchronization
      when: objects_ok | bool
      block:
        - name: Read the project
          ansible.builtin.uri:
            url: "{{ api }}/api/controller/v2/projects/?name={{ project_name | urlencode }}"
            <<: *auth
          register: proj
          until: proj.json.count | int > 0 and proj.json.results[0].status in ['successful', 'failed', 'error', 'canceled']
          retries: 30
          delay: 10
        - ansible.builtin.assert:
            that: proj.json.results[0].status == 'successful'
            fail_msg: "project sync {{ proj.json.results[0].status }}"
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '04_project_sync', 'name': 'Project synchronization', 'status': 'PASSED',
                               'details': project_name ~ ' synced from ' ~ smoke_scm_url}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '04_project_sync', 'name': 'Project synchronization', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('sync failed'))[:300]}] }}"

    # 05 ---------------------------------------------------------------------------------
    - name: 05 Job execution
      when: objects_ok | bool
      block:
        - ansible.builtin.include_tasks: run_template.yml
          vars:
            test_id: "05_job"
            test_name: "Job execution"
            template_kind: job_templates
            job_kind: jobs
            template_name: "{{ job_name }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '05_job', 'name': 'Job execution', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('job failed'))[:300]}] }}"
@@SURVEY@@
    # 06 ---------------------------------------------------------------------------------
    - name: 06 Workflow execution
      when: objects_ok | bool
      block:
        - ansible.builtin.include_tasks: run_template.yml
          vars:
            test_id: "06_workflow"
            test_name: "Workflow execution"
            template_kind: workflow_job_templates
            job_kind: workflow_jobs
            template_name: "{{ workflow_name }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '06_workflow', 'name': 'Workflow execution', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('workflow failed'))[:300]}] }}"
@@SCHEDULE@@@@CREDTYPE@@
    # skipped tests (objects not created) ---------------------------------------------------
    - name: Mark the tests that could not run
      when: not (objects_ok | bool)
      ansible.builtin.set_fact:
        smoke_results: "{{ smoke_results + [{'id': item, 'name': item, 'status': 'SKIPPED',
                           'details': 'test objects not created'}] }}"
      loop: @@DEPENDENT@@

    # 99 ---------------------------------------------------------------------------------
    - name: 99 Cleanup (reverse dependency order)
      when: smoke_cleanup | bool
      block:
        - ansible.builtin.include_tasks: cleanup.yml
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '99_cleanup', 'name': 'Cleanup', 'status': 'PASSED',
                               'details': 'test objects deleted'}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '99_cleanup', 'name': 'Cleanup', 'status': 'FAILED',
                               'details': 'some test objects were not deleted: run the suite again with only cleanup '
                                          '(ansible-playbook playbooks/cleanup_only.yml)'}] }}"

    # results ----------------------------------------------------------------------------
    - name: Write the results
      vars:
        summary:
          passed: "{{ smoke_results | selectattr('status', 'equalto', 'PASSED') | list | length }}"
          failed: "{{ smoke_results | selectattr('status', 'equalto', 'FAILED') | list | length }}"
          skipped: "{{ smoke_results | selectattr('status', 'equalto', 'SKIPPED') | list | length }}"
      block:
        - ansible.builtin.file:
            path: "{{ reports_dir }}"
            state: directory
            mode: "0755"
        - ansible.builtin.copy:
            dest: "{{ reports_dir }}/smoke-results.json"
            mode: "0644"
            content: "{{ {'suite': 'aap-migration-smoke', 'migration_id': migration_id, 'phase': smoke_phase,
                          'aap_hostname': aap_hostname, 'controller_version': controller_version | default(''),
                          'started': suite_started, 'finished': now(utc=true).isoformat(),
                          'summary': summary, 'tests': smoke_results} | to_nice_json }}"
        - ansible.builtin.template:
            src: "{{ playbook_dir }}/../templates/smoke-report.html.j2"
            dest: "{{ reports_dir }}/smoke-report.html"
            mode: "0644"
        - ansible.builtin.debug:
            msg: "Smoke test {{ smoke_phase }}: {{ summary.passed }} passed, {{ summary.failed }} failed,
                  {{ summary.skipped }} skipped. Upload reports/smoke-results.json to AAP Odyssey."
"""

SURVEY_BLOCK = r"""
    # 05b --------------------------------------------------------------------------------
    - name: 05b Survey job
      when: objects_ok | bool
      block:
        - ansible.builtin.include_tasks: run_template.yml
          vars:
            test_id: "05b_survey"
            test_name: "Survey job"
            template_kind: job_templates
            job_kind: jobs
            template_name: "{{ smoke_prefix }}-survey-job"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '05b_survey', 'name': 'Survey job', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('survey job failed'))[:300]}] }}"
"""

SCHEDULE_BLOCK = r"""
    # 07 ---------------------------------------------------------------------------------
    - name: 07 Schedule (disabled)
      when: objects_ok | bool
      block:
        - ansible.builtin.uri:
            url: "{{ api }}/api/controller/v2/schedules/?name={{ (smoke_prefix ~ '-schedule') | urlencode }}"
            <<: *auth
          register: sched
        - ansible.builtin.assert:
            that:
              - sched.json.count | int == 1
              - not sched.json.results[0].enabled
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '07_schedule', 'name': 'Schedule (disabled)', 'status': 'PASSED',
                               'details': 'schedule created, disabled, next run ' ~ (sched.json.results[0].next_run | default('none'))}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '07_schedule', 'name': 'Schedule (disabled)', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('schedule not found'))[:300]}] }}"
"""

CREDTYPE_BLOCK = r"""
    # 08 ---------------------------------------------------------------------------------
    - name: 08 Custom credential type
      when: objects_ok | bool
      block:
        - ansible.builtin.uri:
            url: "{{ api }}/api/controller/v2/credential_types/?name={{ (smoke_prefix ~ '-credential-type') | urlencode }}"
            <<: *auth
          register: ctype
        - ansible.builtin.assert:
            that: ctype.json.count | int == 1
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '08_credential_type', 'name': 'Custom credential type', 'status': 'PASSED',
                               'details': 'custom credential type created with inputs and injectors'}] }}"
      rescue:
        - ansible.builtin.set_fact:
            smoke_results: "{{ smoke_results + [{'id': '08_credential_type', 'name': 'Custom credential type', 'status': 'FAILED',
                               'details': (ansible_failed_result.msg | default('credential type not found'))[:300]}] }}"
"""

RUN_TEMPLATE = r"""---
# Launch a job or workflow template by name, wait for the end, record the result.
- name: "{{ test_name }}: find the template"
  ansible.builtin.uri:
    url: "{{ api }}/api/controller/v2/{{ template_kind }}/?name={{ template_name | urlencode }}"
    user: "{{ aap_username }}"
    password: "{{ aap_password }}"
    force_basic_auth: true
    validate_certs: "{{ aap_validate_certs }}"
  register: tpl

- name: "{{ test_name }}: launch"
  ansible.builtin.uri:
    url: "{{ api }}/api/controller/v2/{{ template_kind }}/{{ tpl.json.results[0].id }}/launch/"
    method: POST
    body_format: json
    body: {}
    status_code: [201]
    user: "{{ aap_username }}"
    password: "{{ aap_password }}"
    force_basic_auth: true
    validate_certs: "{{ aap_validate_certs }}"
  register: launched

- name: "{{ test_name }}: wait for the end"
  ansible.builtin.uri:
    url: "{{ api }}/api/controller/v2/{{ job_kind }}/{{ launched.json.id }}/"
    user: "{{ aap_username }}"
    password: "{{ aap_password }}"
    force_basic_auth: true
    validate_certs: "{{ aap_validate_certs }}"
  register: job
  until: job.json.status in ['successful', 'failed', 'error', 'canceled']
  retries: "{{ (smoke_job_timeout | int / 10) | int }}"
  delay: 10

- name: "{{ test_name }}: check"
  ansible.builtin.assert:
    that: job.json.status == 'successful'
    fail_msg: "{{ job_kind }} {{ launched.json.id }} ended {{ job.json.status }}"

- name: "{{ test_name }}: record"
  ansible.builtin.set_fact:
    smoke_results: "{{ smoke_results + [{'id': test_id, 'name': test_name, 'status': 'PASSED',
                       'details': job_kind ~ ' ' ~ launched.json.id ~ ' successful in ' ~ (job.json.elapsed | default(0)) ~ ' s'}] }}"
"""

CLEANUP = r"""---
# Delete the test objects in reverse dependency order (as aap26-smoke-tests/99_cleanup.yml).
- name: Delete the workflow
  ansible.controller.workflow_job_template:
    name: "{{ smoke_prefix }}-workflow"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"

- name: Delete the job templates (and their schedules)
  ansible.controller.job_template:
    name: "{{ item }}"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"
  loop:
    - "{{ smoke_prefix }}-job"
    - "{{ smoke_prefix }}-survey-job"

- name: Delete the inventory (and its host)
  ansible.controller.inventory:
    name: "{{ smoke_prefix }}-inventory"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"

- name: Delete the project
  ansible.controller.project:
    name: "{{ smoke_prefix }}-project"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"

- name: Delete the custom credential type
  ansible.controller.credential_type:
    name: "{{ smoke_prefix }}-credential-type"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"

- name: Delete the organization
  ansible.controller.organization:
    name: "{{ smoke_prefix }}-org"
    state: absent
    controller_host: "{{ aap_hostname }}"
    controller_username: "{{ aap_username }}"
    controller_password: "{{ aap_password }}"
    validate_certs: "{{ aap_validate_certs }}"
"""

CLEANUP_ONLY = """---
# Delete the smoke test objects left by an interrupted run.
#   ansible-playbook playbooks/cleanup_only.yml --ask-vault-pass
- name: AAP smoke test cleanup
  hosts: localhost
  connection: local
  gather_facts: false
  vars_files:
    - ../vars/connection.yml
    - ../vars/smoke.yml
  tasks:
    - ansible.builtin.include_tasks: cleanup.yml
"""

REPORT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AAP smoke test {{ smoke_phase }}</title>
<style>body{font-family:'Red Hat Text',Arial,sans-serif;margin:24px;color:#151515}table{border-collapse:collapse}
td,th{border:1px solid #d2d2d2;padding:6px 10px;text-align:left}.PASSED{color:#3e8635}.FAILED{color:#c9190b}
.SKIPPED{color:#6a6e73}</style></head><body>
<h1>AAP platform smoke test — {{ smoke_phase }}</h1>
<p>Migration {{ migration_id }} · {{ aap_hostname }} · controller {{ controller_version | default('') }} ·
started {{ suite_started }}</p>
<table><tr><th>Test</th><th>Status</th><th>Details</th></tr>
{% for t in smoke_results %}<tr><td>{{ t.name }}</td><td class="{{ t.status }}">{{ t.status }}</td>
<td>{{ t.details }}</td></tr>
{% endfor %}</table></body></html>
"""


def generate_suite(work: Path, migration_id: str, out_root: Path | None = None) -> dict[str, Any]:
    feats = estate_features(work)
    plan = test_plan(feats)
    docs = objects(feats)
    validation = validate(docs)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (out_root or work / "smoke") / stamp
    (out / "playbooks").mkdir(parents=True)
    (out / "vars").mkdir()
    (out / "objects").mkdir()
    (out / "templates").mkdir()
    playbook = (PLAYBOOK
                .replace("@@SURVEY@@", SURVEY_BLOCK if feats.get("surveys") else "")
                .replace("@@SCHEDULE@@", SCHEDULE_BLOCK if feats.get("schedules") else "")
                .replace("@@CREDTYPE@@", CREDTYPE_BLOCK if feats.get("custom_credential_types") else "")
                .replace("@@DEPENDENT@@", json.dumps([t["id"] for t in plan
                                                      if t["id"] not in ("01_availability", "02_authentication",
                                                                         "03_create_objects", "99_cleanup")])))
    (out / "playbooks" / "smoke.yml").write_text(playbook, encoding="utf-8")
    (out / "playbooks" / "run_template.yml").write_text(RUN_TEMPLATE, encoding="utf-8")
    (out / "playbooks" / "cleanup.yml").write_text(CLEANUP, encoding="utf-8")
    (out / "playbooks" / "cleanup_only.yml").write_text(CLEANUP_ONLY, encoding="utf-8")
    (out / "objects" / "smoke-objects.yml").write_text(
        "---\n# Test objects (infra.aap_configuration dispatch variables). Names use smoke_prefix.\n"
        "# Empty optional values (credential, execution_environment) are removed by the playbook.\n"
        + _unsafe(casc_mod.dump(docs)), encoding="utf-8")
    short = "".join(ch for ch in migration_id.lower() if ch.isalnum() or ch == "-")[:20].strip("-") or "test"
    (out / "vars" / "smoke.yml").write_text(SMOKE_VARS.format(migration_id=migration_id, short=short), encoding="utf-8")
    (out / "vars" / "connection.yml").write_text(CONNECTION, encoding="utf-8")
    (out / "ansible.cfg").write_text(ANSIBLE_CFG, encoding="utf-8")
    (out / "requirements.yml").write_text(REQUIREMENTS, encoding="utf-8")
    (out / "inventory.yml").write_text(INVENTORY, encoding="utf-8")
    (out / "templates" / "smoke-report.html.j2").write_text(REPORT_TEMPLATE, encoding="utf-8")
    readme = README.format(
        migration_id=migration_id, generated=time.strftime("%Y-%m-%d %H:%M"), feature_source=feats["source"],
        tests="\n".join(f"| {t['id'][:3].rstrip('_')} | {t['name']} | {t['why']} |" for t in plan),
        custom="\n".join(f"| `{f}` | `{v}` | {w} |" for f, v, w in CUSTOMIZATION),
        errors=len(validation["errors"]), warnings=len(validation["warnings"]))
    (out / "README.md").write_text(readme, encoding="utf-8")
    summary = {"stamp": stamp, "migration_id": migration_id, "features": feats, "tests": plan,
               "customization": [{"file": f, "variable": v, "why": w} for f, v, w in CUSTOMIZATION],
               "validation": validation}
    (out / "suite-summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    archive = shutil.make_archive(str(out), "zip", out.parent, out.name)
    summary.update({"output_dir": str(out), "zip": archive})
    return summary


def validate_results(data: Any) -> dict:
    if not isinstance(data, dict) or data.get("suite") != SUITE_NAME or not isinstance(data.get("tests"), list):
        raise ValueError("not a smoke-results.json written by the suite (missing suite/tests)")
    phase = data.get("phase")
    if phase not in ("pre", "post", "patch"):
        raise ValueError("phase must be pre, post or patch")
    for t in data["tests"]:
        if not isinstance(t, dict) or t.get("status") not in ("PASSED", "FAILED", "SKIPPED"):
            raise ValueError("every test needs a status PASSED, FAILED or SKIPPED")
    return data


def save_results(work: Path, data: dict) -> Path:
    data = validate_results(data)
    d = work / "smoke" / "results"
    d.mkdir(parents=True, exist_ok=True)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
    while any(d.glob(f"{stamp}-{n:02d}-*.json")):
        n += 1
    p = d / f"{stamp}-{n:02d}-{data['phase']}.json"
    p.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return p


def load_results(work: Path) -> list[dict]:
    d = work / "smoke" / "results"
    out = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            data = validate_results(json.loads(p.read_text(encoding="utf-8")))
            data["_file"] = p.relative_to(work).as_posix()
            out.append(data)
        except (ValueError, json.JSONDecodeError):
            continue
    return out


def passed(run: dict) -> bool:
    return not any(t["status"] == "FAILED" for t in run["tests"]) and any(t["status"] == "PASSED" for t in run["tests"])


def compare(baseline: dict | None, current: dict) -> list[dict]:
    """Regression view: tests whose status changed from the baseline."""
    before = {t["id"]: t["status"] for t in (baseline or {}).get("tests", [])}
    out = []
    for t in current["tests"]:
        b = before.get(t["id"])
        if b and b != t["status"]:
            out.append({"id": t["id"], "name": t["name"], "before": b, "now": t["status"], "details": t.get("details")})
    return out
