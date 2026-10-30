"""Configuration as Code (CasC) for the target AAP in the ``infra.aap_configuration`` format.

Generation is deterministic (no LLM): objects are read from

* ``live``      the target AAP API (controller + gateway), the truth after migration
* ``migration`` the RAW export of the migration, restricted to the objects the
                state database marks as imported (works offline)

and converted to the variables consumed by the ``infra.aap_configuration.dispatch``
role (collection 4.10.0): ``aap_organizations``, ``controller_projects``,
``controller_templates``, ``controller_workflows`` ... References are written
by name, secrets become ``{{ vaulted_* }}`` placeholders listed in
``env_variables.yml``.

The LLM (assistant) uses this generator as a tool and validates anything it
writes with :func:`validate_documents`, so field names come from the
collection schema, not from the model's memory.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

import yaml

SCHEMA = json.loads(resources.files("migration_ai").joinpath("casc_schema.json").read_text())
ALLOWED = {var: set(spec["fields"]) for var, spec in SCHEMA["vars"].items()}
NODE_FIELDS = {"identifier", "unified_job_template", "lookup_organization", "approval_node",
               "all_parents_must_converge", "success_nodes", "failure_nodes", "always_nodes", "credentials",
               "extra_data", "inventory", "limit", "job_type", "job_tags", "skip_tags", "diff_mode", "verbosity",
               "scm_branch", "execution_environment", "forks", "instance_groups", "job_slice_count", "timeout",
               "labels"}

TYPE_MAP: list[tuple[str, str, str, bool]] = [
    ("organizations", "aap_organizations", "organizations.yml", False),
    ("teams", "aap_teams", "teams.yml", False),
    ("users", "aap_user_accounts", "users.yml", True),
    ("credential_types", "controller_credential_types", "credential_types.yml", True),
    ("instance_groups", "controller_instance_groups", "instance_groups.yml", True),
    ("execution_environments", "controller_execution_environments", "execution_environments.yml", True),
    ("credentials", "controller_credentials", "credentials.yml", False),
    ("projects", "controller_projects", "projects.yml", False),
    ("inventories", "controller_inventories", "inventories.yml", False),
    ("inventory_sources", "controller_inventory_sources", "inventory_sources.yml", False),
    ("hosts", "controller_hosts", "hosts.yml", False),
    ("notification_templates", "controller_notifications", "notifications.yml", False),
    ("job_templates", "controller_templates", "job_templates.yml", False),
    ("workflow_job_templates", "controller_workflows", "workflows.yml", False),
    ("schedules", "controller_schedules", "schedules.yml", False),
]
CASC_TYPES = [t[0] for t in TYPE_MAP]
VAR_OF = {t[0]: t[1] for t in TYPE_MAP}

_JT_FIELDS = ["description", "job_type", "playbook", "forks", "limit", "verbosity", "extra_vars", "job_tags",
              "skip_tags", "start_at_task", "diff_mode", "use_fact_cache", "host_config_key", "become_enabled",
              "allow_simultaneous", "timeout", "job_slice_count", "scm_branch", "survey_enabled",
              "prevent_instance_group_fallback", "force_handlers"]
_WF_FIELDS = ["description", "extra_vars", "allow_simultaneous", "limit", "scm_branch", "job_tags", "skip_tags",
              "survey_enabled"]
_SCHEDULE_FIELDS = ["description", "rrule", "enabled", "limit", "job_type", "job_tags", "skip_tags", "diff_mode",
                    "verbosity", "scm_branch", "forks", "job_slice_count", "timeout"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "unnamed"


def _sf(obj: dict) -> dict:
    return obj.get("summary_fields") or {}


def _ref(obj: dict, field: str) -> str | None:
    v = _sf(obj).get(field)
    return v.get("name") if isinstance(v, dict) else None


def _org(obj: dict, org_names: dict[int, str]) -> str | None:
    name = _ref(obj, "organization")
    if name:
        return name
    oid = obj.get("organization")
    return org_names.get(oid) if isinstance(oid, int) else None


def _clean(d: dict) -> dict:
    """Drop empty values so the CasC stays readable (defaults are applied by the collection)."""
    return {k: v for k, v in d.items() if v is not None and not (isinstance(v, (str, list, dict)) and not v)}


def _extra_vars(value: Any) -> Any:
    if isinstance(value, str):
        txt = value.strip()
        if not txt or txt in ("---", "{}"):
            return None
        try:
            return yaml.safe_load(txt)
        except yaml.YAMLError:
            return txt
    return value or None


class SecretCollector:
    def __init__(self) -> None:
        self.vars: dict[str, str] = {}

    def placeholder(self, *parts: str) -> str:
        name = "vaulted_" + "_".join(slug(p) for p in parts)
        self.vars[name] = "CHANGE_ME"
        return "{{ " + name + " }}"

    def scrub(self, data: Any, *parts: str) -> Any:
        if isinstance(data, dict):
            return {k: self.scrub(v, *parts, k) for k, v in data.items()}
        if isinstance(data, list):
            return [self.scrub(v, *parts, str(i)) for i, v in enumerate(data)]
        if data == "$encrypted$":
            return self.placeholder(*parts)
        return data


def convert(objects: dict[str, list[dict]], organization: str | None = None,
            include: Iterable[str] = CASC_TYPES, include_hosts: bool = False,
            include_users: bool = False) -> tuple[dict[str, dict[str, list]], dict[str, str], list[str]]:
    """Convert API-shaped objects to CasC variables.

    Returns ({scope: {var: [items]}}, secret placeholders, notes). Scope is the
    organization name or ``_global`` for objects without organization.
    """
    include = set(include)
    if not include_hosts:
        include.discard("hosts")
    if not include_users:
        include.discard("users")
    org_names = {o["id"]: o["name"] for o in objects.get("organizations", []) if "id" in o}
    want_org = organization.lower() if organization else None
    secrets = SecretCollector()
    out: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    notes: list[str] = []
    ujt_names = {}
    for t in ("job_templates", "workflow_job_templates", "projects", "inventory_sources"):
        for o in objects.get(t, []):
            ujt_names[(t, o.get("id"))] = (o.get("name"), _org(o, org_names))

    def scope_of(rtype: str, obj: dict) -> str | None:
        org = obj.get("name") if rtype == "organizations" else _org(obj, org_names)
        if rtype in ("inventory_sources", "hosts") and not org:
            org = (_sf(obj).get("inventory") or {}).get("organization_name") or obj.get("_inventory_org")
        if rtype == "schedules" and not org:
            org = obj.get("_parent_org")
        if want_org and (org or "").lower() != want_org:
            return None
        return org or "_global"

    for rtype, var, _file, _is_global in TYPE_MAP:
        if rtype not in include:
            continue
        for obj in objects.get(rtype, []):
            if rtype in ("credential_types", "execution_environments") and obj.get("managed"):
                continue
            if rtype == "credentials" and obj.get("managed"):
                continue
            if rtype == "instance_groups" and obj.get("name") in ("controlplane", "default"):
                continue
            scope = scope_of(rtype, obj)
            if scope is None:
                no_org = not _org(obj, org_names)
                if rtype in ("credential_types", "instance_groups", "execution_environments", "users") or (
                        rtype == "credentials" and no_org):
                    scope = "_global"
                else:
                    continue
            item = _convert_one(rtype, obj, org_names, secrets, ujt_names, notes)
            if item:
                out[scope][var].append(item)
    result = {k: dict(v) for k, v in out.items()}
    if want_org and "_global" in result:
        result["_global"] = _prune_global(result, notes)
        if not any(result["_global"].values()):
            result.pop("_global")
    return result, secrets.vars, notes


def _referenced(scopes: dict[str, dict[str, list]]) -> dict[str, set[str]]:
    """Names of global objects referenced by the organization scopes (credentials, EEs, IGs, types)."""
    refs: dict[str, set[str]] = defaultdict(set)
    for scope, docs in scopes.items():
        if scope == "_global":
            continue
        for var, items in docs.items():
            for it in items:
                for f in ("credential", "default_environment", "execution_environment"):
                    v = it.get(f)
                    if v:
                        refs["credentials" if f == "credential" else "ees"].add(v)
                for c in it.get("credentials") or []:
                    refs["credentials"].add(c)
                for c in it.get("galaxy_credentials") or []:
                    refs["credentials"].add(c)
                for ig in it.get("instance_groups") or []:
                    refs["igs"].add(ig)
                if var == "controller_credentials" and it.get("credential_type"):
                    refs["types"].add(it["credential_type"])
                for n in it.get("simplified_workflow_nodes") or []:
                    for c in n.get("credentials") or []:
                        refs["credentials"].add(c)
                    if n.get("execution_environment"):
                        refs["ees"].add(n["execution_environment"])
                    for ig in n.get("instance_groups") or []:
                        refs["igs"].add(ig)
    return refs


def _prune_global(scopes: dict[str, dict[str, list]], notes: list[str]) -> dict[str, list]:
    """With an organization filter keep only the global objects that organization uses."""
    refs = _referenced(scopes)
    g = scopes["_global"]
    creds = [c for c in g.get("controller_credentials", []) if c.get("name") in refs["credentials"]]
    for c in creds:
        if c.get("credential_type"):
            refs["types"].add(c["credential_type"])
    keep = {
        "controller_credentials": creds,
        "controller_credential_types": [t for t in g.get("controller_credential_types", [])
                                        if t.get("name") in refs["types"]],
        "controller_execution_environments": [e for e in g.get("controller_execution_environments", [])
                                              if e.get("name") in refs["ees"]],
        "controller_instance_groups": [i for i in g.get("controller_instance_groups", []) if i.get("name") in refs["igs"]],
    }
    for ee in keep["controller_execution_environments"]:
        if ee.get("credential") and ee["credential"] not in {c["name"] for c in keep["controller_credentials"]}:
            extra = [c for c in g.get("controller_credentials", []) if c.get("name") == ee["credential"]]
            keep["controller_credentials"] += extra
    if g.get("aap_user_accounts"):
        keep["aap_user_accounts"] = g["aap_user_accounts"]
        notes.append("users have no organization in the export: all included users are kept in _global")
    dropped = sum(len(v) for v in g.values()) - sum(len(v) for v in keep.values())
    if dropped:
        notes.append(f"_global: {dropped} shared objects not used by the selected organization were left out "
                     "(credential types, execution environments, instance groups, credentials without organization)")
    return {k: v for k, v in keep.items() if v}


def _convert_one(rtype: str, o: dict, org_names: dict, secrets: SecretCollector, ujt_names: dict,
                 notes: list[str]) -> dict | None:
    org = _org(o, org_names)
    name = o.get("name")
    if rtype == "organizations":
        return _clean({"name": name, "description": o.get("description"), "max_hosts": o.get("max_hosts") or None,
                       "default_environment": _ref(o, "default_environment")})
    if rtype == "teams":
        return _clean({"name": name, "organization": org, "description": o.get("description")})
    if rtype == "users":
        return _clean({"username": o.get("username"), "email": o.get("email"), "first_name": o.get("first_name"),
                       "last_name": o.get("last_name"), "is_superuser": o.get("is_superuser") or None,
                       "password": secrets.placeholder("user", o.get("username", ""), "password")})
    if rtype == "credential_types":
        return _clean({"name": name, "description": o.get("description"), "kind": o.get("kind"),
                       "inputs": o.get("inputs"), "injectors": o.get("injectors")})
    if rtype == "instance_groups":
        if o.get("policy_instance_list"):
            notes.append(f"instance group '{name}': policy_instance_list omitted (VM hostnames do not exist on OpenShift)")
        return _clean({"name": name, "is_container_group": o.get("is_container_group"),
                       "max_concurrent_jobs": o.get("max_concurrent_jobs") or None,
                       "max_forks": o.get("max_forks") or None, "pod_spec_override": o.get("pod_spec_override"),
                       "credential": _ref(o, "credential")})
    if rtype == "execution_environments":
        return _clean({"name": name, "description": o.get("description"), "image": o.get("image"),
                       "pull": o.get("pull"), "organization": org, "credential": _ref(o, "credential")})
    if rtype == "credentials":
        inputs = o.get("inputs") if isinstance(o.get("inputs"), dict) else {}
        return _clean({"name": name, "description": o.get("description"), "organization": org,
                       "credential_type": (_sf(o).get("credential_type") or {}).get("name"),
                       "inputs": secrets.scrub(inputs, "credential", org or "global", name or ""),
                       "update_secrets": False})
    if rtype == "projects":
        scm = o.get("scm_type") or ""
        item = {"name": name, "description": o.get("description"), "organization": org,
                "scm_type": scm or "manual", "scm_url": o.get("scm_url"), "scm_branch": o.get("scm_branch"),
                "scm_refspec": o.get("scm_refspec"), "credential": _ref(o, "credential"),
                "scm_clean": o.get("scm_clean") or None, "scm_delete_on_update": o.get("scm_delete_on_update") or None,
                "scm_update_on_launch": o.get("scm_update_on_launch") or None,
                "scm_update_cache_timeout": o.get("scm_update_cache_timeout") or None,
                "allow_override": o.get("allow_override") or None, "timeout": o.get("timeout") or None,
                "default_environment": _ref(o, "default_environment")}
        if not scm:
            item["local_path"] = o.get("local_path")
            notes.append(f"project '{name}' is manual (local_path): it needs a persistent projects volume on the target AAP")
        return _clean(item)
    if rtype == "inventories":
        item = {"name": name, "description": o.get("description"), "organization": org,
                "variables": _extra_vars(o.get("variables"))}
        kind = o.get("kind") or ""
        if kind:
            item["kind"] = kind
            item["host_filter"] = o.get("host_filter")
        return _clean(item)
    if rtype == "inventory_sources":
        src = o.get("source") or ""
        if src == "constructed":
            return None
        creds = _sf(o).get("credentials") or []
        cred = _ref(o, "credential") or (creds[0].get("name") if creds and isinstance(creds[0], dict) else None)
        return _clean({"name": name, "description": o.get("description"), "inventory": _ref(o, "inventory"),
                       "organization": org, "source": src, "source_path": o.get("source_path"),
                       "source_project": _ref(o, "source_project"), "source_vars": _extra_vars(o.get("source_vars")),
                       "credential": cred, "overwrite": o.get("overwrite") or None,
                       "overwrite_vars": o.get("overwrite_vars") or None,
                       "update_on_launch": o.get("update_on_launch") or None,
                       "update_cache_timeout": o.get("update_cache_timeout") or None,
                       "timeout": o.get("timeout") or None, "verbosity": o.get("verbosity") or None,
                       "execution_environment": _ref(o, "execution_environment"), "limit": o.get("limit")})
    if rtype == "hosts":
        return _clean({"name": name, "description": o.get("description"), "inventory": _ref(o, "inventory"),
                       "enabled": o.get("enabled", True), "variables": _extra_vars(o.get("variables"))})
    if rtype == "notification_templates":
        cfg = o.get("notification_configuration") or {}
        return _clean({"name": name, "description": o.get("description"), "organization": org,
                       "notification_type": o.get("notification_type"),
                       "notification_configuration": secrets.scrub(cfg, "notification", org or "", name or ""),
                       "messages": o.get("messages")})
    if rtype == "job_templates":
        item = {"name": name, "organization": org, "project": _ref(o, "project"),
                "inventory": _ref(o, "inventory"), "execution_environment": _ref(o, "execution_environment"),
                "credentials": [c["name"] for c in (_sf(o).get("credentials") or []) if isinstance(c, dict)],
                "labels": [lb["name"] for lb in ((_sf(o).get("labels") or {}).get("results") or [])],
                "instance_groups": [ig["name"] for ig in (o.get("_instance_groups") or [])]}
        for f in _JT_FIELDS:
            item[f] = o.get(f) or None
        item["extra_vars"] = _extra_vars(o.get("extra_vars"))
        for k, v in o.items():
            if k.startswith("ask_") and k.endswith("_on_launch") and v:
                item[k] = True
        if o.get("survey_spec"):
            item["survey_spec"] = o["survey_spec"]
        return _clean(item)
    if rtype == "workflow_job_templates":
        item = {"name": name, "organization": org, "inventory": _ref(o, "inventory")}
        for f in _WF_FIELDS:
            item[f] = o.get(f) or None
        item["extra_vars"] = _extra_vars(o.get("extra_vars"))
        for k, v in o.items():
            if k.startswith("ask_") and k.endswith("_on_launch") and v:
                item[k] = True
        if o.get("survey_spec"):
            item["survey_spec"] = o["survey_spec"]
        nodes = _workflow_nodes(o.get("nodes") or [], name or "", notes)
        if nodes:
            item["simplified_workflow_nodes"] = nodes
        return _clean(item)
    if rtype == "schedules":
        ujt = _sf(o).get("unified_job_template") or {}
        ujt_name = ujt.get("name")
        if not ujt_name:
            notes.append(f"schedule '{name}' skipped: no parent template")
            return None
        item = {"name": name, "unified_job_template": ujt_name, "organization": o.get("_parent_org"),
                "extra_data": _extra_vars(o.get("extra_data")), "inventory": _ref(o, "inventory")}
        for f in _SCHEDULE_FIELDS:
            item[f] = o.get(f) if f == "enabled" else (o.get(f) or None)
        return _clean(item)
    return None


def _workflow_nodes(nodes: list[dict], wf_name: str, notes: list[str]) -> list[dict]:
    ident = {}
    for n in nodes:
        nid = n.get("id")
        ident[nid] = n.get("identifier") or f"node{nid}"
    out = []
    for n in nodes:
        sf = _sf(n).get("unified_job_template") or {}
        node: dict[str, Any] = {"identifier": ident[n.get("id")]}
        if sf.get("unified_job_type") == "workflow_approval" or n.get("approval_node"):
            node["approval_node"] = {"name": sf.get("name") or "Approval", "description": sf.get("description", ""),
                                     "timeout": sf.get("timeout", 0)}
        elif sf.get("name"):
            node["unified_job_template"] = sf["name"]
        else:
            notes.append(f"workflow '{wf_name}': node {node['identifier']} has no template (deleted) — omitted")
            continue
        for rel in ("success_nodes", "failure_nodes", "always_nodes"):
            ids = [ident[i] for i in (n.get(rel) or []) if i in ident]
            if ids:
                node[rel] = ids
        if n.get("all_parents_must_converge"):
            node["all_parents_must_converge"] = True
        if n.get("extra_data"):
            node["extra_data"] = n["extra_data"]
        out.append(node)
    return out


def objects_from_migration(work_dir: Path, only_imported: bool = True) -> dict[str, list[dict]]:
    """RAW export objects, optionally only those the state DB marks completed (or pre-existing)."""
    from aap_migration.analysis.premigration import load_inventory_from_export

    from migration_ai.toolkit import DB_RELATIVE, StateDB

    inv = load_inventory_from_export(work_dir / "exports")
    if only_imported:
        ok = set()
        for r in StateDB(work_dir / DB_RELATIVE).import_rows():
            msg = r.get("error_message") or ""
            if r["status"] == "completed" or (r["status"] == "skipped" and "Pre-existing" in msg):
                ok.add((r["resource_type"], int(r["source_id"])))
        if ok:
            inv = {t: [o for o in items if (t, o.get("_source_id", o.get("id"))) in ok
                       or t in ("organizations", "credential_types", "instance_groups", "execution_environments")]
                   for t, items in inv.items()}
    _annotate(inv)
    return inv


def _annotate(inv: dict[str, list[dict]]) -> None:
    """Fill helper fields used by the converter (org of schedules / inventory children)."""
    org_names = {o["id"]: o["name"] for o in inv.get("organizations", []) if "id" in o}
    inv_org = {i.get("id"): _org(i, org_names) for i in inv.get("inventories", [])}
    ujt_org = {}
    for t, ujt_type in (("job_templates", "job"), ("workflow_job_templates", "workflow_job"),
                        ("projects", "project_update"), ("inventory_sources", "inventory_update")):
        for o in inv.get(t, []):
            ujt_org[(ujt_type, o.get("id"))] = _org(o, org_names) or inv_org.get(o.get("inventory"))
    for t in ("inventory_sources", "hosts"):
        for o in inv.get(t, []):
            o.setdefault("_inventory_org", inv_org.get(o.get("inventory")))
    for s in inv.get("schedules", []):
        ujt = _sf(s).get("unified_job_template") or {}
        s.setdefault("_parent_org", ujt_org.get((ujt.get("unified_job_type"), s.get("unified_job_template"))))


def objects_from_live(controller_url: str, token: str, verify: bool = True, include_hosts: bool = False,
                      organization: str | None = None) -> dict[str, list[dict]]:
    """Read the target AAP objects from the controller API (read-only GETs)."""
    from urllib.parse import urlsplit

    import httpx

    base = controller_url.rstrip("/")
    parts = urlsplit(base)
    root = f"{parts.scheme}://{parts.netloc}"
    client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, verify=verify, timeout=60)

    def get_all(path: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        url: str | None = f"{base}/{path.strip('/')}/"
        p: dict | None = {"page_size": 200, **(params or {})}
        while url:
            r = client.get(url, params=p)
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("results", []))
            nxt = data.get("next")
            url = (root + nxt if nxt and nxt.startswith("/") else nxt) or None
            p = None
        return items

    def get_one(path: str) -> dict:
        r = client.get(f"{base}/{path.strip('/')}/")
        return r.json() if r.status_code == 200 else {}

    types = [t for t in CASC_TYPES if t != "users" and (include_hosts or t != "hosts")]
    objs: dict[str, list[dict]] = {}
    for t in types:
        params = {}
        if organization and t in ("teams", "credentials", "projects", "inventories", "job_templates",
                                  "workflow_job_templates", "notification_templates"):
            params["organization__name__iexact"] = organization
        objs[t] = get_all(t, params)
    for jt in objs.get("job_templates", []):
        if jt.get("survey_enabled"):
            jt["survey_spec"] = get_one(f"job_templates/{jt['id']}/survey_spec")
        jt["_instance_groups"] = get_one(f"job_templates/{jt['id']}/instance_groups").get("results", [])
    for wf in objs.get("workflow_job_templates", []):
        wf["nodes"] = get_all(f"workflow_job_templates/{wf['id']}/workflow_nodes")
        if wf.get("survey_enabled"):
            wf["survey_spec"] = get_one(f"workflow_job_templates/{wf['id']}/survey_spec")
    client.close()
    _annotate(objs)
    return objs


class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _str_presenter)


def dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)


PLAYBOOK = """---
# Apply the generated Configuration as Code to the target AAP.
#   ansible-galaxy collection install -r requirements.yml
#   ansible-playbook configure.yml -e casc_scope=_global          # shared objects first
#   ansible-playbook configure.yml -e casc_scope=<organization>   # then each organization
# Connection: export AAP_HOSTNAME, AAP_TOKEN (or AAP_USERNAME/AAP_PASSWORD), AAP_VALIDATE_CERTS
- name: Configure the target AAP from code
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    casc_scope: _global
    aap_hostname: "{{ lookup('env', 'AAP_HOSTNAME') }}"
    aap_token: "{{ lookup('env', 'AAP_TOKEN') | default(omit, true) }}"
    aap_username: "{{ lookup('env', 'AAP_USERNAME') | default(omit, true) }}"
    aap_password: "{{ lookup('env', 'AAP_PASSWORD') | default(omit, true) }}"
    aap_validate_certs: "{{ lookup('env', 'AAP_VALIDATE_CERTS') | default(true, true) | bool }}"
  pre_tasks:
    - name: Load the objects of the selected scope
      ansible.builtin.include_vars:
        dir: "{{ playbook_dir }}/orgs/{{ casc_scope }}"
        extensions: [yml]
    - name: Load secret values (fill env_variables.yml, ideally with ansible-vault)
      ansible.builtin.include_vars:
        file: "{{ playbook_dir }}/env_variables.yml"
  roles:
    - infra.aap_configuration.dispatch
"""

REQUIREMENTS = """---
collections:
  - name: infra.aap_configuration
    version: ">=4.10.0"
  - name: ansible.platform      # certified, from Automation Hub
  - name: ansible.controller    # certified, from Automation Hub
"""


def write_casc(scopes: dict[str, dict[str, list]], secrets: dict[str, str], notes: list[str], out_dir: Path,
               source: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for scope, vars_ in sorted(scopes.items()):
        sdir = out_dir / "orgs" / (scope if scope == "_global" else slug(scope))
        sdir.mkdir(parents=True, exist_ok=True)
        for _rtype, var, fname, _g in TYPE_MAP:
            if vars_.get(var):
                (sdir / fname).write_text("---\n" + dump({var: vars_[var]}), encoding="utf-8")
                files.append(str((sdir / fname).relative_to(out_dir)))
    (out_dir / "env_variables.yml").write_text(
        "---\n# Secret values referenced by the CasC. Encrypt this file with ansible-vault.\n"
        + dump(dict(sorted(secrets.items())) or {}), encoding="utf-8")
    (out_dir / "configure.yml").write_text(PLAYBOOK, encoding="utf-8")
    (out_dir / "requirements.yml").write_text(REQUIREMENTS, encoding="utf-8")
    counts = {scope: {var: len(items) for var, items in v.items()} for scope, v in scopes.items()}
    validation = validate_tree(out_dir)
    summary = {"source": source, "collection": f"{SCHEMA['collection']} {SCHEMA['version']}",
               "scopes": counts, "secrets": len(secrets), "files": files, "notes": notes,
               "validation": validation}
    readme = ["# Configuration as Code — target AAP", "",
              f"Generated from **{source}** in the `{SCHEMA['collection']}` format (collection {SCHEMA['version']}).", "",
              "Apply order: `_global` first (credential types, execution environments, instance groups), then each "
              "organization. Fill `env_variables.yml` (secrets) before applying.", "",
              "```bash", "ansible-galaxy collection install -r requirements.yml",
              "export AAP_HOSTNAME=https://aap26.example.com AAP_TOKEN=...",
              "ansible-playbook configure.yml -e casc_scope=_global"]
    readme += [f"ansible-playbook configure.yml -e casc_scope={slug(s)}" for s in sorted(scopes) if s != "_global"]
    readme += ["```", "", "## Content", "", "| Scope | Objects |", "|---|---|"]
    for scope, c in sorted(counts.items()):
        readme.append(f"| {scope} | " + ", ".join(f"{k}: {v}" for k, v in c.items()) + " |")
    if notes:
        readme += ["", "## Notes", ""] + [f"* {n}" for n in notes[:100]]
    readme += ["", "## Validation", "", f"Errors: {len(validation['errors'])}, warnings: {len(validation['warnings'])}"]
    readme += [f"* {e}" for e in validation["errors"][:50]] + [f"* {w}" for w in validation["warnings"][:50]]
    (out_dir / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    (out_dir / "casc-summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def validate_documents(docs: dict[str, Any], known_names: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    """Check CasC variables against the infra.aap_configuration field lists."""
    errors: list[str] = []
    warnings: list[str] = []
    known = known_names or defaultdict(set)
    for var, items in docs.items():
        if var not in ALLOWED:
            if var.startswith(("vaulted_", "aap_hostname", "aap_token", "aap_username", "aap_password",
                               "aap_validate_certs")):
                continue
            warnings.append(f"'{var}' is not a dispatch variable of {SCHEMA['collection']} {SCHEMA['version']}")
            continue
        if not isinstance(items, list):
            errors.append(f"'{var}' must be a list")
            continue
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                errors.append(f"{var}[{i}] must be a mapping")
                continue
            ident = it.get("name") or it.get("username")
            if not ident:
                errors.append(f"{var}[{i}] has no name")
            unknown = set(it) - ALLOWED[var]
            if unknown:
                errors.append(f"{var} '{ident}': unknown field(s) {sorted(unknown)}")
            for n in it.get("simplified_workflow_nodes") or []:
                bad = set(n) - NODE_FIELDS
                if bad:
                    errors.append(f"{var} '{ident}': unknown workflow node field(s) {sorted(bad)}")
    refs = {"controller_templates": [("project", "controller_projects"), ("inventory", "controller_inventories")],
            "controller_inventory_sources": [("inventory", "controller_inventories"),
                                             ("source_project", "controller_projects")],
            "controller_schedules": [("unified_job_template", "_ujt")]}
    known = dict(known)
    known["_ujt"] = set().union(*(known.get(v, set()) for v in (
        "controller_templates", "controller_workflows", "controller_projects", "controller_inventory_sources")))
    for var, checks in refs.items():
        for it in docs.get(var, []) or []:
            if not isinstance(it, dict):
                continue
            for field, target in checks:
                val = it.get(field)
                if val and val not in known.get(target, set()):
                    warnings.append(f"{var} '{it.get('name')}': {field} '{val}' is not defined in this CasC "
                                    "(must already exist on the target AAP or be applied from another scope)")
    for wf in docs.get("controller_workflows", []) or []:
        if not isinstance(wf, dict):
            continue
        for n in wf.get("simplified_workflow_nodes") or []:
            ujt = n.get("unified_job_template") if isinstance(n, dict) else None
            if ujt and ujt not in known["_ujt"]:
                warnings.append(f"controller_workflows '{wf.get('name')}': node '{n.get('identifier')}' uses "
                                f"'{ujt}', not defined in this CasC (it may have failed the migration)")
    return {"errors": errors, "warnings": warnings}


def validate_tree(root: Path) -> dict[str, list[str]]:
    docs_by_file = {}
    for f in sorted(root.rglob("*.yml")):
        if f.name in ("configure.yml", "requirements.yml", "env_variables.yml"):
            continue
        try:
            docs_by_file[f] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            return {"errors": [f"{f}: invalid YAML: {exc}"], "warnings": []}
    known: dict[str, set[str]] = defaultdict(set)
    merged: dict[str, list] = defaultdict(list)
    for d in docs_by_file.values():
        for var, items in d.items():
            if isinstance(items, list):
                merged[var].extend(items)
                known[var].update(i.get("name") for i in items if isinstance(i, dict) and i.get("name"))
    return validate_documents(dict(merged), known)


def validate_text(text: str) -> dict[str, list[str]]:
    try:
        docs = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return {"errors": [f"invalid YAML: {exc}"], "warnings": []}
    if not isinstance(docs, dict):
        return {"errors": ["the document must be a mapping of dispatch variables"], "warnings": []}
    known: dict[str, set[str]] = defaultdict(set)
    for var, items in docs.items():
        if isinstance(items, list):
            known[var].update(i.get("name") for i in items if isinstance(i, dict) and i.get("name"))
    return validate_documents(docs, known)


def schema_reference(var: str | None = None) -> dict:
    if var:
        return {var: sorted(ALLOWED.get(var, []))}
    return {v: s["role"] for v, s in SCHEMA["vars"].items()}
