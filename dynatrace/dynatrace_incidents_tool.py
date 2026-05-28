#!/usr/bin/env python3
"""CLI para consultar incidentes abertos no Dynatrace SaaS.

Suporta dois modos de autenticacao (auto-detectados):
  1. Classic API  - /api/v2/problems  (Api-Token + scope problems.read)
  2. Platform DQL - /platform/storage/query/v1/query:execute  (Bearer JWT)

Variaveis de ambiente:
  DYNATRACE_ENV_URL          URL base do ambiente (ex: https://xxx.live.dynatrace.com)
  DYNATRACE_API_TOKEN        Access token classico (Api-Token)
                             Scopes: problems.read, entities.read, metrics.read, events.read
  DYNATRACE_PLATFORM_TOKEN   Platform/OAuth token (Bearer JWT)
                             Scopes: storage:events:read, storage:metrics:read, storage:entities:read,
                             storage:buckets:read, storage:bucket-definitions:read,
                             environment-api:problems:read, app-engine:apps:run,
                             app-engine:edge-connects:read, app-settings:objects:read,
                             settings:objects:write, extensions:definitions:read,
                             extensions:configurations:read, slo:slos:read,
                             document:documents:read, automation:workflows:read
  DYNATRACE_AUTH_MODE        auto | classic | dql  (padrao: auto)
  DASHBOARD_URLS             URLs de dashboards separados por virgula

Uso:
  ./view_dyna_incidents "rds timeout"
  ./view_dyna_incidents --query "rds timeout" --limit 10
  ./view_dyna_incidents --diagnose
"""

from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import getpass
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# config.ini — configurações gerenciáveis sem alterar código
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_CONFIG_FILE = _SCRIPT_DIR / "config.ini"

_INI = configparser.ConfigParser()
if _CONFIG_FILE.exists():
    _INI.read(_CONFIG_FILE, encoding="utf-8")


def _ini_get(section: str, key: str, fallback: str = "") -> str:
    """Lê valor do config.ini com fallback."""
    return _INI.get(section, key, fallback=fallback).strip()


def _ini_int(section: str, key: str, fallback: int = 0) -> int:
    """Lê inteiro do config.ini com fallback."""
    return _INI.getint(section, key, fallback=fallback)


def _ini_float(section: str, key: str, fallback: float = 0.0) -> float:
    """Lê float do config.ini com fallback."""
    return _INI.getfloat(section, key, fallback=fallback)


# Defaults do config.ini (com fallbacks hardcoded caso o arquivo não exista)
DEFAULT_WINDOW = _ini_get("defaults", "window", "5m")
DEFAULT_LIMIT = _ini_int("defaults", "limit", 5)
DEFAULT_QUERY_TIMEOUT = _ini_int("defaults", "query_timeout", 120)
DEFAULT_ENTITY_TIMEOUT = _ini_int("defaults", "entity_timeout", 8)
DEFAULT_MAX_ENTITIES = _ini_int("defaults", "max_entities_per_incident", 2)
DEFAULT_MAX_CALLERS = _ini_int("defaults", "max_callers_per_entity", 2)


# ---------------------------------------------------------------------------
# Caches de token interativo
# ---------------------------------------------------------------------------
_CACHE_API_TOKEN = Path("/tmp/dynatrace-incidentes-api-token")
_CACHE_PLATFORM_TOKEN = Path("/tmp/dynatrace-incidentes-platform-token")

_DEFAULT_ENV_URL = ""


def _normalize_token(raw: str) -> str:
    value = str(raw or "").strip().strip('"').strip("'")
    if not value:
        return ""

    # Aceita entradas copiadas como: export DYNATRACE_API_TOKEN=dt0c01...
    if value.startswith("export "):
        value = value[len("export "):].strip()
    if "=" in value and value.split("=", 1)[0].upper().startswith("DYNATRACE_"):
        value = value.split("=", 1)[1].strip().strip('"').strip("'")

    return value


def _read_cache(path: Path) -> str:
    if path.exists():
        return _normalize_token(path.read_text(encoding="utf-8"))
    return ""


def _write_cache(path: Path, value: str) -> None:
    path.write_text(_normalize_token(value), encoding="utf-8")
    os.chmod(path, 0o600)


def _prompt_token(kind: str, hint: str, cache_path: Path) -> str:
    print()
    if hint:
        print(hint)
        print()
    token = _normalize_token(getpass.getpass(f"Cole aqui o {kind}: "))
    if not token:
        raise ValueError(f"{kind} nao informado")
    _write_cache(cache_path, token)
    print(f"Token salvo em cache: {cache_path}")
    return token


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Config:
    env_url: str
    api_token: str
    platform_token: str
    auth_mode: str
    dashboard_urls: list[str] = field(default_factory=list)


@dataclass
class TimeFilter:
    from_ms: int | None = None
    to_ms: int | None = None
    open_only: bool = True
    dql_from: str | None = None
    dql_to: str | None = None


def load_config() -> Config:
    env_url = os.getenv("DYNATRACE_ENV_URL", _DEFAULT_ENV_URL).strip().rstrip("/")

    api_token = (
        os.getenv("DYNATRACE_API_TOKEN", "").strip()
        or _read_cache(_CACHE_API_TOKEN)
    )
    platform_token = (
        os.getenv("DYNATRACE_PLATFORM_TOKEN", "").strip()
        or _read_cache(_CACHE_PLATFORM_TOKEN)
    )

    auth_mode = os.getenv("DYNATRACE_AUTH_MODE", "auto").strip().lower()
    if auth_mode not in {"auto", "classic", "dql"}:
        raise ValueError("DYNATRACE_AUTH_MODE invalido. Use: auto, classic ou dql")

    dashboards_raw = os.getenv("DASHBOARD_URLS", "").strip()
    dashboard_urls = [u.strip() for u in dashboards_raw.split(",") if u.strip()]

    # Prompt interativo apenas se estritamente necessario
    if auth_mode == "classic" and not api_token:
        api_token = _prompt_token(
            "DYNATRACE_API_TOKEN (classic)",
            "Crie um Access Token em https://<tenant>.live.dynatrace.com/ui/access-tokens\n"
            "Scopes: problems.read, entities.read, metrics.read, events.read\n"
            "\n"
            ">>> IMPORTANTE: Leia o README.md para a lista completa de permissoes necessarias! <<<",
            _CACHE_API_TOKEN,
        )
    elif auth_mode == "dql" and not platform_token:
        platform_token = _prompt_token(
            "DYNATRACE_PLATFORM_TOKEN (DQL/Grail)",
            "Crie um Platform token em https://myaccount.dynatrace.com -> Security -> Access Tokens\n"
            "Scopes:\n"
            "  storage:events:read, storage:metrics:read, storage:entities:read\n"
            "  storage:buckets:read, storage:bucket-definitions:read\n"
            "  environment-api:problems:read, app-engine:apps:run\n"
            "  app-engine:edge-connects:read, app-settings:objects:read\n"
            "  settings:objects:write, extensions:definitions:read\n"
            "  extensions:configurations:read, slo:slos:read\n"
            "  document:documents:read, automation:workflows:read\n"
            "\n"
            ">>> IMPORTANTE: Leia o README.md para a lista completa de permissoes necessarias! <<<",
            _CACHE_PLATFORM_TOKEN,
        )
    elif auth_mode == "auto" and not api_token and not platform_token:
        print("Nenhum token encontrado. Vamos configurar o acesso:")
        print()
        print("Opcao 1 (recomendado): Access Token classico")
        print(f"  -> Acesse: {env_url}/ui/access-tokens")
        print("  -> Scopes: problems.read, entities.read, metrics.read, events.read")
        print()
        print("Opcao 2: Platform token (Bearer JWT para DQL/Grail)")
        print("  -> Acesse: https://myaccount.dynatrace.com -> Security -> Access Tokens")
        print("  -> Scopes:")
        print("       storage:events:read, storage:metrics:read, storage:entities:read")
        print("       storage:buckets:read, storage:bucket-definitions:read")
        print("       environment-api:problems:read, app-engine:apps:run")
        print("       app-engine:edge-connects:read, app-settings:objects:read")
        print("       settings:objects:write, extensions:definitions:read")
        print("       extensions:configurations:read, slo:slos:read")
        print("       document:documents:read, automation:workflows:read")
        print()
        print("  >>> IMPORTANTE: Leia o README.md para a lista completa de permissoes necessarias! <<<")
        print()
        choice = input("Qual token voce tem? [1/2] ").strip()
        if choice == "2":
            platform_token = _prompt_token("DYNATRACE_PLATFORM_TOKEN", "", _CACHE_PLATFORM_TOKEN)
        else:
            api_token = _prompt_token("DYNATRACE_API_TOKEN", "", _CACHE_API_TOKEN)

    return Config(
        env_url=env_url,
        api_token=api_token,
        platform_token=platform_token,
        auth_mode=auth_mode,
        dashboard_urls=dashboard_urls,
    )


# ---------------------------------------------------------------------------
# Helpers HTTP
# ---------------------------------------------------------------------------
def _short_body(text: str, max_len: int = 300) -> str:
    compact = " ".join(text.split())
    return compact[:max_len] + ("..." if len(compact) > max_len else "")


def _get(url: str, headers: dict[str, str], params: dict[str, Any] | None = None, timeout: int = 30) -> requests.Response:
    return requests.get(url, headers=headers, params=params, timeout=timeout)


def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 30) -> requests.Response:
    return requests.post(url, headers={**headers, "Content-Type": "application/json"}, json=payload, timeout=timeout)


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _parse_relative_window(value: str) -> tuple[int, str]:
    """Return (delta_ms, normalized_value) e.g. (10800000, '3h')."""
    v = value.strip().lower()
    # Aceita numero puro → assume horas
    if re.fullmatch(r"\d+", v):
        v = v + "h"
        print(f"Aviso: --window={value.strip()} sem unidade, assumindo {v} (horas)."
              " Use sufixo explicito: s, m, h, d", file=sys.stderr)
    match = re.fullmatch(r"(\d+)\s*([smhd])", v)
    if not match:
        raise ValueError("Formato invalido para --window. Use exemplos: 10m, 5m, 2h, 1d")

    amount = int(match.group(1))
    unit = match.group(2)
    unit_ms = {
        "s": 1000,
        "m": 60 * 1000,
        "h": 60 * 60 * 1000,
        "d": 24 * 60 * 60 * 1000,
    }
    return amount * unit_ms[unit], f"{amount}{unit}"


def _normalize_datetime_text(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"\s+", " ", v)

    # Aceita formatos como "01-01-2026 10h15m", "01-01-2026 11h00" e "01/01/2026 11:00"
    v = v.replace("/", "-")

    # Se termina com "Xh" sem minutos (ex: "13h"), adiciona ":00"
    v = re.sub(r"(\d{1,2})h$", r"\1:00", v)

    # Normalizar "XhYm" ou "Xh Y" para "X:Y"
    v = re.sub(r"(\d{1,2})h\s*(\d{1,2})m?", r"\1:\2", v)

    # Converte os h restantes para : e remove m remanescente
    v = v.replace("h", ":")
    v = v.replace("m", "")
    return v


def _parse_datetime_to_ms(value: str) -> int:
    normalized = _normalize_datetime_text(value)
    formats = ["%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S"]
    last_error: Exception | None = None
    for fmt in formats:
        try:
            dt = datetime.strptime(normalized, fmt)
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception as ex:
            last_error = ex
    raise ValueError(
        "Formato invalido para data/hora. Use exemplos: "
        "01-01-2026 10h15m, 01-01-2026 11h00, 01-01-2026 11:00"
    ) from last_error


def _resolve_time_filter(args: argparse.Namespace) -> TimeFilter:
    window_raw = (args.window or "").strip()
    from_raw = (args.from_time or "").strip()
    to_raw = (args.to_time or "").strip()

    if window_raw and (from_raw or to_raw):
        raise ValueError("Use --window ou --from/--to, nao ambos ao mesmo tempo")

    # Padrao: incidentes abertos na janela configurada em config.ini
    if not window_raw and not from_raw and not to_raw:
        delta_ms, window_norm = _parse_relative_window(DEFAULT_WINDOW)
        now_ms = _now_ms()
        return TimeFilter(
            from_ms=now_ms - delta_ms,
            to_ms=now_ms,
            open_only=True,
            dql_from=f"now()-{window_norm}",
            dql_to="now()",
        )

    # Janela relativa (ex.: 10m)
    if window_raw:
        delta_ms, window_norm = _parse_relative_window(window_raw)
        now_ms = _now_ms()
        return TimeFilter(
            from_ms=now_ms - delta_ms,
            to_ms=now_ms,
            open_only=False,
            dql_from=f"now()-{window_norm}",
            dql_to="now()",
        )

    # Janela absoluta
    if not (from_raw and to_raw):
        raise ValueError("Para intervalo absoluto informe ambos: --from e --to")

    from_ms = _parse_datetime_to_ms(from_raw)
    to_ms = _parse_datetime_to_ms(to_raw)
    if from_ms >= to_ms:
        raise ValueError("--from precisa ser menor que --to")

    # Helper to convert ms to ISO format
    def ms_to_iso(ms: int) -> str:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    return TimeFilter(
        from_ms=from_ms,
        to_ms=to_ms,
        open_only=False,
        dql_from=f'"{ms_to_iso(from_ms)}"',
        dql_to=f'"{ms_to_iso(to_ms)}"',
    )


# ---------------------------------------------------------------------------
# Classic API  (/api/v2/problems)
# ---------------------------------------------------------------------------
def _classic_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Api-Token {token}"}


def _classic_base_url(cfg: Config) -> str:
    base = cfg.env_url
    if ".apps.dynatrace.com" in base:
        base = base.replace(".apps.dynatrace.com", ".live.dynatrace.com")
    return base


def fetch_open_incidents_classic(
    cfg: Config,
    query: str,
    limit: int,
    time_filter: TimeFilter,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    url = f"{_classic_base_url(cfg)}/api/v2/problems"
    headers = _classic_headers(cfg.api_token)

    # API limita pageSize=10 quando fields esta presente
    params: dict[str, Any] = {
        "pageSize": 10,
        "fields": "+evidenceDetails,+affectedEntities",
    }
    if time_filter.open_only:
        params["problemSelector"] = 'status("OPEN")'
    if time_filter.from_ms is not None:
        params["from"] = time_filter.from_ms
    if time_filter.to_ms is not None:
        params["to"] = time_filter.to_ms

    q = query.strip().lower()
    results: list[dict[str, Any]] = []
    next_page_key: str | None = None

    while len(results) < limit:
        if next_page_key:
            params = {"nextPageKey": next_page_key}  # nextPageKey substitui todos os outros params
        resp = _get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        problems = body.get("problems", [])
        next_page_key = body.get("nextPageKey")

        for prob in problems:
            if q:
                text = json.dumps(prob, ensure_ascii=True).lower()
                if q not in text:
                    continue
            results.append(prob)
            if len(results) >= limit:
                break

        if not next_page_key or not problems:
            break

    return results


def _infer_origin_classic(prob: dict[str, Any]) -> str:
    tokens: list[str] = []
    for ent in prob.get("affectedEntities", []):
        tokens += [str(ent.get("name", "")), str(ent.get("entityId", "")), str(ent.get("entityType", ""))]
    root = prob.get("rootCauseEntity") or {}
    tokens += [str(root.get("name", "")), str(root.get("entityId", "")), str(root.get("entityType", ""))]
    for ev in (prob.get("evidenceDetails") or {}).get("details", []):
        tokens += [str(ev.get("displayName", "")), str(ev.get("rootCauseRelevance", ""))]
    corpus = " ".join(tokens).lower()

    if any(s in corpus for s in ["aws", "ec2", "rds", "alb", "nlb", "lambda", "eks", "s3", "cloudwatch", "dynamodb", "amazon"]):
        return "AWS"
    if any(s in corpus for s in ["azure", "aks", "vmss", "microsoft", "westus", "eastus", "brazilsouth", "southcentralus", "centralus"]):
        return "Azure"
    if any(s in corpus for s in ["gcp", "gke", "google", "cloud run", "bigquery"]):
        return "GCP"
    if "grafana" in corpus:
        return "Grafana"
    if any(s in corpus for s in ["kubernetes", "k8s", "openshift", "pod", "namespace", "cluster"]):
        return "Kubernetes"
    if root.get("entityType"):
        return f"Dynatrace/{root['entityType']}"
    return "Nao identificado"


def _extract_entity_ref(entity_item: dict[str, Any]) -> tuple[str, str]:
    entity_ref = entity_item.get("entityId")
    if isinstance(entity_ref, dict):
        return str(entity_ref.get("id", "")), str(entity_ref.get("type", ""))
    if isinstance(entity_ref, str):
        return entity_ref, ""
    return "", ""


def _fetch_entity_details(
    cfg: Config,
    entity_id: str,
    cache: dict[str, dict[str, Any]],
    timeout: int = 20,
) -> dict[str, Any] | None:
    if not entity_id:
        return None
    if entity_id in cache:
        return cache[entity_id]

    url = f"{_classic_base_url(cfg)}/api/v2/entities/{entity_id}"
    resp = _get(
        url,
        headers=_classic_headers(cfg.api_token),
        params={"fields": "+properties,+tags,+fromRelationships,+toRelationships"},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        return None

    data = resp.json()
    cache[entity_id] = data
    return data


def _resolve_k8s_metadata_from_entities(
    cfg: Config,
    entity_ids: list[str],
    cache: dict[str, dict[str, Any]] | None = None,
    timeout: int = 20,
) -> dict[str, str]:
    """Traverse SERVICE → PROCESS_GROUP → metadata to extract K8s namespace/workload/cluster.

    Also checks HOST entities for Azure resource group to infer cluster name.
    Returns dict with keys: k8s_namespace, k8s_workload_name, k8s_cluster.
    """
    if cache is None:
        cache = {}
    result: dict[str, str] = {"k8s_namespace": "", "k8s_workload_name": "", "k8s_cluster": ""}
    if not cfg.api_token:
        return result

    for eid in entity_ids[:5]:
        data = _fetch_entity_details(cfg, eid, cache=cache, timeout=timeout)
        if not data:
            continue
        from_rels = data.get("fromRelationships") or {}

        # Check process groups for K8s metadata
        pg_refs = from_rels.get("runsOn", [])
        for pg_ref in pg_refs[:3]:
            pg_id = pg_ref.get("id", "") if isinstance(pg_ref, dict) else str(pg_ref)
            if not pg_id.startswith("PROCESS_GROUP"):
                continue
            pg_data = _fetch_entity_details(cfg, pg_id, cache=cache, timeout=timeout)
            if not pg_data:
                continue
            pg_props = pg_data.get("properties") or {}
            metadata_list = pg_props.get("metadata") or []
            for m in metadata_list:
                if not isinstance(m, dict):
                    continue
                key = m.get("key", "")
                val = m.get("value", "")
                if key == "KUBERNETES_NAMESPACE" and val and not result["k8s_namespace"]:
                    result["k8s_namespace"] = val
                elif key == "KUBERNETES_CONTAINER_NAME" and val and not result["k8s_workload_name"]:
                    result["k8s_workload_name"] = val
            if result["k8s_namespace"] and result["k8s_workload_name"]:
                break

        # Check hosts for Azure resource group → cluster name
        if not result["k8s_cluster"]:
            host_refs = from_rels.get("runsOnHost", [])
            for host_ref in host_refs[:2]:
                host_id = host_ref.get("id", "") if isinstance(host_ref, dict) else str(host_ref)
                if not host_id.startswith("HOST"):
                    continue
                host_data = _fetch_entity_details(cfg, host_id, cache=cache, timeout=timeout)
                if not host_data:
                    continue
                host_props = host_data.get("properties") or {}
                rg = host_props.get("azureResourceGroupName", "")
                # MC_<rg>_<cluster-name>_<region>
                aks_match = re.match(r"^MC_[^_]+_([^_]+)_[a-z]+$", rg)
                if aks_match:
                    result["k8s_cluster"] = aks_match.group(1)
                    break

        if result["k8s_namespace"] and result["k8s_workload_name"]:
            break

    return result


def _execute_dql(cfg: Config, dql: str, timeout: int = 20) -> list[dict[str, Any]]:
    """Execute an arbitrary DQL query using the Platform token."""
    if not cfg.platform_token:
        return []
    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    headers = _dql_headers(cfg.platform_token)
    try:
        resp = _post(url, headers=headers, payload={"query": dql}, timeout=timeout)
        if resp.status_code >= 400:
            return []
        body = resp.json()
        records = body.get("records") or (body.get("result") or {}).get("records")
        if records is not None:
            return [r for r in records if isinstance(r, dict)]
        # Handle async polling
        request_token = body.get("requestToken")
        if not request_token:
            return []
        poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
        for _ in range(8):
            poll_resp = requests.get(
                poll_url, headers=headers,
                params={"request-token": request_token}, timeout=timeout,
            )
            if poll_resp.status_code >= 400:
                return []
            poll_body = poll_resp.json()
            poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
            if poll_records is not None:
                return [r for r in poll_records if isinstance(r, dict)]
            if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
                return []
            time.sleep(0.4)
    except Exception:
        pass
    return []


def _enrich_entity_via_dql(cfg: Config, entity_ids: list[str]) -> dict[str, str]:
    """Fetch account, region, platform, k8s info from entity metadata via DQL.

    Strategy:
    1. Query service entity names — parse account/region from name pattern
       (e.g. "svc-name-697446462027-us-east-1 in us-east-1")
    2. Look at callers (who calls this service) for the same name pattern
    3. Look at callees of callers for the same pattern
    4. Find matching cloud_application (K8s workload) for cluster/namespace

    Returns dict with keys: account, region, platform, k8s_cluster, k8s_namespace,
    cloud_aws_status.
    """
    result: dict[str, str] = {}
    if not entity_ids or not cfg.platform_token:
        return result

    svc_ids = [eid for eid in entity_ids if isinstance(eid, str) and eid.startswith("SERVICE-")]
    if not svc_ids:
        return result

    # Helper: extract account/region from entity name
    # Pattern: "name-<12digits>-<region> in <region>" or "name-<12digits>-<region>"
    _acct_re = re.compile(r"(\d{12})")
    _region_re = re.compile(r"\b([a-z]{2}-(?:east|west|south|north|central|northeast|southeast|northwest|southwest)-\d)\b")

    def _extract_from_name(name: str) -> tuple[str, str]:
        acct = ""
        region = ""
        m_acct = _acct_re.search(name)
        if m_acct:
            acct = m_acct.group(1)
        m_region = _region_re.search(name)
        if m_region:
            region = m_region.group(1)
        return acct, region

    # 1. Get names of affected services
    id_list = ", ".join(f'"{eid}"' for eid in svc_ids[:5])
    svc_recs = _execute_dql(
        cfg,
        f'fetch dt.entity.service'
        f' | filter in(id, array({id_list}))'
        f' | fields id, entity.name'
        f' | limit 5',
    )
    affected_names: list[str] = []
    for rec in svc_recs:
        name = rec.get("entity.name", "")
        if name:
            affected_names.append(name)
            acct, region = _extract_from_name(name)
            if acct and not result.get("account"):
                result["account"] = acct
            if region and not result.get("region"):
                result["region"] = region

    # 2. Look at callers (who calls this service) — topology traversal
    if not result.get("account"):
        for eid in svc_ids[:2]:
            caller_recs = _execute_dql(
                cfg,
                f'fetch dt.entity.service'
                f' | fieldsAdd calls[dt.entity.service]'
                f' | filter in("{eid}", calls[dt.entity.service])'
                f' | fields entity.name, id, calls[dt.entity.service]'
                f' | limit 5',
            )
            for caller in caller_recs:
                caller_name = caller.get("entity.name", "")
                acct, region = _extract_from_name(caller_name)
                if acct and not result.get("account"):
                    result["account"] = acct
                if region and not result.get("region"):
                    result["region"] = region

                # 3. Check callees of the caller — the Lambda/external service
                #    may have account in its name
                if not result.get("account"):
                    callees = caller.get("calls[dt.entity.service]") or []
                    if callees:
                        callee_ids = ", ".join(f'"{c}"' for c in callees[:10] if isinstance(c, str))
                        callee_recs = _execute_dql(
                            cfg,
                            f'fetch dt.entity.service'
                            f' | filter in(id, array({callee_ids}))'
                            f' | fields id, entity.name'
                            f' | limit 10',
                        )
                        for callee in callee_recs:
                            callee_name = callee.get("entity.name", "")
                            acct, region = _extract_from_name(callee_name)
                            if acct and not result.get("account"):
                                result["account"] = acct
                            if region and not result.get("region"):
                                result["region"] = region
                            if result.get("account") and result.get("region"):
                                break

            if result.get("account"):
                break

    # 4. Find matching cloud_application for K8s metadata
    #    Try affected names first, then caller names (e.g., "card-management-prod"
    #    may not match K8s workload "card-management-service")
    ca_search_names = list(affected_names)
    # Also add caller names for broader K8s matching
    for eid in svc_ids[:2]:
        caller_recs = _execute_dql(
            cfg,
            f'fetch dt.entity.service'
            f' | fieldsAdd calls[dt.entity.service]'
            f' | filter in("{eid}", calls[dt.entity.service])'
            f' | fields entity.name'
            f' | limit 3',
        )
        for cr in caller_recs:
            cn = cr.get("entity.name", "")
            if cn and cn not in ca_search_names:
                ca_search_names.append(cn)

    for svc_name in ca_search_names[:5]:
        if result.get("k8s_cluster") and result.get("k8s_namespace"):
            break
        # Use base name (remove account/region suffix if present)
        base_name = re.sub(r"-\d{12}-[a-z]{2}-\w+-\d.*$", "", svc_name)
        safe_name = base_name.replace('"', '\\"')
        ca_recs = _execute_dql(
            cfg,
            f'fetch dt.entity.cloud_application'
            f' | filter contains(entity.name, "{safe_name}")'
            f' | fieldsAdd belongs_to[dt.entity.cloud_application_namespace]'
            f', clustered_by[dt.entity.kubernetes_cluster]'
            f' | limit 3',
        )
        for ca in ca_recs:
            ns_id = ca.get("belongs_to[dt.entity.cloud_application_namespace]")
            cluster_id = ca.get("clustered_by[dt.entity.kubernetes_cluster]")

            if ns_id and not result.get("k8s_namespace"):
                if isinstance(ns_id, list):
                    ns_id = ns_id[0] if ns_id else ""
                if ns_id:
                    ns_recs = _execute_dql(
                        cfg,
                        f'fetch dt.entity.cloud_application_namespace'
                        f' | filter id == "{ns_id}"'
                        f' | fields entity.name'
                        f' | limit 1',
                    )
                    if ns_recs:
                        result["k8s_namespace"] = ns_recs[0].get("entity.name", "")

            if cluster_id and not result.get("k8s_cluster"):
                if isinstance(cluster_id, list):
                    cluster_id = cluster_id[0] if cluster_id else ""
                if cluster_id:
                    cl_recs = _execute_dql(
                        cfg,
                        f'fetch dt.entity.kubernetes_cluster'
                        f' | filter id == "{cluster_id}"'
                        f' | fields entity.name'
                        f' | limit 1',
                    )
                    if cl_recs:
                        result["k8s_cluster"] = cl_recs[0].get("entity.name", "")

    # 5. Query K8s node status for the cluster
    if result.get("k8s_cluster") and not result.get("k8s_nodes"):
        cluster_name = result["k8s_cluster"].replace('"', '\\"')
        node_recs = _execute_dql(
            cfg,
            f'fetch dt.entity.kubernetes_node'
            f' | filter clustered_by[dt.entity.kubernetes_cluster] == "{cluster_name}"'
            f'    OR contains(toString(clustered_by[dt.entity.kubernetes_cluster]), "{cluster_name}")'
            f' | fields entity.name, state, kubernetesLabels'
            f' | limit 50',
        )
        # Fallback: query nodes by cluster entity ID
        if not node_recs:
            # Find cluster ID for filter
            cl_id_recs = _execute_dql(
                cfg,
                f'fetch dt.entity.kubernetes_cluster'
                f' | filter entity.name == "{cluster_name}"'
                f' | fields id'
                f' | limit 1',
            )
            if cl_id_recs:
                cl_id = cl_id_recs[0].get("id", "")
                if cl_id:
                    node_recs = _execute_dql(
                        cfg,
                        f'fetch dt.entity.kubernetes_node'
                        f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
                        f' | filter in("{cl_id}", clustered_by[dt.entity.kubernetes_cluster])'
                        f' | fields entity.name, state'
                        f' | limit 50',
                    )
        if node_recs:
            nodes_info = []
            for nr in node_recs:
                node_name = nr.get("entity.name", "unknown")
                node_state = nr.get("state", nr.get("dt.kubernetes.node.status", "UNKNOWN"))
                if isinstance(node_state, dict):
                    node_state = node_state.get("state", "UNKNOWN")
                nodes_info.append(f"{node_name}={node_state}")
            result["k8s_nodes"] = nodes_info

    # Infer platform
    if not result.get("platform"):
        if result.get("k8s_cluster"):
            result["platform"] = "Kubernetes"

    # Cloud integration status
    result["cloud_aws_status"] = "ON" if result.get("account") else "OFF_DYNA"

    return result


def _fetch_event_details(
    cfg: Config,
    event_id: str,
    cache: dict[str, dict[str, Any]],
    timeout: int = 20,
) -> dict[str, Any] | None:
    if not event_id:
        return None
    if event_id in cache:
        return cache[event_id]

    url = f"{_classic_base_url(cfg)}/api/v2/events/{event_id}"
    resp = _get(url, headers=_classic_headers(cfg.api_token), timeout=timeout)
    if resp.status_code >= 400:
        return None

    data = resp.json()
    cache[event_id] = data
    return data


def _classify_platform_from_text(text: str) -> str:
    t = text.lower()
    # K8s/container signals take priority over Lambda (service may be on EKS but called via Lambda)
    if ("eks" in t or "containerd" in t) and ("kubernetes" in t or "cloud_application" in t or "container" in t):
        return "Kubernetes"
    if "aks" in t or "vmss" in t:
        return "Azure/AKS"
    if "eks" in t or "containerd" in t:
        return "Kubernetes"
    if "aws_lambda" in t or ("lambda" in t and "containerd" not in t):
        return "AWS Lambda"
    if "dynamodb" in t:
        return "AWS DynamoDB"
    if "rds" in t:
        return "AWS RDS"
    if any(s in t for s in ["azure", "microsoft", "brazilsouth", "eastus", "westus", "southcentralus"]):
        return "Azure"
    if any(s in t for s in ["gcp", "gke", "google", "cloud run"]):
        return "GCP"
    if "kubernetes" in t or "k8s" in t or "openshift" in t:
        return "Kubernetes"
    if "aws" in t or "amazon" in t:
        return "AWS"
    if "grafana" in t:
        return "Grafana"
    return "Nao identificado"


def _first_match(pattern: str, values: list[str]) -> str:
    for value in values:
        if not value:
            continue
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return ""


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _format_epoch_ms(value: Any) -> str:
    try:
        v = int(value)
        if v <= 0:
            return "em aberto"
        dt = datetime.fromtimestamp(v / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "nao informado"


def _metric_label(name: str) -> str:
    pretty = name.replace("_", " ").strip()
    mapping = {
        "error rate": "Taxa de falha",
        "response time": "Tempo de resposta",
        "cpu": "CPU",
        "memory": "Memoria",
        "load": "Carga",
        "total load": "Carga total",
        "affected load": "Carga afetada",
    }
    return mapping.get(pretty, pretty.capitalize() if pretty else "Metrica")


def _format_metric_value(metric_name: str, value: float) -> str:
    m = metric_name.lower()
    if "rate" in m or "ratio" in m or "failure" in m or "error" in m:
        # Muitas metricas de taxa no evento vem em [0,1]
        if 0.0 <= value <= 1.0:
            return f"{value * 100:.2f}%"
        return f"{value:.2f}%"
    if "cpu" in m or "util" in m:
        if 0.0 <= value <= 1.0:
            return f"{value * 100:.2f}%"
        return f"{value:.2f}%"
    if "time" in m or "latency" in m:
        return f"{value:.2f} ms"
    return f"{value:.2f}"


def _extract_metric_hints_from_dql(dql_query: str) -> str:
    if not dql_query:
        return ""

    hints: list[str] = []
    q = dql_query.lower()

    if "dt.service.request.failure_count" in q and "dt.service.request.count" in q:
        hints.append("falhas/total de requisicoes")
    if "cpu" in q:
        hints.append("cpu")
    if "response" in q or "latency" in q:
        hints.append("tempo de resposta")

    return ", ".join(hints)


def _extract_threshold_summary_from_event(event: dict[str, Any]) -> list[str]:
    summary: list[str] = []
    props_list = event.get("properties") or []
    props: dict[str, str] = {}
    for item in props_list:
        if isinstance(item, dict):
            key = str(item.get("key", ""))
            value = str(item.get("value", ""))
            if key:
                props[key] = value

    baseline_prefix = "dt.event.baseline."
    handled: set[str] = set()

    for key, raw_value in props.items():
        if not key.startswith(baseline_prefix):
            continue
        if key.endswith("_reference"):
            continue

        metric_key = key[len(baseline_prefix):]
        if metric_key in handled:
            continue

        ref_key = f"{baseline_prefix}{metric_key}_reference"
        current = _to_float(raw_value)
        reference = _to_float(props.get(ref_key, ""))
        if current is None or reference is None:
            continue

        direction = "acima" if current >= reference else "abaixo"
        delta = abs(current - reference)
        metric_label = _metric_label(metric_key)
        current_fmt = _format_metric_value(metric_key, current)
        ref_fmt = _format_metric_value(metric_key, reference)
        delta_fmt = _format_metric_value(metric_key, delta)
        summary.append(
            f"{metric_label}: limite/referencia {ref_fmt}, atual {current_fmt} ({direction} {delta_fmt})"
        )
        handled.add(metric_key)

    # Tenta extrair mensagem direta do evento para casos sem pares current/reference.
    description = props.get("dt.event.description", "").strip()
    if description:
        first_line = description.splitlines()[0].strip()
        if first_line and first_line not in summary:
            summary.append(first_line)

    return summary[:5]


def _extract_threshold_detailed_from_event(event: dict[str, Any]) -> list[str]:
    details: list[str] = []

    start_time = _format_epoch_ms(event.get("startTime"))
    end_time = _format_epoch_ms(event.get("endTime"))
    details.append(f"janela: inicio {start_time}, fim {end_time}")

    props_list = event.get("properties") or []
    props: dict[str, str] = {}
    for item in props_list:
        if isinstance(item, dict):
            key = str(item.get("key", ""))
            value = str(item.get("value", ""))
            if key:
                props[key] = value

    timeout_raw = props.get("dt.event.timeout", "")
    timeout_num = _to_float(timeout_raw)
    if timeout_num is not None:
        details.append(f"janela de avaliacao configurada: {timeout_num:.0f} min")

    dql_query = props.get("dt.event.dql_query", "")
    dql_hint = _extract_metric_hints_from_dql(dql_query)
    if dql_hint:
        details.append(f"fonte/metodo da metrica: {dql_hint}")

    baseline_prefix = "dt.event.baseline."
    for key, raw_value in props.items():
        if not key.startswith(baseline_prefix) or key.endswith("_reference"):
            continue

        metric_key = key[len(baseline_prefix):]
        reference = _to_float(props.get(f"{baseline_prefix}{metric_key}_reference", ""))
        current = _to_float(raw_value)
        if reference is None or current is None:
            continue

        metric_label = _metric_label(metric_key)
        current_fmt = _format_metric_value(metric_key, current)
        ref_fmt = _format_metric_value(metric_key, reference)
        delta = current - reference
        delta_fmt = _format_metric_value(metric_key, abs(delta))
        if delta >= 0:
            direction = "ultrapassou"
        else:
            direction = "ficou abaixo"
        details.append(f"{metric_label}: limite/referencia {ref_fmt}, atual {current_fmt}, {direction} em {delta_fmt}")

    desc = props.get("dt.event.description", "").strip()
    if desc:
        details.append(f"descricao: {desc.splitlines()[0].strip()}")

    return details[:8]


def _extract_threshold_summary(prob: dict[str, Any], enriched_events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []

    for event in enriched_events:
        event_title = str(event.get("title", "")).strip()
        event_type = str(event.get("eventType", "")).strip()
        parts = _extract_threshold_summary_from_event(event)
        for part in parts:
            if event_title:
                lines.append(f"{event_title}: {part}")
            elif event_type:
                lines.append(f"{event_type}: {part}")
            else:
                lines.append(part)

    if lines:
        unique_lines = []
        seen = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            unique_lines.append(line)
        return unique_lines[:6]

    # Fallback: tenta algo do proprio problema
    ev_details = (prob.get("evidenceDetails") or {}).get("details", [])
    for detail in ev_details:
        if not isinstance(detail, dict):
            continue
        label = str(detail.get("displayName", "")).strip()
        event_type = str(detail.get("eventType", "")).strip()
        if label:
            lines.append(label)
        elif event_type:
            lines.append(event_type)

    return lines[:3]


def _extract_threshold_detailed(prob: dict[str, Any], enriched_events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []

    for event in enriched_events:
        title = str(event.get("title", "")).strip() or str(event.get("eventType", "")).strip() or "Evento"
        for detail in _extract_threshold_detailed_from_event(event):
            lines.append(f"{title}: {detail}")

    if lines:
        uniq: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line in seen:
                continue
            seen.add(line)
            uniq.append(line)
        return uniq[:10]

    # Fallback minimo quando nao ha dados de evento detalhado
    return _extract_threshold_summary(prob, enriched_events)


def _unique_join(values: list[str], max_items: int = 5) -> str:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        v = str(value).strip()
        if not v:
            continue
        if v in seen:
            continue
        seen.add(v)
        ordered.append(v)
        if len(ordered) >= max_items:
            break
    return ", ".join(ordered)


def _extract_extended_metadata(
    prob: dict[str, Any],
    enriched_entities: list[dict[str, Any]],
) -> dict[str, str]:
    region_candidates: list[str] = []
    account_candidates: list[str] = []
    cloud_provider_candidates: list[str] = []
    cloud_service_candidates: list[str] = []
    cloud_id_candidates: list[str] = []
    service_type_candidates: list[str] = []
    azure_subscription_candidates: list[str] = []
    azure_resource_group_candidates: list[str] = []
    tech_candidates: list[str] = []
    cluster_candidates: list[str] = []
    namespace_candidates: list[str] = []
    entity_type_candidates: list[str] = []

    for entity in enriched_entities:
        props = entity.get("properties") or {}
        tags = entity.get("tags") or []
        entity_type_candidates.append(str(entity.get("type", "")))
        service_type_candidates.append(str(props.get("serviceType", "")))

        for tech in props.get("softwareTechnologies", []) or []:
            if isinstance(tech, dict):
                tech_candidates.append(str(tech.get("type", "")))

        # Campos comuns no Dynatrace para cloud metadata
        region_candidates.extend(
            [
                str(props.get("publicCloudRegion", "")),
                str(props.get("awsRegion", "")),
                str(props.get("region", "")),
                str(props.get("detectedName", "")),
                str(props.get("customizedName", "")),
                str(props.get("publicCloudId", "")),
            ]
        )

        account_candidates.extend(
            [
                str(props.get("awsAccountId", "")),
                str(props.get("accountId", "")),
                str(props.get("publicCloudId", "")),
                str(props.get("detectedName", "")),
                str(props.get("customizedName", "")),
            ]
        )

        cloud_id = str(props.get("publicCloudId", "")).strip()
        if cloud_id:
            cloud_id_candidates.append(cloud_id)
            arn_match = re.search(r"^arn:([^:]+):([^:]*):([^:]*):([^:]*):", cloud_id)
            if arn_match:
                cloud_provider_candidates.append(arn_match.group(1))
                cloud_service_candidates.append(arn_match.group(2))
                region_candidates.append(arn_match.group(3))
                account_candidates.append(arn_match.group(4))
            # Azure resource ID: /subscriptions/<uuid>/resourceGroups/<rg>/...
            azure_rid_match = re.search(
                r"/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/]+)",
                cloud_id, re.IGNORECASE,
            )
            if azure_rid_match:
                azure_subscription_candidates.append(azure_rid_match.group(1))
                rg_name = azure_rid_match.group(2)
                azure_resource_group_candidates.append(rg_name)
                # Resource group often contains region
                region_candidates.append(rg_name)
                cloud_provider_candidates.append("Azure")

        cloud_provider_candidates.extend(
            [
                str(props.get("cloudProvider", "")),
                str(props.get("databaseVendor", "")),
            ]
        )

        # Azure-specific properties from Dynatrace entity
        azure_sub = str(props.get("azureSubscriptionUuid", "") or props.get("AzureSubscriptionUuid", "")).strip()
        if azure_sub:
            azure_subscription_candidates.append(azure_sub)
        azure_rg = str(props.get("azureResourceGroupName", "") or props.get("resourceGroupName", "")).strip()
        if azure_rg:
            azure_resource_group_candidates.append(azure_rg)
            # Azure resource groups often contain the region (e.g., MC_rg-..._brazilsouth)
            region_candidates.append(azure_rg)
        azure_region = str(props.get("azureRegion", "") or props.get("azureLocation", "")).strip()
        if azure_region:
            region_candidates.append(azure_region)

        cluster_candidates.extend(
            [
                str(props.get("kubernetesCluster", "")),
                str(props.get("k8sClusterName", "")),
                str(props.get("clusterName", "")),
            ]
        )
        namespace_candidates.extend(
            [
                str(props.get("kubernetesNamespace", "")),
                str(props.get("k8sNamespace", "")),
                str(props.get("namespace", "")),
            ]
        )

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            account_candidates.append(str(tag.get("value", "")))
            account_candidates.append(str(tag.get("stringRepresentation", "")))
            region_candidates.append(str(tag.get("value", "")))
            region_candidates.append(str(tag.get("stringRepresentation", "")))
            key = str(tag.get("key", "")).lower()
            value = str(tag.get("value", ""))
            if "cluster" in key:
                cluster_candidates.append(value)
            if "namespace" in key:
                namespace_candidates.append(value)
            if "region" in key:
                region_candidates.append(value)
            if "account" in key:
                account_candidates.append(value)

    # Fallback: usa o payload do problema inteiro
    prob_blob = json.dumps(prob, ensure_ascii=True)
    region_candidates.append(prob_blob)
    account_candidates.append(prob_blob)
    cluster_candidates.append(prob_blob)
    namespace_candidates.append(prob_blob)

    # Ex.: us-east-1, sa-east-1, eu-west-2 (AWS)
    region = _first_match(r"\b([a-z]{2}-[a-z]+-\d)\b", region_candidates)
    # Azure regions: brazilsouth, eastus, eastus2, westeurope, etc.
    if not region:
        region = _first_match(
            r"(?<![a-zA-Z])(brazilsouth|brazilsoutheast|eastus|eastus2|westus|westus2|westus3|"
            r"centralus|northcentralus|southcentralus|westcentralus|"
            r"canadacentral|canadaeast|"
            r"westeurope|northeurope|uksouth|ukwest|francecentral|francesouth|"
            r"germanywestcentral|switzerlandnorth|norwayeast|swedencentral|"
            r"australiaeast|australiasoutheast|japaneast|japanwest|"
            r"southeastasia|eastasia|koreacentral|koreasouth|"
            r"centralindia|southindia|westindia|"
            r"southafricanorth|uaenorth)(?![a-zA-Z])",
            region_candidates,
        )
    # Conta AWS: 12 digitos (incluindo ARN)
    account = _first_match(r"\b(\d{12})\b", account_candidates)

    # Azure subscription UUID
    azure_subscription = _unique_join(azure_subscription_candidates, max_items=1)
    if not azure_subscription:
        azure_subscription = _first_match(
            r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
            azure_subscription_candidates,
        )
    azure_resource_group = _unique_join(azure_resource_group_candidates, max_items=1)

    # Fallback: subscription do ambiente (exportada pelo bash)
    if not azure_subscription:
        azure_subscription = os.environ.get("AZURE_SUBSCRIPTION_ID", "")

    # Resolve subscription name from env or az CLI
    azure_subscription_name = ""
    env_sub_id = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    env_sub_name = os.environ.get("AZURE_SUBSCRIPTION_NAME", "")
    if azure_subscription and env_sub_name:
        # Se o UUID bate com o env, usa o nome do env
        if azure_subscription == env_sub_id or not env_sub_id:
            azure_subscription_name = env_sub_name
    if azure_subscription and not azure_subscription_name:
        # Tenta resolver via az account list
        try:
            import subprocess as _sp
            _az_out = _sp.run(
                ["az", "account", "list", "--query", f"[?id=='{azure_subscription}'].name", "-o", "tsv"],
                capture_output=True, text=True, timeout=10,
            )
            if _az_out.returncode == 0 and _az_out.stdout.strip():
                azure_subscription_name = _az_out.stdout.strip()
        except Exception:
            pass

    cluster = _first_match(r"(?:cluster[:=\s/_-]+)([A-Za-z0-9._-]{4,})", cluster_candidates)
    # AKS: resource group MC_<rg>_<cluster-name>_<region> → extract cluster name
    if not cluster or cluster == "name":
        for rg in azure_resource_group_candidates:
            # MC_rg-xxx_clustername_region pattern
            aks_rg_match = re.match(r"^MC_[^_]+_([^_]+)_[a-z]+$", rg)
            if aks_rg_match:
                cluster = aks_rg_match.group(1)
                break
    # AKS nodes: aks-<nodepool>-<id>-vmss<suffix> → cluster pode ser inferido
    if not cluster or cluster == "name":
        for ent in enriched_entities:
            ent_name = str(ent.get("displayName", ""))
            aks_match = re.match(r"^(aks-[a-z0-9]+)-\d+-vmss", ent_name)
            if aks_match:
                cluster = ent_name
                break
    namespace = _first_match(r"(?:namespace[:=\s/_-]+)([A-Za-z0-9._-]+)", namespace_candidates)

    cloud_provider = _unique_join(cloud_provider_candidates, max_items=1)
    cloud_service = _unique_join(cloud_service_candidates, max_items=1)
    cloud_id = _unique_join(cloud_id_candidates, max_items=1)
    service_type = _unique_join(service_type_candidates, max_items=2)
    technologies = _unique_join(tech_candidates, max_items=4)
    entity_types = _unique_join(entity_type_candidates, max_items=4)

    if cloud_provider.lower() == "aws":
        cloud_provider = "AWS"
    elif cloud_provider.lower() in ("azure", "microsoft"):
        cloud_provider = "Azure"

    # Detectar Azure pelo nome de host AKS (aks-*-vmss*)
    if not cloud_provider or cloud_provider == "Nao identificado":
        for ent in enriched_entities:
            ent_name = str(ent.get("displayName", "")).lower()
            if "aks-" in ent_name and "vmss" in ent_name:
                cloud_provider = "Azure"
                break

    # Se tem subscription Azure, cloud_provider deve ser Azure
    if azure_subscription and (not cloud_provider or cloud_provider == "Nao identificado"):
        cloud_provider = "Azure"

    # Conta: se Azure e sem AWS account, usa subscription
    effective_account = account
    if not effective_account and azure_subscription:
        effective_account = azure_subscription

    return {
        "account_origin": effective_account or "Nao identificado",
        "region": region or "Nao identificado",
        "cloud_provider": cloud_provider or "Nao identificado",
        "cloud_service": cloud_service or "Nao identificado",
        "cloud_id": cloud_id or "Nao identificado",
        "service_type": service_type or "Nao identificado",
        "technologies": technologies or "Nao identificado",
        "k8s_cluster": cluster or "Nao identificado",
        "k8s_namespace": namespace or "Nao identificado",
        "entity_types": entity_types or "Nao identificado",
        "cloud_aws_status": "ON" if account else "OFF_DYNA",
        "azure_subscription": azure_subscription or "",
        "azure_subscription_name": azure_subscription_name or "",
        "azure_resource_group": azure_resource_group or "",
    }


def _build_workload_and_platform(
    prob: dict[str, Any],
    enriched_entities: list[dict[str, Any]],
) -> tuple[str, str, str]:
    workload_parts: list[str] = []
    platform_tokens: list[str] = []

    for entity in enriched_entities:
        e_type = str(entity.get("type", "")).strip() or "UNKNOWN"
        name = str(entity.get("displayName", "")).strip() or "sem-nome"
        props = entity.get("properties") or {}
        service_type = str(props.get("serviceType", "")).strip()
        techs = props.get("softwareTechnologies") or []
        tech_types = [str(t.get("type", "")) for t in techs if isinstance(t, dict)]

        if service_type:
            workload_parts.append(f"{name} [{e_type}/{service_type}]")
        else:
            workload_parts.append(f"{name} [{e_type}]")

        platform_tokens.append(name)
        platform_tokens.append(e_type)
        platform_tokens.append(service_type)
        platform_tokens.extend(tech_types)
        platform_tokens.append(str(props.get("detectedName", "")))
        platform_tokens.append(str(props.get("publicDomainName", "")))
        platform_tokens.append(str(props.get("publicCloudId", "")))
        platform_tokens.append(str(props.get("databaseVendor", "")))
        for host in props.get("databaseHostNames", []) or []:
            platform_tokens.append(str(host))

    if not workload_parts:
        for ent in prob.get("affectedEntities", []):
            ent_name = str(ent.get("name", "")).strip() or "sem-nome"
            ent_id, ent_type = _extract_entity_ref(ent)
            if ent_type:
                workload_parts.append(f"{ent_name} [{ent_type}]")
                platform_tokens.append(ent_type)
            else:
                workload_parts.append(ent_name)
            platform_tokens.append(ent_name)
            platform_tokens.append(ent_id)

    workload = ", ".join(workload_parts[:5]) if workload_parts else "Nao identificado"
    platform = _classify_platform_from_text(" ".join(platform_tokens))
    origin = platform if platform != "Nao identificado" else _infer_origin_classic(prob)
    return workload, platform, origin


def fetch_davis_analysis_classic(
    cfg: Config,
    problem_id: str,
    timeout: int = 20,
) -> dict[str, Any]:
    """Busca detalhes completos de um problema via Classic API (/api/v2/problems/{id}).

    Retorna dict com evidenceDetails, impactAnalysis, rootCauseEntity etc.
    """
    if not cfg.api_token or not problem_id:
        return {}

    base = _classic_base_url(cfg)
    url = f"{base}/api/v2/problems/{problem_id}"
    headers = _classic_headers(cfg.api_token)

    try:
        resp = _get(url, headers=headers,
                    params={"fields": "+evidenceDetails,+impactAnalysis"},
                    timeout=timeout)
        if resp.status_code >= 400:
            return {}
        return resp.json()
    except Exception:
        return {}


def _refine_platform(platform: str, metadata: dict[str, str]) -> str:
    """Refine platform when cloud provider context is available."""
    p = (platform or "").lower()
    cloud = (metadata.get("cloud_provider") or "").lower()
    # If platform is generic K8s but cloud is Azure → Azure/AKS
    if p in ("kubernetes", "k8s") and "azure" in cloud:
        return "Azure/AKS"
    # If platform is generic K8s but cloud is AWS → EKS
    if p in ("kubernetes", "k8s") and "aws" in cloud:
        return "Kubernetes"  # keep as-is, AWS EKS still shows as Kubernetes
    return platform


def to_output_item_classic(cfg: Config, prob: dict[str, Any]) -> dict[str, Any]:
    problem_id = prob.get("problemId", "")
    display_id = prob.get("displayId", problem_id or "N/A")
    title = prob.get("title", "Sem titulo")
    status = prob.get("status", "OPEN")
    base = _classic_base_url(cfg)
    problem_url = f"{base}/ui/problems/{problem_id}" if problem_id else base
    affected = [e.get("name", e.get("entityId", "")) for e in prob.get("affectedEntities", [])]
    enriched_entities = prob.get("_enriched_entities") or []
    enriched_events = prob.get("_enriched_events") or []
    workload, platform, origin = _build_workload_and_platform(prob, enriched_entities)
    metadata = _extract_extended_metadata(prob, enriched_entities)

    # Tenta extrair thresholds dos enriched_events
    threshold_summary = _extract_threshold_summary(prob, enriched_events)
    threshold_detailed = _extract_threshold_detailed(prob, enriched_events)

    # Se não tem thresholds (sem events.read), busca via /api/v2/problems/{id}
    if not threshold_summary and cfg.api_token and problem_id:
        prob_detail = fetch_davis_analysis_classic(cfg, problem_id)
        if prob_detail:
            ev_details = (prob_detail.get("evidenceDetails") or {}).get("details", [])
            inline_events = []
            for ev in ev_details:
                if isinstance(ev, dict):
                    ev_data = ev.get("data") or {}
                    if isinstance(ev_data, dict) and (ev_data.get("properties") or ev_data.get("eventType")):
                        inline_events.append(ev_data)
            if inline_events:
                threshold_summary = _extract_threshold_summary(prob, inline_events)
                threshold_detailed = _extract_threshold_detailed(prob, inline_events)

    return {
        "alarm": f"{display_id} - {title} ({status})",
        "problem_id": problem_id,
        "problem_url": problem_url,
        "dashboard_urls": cfg.dashboard_urls,
        "origin": origin,
        "platform": _refine_platform(platform, metadata),
        "workload": workload,
        **metadata,
        "threshold_summary": threshold_summary,
        "threshold_detailed": threshold_detailed,
        "affected_entities": affected,
    }


def _enrich_from_relationships(
    cfg: Config,
    entity: dict[str, Any],
    details: list[dict[str, Any]],
    cache: dict[str, dict[str, Any]],
    timeout: int = 20,
) -> None:
    """Para serviços sem metadata AWS direta, busca nas relações (callers, hosts, runsOn)."""
    props = entity.get("properties") or {}

    # Já tem metadata AWS? Não precisa buscar
    if props.get("publicCloudId") or props.get("awsRegion") or props.get("publicCloudRegion"):
        return

    def _has_aws_metadata(p: dict[str, Any]) -> bool:
        return bool(
            p.get("publicCloudId") or p.get("publicCloudRegion")
            or p.get("awsRegion") or p.get("awsAccountId")
        )

    def _try_fetch_related(entity_refs: list, max_refs: int = 3) -> bool:
        """Busca metadata AWS em entidades relacionadas. Retorna True se encontrou."""
        for ref in entity_refs[:max_refs]:
            ref_id = ""
            if isinstance(ref, dict):
                ref_id = str(ref.get("id", ""))
            elif isinstance(ref, str):
                ref_id = ref
            if not ref_id:
                continue
            ref_data = _fetch_entity_details(cfg, ref_id, cache=cache, timeout=timeout)
            if not ref_data:
                continue
            ref_props = ref_data.get("properties") or {}
            if _has_aws_metadata(ref_props):
                details.append(ref_data)
                return True
        return False

    to_rels = entity.get("toRelationships") or {}
    from_rels = entity.get("fromRelationships") or {}

    # 1. Callers (toRelationships.calls) — quem chama este serviço
    if _try_fetch_related(to_rels.get("calls", [])):
        return

    # 2. Hosts/infra (fromRelationships.runsOn) — onde o serviço roda
    if _try_fetch_related(from_rels.get("runsOn", [])):
        return

    # 3. Process groups (fromRelationships.isInstanceOf / runsOnHost)
    if _try_fetch_related(from_rels.get("isInstanceOf", [])):
        return

    # 4. Qualquer outra relação from que possa ter AWS metadata
    for rel_type, refs in from_rels.items():
        if rel_type in ("runsOn", "isInstanceOf"):
            continue  # Já tentamos
        if isinstance(refs, list) and _try_fetch_related(refs):
            return


def enrich_problems_classic(cfg: Config, problems: list[dict[str, Any]], timeout: int = 20) -> list[dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    event_cache: dict[str, dict[str, Any]] = {}
    enriched: list[dict[str, Any]] = []

    for prob in problems:
        p = dict(prob)
        details: list[dict[str, Any]] = []
        for ent in (prob.get("affectedEntities") or [])[:5]:
            ent_id, _ = _extract_entity_ref(ent)
            if not ent_id:
                continue
            data = _fetch_entity_details(cfg, ent_id, cache=cache, timeout=timeout)
            if data:
                details.append(data)
                # Para serviços externos sem AWS metadata, buscar nos callers
                _enrich_from_relationships(cfg, data, details, cache, timeout)
        p["_enriched_entities"] = details

        event_details: list[dict[str, Any]] = []
        for ev in ((prob.get("evidenceDetails") or {}).get("details", []) or [])[:10]:
            if not isinstance(ev, dict):
                continue
            event_id = str(ev.get("eventId", "")).strip()
            ev_data = None
            if event_id:
                ev_data = _fetch_event_details(cfg, event_id, cache=event_cache, timeout=timeout)
            # Fallback: usa o próprio evidence data como evento (tem properties, startTime, etc.)
            if not ev_data:
                ev_inline = ev.get("data") or {}
                if isinstance(ev_inline, dict) and (ev_inline.get("properties") or ev_inline.get("eventType")):
                    ev_data = ev_inline
            if ev_data:
                event_details.append(ev_data)
        p["_enriched_events"] = event_details

        enriched.append(p)

    return enriched


# ---------------------------------------------------------------------------
# Platform DQL  (/platform/storage/query/v1/query:execute)
# ---------------------------------------------------------------------------
def _dql_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _dql_base_url(cfg: Config) -> str:
    base = cfg.env_url
    if ".live.dynatrace.com" in base:
        base = base.replace(".live.dynatrace.com", ".apps.dynatrace.com")
    return base


def fetch_open_incidents_dql(
    cfg: Config,
    query: str,
    limit: int,
    time_filter: TimeFilter,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    headers = _dql_headers(cfg.platform_token)

    safe_q = query.replace('"', '\\"')

    # Use Davis problems table with proper timeframe syntax
    dql_from = time_filter.dql_from or "now()-6h"
    dql_to = time_filter.dql_to or "now()"

    filters: list[str] = []
    if safe_q:
        filters.append(
            f'contains(lower(toString(affected_entity_names)), "{safe_q}") '
            f'or contains(lower(toString(related_entity_names)), "{safe_q}") '
            f'or contains(lower(toString(event.name)), "{safe_q}")'
        )

    filter_clause = ""
    if filters:
        filter_clause = "| filter " + " and ".join(f"({f})" for f in filters) + " "

    dql = (
        f'fetch dt.davis.problems, from:{dql_from}, to:{dql_to} '
        f'{filter_clause}'
        f'| sort timestamp desc '
        f'| limit {max(1, limit)}'
    )

    resp = _post(url, headers=headers, payload={"query": dql}, timeout=timeout)
    if resp.status_code >= 400:
        print(f"Erro HTTP {resp.status_code}: {_short_body(resp.text)}", file=sys.stderr)
        return []
    body = resp.json()

    records = body.get("records") or (body.get("result") or {}).get("records")
    if records is not None:
        return [r for r in records if isinstance(r, dict)]

    # Handle async DQL query (202 response)
    request_token = body.get("requestToken")
    if not request_token:
        return []

    # Poll for results
    poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
    for _ in range(10):
        poll_resp = requests.get(
            poll_url,
            headers=headers,
            params={"request-token": request_token},
            timeout=timeout,
        )
        if poll_resp.status_code >= 400:
            return []
        poll_body = poll_resp.json()

        poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
        if poll_records is not None:
            return [r for r in poll_records if isinstance(r, dict)]

        if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
            break

        time.sleep(0.4)

    return []


def _infer_origin_dql(record: dict[str, Any]) -> str:
    corpus = json.dumps(record, ensure_ascii=True).lower()
    if any(s in corpus for s in ["aws", "ec2", "rds", "alb", "nlb", "lambda", "eks", "s3", "cloudwatch", "dynamodb", "amazon"]):
        return "AWS"
    if "grafana" in corpus:
        return "Grafana"
    if any(s in corpus for s in ["kubernetes", "k8s", "openshift", "pod", "namespace", "cluster"]):
        return "Kubernetes"
    return "Nao identificado"


def _extract_thresholds_from_description(description: str) -> tuple[list[str], list[str]]:
    """Extrai threshold_summary e threshold_detailed da descricao do evento."""
    summary: list[str] = []
    detailed: list[str] = []

    if not description:
        return summary, detailed

    lines = description.split('\n')
    for line in lines:
        line = line.strip()
        # Remove bullets/dashes
        if line.startswith('- '):
            line = line[2:].strip()
        if line.startswith('* '):
            line = line[2:].strip()

        if not line or line.startswith('#'):
            continue

        # Summary: linhas com percentages ou valores numericos
        if '%' in line or ('increased' in line.lower() and any(c.isdigit() for c in line)):
            if line not in summary:
                summary.append(line)
        # Detailed: linhas com keywords relevantes
        elif any(keyword in line.lower() for keyword in ['error', 'failure', 'rate', 'latency', 'entities', 'affected', 'threshold', 'limit']):
            if line not in detailed and line:
                detailed.append(line)

    return summary, detailed


def _extract_entity_types_from_record(record: dict[str, Any]) -> str:
    """Extrai tipos de entidade do registro DQL."""
    entity_types = record.get('affected_entity_types', [])
    if isinstance(entity_types, list) and entity_types:
        # Mapeia tipos de entidade para nomes legíveis
        mapping = {
            'dt.entity.service': 'SERVICE',
            'dt.entity.host': 'HOST',
            'dt.entity.process_group': 'PROCESS_GROUP',
            'dt.entity.database': 'DATABASE',
            'dt.entity.kubernetes_node': 'K8S_NODE',
            'dt.entity.kubernetes_cluster': 'K8S_CLUSTER',
        }
        types = [mapping.get(t, t.split('.')[-1].upper()) for t in entity_types]
        return ', '.join(types)
    return "Nao identificado"


def _extract_technologies_from_record(record: dict[str, Any]) -> str:
    """Extrai tecnologias afetadas do registro."""
    corpus = json.dumps(record, ensure_ascii=True)
    techs = set()

    # Mapeamento de keywords para tecnologias
    tech_map = {
        'aws_lambda': 'AWS_LAMBDA',
        'dynamodb': 'AWS_DYNAMO_DB',
        'rds': 'RDS',
        'nodejs': 'NODE_JS',
        'python': 'PYTHON',
        'java': 'JAVA',
        'libc': 'LIBC',
        'kubernetes': 'KUBERNETES',
        'docker': 'DOCKER',
        'postgresql': 'POSTGRESQL',
        'mysql': 'MYSQL',
    }

    corpus_lower = corpus.lower()
    for keyword, tech_name in tech_map.items():
        if keyword in corpus_lower:
            techs.add(tech_name)

    return ', '.join(sorted(techs)) if techs else "Nao identificado"


def to_output_item_dql(cfg: Config, record: dict[str, Any]) -> dict[str, Any]:
    def first(*keys: str, default: str = "") -> str:
        for k in keys:
            # Try direct key lookup first
            v = record.get(k)
            if v is not None and str(v).strip():
                return str(v)
            # Try with underscores instead of dots (for compatibility)
            k_alt = k.replace('.', '_')
            if k_alt != k:
                v = record.get(k_alt)
                if v is not None and str(v).strip():
                    return str(v)
        return default

    problem_id = first("event.id", "problemId", "id", "eventId")
    title = first("event.name", "title", "name", "description", default="Sem titulo")
    display_id = first("display_id", "displayId", "problemDisplayId", "event.code", default=problem_id or "N/A")
    status = first("event.status", "status", "state", "lifecycle", default="CLOSED")
    base = _classic_base_url(cfg)  # UI sempre no dominio .live
    problem_url = f"{base}/ui/problems/{problem_id}" if problem_id else base

    # Account extraction — prefer explicit fields over blob regex
    account = ""
    # 1. Try explicit aws.account.id field
    aws_acct = record.get("aws.account.id")
    if isinstance(aws_acct, list) and aws_acct:
        account = str(aws_acct[0])
    elif isinstance(aws_acct, str) and aws_acct.strip():
        account = aws_acct.strip()

    # 2. Fallback: enrich account/region from entity relationships (Classic API)
    _enriched_region = ""
    if cfg.api_token:
        need_account = not account
        entity_ids = record.get("affected_entity_ids") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        cache: dict[str, dict[str, Any]] = {}
        for eid in entity_ids[:DEFAULT_MAX_ENTITIES]:
            if not isinstance(eid, str) or not eid.strip():
                continue
            entity_data = _fetch_entity_details(cfg, eid, cache=cache, timeout=DEFAULT_ENTITY_TIMEOUT)
            if not entity_data:
                continue
            # Check entity itself
            eprops = entity_data.get("properties") or {}
            # Region from entity properties
            if not _enriched_region:
                _enriched_region = str(eprops.get("publicCloudRegion", "")).strip()
            pcid = str(eprops.get("publicCloudId", ""))
            if pcid:
                if not _enriched_region:
                    region_match = re.search(r"arn:[^:]+:[^:]+:([a-z]{2}-[a-z]+-\d):", pcid)
                    if region_match:
                        _enriched_region = region_match.group(1)
                if need_account:
                    acct_match = re.search(r":(\d{12}):", pcid)
                    if acct_match:
                        account = acct_match.group(1)
                if account and _enriched_region:
                    break
            # Check callers (toRelationships.calls)
            if not account or not _enriched_region:
                to_rels = entity_data.get("toRelationships") or {}
                for ref in (to_rels.get("calls") or [])[:DEFAULT_MAX_CALLERS]:
                    ref_id = ref.get("id", ref) if isinstance(ref, dict) else str(ref)
                    if not ref_id:
                        continue
                    ref_data = _fetch_entity_details(cfg, ref_id, cache=cache, timeout=DEFAULT_ENTITY_TIMEOUT)
                    if not ref_data:
                        continue
                    ref_props = ref_data.get("properties") or {}
                    ref_pcid = str(ref_props.get("publicCloudId", ""))
                    if ref_pcid:
                        if not _enriched_region:
                            region_match = re.search(r"arn:[^:]+:[^:]+:([a-z]{2}-[a-z]+-\d):", ref_pcid)
                            if region_match:
                                _enriched_region = region_match.group(1)
                        if need_account and not account:
                            acct_match = re.search(r":(\d{12}):", ref_pcid)
                            if acct_match:
                                account = acct_match.group(1)
                        if account and _enriched_region:
                            break
            if account and _enriched_region:
                break

    # 3. Fallback: enrich via DQL entity queries (topology traversal)
    _dql_enriched: dict[str, str] = {}
    if (not account or not _enriched_region) and cfg.platform_token:
        entity_ids = record.get("affected_entity_ids") or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        entity_ids = [eid for eid in entity_ids if isinstance(eid, str) and eid.strip()]
        if entity_ids:
            _dql_enriched = _enrich_entity_via_dql(cfg, entity_ids)
            if not account and _dql_enriched.get("account"):
                account = _dql_enriched["account"]
            if not _enriched_region and _dql_enriched.get("region"):
                _enriched_region = _dql_enriched["region"]
    # Always set cloud_aws_status even if dql enrichment was skipped
    if not _dql_enriched.get("cloud_aws_status"):
        _dql_enriched["cloud_aws_status"] = "ON" if account else "OFF_DYNA"

    if not account:
        account = "Nao identificado"

    blob = json.dumps(record, ensure_ascii=True)
    region = _first_match(r"\b([a-z]{2}-[a-z]+-\d)\b", [blob]) or _enriched_region or "Nao identificado"

    # Extract entity information from smartscape
    affected_entities = []
    smartscape_affected = record.get('smartscape.affected_entities', [])
    if isinstance(smartscape_affected, list):
        for entity in smartscape_affected:
            if isinstance(entity, dict):
                name = entity.get('name', '')
                if name:
                    affected_entities.append(name)

    # Extract thresholds from description
    description = record.get('event.description', '')
    threshold_summary, threshold_detailed = _extract_thresholds_from_description(description)

    # Extract K8s info if available
    k8s_cluster = "Nao identificado"
    k8s_namespace = "Nao identificado"
    k8s_cluster_list = record.get('k8s.cluster.name', [])
    k8s_namespace_list = record.get('k8s.namespace.name', [])
    if isinstance(k8s_cluster_list, list) and k8s_cluster_list:
        k8s_cluster = k8s_cluster_list[0]
    if isinstance(k8s_namespace_list, list) and k8s_namespace_list:
        k8s_namespace = k8s_namespace_list[0]

    # Enrich K8s from DQL if record didn't have it
    if _dql_enriched:
        if k8s_cluster == "Nao identificado" and _dql_enriched.get("k8s_cluster"):
            k8s_cluster = _dql_enriched["k8s_cluster"]
        if k8s_namespace == "Nao identificado" and _dql_enriched.get("k8s_namespace"):
            k8s_namespace = _dql_enriched["k8s_namespace"]

    # K8s node status from DQL enrichment
    k8s_nodes = _dql_enriched.get("k8s_nodes", []) if _dql_enriched else []

    # Infer platform from cloud references
    platform = "Nao identificado"
    if _dql_enriched.get("platform"):
        platform = _dql_enriched["platform"]
    elif 'aws' in blob.lower() or record.get('dt.entity.kubernetes_cluster'):
        if k8s_cluster != "Nao identificado":
            platform = "Kubernetes"
        elif 'lambda' in blob.lower():
            platform = "AWS Lambda"
        elif 'eks' in blob.lower():
            platform = "AWS EKS"
        else:
            platform = "AWS"

    # Extract workload (services affected)
    workload_names = record.get('affected_entity_names', [])
    workload = str(workload_names) if isinstance(workload_names, list) else "Nao identificado"

    # Infer origin — use enriched data if blob has no AWS keywords
    origin = _infer_origin_dql(record)
    if origin == "Nao identificado":
        if account and account != "Nao identificado":
            origin = "AWS"
        elif _dql_enriched.get("k8s_cluster"):
            origin = "Kubernetes"

    # Infer cloud_provider from enrichment
    cloud_provider = "Nao identificado"
    if 'aws' in blob.lower() or (account and account != "Nao identificado"):
        cloud_provider = "AWS"

    return {
        "alarm": f"{display_id} - {title} ({status})",
        "problem_id": problem_id,
        "problem_url": problem_url,
        "dashboard_urls": cfg.dashboard_urls,
        "origin": origin,
        "platform": platform,
        "workload": workload,
        "account_origin": account,
        "region": region,
        "cloud_provider": cloud_provider,
        "cloud_service": "lambda" if "lambda" in blob.lower() else "Nao identificado",
        "cloud_id": "Nao identificado",
        "service_type": "Nao identificado",
        "technologies": _extract_technologies_from_record(record),
        "k8s_cluster": k8s_cluster,
        "k8s_namespace": k8s_namespace,
        "k8s_nodes": k8s_nodes,
        "entity_types": _extract_entity_types_from_record(record),
        "threshold_summary": threshold_summary,
        "threshold_detailed": threshold_detailed,
        "affected_entities": affected_entities,
        "cloud_aws_status": _dql_enriched.get("cloud_aws_status", "ON" if account else "OFF_DYNA"),
    }


# ---------------------------------------------------------------------------
# Davis Root Cause Analysis via DQL
# ---------------------------------------------------------------------------
def fetch_davis_causation_dql(
    cfg: Config,
    problem_display_id: str,
    time_filter: TimeFilter,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Busca eventos com correlacao/causation para um problema específico via DQL."""
    if not cfg.platform_token:
        return []

    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    headers = _dql_headers(cfg.platform_token)

    dql_from = time_filter.dql_from or "now()-6h"
    dql_to = time_filter.dql_to or "now()"

    safe_id = problem_display_id.replace('"', '\\"')
    dql = (
        f'fetch dt.davis.problems, from:{dql_from}, to:{dql_to} '
        f'| filter display_id == "{safe_id}" '
        f'| fields display_id, dt.event.causation_chain, dt.event.root_cause_entity, event.description'
    )

    resp = _post(url, headers=headers, payload={"query": dql}, timeout=timeout)
    if resp.status_code >= 400:
        return []
    body = resp.json()

    records = body.get("records") or (body.get("result") or {}).get("records")
    if records is not None:
        return [r for r in records if isinstance(r, dict)]

    # Handle async query
    request_token = body.get("requestToken")
    if not request_token:
        return []

    poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
    for _ in range(10):
        poll_resp = requests.get(
            poll_url,
            headers=headers,
            params={"request-token": request_token},
            timeout=timeout,
        )
        if poll_resp.status_code >= 400:
            return []
        poll_body = poll_resp.json()

        poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
        if poll_records is not None:
            return [r for r in poll_records if isinstance(r, dict)]

        if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
            break

        time.sleep(0.4)

    return []


def fetch_workload_health_dql(
    cfg: Config,
    workload_name: str,
    time_filter: TimeFilter,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Busca métricas de saúde do workload (serviço) em Kubernetes."""
    if not cfg.platform_token or not workload_name:
        return []

    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    headers = _dql_headers(cfg.platform_token)

    dql_from = time_filter.dql_from or "now()-6h"
    dql_to = time_filter.dql_to or "now()"
    safe_workload = workload_name.replace('"', '\\"')

    # Query para trazer problemas, falhas e latência do workload
    dql = (
        f'fetch dt.davis.problems, from:{dql_from}, to:{dql_to} '
        f'| filter contains(lower(toString(affected_entity_names)), "{safe_workload}") '
        f'| fields display_id, event.status, event.name, event.description, timestamp, resolved_problem_duration '
        f'| sort timestamp desc '
        f'| limit 10'
    )

    resp = _post(url, headers=headers, payload={"query": dql}, timeout=timeout)
    if resp.status_code >= 400:
        return []
    body = resp.json()

    records = body.get("records") or (body.get("result") or {}).get("records")
    if records is not None:
        return [r for r in records if isinstance(r, dict)]

    # Handle async query
    request_token = body.get("requestToken")
    if not request_token:
        return []

    poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
    for _ in range(10):
        poll_resp = requests.get(
            poll_url,
            headers=headers,
            params={"request-token": request_token},
            timeout=timeout,
        )
        if poll_resp.status_code >= 400:
            return []
        poll_body = poll_resp.json()

        poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
        if poll_records is not None:
            return [r for r in poll_records if isinstance(r, dict)]

        if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
            break

        time.sleep(0.4)

    return []


def fetch_k8s_resource_metrics(
    cfg: Config,
    workload_name: str,
    k8s_cluster: str,
    k8s_namespace: str,
    time_filter: TimeFilter,
    timeout: int = 30,
    k8s_workload_name: str = "",
) -> dict[str, Any]:
    """Busca métricas detalhadas de recursos K8s: CPU/Memory, OOM, Throttling, Node issues."""
    if not (cfg.platform_token or cfg.api_token):
        return {}

    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    headers = _dql_headers(cfg.platform_token)

    dql_from = time_filter.dql_from or "now()-6h"
    dql_to = time_filter.dql_to or "now()"
    safe_workload = workload_name.replace('"', '\\"').lower()
    safe_ns = k8s_namespace.replace('"', '\\"')

    metrics = {
        "cpu_usage": None,
        "memory_usage": None,
        "cpu_requests": None,
        "memory_requests": None,
        "cpu_limits": None,
        "memory_limits": None,
        "has_oom": False,
        "has_throttling": False,
        "has_node_issues": False,
        "needs_hpa_improvement": False,
        "events": [],
    }

    # Tenta múltiplas queries para obter métricas de recursos
    queries = [
        f'fetch dt.kubernetes.container, from:{dql_from}, to:{dql_to} | filter k8s.namespace.name == "{safe_ns}" | fields cpu.usage, memory.usage, cpu.requests, memory.requests, cpu.limits, memory.limits | limit 5',
        f'fetch dt.kubernetes.pod, from:{dql_from}, to:{dql_to} | filter k8s.namespace.name == "{safe_ns}" | fields cpu.usage, memory.usage, cpu.requests, memory.requests, cpu.limits, memory.limits | limit 5',
    ]

    for dql in queries:
        try:
            resp = _post(url, headers=headers, payload={"query": dql}, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()

            records = body.get("records") or (body.get("result") or {}).get("records")
            if records is None:
                request_token = body.get("requestToken")
                if request_token:
                    poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
                    for _ in range(8):
                        poll_resp = requests.get(poll_url, headers=headers, params={"request-token": request_token}, timeout=timeout)
                        poll_resp.raise_for_status()
                        poll_body = poll_resp.json()
                        poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
                        if poll_records is not None and len(poll_records) > 0:
                            records = poll_records
                            break
                        if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
                            break
                        time.sleep(0.3)

            if records and len(records) > 0:
                for record in records[:1]:
                    if isinstance(record, dict):
                        cpu_u = record.get("cpu.usage")
                        mem_u = record.get("memory.usage")
                        cpu_l = record.get("cpu.limits")
                        mem_l = record.get("memory.limits")

                        if cpu_u is not None:
                            metrics["cpu_usage"] = cpu_u
                        if mem_u is not None:
                            metrics["memory_usage"] = mem_u
                        if cpu_l is not None:
                            metrics["cpu_limits"] = cpu_l
                        if mem_l is not None:
                            metrics["memory_limits"] = mem_l

                        if record.get("cpu.requests"):
                            metrics["cpu_requests"] = record.get("cpu.requests")
                        if record.get("memory.requests"):
                            metrics["memory_requests"] = record.get("memory.requests")

                        # Verifica se precisa HPA
                        if cpu_u and cpu_l and float(cpu_l) > 0:
                            try:
                                if (float(cpu_u) / float(cpu_l)) * 100 > 80:
                                    metrics["needs_hpa_improvement"] = True
                            except:
                                pass
                        if mem_u and mem_l and float(mem_l) > 0:
                            try:
                                if (float(mem_u) / float(mem_l)) * 100 > 80:
                                    metrics["needs_hpa_improvement"] = True
                            except:
                                pass

                if any([metrics["cpu_usage"], metrics["memory_usage"]]):
                    break
        except Exception:
            pass

    # Busca eventos de OOM, Throttling
    try:
        event_query = f'fetch dt.kubernetes.events, from:{dql_from}, to:{dql_to} | filter k8s.namespace.name == "{safe_ns}" | fields message, reason | limit 15'
        resp = _post(url, headers=headers, payload={"query": event_query}, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()

        records = body.get("records") or (body.get("result") or {}).get("records")
        if records is None:
            request_token = body.get("requestToken")
            if request_token:
                poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
                for _ in range(5):
                    poll_resp = requests.get(poll_url, headers=headers, params={"request-token": request_token}, timeout=timeout)
                    poll_resp.raise_for_status()
                    poll_body = poll_resp.json()
                    poll_records = poll_body.get("records") or (poll_body.get("result") or {}).get("records")
                    if poll_records is not None:
                        records = poll_records
                        break
                    if str(poll_body.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
                        break
                    time.sleep(0.3)

        if records:
            for record in records:
                if isinstance(record, dict):
                    msg = (record.get("message", "") or "").lower()
                    reason = (record.get("reason", "") or "").lower()
                    combined = f"{msg} {reason}"
                    if "oom" in combined or "out of memory" in combined:
                        metrics["has_oom"] = True
                    if "throttl" in combined:
                        metrics["has_throttling"] = True
                    if record.get("reason") and record.get("reason") not in metrics["events"]:
                        metrics["events"].append(record.get("reason"))
    except Exception:
        pass

    # Fallback: Metrics API v2 para métricas de recurso do workload
    if metrics["cpu_usage"] is None and metrics["memory_usage"] is None:
        from_ts = str(time_filter.from_ms) if time_filter.from_ms else None
        to_ts = str(time_filter.to_ms) if time_filter.to_ms else None

        # Usa nomes builtin:kubernetes.workload.* (Metrics API v2)
        selectors = (
            "builtin:kubernetes.workload.cpu_usage:splitBy():avg,"
            "builtin:kubernetes.workload.memory_working_set:splitBy():avg,"
            "builtin:kubernetes.workload.limits_cpu:splitBy():avg,"
            "builtin:kubernetes.workload.limits_memory:splitBy():avg,"
            "builtin:kubernetes.workload.requests_cpu:splitBy():avg,"
            "builtin:kubernetes.workload.requests_memory:splitBy():avg,"
            "builtin:kubernetes.workload.cpu_throttled:splitBy():sum,"
            "builtin:kubernetes.container.oom_kills:splitBy():sum"
        )

        # Remove wildcards do nome para entity selector
        clean_wl = re.sub(r"[\*\?]+$", "", workload_name).rstrip("-")
        entity_sel = f'type("CLOUD_APPLICATION"),entityName("{clean_wl}")'

        result = _query_metrics_v2(
            cfg,
            selector=selectors,
            entity_selector=entity_sel,
            from_ts=from_ts,
            to_ts=to_ts,
            timeout=timeout,
        )

        if result:
            cpu_u = _avg_metric_value(result, "builtin:kubernetes.workload.cpu_usage")
            mem_u = _avg_metric_value(result, "builtin:kubernetes.workload.memory_working_set")
            cpu_l = _avg_metric_value(result, "builtin:kubernetes.workload.limits_cpu")
            mem_l = _avg_metric_value(result, "builtin:kubernetes.workload.limits_memory")
            cpu_r = _avg_metric_value(result, "builtin:kubernetes.workload.requests_cpu")
            mem_r = _avg_metric_value(result, "builtin:kubernetes.workload.requests_memory")
            throttled = _sum_metric_value(result, "builtin:kubernetes.workload.cpu_throttled")
            oom = _sum_metric_value(result, "builtin:kubernetes.container.oom_kills")

            if cpu_u is not None:
                metrics["cpu_usage"] = cpu_u
            if mem_u is not None:
                metrics["memory_usage"] = mem_u
            if cpu_l is not None:
                metrics["cpu_limits"] = cpu_l
            if mem_l is not None:
                metrics["memory_limits"] = mem_l
            if cpu_r is not None:
                metrics["cpu_requests"] = cpu_r
            if mem_r is not None:
                metrics["memory_requests"] = mem_r
            if throttled and throttled > 0:
                metrics["has_throttling"] = True
            if oom and oom > 0:
                metrics["has_oom"] = True

            # Verifica se precisa HPA
            if metrics["cpu_usage"] and metrics["cpu_limits"]:
                try:
                    if (float(metrics["cpu_usage"]) / float(metrics["cpu_limits"])) * 100 > 80:
                        metrics["needs_hpa_improvement"] = True
                except Exception:
                    pass
            if metrics["memory_usage"] and metrics["memory_limits"]:
                try:
                    if (float(metrics["memory_usage"]) / float(metrics["memory_limits"])) * 100 > 80:
                        metrics["needs_hpa_improvement"] = True
                except Exception:
                    pass

    # Fallback: Metrics API v2 com CONTAINER_GROUP_INSTANCE (quando CLOUD_APPLICATION nao existe)
    # Usa builtin:containers.* que retorna CPU/Mem usage + limits para containers K8s
    if metrics["cpu_usage"] is None and metrics["memory_usage"] is None and cfg.api_token:
        # Prioriza workload_name (iteração atual) sobre k8s_workload_name (entity resolution do primeiro)
        pgi_search = workload_name or k8s_workload_name
        # Remove wildcards e trailing hyphens para entityName.contains()
        pgi_search = re.sub(r"[\*\?]+$", "", pgi_search).rstrip("-")
        if pgi_search:
            from_ts = str(time_filter.from_ms) if time_filter.from_ms else None
            to_ts = str(time_filter.to_ms) if time_filter.to_ms else None

            # Container metrics: usage + limits (CONTAINER_GROUP_INSTANCE)
            cgi_selectors = (
                "builtin:containers.cpu.usageMilliCores:splitBy():avg,"
                "builtin:containers.cpu.limit:splitBy():avg,"
                "builtin:containers.memory.residentSetBytes:splitBy():avg,"
                "builtin:containers.memory.limitBytes:splitBy():avg"
            )
            cgi_entity_sel = f'type("CONTAINER_GROUP_INSTANCE"),entityName.contains("{pgi_search}")'
            cgi_result = _query_metrics_v2(
                cfg,
                selector=cgi_selectors,
                entity_selector=cgi_entity_sel,
                from_ts=from_ts,
                to_ts=to_ts,
                timeout=timeout,
            )
            if cgi_result:
                cpu_u = _avg_metric_value(cgi_result, "builtin:containers.cpu.usageMilliCores")
                cpu_l = _avg_metric_value(cgi_result, "builtin:containers.cpu.limit")
                mem_u = _avg_metric_value(cgi_result, "builtin:containers.memory.residentSetBytes")
                mem_l = _avg_metric_value(cgi_result, "builtin:containers.memory.limitBytes")
                if cpu_u is not None:
                    metrics["cpu_usage"] = cpu_u
                if cpu_l is not None:
                    metrics["cpu_limits"] = cpu_l
                if mem_u is not None:
                    metrics["memory_usage"] = mem_u
                if mem_l is not None:
                    metrics["memory_limits"] = mem_l

            # Fallback PGI se CGI não retornou (mantém comportamento anterior)
            if metrics["cpu_usage"] is None and metrics["memory_usage"] is None:
                pgi_selectors = (
                    "builtin:tech.generic.cpu.usage:splitBy():avg,"
                    "builtin:tech.generic.mem.workingSetSize:splitBy():avg"
                )
                pgi_entity_sel = f'type("PROCESS_GROUP_INSTANCE"),entityName.contains("{pgi_search}")'
                pgi_result = _query_metrics_v2(
                    cfg,
                    selector=pgi_selectors,
                    entity_selector=pgi_entity_sel,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    timeout=timeout,
                )
                if pgi_result:
                    cpu_u = _avg_metric_value(pgi_result, "builtin:tech.generic.cpu.usage")
                    mem_u = _avg_metric_value(pgi_result, "builtin:tech.generic.mem.workingSetSize")
                    if cpu_u is not None:
                        metrics["cpu_usage"] = cpu_u
                    if mem_u is not None:
                        metrics["memory_usage"] = mem_u

    # Fallback: dtctl para métricas de workload quando DQL e Metrics API falham
    if metrics["cpu_usage"] is None and metrics["memory_usage"] is None and _dtctl_available():
        _dtctl_enrich_workload_metrics(workload_name, k8s_namespace, metrics)

    return metrics


def _dtctl_enrich_workload_metrics(workload_name: str, namespace: str, metrics: dict[str, Any]) -> None:
    """Enrich workload metrics via dtctl DQL queries."""
    safe_name = workload_name.replace('"', '\\"').lower()

    # 1. CPU/Memory usage via timeseries (cloud_application)
    dql = (
        f'timeseries avg_cpu = avg(dt.kubernetes.workload.cpu_usage), from:-1h'
        f', filter: {{k8s.workload.name == "{safe_name}"}}'
    )
    records = _dtctl_query(dql, timeout=30)
    if records:
        for r in records:
            val = r.get("avg_cpu")
            if isinstance(val, list) and val:
                val = val[-1] if val[-1] is not None else next((v for v in reversed(val) if v is not None), None)
            if val is not None:
                metrics["cpu_usage"] = float(val)
                break

    dql_mem = (
        f'timeseries avg_mem = avg(dt.kubernetes.workload.memory_working_set), from:-1h'
        f', filter: {{k8s.workload.name == "{safe_name}"}}'
    )
    records_mem = _dtctl_query(dql_mem, timeout=30)
    if records_mem:
        for r in records_mem:
            val = r.get("avg_mem")
            if isinstance(val, list) and val:
                val = val[-1] if val[-1] is not None else next((v for v in reversed(val) if v is not None), None)
            if val is not None:
                metrics["memory_usage"] = float(val)
                break

    # 2. Requests/Limits
    dql_req = (
        f'timeseries avg_cpu_req = avg(dt.kubernetes.workload.requests_cpu),'
        f' avg_mem_req = avg(dt.kubernetes.workload.requests_memory),'
        f' avg_cpu_lim = avg(dt.kubernetes.workload.limits_cpu),'
        f' avg_mem_lim = avg(dt.kubernetes.workload.limits_memory),'
        f' from:-1h, filter: {{k8s.workload.name == "{safe_name}"}}'
    )
    records_req = _dtctl_query(dql_req, timeout=30)
    if records_req:
        for r in records_req:
            for key, metric_key in [
                ("avg_cpu_req", "cpu_requests"),
                ("avg_mem_req", "memory_requests"),
                ("avg_cpu_lim", "cpu_limits"),
                ("avg_mem_lim", "memory_limits"),
            ]:
                val = r.get(key)
                if isinstance(val, list) and val:
                    val = val[-1] if val[-1] is not None else next((v for v in reversed(val) if v is not None), None)
                if val is not None and metrics.get(metric_key) is None:
                    metrics[metric_key] = float(val)

    # 3. Events: OOM, throttling
    dql_events = (
        f'fetch events, from:-2h'
        f' | filter event.kind == "K8S_EVENT"'
        f' | filter matchesPhrase(content, "{safe_name}")'
        f' | fields content, dt.event.description'
        f' | limit 20'
    )
    records_events = _dtctl_query(dql_events, timeout=20)
    if records_events:
        for r in records_events:
            text = (str(r.get("content", "")) + " " + str(r.get("dt.event.description", ""))).lower()
            if "oom" in text or "out of memory" in text:
                metrics["has_oom"] = True
            if "throttl" in text:
                metrics["has_throttling"] = True


def _query_metrics_v2(
    cfg: Config,
    selector: str,
    entity_selector: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    resolution: str = "1h",
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Consulta Metrics API v2 e retorna lista de data-points.

    Tenta platform_token (Bearer) primeiro, depois api_token (Api-Token).
    Retorna lista de dicts com 'metricId', 'data' (lista de series com
    'dimensions', 'timestamps', 'values').
    """
    base = _classic_base_url(cfg)
    url = f"{base}/api/v2/metrics/query"

    params: dict[str, Any] = {
        "metricSelector": selector,
        "resolution": resolution,
    }
    if entity_selector:
        params["entitySelector"] = entity_selector
    if from_ts:
        params["from"] = from_ts
    if to_ts:
        params["to"] = to_ts

    # Tenta com cada token disponível
    for token, scheme in [
        (cfg.platform_token, "Bearer"),
        (cfg.api_token, "Api-Token"),
    ]:
        if not token:
            continue
        headers = {"Authorization": f"{scheme} {token}"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code == 401:
                continue  # token não serve para este endpoint
            resp.raise_for_status()
            body = resp.json()
            result = body.get("result", [])
            if result:
                return result
        except Exception:
            continue
    return []


def _avg_metric_value(result: list[dict[str, Any]], metric_id_prefix: str) -> float | None:
    """Extrai a média do primeiro resultado cujo metricId começa com o prefixo."""
    for item in result:
        mid = item.get("metricId", "")
        if not mid.startswith(metric_id_prefix):
            continue
        for series in item.get("data", []):
            values = series.get("values", [])
            nums = [v for v in values if v is not None]
            if nums:
                return sum(nums) / len(nums)
    return None


def _sum_metric_value(result: list[dict[str, Any]], metric_id_prefix: str) -> float | None:
    """Extrai a soma do primeiro resultado cujo metricId começa com o prefixo."""
    for item in result:
        mid = item.get("metricId", "")
        if not mid.startswith(metric_id_prefix):
            continue
        total = 0.0
        found = False
        for series in item.get("data", []):
            for v in series.get("values", []):
                if v is not None:
                    total += v
                    found = True
        if found:
            return total
    return None


def fetch_k8s_cluster_infra_metrics(
    cfg: Config,
    k8s_cluster: str,
    k8s_namespace: str,
    time_filter: TimeFilter,
    timeout: int = 30,
) -> dict[str, Any]:
    """Busca métricas reais de infraestrutura K8s no período do problema.

    Estratégia: tenta DQL timeseries primeiro; se vier vazio, faz fallback
    para Metrics API v2 (/api/v2/metrics/query) que é o mesmo backend que
    o app Kubernetes usa no navegador.
    """
    if not (cfg.platform_token or cfg.api_token) or not k8s_cluster:
        return {}

    dql_from = time_filter.dql_from or "now()-6h"
    dql_to = time_filter.dql_to or "now()"
    safe_cluster = k8s_cluster.replace('"', '\\"')
    safe_ns = k8s_namespace.replace('"', '\\"')

    infra: dict[str, Any] = {
        "cluster_cpu_total": None,
        "cluster_memory_total": None,
        "cluster_cpu_available": None,
        "cluster_memory_available": None,
        "cluster_cpu_used": None,
        "cluster_memory_used": None,
        "node_count": 0,
        "node_ready": 0,
        "node_not_ready": 0,
        "nodes": [],
        "pod_count": 0,
        "pod_running": 0,
        "pod_pending": 0,
        "pod_failed": 0,
        "namespace_cpu_used": None,
        "namespace_memory_used": None,
        "disk_pressure": False,
        "memory_pressure": False,
        "cpu_pressure": False,
    }

    # ── helper DQL ────────────────────────────────────────────────────
    dql_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    dql_headers = _dql_headers(cfg.platform_token) if cfg.platform_token else {}

    def _execute_dql(query: str) -> list[dict[str, Any]]:
        if not cfg.platform_token:
            return []
        try:
            resp = _post(dql_url, headers=dql_headers, payload={"query": query}, timeout=timeout)
            resp.raise_for_status()
            body = resp.json()
            records = body.get("records") or (body.get("result") or {}).get("records")
            if records is not None:
                return [r for r in records if isinstance(r, dict)]
            request_token = body.get("requestToken")
            if not request_token:
                return []
            poll_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:poll"
            for _ in range(8):
                pr = requests.get(poll_url, headers=dql_headers, params={"request-token": request_token}, timeout=timeout)
                pr.raise_for_status()
                pb = pr.json()
                prec = pb.get("records") or (pb.get("result") or {}).get("records")
                if prec is not None:
                    return [r for r in prec if isinstance(r, dict)]
                if str(pb.get("state", "")).upper() in {"FAILED", "CANCELED", "CANCELLED"}:
                    break
                time.sleep(0.3)
        except Exception:
            pass
        return []

    # ── 1. Tenta DQL timeseries ──────────────────────────────────────
    q_cpu = _execute_dql(
        f'timeseries cpu_usage = sum(dt.kubernetes.container.cpu_usage), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd avg_cpu_usage = arrayAvg(cpu_usage) | limit 1'
    )
    if q_cpu:
        infra["cluster_cpu_used"] = q_cpu[0].get("avg_cpu_usage")
        infra["namespace_cpu_used"] = infra["cluster_cpu_used"]

    q_mem = _execute_dql(
        f'timeseries mem = sum(dt.kubernetes.container.memory_working_set), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd avg_mem = arrayAvg(mem) | limit 1'
    )
    if q_mem:
        infra["cluster_memory_used"] = q_mem[0].get("avg_mem")
        infra["namespace_memory_used"] = infra["cluster_memory_used"]

    q_lim_cpu = _execute_dql(
        f'timeseries lc = sum(dt.kubernetes.container.limits_cpu), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd avg_lc = arrayAvg(lc) | limit 1'
    )
    if q_lim_cpu:
        infra["cluster_cpu_total"] = q_lim_cpu[0].get("avg_lc")

    q_lim_mem = _execute_dql(
        f'timeseries lm = sum(dt.kubernetes.container.limits_memory), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd avg_lm = arrayAvg(lm) | limit 1'
    )
    if q_lim_mem:
        infra["cluster_memory_total"] = q_lim_mem[0].get("avg_lm")

    q_throttle = _execute_dql(
        f'timeseries th = sum(dt.kubernetes.container.cpu_throttled), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd t = arraySum(th) | limit 1'
    )
    if q_throttle and (q_throttle[0].get("t", 0) or 0) > 0:
        infra["cpu_pressure"] = True

    q_oom = _execute_dql(
        f'timeseries om = sum(dt.kubernetes.container.oom_kills), '
        f'by: {{k8s.cluster.name, k8s.namespace.name}} '
        f'| filter k8s.cluster.name == "{safe_cluster}" and k8s.namespace.name == "{safe_ns}" '
        f'| fieldsAdd o = arraySum(om) | limit 1'
    )
    if q_oom and (q_oom[0].get("o", 0) or 0) > 0:
        infra["memory_pressure"] = True

    # ── 2. Fallback: Metrics API v2 (mesma fonte do app Kubernetes) ──
    dql_got_data = infra["cluster_cpu_used"] is not None
    if not dql_got_data:
        # Resolve timeframe para Metrics API
        from_ts = None
        to_ts = None
        if time_filter.from_ms:
            from_ts = str(time_filter.from_ms)
        elif dql_from.startswith("now()"):
            from_ts = dql_from.replace("now()", "now")
        if time_filter.to_ms:
            to_ts = str(time_filter.to_ms)

        entity_sel = f'type("KUBERNETES_CLUSTER"),entityName("{k8s_cluster}")' if k8s_cluster else None
        selectors = (
            "builtin:kubernetes.node.cpu_usage:splitBy():avg,"
            "builtin:kubernetes.node.cpu_allocatable:splitBy():avg,"
            "builtin:kubernetes.node.limits_cpu:splitBy():avg,"
            "builtin:kubernetes.node.memory_working_set:splitBy():avg,"
            "builtin:kubernetes.node.memory_allocatable:splitBy():avg,"
            "builtin:kubernetes.node.limits_memory:splitBy():avg,"
            "builtin:kubernetes.node.cpu_throttled:splitBy():sum,"
            "builtin:kubernetes.container.oom_kills:splitBy():sum,"
            "builtin:kubernetes.container.restarts:splitBy():sum,"
            "builtin:kubernetes.node.pods:splitBy():avg,"
            "builtin:kubernetes.nodes:splitBy():avg,"
            "builtin:kubernetes.pods:splitBy():avg"
        )

        result = _query_metrics_v2(
            cfg,
            selector=selectors,
            entity_selector=entity_sel,
            from_ts=from_ts,
            to_ts=to_ts,
            timeout=timeout,
        )

        if result:
            cpu_u = _avg_metric_value(result, "builtin:kubernetes.node.cpu_usage")
            cpu_l = _avg_metric_value(result, "builtin:kubernetes.node.limits_cpu")
            cpu_r = _avg_metric_value(result, "builtin:kubernetes.node.cpu_allocatable")
            mem_u = _avg_metric_value(result, "builtin:kubernetes.node.memory_working_set")
            mem_l = _avg_metric_value(result, "builtin:kubernetes.node.limits_memory")
            mem_r = _avg_metric_value(result, "builtin:kubernetes.node.memory_allocatable")
            throttled = _sum_metric_value(result, "builtin:kubernetes.node.cpu_throttled")
            oom = _sum_metric_value(result, "builtin:kubernetes.container.oom_kills")
            restarts = _sum_metric_value(result, "builtin:kubernetes.container.restarts")
            node_cpu_alloc = _avg_metric_value(result, "builtin:kubernetes.node.cpu_allocatable")
            node_mem_alloc = _avg_metric_value(result, "builtin:kubernetes.node.memory_allocatable")
            pods_avg = _avg_metric_value(result, "builtin:kubernetes.pods")

            if cpu_u is not None:
                infra["cluster_cpu_used"] = cpu_u
                infra["namespace_cpu_used"] = cpu_u
            if cpu_l is not None:
                infra["cluster_cpu_total"] = cpu_l
            elif node_cpu_alloc is not None:
                infra["cluster_cpu_total"] = node_cpu_alloc
            if mem_u is not None:
                infra["cluster_memory_used"] = mem_u
                infra["namespace_memory_used"] = mem_u
            if mem_l is not None:
                infra["cluster_memory_total"] = mem_l
            elif node_mem_alloc is not None:
                infra["cluster_memory_total"] = node_mem_alloc

            if throttled and throttled > 0:
                infra["cpu_pressure"] = True
            if oom and oom > 0:
                infra["memory_pressure"] = True
            if restarts and restarts > 0:
                infra["disk_pressure"] = True  # usar disk_pressure como flag de restarts

            if pods_avg is not None:
                infra["pod_count"] = int(pods_avg)
                infra["pod_running"] = int(pods_avg)  # Metrics API não separa por phase

    # ── 3. Calcula percentuais derivados ─────────────────────────────
    if infra["cluster_cpu_total"] is not None and infra["cluster_cpu_used"] is not None:
        try:
            infra["cluster_cpu_available"] = float(infra["cluster_cpu_total"]) - float(infra["cluster_cpu_used"])
        except Exception:
            pass
    if infra["cluster_memory_total"] is not None and infra["cluster_memory_used"] is not None:
        try:
            infra["cluster_memory_available"] = float(infra["cluster_memory_total"]) - float(infra["cluster_memory_used"])
        except Exception:
            pass

    return infra


def format_workload_health(
    workload_name: str,
    health_records: list[dict[str, Any]],
    metrics: dict[str, Any] | None = None,
    infra: dict[str, Any] | None = None,
    k8s_cluster: str = "",
    k8s_namespace: str = "",
) -> str:
    """Formata insights detalhados de saúde do workload e infraestrutura em Kubernetes."""
    lines: list[str] = []

    if not health_records and not metrics and not infra:
        return f"  ✓ {workload_name}: Sem problemas detectados nos últimos períodos"

    lines.append(f"  ⚠️  {workload_name}:")

    # 0. Cluster e Namespace
    if k8s_cluster or k8s_namespace:
        cluster_str = k8s_cluster or "N/A"
        ns_str = k8s_namespace or "N/A"
        lines.append(f"     📍 Cluster: {cluster_str} | Namespace: {ns_str}")

    # 1. Problemas detectados
    if health_records:
        problems_by_type: dict[str, int] = {}
        closed_count = 0

        for record in health_records:
            pname = record.get("event.name", "Unknown")
            status = record.get("event.status", "UNKNOWN")

            if status == "CLOSED":
                closed_count += 1

            problems_by_type[pname] = problems_by_type.get(pname, 0) + 1

        open_count = len(health_records) - closed_count
        lines.append("     [PROBLEMAS]")
        if open_count > 0:
            lines.append(f"       • {open_count} ABERTO(S)")
        if closed_count > 0:
            lines.append(f"       • {closed_count} FECHADO(S)")

        for ptype, count in list(problems_by_type.items())[:3]:
            lines.append(f"       • {ptype}: {count}x")

    # 2. Infraestrutura do Cluster
    if infra:
        cluster_cpu_u = infra.get("cluster_cpu_used")
        cluster_cpu_t = infra.get("cluster_cpu_total")
        cluster_mem_u = infra.get("cluster_memory_used")
        cluster_mem_t = infra.get("cluster_memory_total")

        lines.append("     [INFRAESTRUTURA]")
        lines.append("       🏗️  Cluster K8s:")

        def _fc(v):
            try:
                return f"{float(v):.0f}m"
            except Exception:
                return str(v)

        def _fm(v):
            try:
                val = float(v)
                if val >= 1073741824:
                    return f"{val / 1073741824:.1f}Gi"
                if val >= 1048576:
                    return f"{val / 1048576:.0f}Mi"
                return f"{val:.0f}B"
            except Exception:
                return str(v)

        if cluster_cpu_u and cluster_cpu_t:
                try:
                    cpu_pct = (float(cluster_cpu_u) / float(cluster_cpu_t)) * 100
                    status_icon = "🔴" if cpu_pct > 80 else "🟡" if cpu_pct > 60 else "🟢"
                    lines.append(f"         {status_icon} CPU: {cpu_pct:.1f}% ({_fc(cluster_cpu_u)} / {_fc(cluster_cpu_t)})")
                except:
                    pass

        if cluster_mem_u and cluster_mem_t:
                try:
                    mem_pct = (float(cluster_mem_u) / float(cluster_mem_t)) * 100
                    status_icon = "🔴" if mem_pct > 80 else "🟡" if mem_pct > 60 else "🟢"
                    lines.append(f"         {status_icon} Memory: {mem_pct:.1f}% ({_fm(cluster_mem_u)} / {_fm(cluster_mem_t)})")
                except:
                    pass

        if not any([cluster_cpu_u, cluster_cpu_t, cluster_mem_u, cluster_mem_t]):
            lines.append("         • Dados de CPU/Memory não retornaram para este período")

        # Nodes
        node_count = infra.get("node_count", 0)
        node_ready = infra.get("node_ready", 0)
        node_not_ready = infra.get("node_not_ready", 0)

        if node_count > 0:
            lines.append(f"       🖥️  Nodes ({node_count} total):")
            lines.append(f"         ✓ Ready: {node_ready}")
            if node_not_ready > 0:
                lines.append(f"         🔴 Not Ready: {node_not_ready}")

        # Node details — always show node CPU/MEM when available
        nodes = infra.get("nodes", [])
        if nodes:
            lines.append("       📊 Uso por Node:")
            for node in nodes[:10]:
                cpu_pct = node.get("cpu_percent")
                mem_pct = node.get("memory_percent")
                node_name = node.get("name", "?")
                node_status = "✓" if node.get("ready") else "🔴"
                cpu_str = f"{cpu_pct:.1f}%" if cpu_pct is not None else "N/A"
                mem_str = f"{mem_pct:.1f}%" if mem_pct is not None else "N/A"
                cpu_icon = "🔴" if cpu_pct and cpu_pct > 80 else "🟡" if cpu_pct and cpu_pct > 60 else "🟢" if cpu_pct else "⚪"
                mem_icon = "🔴" if mem_pct and mem_pct > 80 else "🟡" if mem_pct and mem_pct > 60 else "🟢" if mem_pct else "⚪"
                lines.append(f"         {node_status} {node_name}: {cpu_icon} CPU={cpu_str} | {mem_icon} Mem={mem_str}")

        # Pods
        pod_count = infra.get("pod_count", 0)
        pod_running = infra.get("pod_running", 0)
        pod_pending = infra.get("pod_pending", 0)
        pod_failed = infra.get("pod_failed", 0)

        if pod_count > 0:
            lines.append(f"       🐳 Pods ({pod_count} total no namespace):")
            lines.append(f"         ✓ Running: {pod_running}")
            if pod_pending > 0:
                lines.append(f"         ⏳ Pending: {pod_pending}")
            if pod_failed > 0:
                lines.append(f"         🔴 Failed: {pod_failed}")

        # Pressões detectadas
        pressures = []
        if infra.get("cpu_pressure"):
            pressures.append("CPU")
        if infra.get("memory_pressure"):
            pressures.append("Memory")
        if infra.get("disk_pressure"):
            pressures.append("Disk")

        if pressures:
            lines.append(f"       🚨 Pressão detectada: {', '.join(pressures)}")

        # Node details se houver problema
        nodes = infra.get("nodes", [])
        if nodes and any([n.get("cpu_percent", 0) > 80 or n.get("memory_percent", 0) > 80 for n in nodes]):
            lines.append("       📊 Nodes com alta utilização:")
            for node in nodes[:5]:
                cpu_pct = node.get("cpu_percent")
                mem_pct = node.get("memory_percent")
                node_status = "✓" if node.get("ready") else "🔴"
                cpu_str = f"{cpu_pct:.0f}%" if cpu_pct else "N/A"
                mem_str = f"{mem_pct:.0f}%" if mem_pct else "N/A"
                lines.append(f"         {node_status} {node.get('name')}: CPU={cpu_str} | Mem={mem_str}")

    # 3. Recursos (CPU/Memory) do workload (Pod level)
    if metrics:
        cpu_usage = metrics.get("cpu_usage")
        cpu_limits = metrics.get("cpu_limits")
        mem_usage = metrics.get("memory_usage")
        mem_limits = metrics.get("memory_limits")
        cpu_req = metrics.get("cpu_requests")
        mem_req = metrics.get("memory_requests")

        def _fmt_cpu(v):
            """Formata CPU em mCores."""
            try:
                return f"{float(v):.0f}m"
            except Exception:
                return str(v)

        def _fmt_mem(v):
            """Formata Memory: se > 1Mi mostra em Mi, senão em bytes."""
            try:
                val = float(v)
                if val >= 1073741824:  # >= 1Gi
                    return f"{val / 1073741824:.1f}Gi"
                if val >= 1048576:  # >= 1Mi
                    return f"{val / 1048576:.0f}Mi"
                if val >= 1024:
                    return f"{val / 1024:.0f}Ki"
                return f"{val:.0f}B"
            except Exception:
                return str(v)

        # Só mostra a seção se houver algum dado
        has_resource_data = any([cpu_usage, mem_usage, cpu_req, mem_req, cpu_limits, mem_limits])

        if has_resource_data:
            lines.append("     [RECURSOS-WORKLOAD (Pod)]")

            # CPU Usage vs Limit (percentual)
            if cpu_usage is not None and cpu_limits is not None:
                try:
                    cpu_pct = (float(cpu_usage) / float(cpu_limits)) * 100
                    status_icon = "🔴" if cpu_pct > 80 else "🟡" if cpu_pct > 60 else "🟢"
                    lines.append(f"       {status_icon} CPU Uso/Limit: {cpu_pct:.1f}% ({_fmt_cpu(cpu_usage)} / {_fmt_cpu(cpu_limits)})")
                except:
                    if cpu_usage and cpu_limits:
                        lines.append(f"       • CPU: uso={_fmt_cpu(cpu_usage)} limit={_fmt_cpu(cpu_limits)}")
            elif cpu_usage is not None:
                lines.append(f"       • CPU Uso: {_fmt_cpu(cpu_usage)}")

            # Memory Usage vs Limit (percentual)
            if mem_usage is not None and mem_limits is not None:
                try:
                    mem_pct = (float(mem_usage) / float(mem_limits)) * 100
                    status_icon = "🔴" if mem_pct > 80 else "🟡" if mem_pct > 60 else "🟢"
                    lines.append(f"       {status_icon} Mem Uso/Limit: {mem_pct:.1f}% ({_fmt_mem(mem_usage)} / {_fmt_mem(mem_limits)})")
                except:
                    if mem_usage and mem_limits:
                        lines.append(f"       • Mem: uso={_fmt_mem(mem_usage)} limit={_fmt_mem(mem_limits)}")
            elif mem_usage is not None:
                lines.append(f"       • Mem Uso: {_fmt_mem(mem_usage)}")

            # CPU Usage vs Request (percentual — mostra se está usando mais que o solicitado)
            if cpu_usage is not None and cpu_req is not None:
                try:
                    cpu_req_pct = (float(cpu_usage) / float(cpu_req)) * 100
                    req_icon = "🔴" if cpu_req_pct > 100 else "🟡" if cpu_req_pct > 80 else "🟢"
                    lines.append(f"       {req_icon} CPU Uso/Request: {cpu_req_pct:.1f}% ({_fmt_cpu(cpu_usage)} / {_fmt_cpu(cpu_req)})")
                except:
                    pass

            if mem_usage is not None and mem_req is not None:
                try:
                    mem_req_pct = (float(mem_usage) / float(mem_req)) * 100
                    req_icon = "🔴" if mem_req_pct > 100 else "🟡" if mem_req_pct > 80 else "🟢"
                    lines.append(f"       {req_icon} Mem Uso/Request: {mem_req_pct:.1f}% ({_fmt_mem(mem_usage)} / {_fmt_mem(mem_req)})")
                except:
                    pass

            # Requests (sempre mostrar como referência)
            if cpu_req or mem_req:
                cpu_req_str = _fmt_cpu(cpu_req) if cpu_req else "N/A"
                mem_req_str = _fmt_mem(mem_req) if mem_req else "N/A"
                lines.append(f"       • Requests: CPU {cpu_req_str} | Mem {mem_req_str}")
            if cpu_limits or mem_limits:
                cpu_lim_str = _fmt_cpu(cpu_limits) if cpu_limits else "N/A"
                mem_lim_str = _fmt_mem(mem_limits) if mem_limits else "N/A"
                lines.append(f"       • Limits:   CPU {cpu_lim_str} | Mem {mem_lim_str}")

        # 4. Eventos críticos
        lines.append("     [EVENTOS]")
        has_critical_events = metrics.get("has_oom") or metrics.get("has_throttling") or metrics.get("has_node_issues")

        if metrics.get("has_oom"):
            lines.append("       🔴 OOM (Out of Memory) detectado")
        if metrics.get("has_throttling"):
            lines.append("       🟡 CPU Throttling detectado")
        if metrics.get("has_node_issues"):
            lines.append("       🔴 Node tem/teve problemas de disponibilidade")

        if not has_critical_events:
            lines.append("       ✓ Sem eventos críticos detectados")

        # 5. Recomendações
        if metrics.get("needs_hpa_improvement"):
            lines.append("     [RECOMENDACOES]")
            lines.append("       ⚠️  Considere revisar HPA (High Pod Autoscaler)")
            lines.append("       ⚠️  Recursos próximos aos limites - possível gargalo")

    return "\n".join(lines)


def format_davis_insights(records: list[dict[str, Any]], classic_details: dict[str, Any] | None = None) -> str:
    """Formata insights Davis em texto estruturado para analise.

    Usa classic_details (da Classic API) como fonte primária quando disponível,
    com fallback para records (DQL).
    """
    lines: list[str] = []

    # ── Fonte primária: Classic API (evidenceDetails + impactAnalysis) ──
    if classic_details:
        title = classic_details.get("title", "")
        severity = classic_details.get("severityLevel", "")
        impact_level = classic_details.get("impactLevel", "")
        root_cause_entity = classic_details.get("rootCauseEntity") or {}
        evidence_details = (classic_details.get("evidenceDetails") or {}).get("details", [])
        impact_analysis = (classic_details.get("impactAnalysis") or {}).get("impacts", [])

        lines.append("[DAVIS - ANALISE]")

        # Root cause
        rc_name = root_cause_entity.get("name", "")
        rc_type = ""
        rc_eid = root_cause_entity.get("entityId")
        if isinstance(rc_eid, dict):
            rc_type = rc_eid.get("type", "")
        if rc_name:
            lines.append(f"  Root Cause: {rc_name} ({rc_type})")

        # Evidence
        if evidence_details:
            seen_events: set[str] = set()
            for ev in evidence_details:
                ev_type = ev.get("evidenceType", "")
                ev_title = ev.get("displayName", "")
                entity = ev.get("entity") or {}
                entity_name = entity.get("name", "")
                is_root = ev.get("rootCauseRelevant", False)
                data_ev = ev.get("data") or {}

                # Deduplica
                ev_key = f"{ev_type}:{ev_title}:{entity_name}"
                if ev_key in seen_events:
                    continue
                seen_events.add(ev_key)

                # Ícone por tipo
                icon = "🔴" if is_root else "⚡"
                if ev_type == "EVENT":
                    event_type = data_ev.get("eventType", "")
                    lines.append(f"  {icon} {ev_title} → {entity_name}")

                    # Extrai detalhes relevantes das properties
                    props = data_ev.get("properties", [])
                    if isinstance(props, list):
                        for prop in props:
                            if not isinstance(prop, dict):
                                continue
                            key = prop.get("key", "")
                            value = str(prop.get("value", ""))
                            if key == "change_details":
                                # Deploy changes
                                try:
                                    changes = json.loads(value) if value.startswith("[") else [value]
                                except Exception:
                                    changes = [value]
                                for change in changes:
                                    lines.append(f"       ↳ {change}")
                            elif key == "dt.event.description":
                                for desc_line in value.split("\n"):
                                    desc_line = desc_line.strip()
                                    if desc_line:
                                        lines.append(f"       ↳ {desc_line}")
                elif ev_type == "METRIC":
                    lines.append(f"  {icon} Metric: {ev_title} → {entity_name}")
                elif ev_type == "TRANSACTIONAL":
                    lines.append(f"  {icon} Transactional: {ev_title} → {entity_name}")
                elif ev_type == "MAINTENANCE_WINDOW":
                    lines.append(f"  🔧 Maintenance: {ev_title} → {entity_name}")
                else:
                    lines.append(f"  {icon} {ev_type}: {ev_title} → {entity_name}")

        # Impact analysis
        if impact_analysis:
            lines.append("  Impacto:")
            for impact in impact_analysis:
                imp_type = impact.get("impactType", "")
                imp_entity = impact.get("impactedEntity", {})
                imp_name = imp_entity.get("name", "")
                imp_etype = ""
                imp_eid = imp_entity.get("entityId")
                if isinstance(imp_eid, dict):
                    imp_etype = imp_eid.get("type", "")
                lines.append(f"    → {imp_type}: {imp_name} ({imp_etype})")

        if len(lines) == 1:  # Só tem o header
            lines.append("  Sem evidências detalhadas para este problema")

        return "\n".join(lines)

    # ── Fallback: DQL records ──
    if records:
        for record in records[:3]:
            causation = record.get("dt.event.causation_chain", "")
            root_cause = record.get("dt.event.root_cause_entity", "")
            description = record.get("event.description", "")

            lines.append("[DAVIS - ANALISE]")
            if causation:
                lines.append(f"  Cadeia de Causacao: {causation}")
            if root_cause:
                lines.append(f"  Root Cause Entity: {root_cause}")
            if description:
                for line in description.split('\n'):
                    line = line.strip()
                    if line and any(kw in line.lower() for kw in ['cause', 'root', 'error', 'failure']):
                        lines.append(f"  {line}")

    if not lines:
        lines.append("[DAVIS] Sem evidências de causa-raiz disponíveis para este problema.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recomendacoes / Remediacao (rule-based)
# ---------------------------------------------------------------------------

_RECS_FAILURE_RATE: list[str] = [
    "Verificar logs de erro do servico afetado (stacktraces, exceptions)",
    "Verificar se houve deploy recente e considerar rollback",
    "Checar dependencias (banco, fila, cache) — latencia ou indisponibilidade podem causar erros em cascata",
    "Avaliar circuit breaker / retry policies do servico",
    "Verificar se o volume de requests aumentou (pico de trafego / bot)",
]

_RECS_RESPONSE_TIME: list[str] = [
    "Verificar queries lentas ou locks no banco de dados",
    "Checar dependencias externas — timeout ou degradacao em cascata",
    "Avaliar se limites de CPU/memoria estao sendo atingidos (throttling)",
    "Verificar se houve deploy recente com regressao de performance",
    "Checar connection pool exhaustion (DB / HTTP clients)",
]

_RECS_HOST_UNAVAILABLE: list[str] = [
    "Verificar status do host / instancia (EC2, VM) — reboot, terminated, etc.",
    "Checar conectividade de rede e security groups",
    "Verificar espaco em disco e uso de memoria do host",
    "Checar status do OneAgent no host",
    "Verificar se ha manutencao programada na infra",
]

_RECS_PROCESS_CRASH: list[str] = [
    "Verificar OOM Kills — container/processo atingiu limite de memoria",
    "Checar core dumps e logs do processo",
    "Verificar se houve deploy recente com bug critico",
    "Avaliar resource limits (CPU/Memory) do container/processo",
]

_RECS_AVAILABILITY: list[str] = [
    "Verificar se o servico/processo esta rodando (healthcheck)",
    "Checar conectividade de rede entre componentes",
    "Verificar balanceador de carga e endpoints configurados",
    "Checar DNS e certificados TLS",
]

_RECS_RESOURCE: list[str] = [
    "Verificar metricas de CPU/Memoria — escalonar se necessario",
    "Checar configuracoes de autoscaling (HPA/VPA no K8s, ASG no EC2)",
    "Avaliar otimizacao de codigo ou queries para reduzir consumo",
]

_RECS_K8S_EXTRA: list[str] = [
    "Verificar status dos pods: kubectl get pods -n <namespace> | grep <workload>",
    "Checar logs dos pods: kubectl logs -n <namespace> <pod> --tail=200",
    "Verificar eventos do namespace: kubectl get events -n <namespace> --sort-by=.lastTimestamp",
    "Checar se ha OOM Kills recentes: kubectl describe pod -n <namespace> <pod>",
    "Avaliar rollback do deployment: kubectl rollout undo deployment/<workload> -n <namespace>",
]

_RECS_LAMBDA_EXTRA: list[str] = [
    "Verificar timeout e memory configuration da funcao Lambda",
    "Checar concurrency limits (reserved / provisioned concurrency)",
    "Verificar CloudWatch Logs para erros e cold starts",
    "Avaliar se o trigger esta gerando invocacoes em excesso",
]

_RECS_AKS_EXTRA: list[str] = [
    "Verificar status dos pods: kubectl get pods -n <namespace> | grep <workload>",
    "Checar logs dos pods: kubectl logs -n <namespace> <pod> --tail=200",
    "Verificar eventos do namespace: kubectl get events -n <namespace> --sort-by=.lastTimestamp",
    "Checar se ha OOM Kills recentes: kubectl describe pod -n <namespace> <pod>",
    "Verificar activity log do AKS: az monitor activity-log list --resource-group <rg> --offset 1h --query \"[?status.value=='Failed']\" -o table",
    "Checar metricas do AKS: az aks show --resource-group <rg> --name <cluster> -o table",
    "Verificar node pool status: az aks nodepool list --resource-group <rg> --cluster-name <cluster> -o table",
    "Consultar diagnosticos do AKS: az monitor metrics list --resource <aks-resource-id> --metric node_cpu_usage_percentage --interval PT5M",
    "Verificar eventos recentes no Azure: az monitor activity-log list --resource-group <rg> --offset 2h -o table",
]

_RECS_DEPLOY_DETECTED: list[str] = [
    "Deploy recente detectado — avaliar rollback imediato se impacto critico",
    "Comparar versao deployada com versao anterior (diff de configuracao/codigo)",
    "Verificar se o deploy alterou variaveis de ambiente ou configuracoes",
]

_PROBLEM_TYPE_MAP: dict[str, list[str]] = {
    "failure rate": _RECS_FAILURE_RATE,
    "error rate": _RECS_FAILURE_RATE,
    "response time": _RECS_RESPONSE_TIME,
    "slowdown": _RECS_RESPONSE_TIME,
    "high response": _RECS_RESPONSE_TIME,
    "unavailable": _RECS_HOST_UNAVAILABLE,
    "host": _RECS_HOST_UNAVAILABLE,
    "process crash": _RECS_PROCESS_CRASH,
    "crash": _RECS_PROCESS_CRASH,
    "availability": _RECS_AVAILABILITY,
    "connectivity": _RECS_AVAILABILITY,
    "cpu": _RECS_RESOURCE,
    "memory": _RECS_RESOURCE,
    "disk": _RECS_RESOURCE,
    "saturation": _RECS_RESOURCE,
    "resource": _RECS_RESOURCE,
}


def _generate_recommendations(
    title: str,
    platform: str,
    classic_details: dict[str, Any] | None,
) -> list[str]:
    """Gera lista de recomendacoes rule-based baseadas em evidencia + tipo do problema."""
    title_lower = title.lower()
    recs: list[str] = []

    # 1. Seleciona base de recomendacoes pelo tipo do problema
    matched = False
    for keyword, rec_list in _PROBLEM_TYPE_MAP.items():
        if keyword in title_lower:
            recs.extend(rec_list)
            matched = True
            break
    if not matched:
        recs.append("Verificar logs e metricas do servico afetado na UI do Dynatrace")
        recs.append("Analisar evidencias da causa-raiz na pagina do problema")

    # 2. Detecta deploy e adiciona recomendacoes especificas
    has_deploy = False
    if classic_details:
        evidence = (classic_details.get("evidenceDetails") or {}).get("details", [])
        for ev in evidence:
            ev_type = ev.get("evidenceType", "")
            ev_title = (ev.get("displayName") or "").lower()
            data = ev.get("data") or {}
            event_type = (data.get("eventType") or "").upper()
            if (
                "deploy" in ev_title
                or "change" in ev_title
                or event_type in ("CUSTOM_DEPLOYMENT", "CUSTOM_INFO", "MARKED_FOR_TERMINATION")
            ):
                has_deploy = True
                break

    if has_deploy:
        recs = _RECS_DEPLOY_DETECTED + recs

    # 3. Adiciona recomendacoes por plataforma
    platform_lower = (platform or "").lower()
    if "aks" in platform_lower or ("azure" in platform_lower and "kubernetes" in platform_lower):
        recs.extend(_RECS_AKS_EXTRA)
    elif "kubernetes" in platform_lower or "k8s" in platform_lower:
        recs.extend(_RECS_K8S_EXTRA)
    elif "lambda" in platform_lower:
        recs.extend(_RECS_LAMBDA_EXTRA)

    # 4. Se tem impacto em usuarios, adiciona urgencia
    if classic_details:
        impacts = (classic_details.get("impactAnalysis") or {}).get("impacts", [])
        for imp in impacts:
            users = imp.get("estimatedAffectedUsers", 0) or 0
            if users > 0:
                imp_name = (imp.get("impactedEntity") or {}).get("name", "")
                recs.insert(0, f"⚠ Usuarios afetados detectados ({users}) em {imp_name} — priorizar resolucao")
                break

    # Deduplica mantendo ordem
    seen: set[str] = set()
    unique: list[str] = []
    for r in recs:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def format_recommendations(
    title: str,
    platform: str,
    classic_details: dict[str, Any] | None,
) -> str:
    """Formata bloco de recomendacoes para exibicao."""
    recs = _generate_recommendations(title, platform, classic_details)
    if not recs:
        return ""
    lines = ["[RECOMENDACOES / REMEDIACAO]"]
    for i, r in enumerate(recs, 1):
        lines.append(f"  {i}. {r}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Selecao automatica de modo
# ---------------------------------------------------------------------------
def _probe_classic(cfg: Config) -> bool:
    if not cfg.api_token:
        return False
    url = f"{_classic_base_url(cfg)}/api/v2/problems"
    try:
        resp = _get(url, _classic_headers(cfg.api_token), params={"problemSelector": 'status("OPEN")', "pageSize": 1}, timeout=10)
        return resp.status_code < 400
    except Exception:
        return False


def _probe_dql(cfg: Config) -> bool:
    if not cfg.platform_token:
        return False
    url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"
    try:
        resp = _post(url, _dql_headers(cfg.platform_token), {"query": "fetch events | limit 1"}, timeout=10)
        return resp.status_code < 400
    except Exception:
        return False


def resolve_mode(cfg: Config) -> str:
    if cfg.auth_mode == "classic":
        return "classic"
    if cfg.auth_mode == "dql":
        # Tenta DQL, mas se falhar e tiver api_token, fallback para classic
        if _probe_dql(cfg):
            return "dql"
        if cfg.api_token and _probe_classic(cfg):
            print("⚠ Platform token sem acesso DQL neste tenant. Usando API classica + dtctl.", file=sys.stderr)
            return "classic"
        return "dql"

    # auto: tenta classic primeiro (mais direto), depois dql
    if _probe_classic(cfg):
        return "classic"
    if _probe_dql(cfg):
        return "dql"

    # Fallback sem probe bem-sucedido
    if cfg.api_token:
        return "classic"
    if cfg.platform_token:
        return "dql"

    raise RuntimeError(
        "Nenhum token configurado. Configure DYNATRACE_API_TOKEN ou DYNATRACE_PLATFORM_TOKEN.\n"
        "Veja README.md para instrucoes."
    )


# ---------------------------------------------------------------------------
# Diagnostico
# ---------------------------------------------------------------------------
def _run_diagnostics(cfg: Config) -> dict[str, Any]:
    report: dict[str, Any] = {
        "env_url": cfg.env_url,
        "auth_mode": cfg.auth_mode,
        "api_token_present": bool(cfg.api_token),
        "platform_token_present": bool(cfg.platform_token),
        "checks": [],
    }

    classic_url = f"{_classic_base_url(cfg)}/api/v2/problems"
    dql_url = f"{_dql_base_url(cfg)}/platform/storage/query/v1/query:execute"

    checks = [
        {
            "name": "classic_problems",
            "method": "GET",
            "url": classic_url,
            "auth_label": "Api-Token",
            "headers": _classic_headers(cfg.api_token) if cfg.api_token else None,
            "params": {"problemSelector": 'status("OPEN")', "pageSize": 1},
            "payload": None,
        },
        {
            "name": "dql_execute",
            "method": "POST",
            "url": dql_url,
            "auth_label": "Bearer",
            "headers": _dql_headers(cfg.platform_token) if cfg.platform_token else None,
            "params": None,
            "payload": {"query": "fetch events | limit 1"},
        },
    ]

    for chk in checks:
        item: dict[str, Any] = {"name": chk["name"], "method": chk["method"], "url": chk["url"], "auth": chk["auth_label"]}
        if chk["headers"] is None:
            item["skipped"] = "token nao configurado"
            report["checks"].append(item)
            continue
        try:
            if chk["method"] == "GET":
                resp = _get(chk["url"], chk["headers"], params=chk["params"])
            else:
                resp = _post(chk["url"], chk["headers"], chk["payload"])
            item["status"] = resp.status_code
            item["ok"] = resp.status_code < 400
            item["body"] = _short_body(resp.text)
        except Exception as ex:
            item["error"] = str(ex)
        report["checks"].append(item)

    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _build_executive_line(item: dict[str, Any]) -> str:
    threshold = ""
    if item.get("threshold_summary"):
        first = str(item["threshold_summary"][0]).strip()
        # Remove repeticao de titulo quando existir
        threshold = re.sub(r"^[^:]+:\s*", "", first, count=1)

    parts = [
        f"Incidente {item.get('alarm', 'N/A')}",
        f"origem={item.get('origin', 'N/A')}",
        f"workload={item.get('workload', 'N/A')}",
        f"conta={item.get('account_origin', 'N/A')}",
        f"regiao={item.get('region', 'N/A')}",
    ]
    if threshold:
        parts.append(f"threshold={threshold}")
    parts.append(f"url={item.get('problem_url', 'N/A')}")
    return " | ".join(parts)


def print_results(items: list[dict[str, Any]], executive: bool = False) -> None:
    if not items:
        print("Nenhum incidente aberto encontrado para a query informada.")
        return
    for idx, item in enumerate(items, start=1):
        print(f"[{idx}] {item['alarm']}")
        print(f"  URL alarme : {item['problem_url']}")
        print(f"  Origem     : {item['origin']}")
        if item.get("platform"):
            print(f"  Plataforma : {item['platform']}")
        if item.get("workload"):
            print(f"  Workload   : {item['workload']}")
        if item.get("account_origin"):
            print(f"  Conta      : {item['account_origin']}")
        if item.get("region"):
            print(f"  Regiao     : {item['region']}")
        if item.get("cloud_provider") and item["cloud_provider"] != "Nao identificado":
            print(f"  Cloud      : {item['cloud_provider']}")
        if item.get("azure_subscription"):
            sub_display = item['azure_subscription']
            if item.get('azure_subscription_name'):
                sub_display = f"{item['azure_subscription_name']} ({item['azure_subscription']})"
            print(f"  Subscricao : {sub_display}")
        if item.get("azure_resource_group"):
            print(f"  Res. Group : {item['azure_resource_group']}")
        if item.get("cloud_service") and item["cloud_service"] != "Nao identificado":
            print(f"  Servico    : {item['cloud_service']}")
        if item.get("service_type") and item["service_type"] != "Nao identificado":
            print(f"  Tipo       : {item['service_type']}")
        if item.get("technologies") and item["technologies"] != "Nao identificado":
            print(f"  Tecnologia : {item['technologies']}")
        if item.get("k8s_cluster") and item["k8s_cluster"] != "Nao identificado":
            print(f"  Cluster    : {item['k8s_cluster']}")
        if item.get("k8s_namespace") and item["k8s_namespace"] != "Nao identificado":
            print(f"  Namespace  : {item['k8s_namespace']}")
        if item.get("k8s_nodes"):
            print(f"  Nodes      : {len(item['k8s_nodes'])} nodes")
            for node_info in item["k8s_nodes"][:10]:
                print(f"    - {node_info}")
            if len(item["k8s_nodes"]) > 10:
                print(f"    ... e mais {len(item['k8s_nodes']) - 10} nodes")
        if item.get("entity_types") and item["entity_types"] != "Nao identificado":
            print(f"  Ent. tipos : {item['entity_types']}")
        if item.get("cloud_id") and item["cloud_id"] != "Nao identificado":
            print(f"  Cloud ID   : {item['cloud_id']}")
        if item.get("threshold_summary"):
            print("  Thresholds :")
            for line in item["threshold_summary"][:6]:
                print(f"    - {line}")
        if item.get("threshold_detailed"):
            print("  Thresholds detalhados:")
            for line in item["threshold_detailed"][:10]:
                print(f"    - {line}")
        if item.get("affected_entities"):
            print(f"  Entidades  : {', '.join(item['affected_entities'][:5])}")
        if item["dashboard_urls"]:
            print("  Dashboards :")
            for dash in item["dashboard_urls"]:
                print(f"    - {dash}")
        else:
            print("  Dashboards : nao configurado")
        # Cloud integration status
        cloud_provider_detected = item.get("cloud_provider", "")
        cloud_aws = item.get("cloud_aws_status", "")
        if cloud_provider_detected.lower() == "azure":
            if item.get("azure_subscription"):
                print(f"  Cloud Azure: ON (subscription detectada)")
            else:
                print(f"  Cloud Azure: OFF (sem integracao Azure no Dynatrace)")
        elif cloud_aws == "OFF_DYNA":
            print("  Cloud AWS  : OFF (sem integracao no Dynatrace)")
        elif cloud_aws:
            print(f"  Cloud AWS  : {cloud_aws}")
        if executive:
            print("  Executivo  :")
            print(f"    { _build_executive_line(item) }")
        print()


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
def is_running_in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consulta incidentes abertos no Dynatrace (Classic API ou DQL/Grail)",
        epilog=(
            "Exemplos:\n"
            "  view_dyna_incidents cdc --window 30m\n"
            "  view_dyna_incidents cdc --from \"21-04-2026 01h00m\" --to \"22-04-2026 11h00m\""
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--query", help="Texto para filtrar incidentes")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Quantidade maxima de incidentes (padrao: {DEFAULT_LIMIT})")
    parser.add_argument("--window", help="Janela relativa, ex: 10m, 5m, 2h, 1d")
    parser.add_argument("--from", dest="from_time", help="Inicio do intervalo (use aspas), ex: \"01-01-2026 10h15m\"")
    parser.add_argument("--to", dest="to_time", help="Fim do intervalo (use aspas), ex: \"01-01-2026 11h00\"")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Saida em JSON")
    parser.add_argument("--executive", action="store_true", help="Inclui linha executiva pronta para Slack/Teams")
    parser.add_argument("--diagnose", action="store_true", help="Diagnostico de autenticacao e endpoints")
    parser.add_argument("--no-dtctl", action="store_true", help="Desabilita enriquecimento via dtctl")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# dtctl integration — Deep topology and events via DQL
# ---------------------------------------------------------------------------

def _dtctl_available() -> bool:
    """Check if dtctl is installed and authenticated."""
    if not shutil.which("dtctl"):
        return False
    try:
        result = subprocess.run(
            ["dtctl", "doctor"],
            capture_output=True, text=True, timeout=15,
        )
        return result.returncode == 0 and "[OK]" in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def _dtctl_query(dql: str, timeout: int = 30) -> list[dict[str, Any]]:
    """Execute a DQL query via dtctl and return records."""
    try:
        result = subprocess.run(
            ["dtctl", "query", dql, "-o", "json", "--plain"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        return data.get("records", [])
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return []


def _dtctl_fetch_open_incidents(
    query: str,
    limit: int,
    time_filter: "TimeFilter",
) -> list[dict[str, Any]]:
    """Fetch open incidents via dtctl DQL when platform token has no DQL access."""
    q = query.strip().lower()
    window = "2h"
    if time_filter.from_ms is not None:
        import time as _time
        delta_ms = int(_time.time() * 1000) - time_filter.from_ms
        hours = max(1, delta_ms // 3_600_000)
        window = f"{hours}h"

    status_filter = ' | filter status == "OPEN"' if time_filter.open_only else ""
    text_filter = f' | filter contains(toString(event.name), "{q}") or contains(toString(affected_entity_ids), "{q}") or contains(toString(display_id), "{q}")' if q else ""

    dql = (
        f"fetch dt.davis.problems, from:-{window}"
        f"{status_filter}"
        f"{text_filter}"
        f" | sort timestamp desc"
        f" | limit {limit}"
    )
    return _dtctl_query(dql, timeout=60)


def _dtctl_get_problem_details(display_id: str, window: str = "48h") -> dict[str, Any] | None:
    """Fetch full problem record from dt.davis.problems."""
    records = _dtctl_query(
        f'fetch dt.davis.problems, from:-{window}'
        f' | filter display_id == "{display_id}"'
        f' | limit 1'
    )
    return records[0] if records else None


def _dtctl_get_davis_events(entity_id: str, window: str = "48h") -> list[dict[str, Any]]:
    """Fetch Davis events for a specific entity."""
    return _dtctl_query(
        f'fetch dt.davis.events, from:-{window}'
        f' | filter in("{entity_id}", dt.entity.service)'
        f' | sort timestamp desc'
        f' | limit 20'
    )


def _dtctl_get_callers(entity_id: str) -> list[dict[str, Any]]:
    """Find services that call the given entity (callers/upstream)."""
    return _dtctl_query(
        f'fetch dt.entity.service'
        f' | fieldsAdd calls[dt.entity.service]'
        f' | filter in("{entity_id}", calls[dt.entity.service])'
        f' | fields entity.name, id'
        f' | limit 20'
    )


def _dtctl_get_dependencies(entity_id: str) -> list[dict[str, Any]]:
    """Get services that the given entity calls (dependencies/downstream)."""
    records = _dtctl_query(
        f'fetch dt.entity.service'
        f' | filter id == "{entity_id}"'
        f' | fieldsAdd calls[dt.entity.service]'
        f' | limit 1'
    )
    if not records:
        return []
    calls = records[0].get("calls[dt.entity.service]") or []
    if not calls:
        return []
    # Resolve names for the called service IDs
    id_list = ", ".join(f'"{c}"' for c in calls[:20])
    return _dtctl_query(
        f'fetch dt.entity.service'
        f' | filter in(id, array({id_list}))'
        f' | fields entity.name, id'
        f' | limit 20'
    )


def _dtctl_resolve_entity_id(service_name: str) -> str | None:
    """Resolve a service name to its entity ID."""
    records = _dtctl_query(
        f'fetch dt.entity.service'
        f' | filter entity.name == "{service_name}"'
        f' | fields id'
        f' | limit 1'
    )
    return records[0]["id"] if records else None


def _dtctl_get_k8s_workload(service_name: str) -> dict[str, Any] | None:
    """Find K8s workload (CLOUD_APPLICATION) matching a service name."""
    records = _dtctl_query(
        f'fetch dt.entity.cloud_application'
        f' | filter contains(entity.name, "{service_name}")'
        f' | fieldsAdd belongs_to[dt.entity.cloud_application_namespace], clustered_by[dt.entity.kubernetes_cluster]'
        f' | limit 3'
    )
    return records[0] if records else None


def _dtctl_resolve_k8s_names(workload: dict[str, Any]) -> dict[str, str]:
    """Resolve namespace and cluster IDs to human-readable names."""
    result = {}
    ns_id = workload.get("belongs_to[dt.entity.cloud_application_namespace]", "")
    cluster_id = workload.get("clustered_by[dt.entity.kubernetes_cluster]", "")
    if ns_id:
        recs = _dtctl_query(
            f'fetch dt.entity.cloud_application_namespace'
            f' | filter id == "{ns_id}"'
            f' | limit 1'
        )
        result["namespace"] = recs[0].get("entity.name", ns_id) if recs else ns_id
    if cluster_id:
        recs = _dtctl_query(
            f'fetch dt.entity.kubernetes_cluster'
            f' | filter id == "{cluster_id}"'
            f' | limit 1'
        )
        result["cluster"] = recs[0].get("entity.name", cluster_id) if recs else cluster_id
    return result


def _dtctl_enrich_cluster_infra(cluster_name: str, infra: dict[str, Any]) -> dict[str, Any]:
    """Enrich cluster infra metrics using dtctl when DQL API is unavailable."""
    safe_cluster = cluster_name.replace('"', '\\"')

    # 1. Node CPU usage
    node_cpu = _dtctl_query(
        f'timeseries cpu = avg(dt.kubernetes.node.cpu_usage_percent), from:-6h, interval:1h'
        f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({safe_cluster})")) }}'
        f' | limit 30'
    )
    nodes: list[dict[str, Any]] = []
    total_cpu = 0.0
    cpu_count = 0

    if node_cpu:
        for nc in node_cpu:
            n_name = nc.get("dt.entity.kubernetes_node", nc.get("entity.name", "?"))
            cpu_vals = nc.get("cpu", [])
            if isinstance(cpu_vals, list) and cpu_vals:
                avg_cpu = sum(v for v in cpu_vals if v) / max(len([v for v in cpu_vals if v]), 1)
                total_cpu += avg_cpu
                cpu_count += 1
                # Find or create node entry
                node_entry = next((n for n in nodes if n["name"] == n_name), None)
                if not node_entry:
                    node_entry = {"name": n_name, "ready": True, "cpu_percent": None, "memory_percent": None}
                    nodes.append(node_entry)
                node_entry["cpu_percent"] = round(avg_cpu, 1)

    # 2. Node Memory usage
    node_mem = _dtctl_query(
        f'timeseries mem = avg(dt.kubernetes.node.memory_usage_percent), from:-6h, interval:1h'
        f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({safe_cluster})")) }}'
        f' | limit 30'
    )
    total_mem = 0.0
    mem_count = 0

    if node_mem:
        for nm in node_mem:
            n_name = nm.get("dt.entity.kubernetes_node", nm.get("entity.name", "?"))
            mem_vals = nm.get("mem", [])
            if isinstance(mem_vals, list) and mem_vals:
                avg_mem = sum(v for v in mem_vals if v) / max(len([v for v in mem_vals if v]), 1)
                total_mem += avg_mem
                mem_count += 1
                node_entry = next((n for n in nodes if n["name"] == n_name), None)
                if not node_entry:
                    node_entry = {"name": n_name, "ready": True, "cpu_percent": None, "memory_percent": None}
                    nodes.append(node_entry)
                node_entry["memory_percent"] = round(avg_mem, 1)

    if nodes:
        infra["nodes"] = nodes
        infra["node_count"] = len(nodes)
        infra["node_ready"] = len(nodes)  # Assume ready since they report metrics
        if cpu_count > 0:
            # Use percentages as proxy for used/total
            avg_cluster_cpu = total_cpu / cpu_count
            infra["cluster_cpu_used"] = avg_cluster_cpu
            infra["cluster_cpu_total"] = 100.0  # Percentage basis
        if mem_count > 0:
            avg_cluster_mem = total_mem / mem_count
            infra["cluster_memory_used"] = avg_cluster_mem
            infra["cluster_memory_total"] = 100.0

    # 3. Pod count
    pod_recs = _dtctl_query(
        f'fetch dt.entity.cloud_application'
        f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
        f' | filter contains(toString(clustered_by[dt.entity.kubernetes_cluster]), "{safe_cluster}")'
        f' | fields id, entity.name, state'
        f' | limit 100'
    )
    if pod_recs:
        running = sum(1 for p in pod_recs if "RUNNING" in str(p.get("state", "")).upper())
        infra["pod_count"] = len(pod_recs)
        infra["pod_running"] = running
        infra["pod_pending"] = len(pod_recs) - running

    return infra


def _dtctl_get_k8s_events(workload_id: str, window: str) -> list[dict[str, Any]]:
    """Get Davis events for a K8s workload (CLOUD_APPLICATION)."""
    return _dtctl_query(
        f'fetch dt.davis.events, from:-{window}'
        f' | filter isNotNull(dt.entity.cloud_application)'
        f' | filter in("{workload_id}", dt.entity.cloud_application[])'
        f' | sort timestamp desc'
        f' | limit 15'
        f' | fields timestamp, event.name, event.type, event.description'
    )


def _dtctl_get_k8s_metrics(workload_name: str, window: str) -> dict[str, Any]:
    """Get CPU and memory timeseries for a K8s workload."""
    result = {}
    cpu_recs = _dtctl_query(
        f'timeseries cpu = avg(dt.kubernetes.container.cpu_usage), from:-{window}, interval:1h,'
        f' filter: {{ in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName({workload_name})")) }}'
        f' | limit 1'
    )
    if cpu_recs and cpu_recs[0].get("cpu"):
        vals = cpu_recs[0]["cpu"]
        result["cpu_avg"] = round(sum(v for v in vals if v) / max(len([v for v in vals if v]), 1), 2)
        result["cpu_max"] = round(max((v for v in vals if v), default=0), 2)

    mem_recs = _dtctl_query(
        f'timeseries mem = avg(dt.kubernetes.container.memory_working_set), from:-{window}, interval:1h,'
        f' filter: {{ in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName({workload_name})")) }}'
        f' | limit 1'
    )
    if mem_recs and mem_recs[0].get("mem"):
        vals = mem_recs[0]["mem"]
        result["mem_avg_mb"] = round(sum(v for v in vals if v) / max(len([v for v in vals if v]), 1) / 1024 / 1024, 1)
        result["mem_max_mb"] = round(max((v for v in vals if v), default=0) / 1024 / 1024, 1)

    # Fetch limits for percentage calculation
    cpu_lim_recs = _dtctl_query(
        f'timeseries lim = avg(dt.kubernetes.container.limits_cpu), from:-{window}, interval:1h,'
        f' filter: {{ in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName({workload_name})")) }}'
        f' | limit 1'
    )
    if cpu_lim_recs and cpu_lim_recs[0].get("lim"):
        vals = cpu_lim_recs[0]["lim"]
        avg_lim = sum(v for v in vals if v) / max(len([v for v in vals if v]), 1)
        if avg_lim > 0:
            result["cpu_limit"] = round(avg_lim, 2)

    mem_lim_recs = _dtctl_query(
        f'timeseries lim = avg(dt.kubernetes.container.limits_memory), from:-{window}, interval:1h,'
        f' filter: {{ in(dt.entity.cloud_application, classicEntitySelector("type(CLOUD_APPLICATION),entityName({workload_name})")) }}'
        f' | limit 1'
    )
    if mem_lim_recs and mem_lim_recs[0].get("lim"):
        vals = mem_lim_recs[0]["lim"]
        avg_lim = sum(v for v in vals if v) / max(len([v for v in vals if v]), 1)
        if avg_lim > 0:
            result["mem_limit_mb"] = round(avg_lim / 1024 / 1024, 1)

    return result


def dtctl_deep_analysis(items: list[dict[str, Any]], time_filter: "TimeFilter") -> str:
    """Perform deep analysis using dtctl: topology, events, dependencies.

    Returns a formatted string block to append to the analysis output.
    """
    output_lines: list[str] = []
    window = "48h"
    if time_filter and time_filter.dql_from:
        # Extract window from dql_from like "now()-24h" → "24h"
        m = re.search(r"now\(\)-(\d+[hmd])", time_filter.dql_from or "")
        if m:
            window = m.group(1)

    for item in items:
        display_id = ""
        alarm = item.get("alarm", "")
        if " - " in alarm:
            display_id = alarm.split(" - ")[0].strip()
        if not display_id:
            continue

        output_lines.append("")
        output_lines.append("=" * 70)
        output_lines.append(f"DTCTL DEEP ANALYSIS — {display_id}")
        output_lines.append("=" * 70)

        # 1. Get full problem from Davis
        problem = _dtctl_get_problem_details(display_id, window)
        if not problem:
            output_lines.append(f"  ⚠ Problema {display_id} nao encontrado via dtctl (window: {window})")
            continue

        root_cause_id = problem.get("root_cause_entity_id", "")
        root_cause_name = problem.get("root_cause_entity_name", "")
        affected_names = problem.get("affected_entity_names", [])
        affected_ids = problem.get("affected_entity_ids", [])
        event_start = problem.get("event.start", "")
        event_end = problem.get("event.end", "")
        users_affected = problem.get("dt.davis.affected_users_count", "")
        description = problem.get("event.description", "")

        output_lines.append("")
        output_lines.append("[PROBLEMA DAVIS]")
        output_lines.append(f"  ID          : {display_id}")
        output_lines.append(f"  Root Cause  : {root_cause_name} ({root_cause_id})")
        output_lines.append(f"  Afetados    : {', '.join(affected_names)}")
        output_lines.append(f"  Inicio      : {event_start}")
        output_lines.append(f"  Fim         : {event_end}")
        output_lines.append(f"  Usuarios    : {users_affected}")
        if description:
            # Clean markdown from description
            clean_desc = re.sub(r'[#*]', '', description).strip()
            for line in clean_desc.split('\n')[:8]:
                stripped = line.strip()
                if stripped:
                    output_lines.append(f"  Descricao   : {stripped}")

        # 2. Topology: who calls the root cause (callers/upstream)
        callers = []
        deps = []
        if root_cause_id:
            output_lines.append("")
            output_lines.append("[TOPOLOGIA — CALLERS (quem chama o root cause)]")
            callers = _dtctl_get_callers(root_cause_id)
            if callers:
                for c in callers:
                    output_lines.append(f"  → {c.get('entity.name', '?')} ({c.get('id', '')})")
            else:
                output_lines.append("  (nenhum caller encontrado)")

            # 3. Topology: dependencies of root cause (downstream)
            output_lines.append("")
            output_lines.append("[TOPOLOGIA — DEPENDENCIAS (quem o root cause chama)]")
            deps = _dtctl_get_dependencies(root_cause_id)
            if deps:
                for d in deps:
                    output_lines.append(f"  → {d.get('entity.name', '?')} ({d.get('id', '')})")
            else:
                output_lines.append("  (nenhuma dependencia encontrada ou servico nao tem calls registrados)")

            # 4. Davis events for root cause
            output_lines.append("")
            output_lines.append(f"[EVENTOS DAVIS — {root_cause_name}]")
            events = _dtctl_get_davis_events(root_cause_id, window)
            if events:
                for ev in events[:10]:
                    ts = ev.get("timestamp", "")[:19]
                    name = ev.get("event.name", "")
                    etype = ev.get("event.type", "")
                    desc = str(ev.get("event.description", ""))[:200]
                    output_lines.append(f"  {ts} | {name} ({etype})")
                    if desc:
                        output_lines.append(f"    {desc}")
            else:
                output_lines.append("  (nenhum evento encontrado)")

        # 5. Events for each caller (check if they have problems too)
        if callers:
            output_lines.append("")
            output_lines.append("[EVENTOS DAVIS — CALLERS]")
            for c in callers[:5]:
                c_id = c.get("id", "")
                c_name = c.get("entity.name", "?")
                c_events = _dtctl_get_davis_events(c_id, window)
                if c_events:
                    output_lines.append(f"  {c_name}:")
                    for ev in c_events[:5]:
                        ts = ev.get("timestamp", "")[:19]
                        name = ev.get("event.name", "")
                        output_lines.append(f"    {ts} | {name}")
                else:
                    output_lines.append(f"  {c_name}: sem eventos")

        # 6. Events for each dependency
        if deps:
            output_lines.append("")
            output_lines.append("[EVENTOS DAVIS — DEPENDENCIAS]")
            for d in deps[:5]:
                d_id = d.get("id", "")
                d_name = d.get("entity.name", "?")
                d_events = _dtctl_get_davis_events(d_id, window)
                if d_events:
                    output_lines.append(f"  {d_name}:")
                    for ev in d_events[:5]:
                        ts = ev.get("timestamp", "")[:19]
                        name = ev.get("event.name", "")
                        output_lines.append(f"    {ts} | {name}")
                else:
                    output_lines.append(f"  {d_name}: sem eventos")

        # 7. Kubernetes deep analysis (if workload is K8s)
        platform = item.get("platform", "")
        cloud_provider = item.get("cloud_provider", "")
        is_k8s = platform.lower() in ("kubernetes", "k8s", "azure/aks") or any(
            k in str(item).lower() for k in ("pod", "namespace", "deployment", "eks", "aks", "vmss", "cluster")
        )
        if is_k8s:
            output_lines.append("")
            output_lines.append("[KUBERNETES — ANALISE PROFUNDA]")

            # Determine cluster name from item metadata
            cluster_name = item.get("k8s_cluster", "")
            if cluster_name in ("", "Nao identificado", "name"):
                cluster_name = ""

            # Try to find cluster from affected entities or resource group
            if not cluster_name:
                rg = item.get("azure_resource_group", "")
                if rg:
                    import re as _re
                    aks_rg_m = _re.match(r"^MC_[^_]+_([^_]+)_", rg)
                    if aks_rg_m:
                        cluster_name = aks_rg_m.group(1)

            # -- 7a. Cluster info
            cluster_id = ""
            if cluster_name:
                cl_recs = _dtctl_query(
                    f'fetch dt.entity.kubernetes_cluster'
                    f' | filter contains(entity.name, "{cluster_name}")'
                    f' | fields id, entity.name, state'
                    f' | limit 5'
                )
                if cl_recs:
                    cluster_id = cl_recs[0].get("id", "")
                    output_lines.append(f"  Cluster     : {cl_recs[0].get('entity.name', cluster_name)} ({cluster_id})")
                    cl_state = cl_recs[0].get("state", "")
                    if cl_state:
                        output_lines.append(f"  Estado      : {cl_state}")
                else:
                    output_lines.append(f"  Cluster     : {cluster_name} (nao encontrado no Dynatrace)")
            else:
                output_lines.append("  Cluster     : (nao identificado)")

            # -- 7b. Nodes do cluster
            output_lines.append("")
            output_lines.append("[KUBERNETES — NODES]")
            node_recs = []
            if cluster_id:
                node_recs = _dtctl_query(
                    f'fetch dt.entity.kubernetes_node'
                    f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
                    f' | filter in("{cluster_id}", clustered_by[dt.entity.kubernetes_cluster])'
                    f' | fields id, entity.name, state, kubernetesLabels'
                    f' | limit 50'
                )
            elif cluster_name:
                node_recs = _dtctl_query(
                    f'fetch dt.entity.kubernetes_node'
                    f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
                    f' | filter contains(toString(clustered_by[dt.entity.kubernetes_cluster]), "{cluster_name}")'
                    f' | fields id, entity.name, state, kubernetesLabels'
                    f' | limit 50'
                )
            if node_recs:
                ready_count = 0
                not_ready = []
                for nr in node_recs:
                    n_name = nr.get("entity.name", "?")
                    n_state = nr.get("state", "UNKNOWN")
                    if isinstance(n_state, dict):
                        n_state = n_state.get("state", "UNKNOWN")
                    if n_state and "RUNNING" in str(n_state).upper():
                        ready_count += 1
                    else:
                        not_ready.append(f"{n_name} ({n_state})")
                output_lines.append(f"  Total       : {len(node_recs)} nodes")
                output_lines.append(f"  Running     : {ready_count}")
                if not_ready:
                    output_lines.append(f"  NOT Ready   : {len(not_ready)}")
                    for nr_info in not_ready[:10]:
                        output_lines.append(f"    ⚠ {nr_info}")
                else:
                    output_lines.append(f"  NOT Ready   : 0")
            else:
                output_lines.append("  (nenhum node encontrado para o cluster)")

            # -- 7c. Pods / Workloads do cluster
            output_lines.append("")
            output_lines.append("[KUBERNETES — PODS / WORKLOADS]")
            pod_recs = []
            if cluster_id:
                pod_recs = _dtctl_query(
                    f'fetch dt.entity.cloud_application'
                    f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
                    f', belongs_to[dt.entity.cloud_application_namespace]'
                    f' | filter in("{cluster_id}", clustered_by[dt.entity.kubernetes_cluster])'
                    f' | fields id, entity.name, state'
                    f', belongs_to[dt.entity.cloud_application_namespace]'
                    f', clustered_by[dt.entity.kubernetes_cluster]'
                    f' | limit 50'
                )
            elif cluster_name:
                pod_recs = _dtctl_query(
                    f'fetch dt.entity.cloud_application'
                    f' | fieldsAdd clustered_by[dt.entity.kubernetes_cluster]'
                    f', belongs_to[dt.entity.cloud_application_namespace]'
                    f' | filter contains(toString(clustered_by[dt.entity.kubernetes_cluster]), "{cluster_name}")'
                    f' | fields id, entity.name, state'
                    f', belongs_to[dt.entity.cloud_application_namespace]'
                    f', clustered_by[dt.entity.kubernetes_cluster]'
                    f' | limit 50'
                )
            if pod_recs:
                running = []
                not_running = []
                for pr in pod_recs:
                    p_name = pr.get("entity.name", "?")
                    p_state = pr.get("state", "UNKNOWN")
                    if isinstance(p_state, dict):
                        p_state = p_state.get("state", "UNKNOWN")
                    if p_state and "RUNNING" in str(p_state).upper():
                        running.append(p_name)
                    else:
                        not_running.append(f"{p_name} ({p_state})")
                output_lines.append(f"  Total workloads : {len(pod_recs)}")
                output_lines.append(f"  Running         : {len(running)}")
                if not_running:
                    output_lines.append(f"  NOT Running     : {len(not_running)}")
                    for pr_info in not_running[:15]:
                        output_lines.append(f"    ⚠ {pr_info}")
                else:
                    output_lines.append(f"  NOT Running     : 0")
            else:
                output_lines.append("  (nenhum workload encontrado para o cluster)")

            # -- 7d. Pod conditions / restarts (via timeseries)
            output_lines.append("")
            output_lines.append("[KUBERNETES — RESTARTS / CRASHLOOPS]")
            restart_recs = _dtctl_query(
                f'timeseries restarts = sum(dt.kubernetes.container.restarts), from:-{window}, interval:1h'
                + (f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({cluster_name})")) }}' if cluster_name else '')
                + f' | filter restarts > 0'
                f' | limit 20'
            )
            if restart_recs:
                output_lines.append(f"  Workloads com restarts nos ultimos {window}:")
                for rr in restart_recs[:15]:
                    # Each record may have a different structure depending on dtctl version
                    r_name = rr.get("dt.entity.cloud_application", rr.get("entity.name", "?"))
                    restarts_vals = rr.get("restarts", [])
                    total_restarts = sum(v for v in restarts_vals if v) if isinstance(restarts_vals, list) else restarts_vals
                    output_lines.append(f"    {r_name}: {total_restarts} restarts")
            else:
                output_lines.append("  (nenhum restart detectado no periodo)")

            # -- 7e. Kubernetes events (Davis events for the cluster)
            output_lines.append("")
            output_lines.append("[KUBERNETES — EVENTOS RECENTES]")
            k8s_davis_events = []
            if cluster_id:
                k8s_davis_events = _dtctl_query(
                    f'fetch dt.davis.events, from:-{window}'
                    f' | filter isNotNull(dt.entity.kubernetes_cluster)'
                    f' | filter in("{cluster_id}", dt.entity.kubernetes_cluster[])'
                    f' | sort timestamp desc'
                    f' | limit 15'
                    f' | fields timestamp, event.name, event.type, event.description'
                )
            if not k8s_davis_events and cluster_name:
                # Fallback: events for nodes/pods that match the cluster
                k8s_davis_events = _dtctl_query(
                    f'fetch dt.davis.events, from:-{window}'
                    f' | filter contains(toString(affected_entity_ids), "{cluster_name}")'
                    f'    OR contains(toString(dt.entity.host), "aks")'
                    f' | sort timestamp desc'
                    f' | limit 15'
                    f' | fields timestamp, event.name, event.type, affected_entity_ids'
                )
            if k8s_davis_events:
                for ev in k8s_davis_events[:15]:
                    ts = ev.get("timestamp", "")[:19]
                    name = ev.get("event.name", "")
                    etype = ev.get("event.type", "")
                    output_lines.append(f"  {ts} | {name} ({etype})")
            else:
                output_lines.append("  (nenhum evento K8s encontrado)")

            # -- 7f. HPA (Horizontal Pod Autoscaler) — if available
            output_lines.append("")
            output_lines.append("[KUBERNETES — HPA (autoscaler)]")
            hpa_recs = _dtctl_query(
                f'timeseries replicas = avg(dt.kubernetes.hpa.current_replicas), from:-{window}, interval:1h'
                + (f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({cluster_name})")) }}' if cluster_name else '')
                + f' | limit 20'
            )
            if hpa_recs:
                for hr in hpa_recs[:10]:
                    hpa_name = hr.get("dt.entity.kubernetes_hpa", hr.get("entity.name", "?"))
                    replicas_vals = hr.get("replicas", [])
                    if isinstance(replicas_vals, list) and replicas_vals:
                        current = replicas_vals[-1] if replicas_vals[-1] else 0
                        max_r = max((v for v in replicas_vals if v), default=0)
                        min_r = min((v for v in replicas_vals if v and v > 0), default=0)
                        output_lines.append(f"  {hpa_name}: atual={int(current)}, min={int(min_r)}, max={int(max_r)} (periodo {window})")
                    else:
                        output_lines.append(f"  {hpa_name}: replicas={replicas_vals}")
            else:
                output_lines.append("  (nenhum HPA encontrado ou metricas indisponiveis)")

            # -- 7g. Node resource usage (CPU/Memory dos nodes)
            output_lines.append("")
            output_lines.append("[KUBERNETES — RECURSOS DOS NODES]")
            node_cpu = _dtctl_query(
                f'timeseries cpu = avg(dt.kubernetes.node.cpu_usage_percent), from:-{window}, interval:1h'
                + (f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({cluster_name})")) }}' if cluster_name else '')
                + f' | limit 20'
            )
            if node_cpu:
                for nc in node_cpu[:10]:
                    n_name = nc.get("dt.entity.kubernetes_node", nc.get("entity.name", "?"))
                    cpu_vals = nc.get("cpu", [])
                    if isinstance(cpu_vals, list) and cpu_vals:
                        avg_cpu = sum(v for v in cpu_vals if v) / max(len([v for v in cpu_vals if v]), 1)
                        max_cpu = max((v for v in cpu_vals if v), default=0)
                        output_lines.append(f"  {n_name}: CPU avg={avg_cpu:.1f}%, max={max_cpu:.1f}%")
            else:
                output_lines.append("  (metricas de nodes nao disponiveis)")

            node_mem = _dtctl_query(
                f'timeseries mem = avg(dt.kubernetes.node.memory_usage_percent), from:-{window}, interval:1h'
                + (f', filter: {{ in(dt.entity.kubernetes_cluster, classicEntitySelector("type(KUBERNETES_CLUSTER),entityName({cluster_name})")) }}' if cluster_name else '')
                + f' | limit 20'
            )
            if node_mem:
                for nm in node_mem[:10]:
                    n_name = nm.get("dt.entity.kubernetes_node", nm.get("entity.name", "?"))
                    mem_vals = nm.get("mem", [])
                    if isinstance(mem_vals, list) and mem_vals:
                        avg_mem = sum(v for v in mem_vals if v) / max(len([v for v in mem_vals if v]), 1)
                        max_mem = max((v for v in mem_vals if v), default=0)
                        output_lines.append(f"  {n_name}: MEM avg={avg_mem:.1f}%, max={max_mem:.1f}%")

            # -- 7h. Workload-specific analysis (if root cause is a known workload)
            if root_cause_name:
                workload = _dtctl_get_k8s_workload(root_cause_name)
                if workload:
                    wl_id = workload.get("id", "")
                    wl_name = workload.get("entity.name", root_cause_name)

                    output_lines.append("")
                    output_lines.append(f"[KUBERNETES — WORKLOAD: {wl_name}]")
                    k8s_names = _dtctl_resolve_k8s_names(workload)
                    ns_name = k8s_names.get("namespace", "")
                    cl_name = k8s_names.get("cluster", "")
                    output_lines.append(f"  ID        : {wl_id}")
                    if ns_name:
                        output_lines.append(f"  Namespace : {ns_name}")
                    if cl_name:
                        output_lines.append(f"  Cluster   : {cl_name}")

                    # Workload CPU/Memory
                    metrics = _dtctl_get_k8s_metrics(wl_name, window)
                    if metrics:
                        if "cpu_avg" in metrics:
                            cpu_limit = metrics.get("cpu_limit")
                            if cpu_limit and cpu_limit > 0:
                                cpu_pct = (metrics["cpu_avg"] / cpu_limit) * 100
                                icon = "🔴" if cpu_pct > 80 else "🟡" if cpu_pct > 60 else "🟢"
                                output_lines.append(f"  {icon} CPU: {cpu_pct:.1f}% ({metrics['cpu_avg']} / {cpu_limit} cores)")
                            else:
                                output_lines.append(f"  CPU avg   : {metrics['cpu_avg']} cores")
                                output_lines.append(f"  CPU max   : {metrics['cpu_max']} cores")
                        if "mem_avg_mb" in metrics:
                            mem_limit = metrics.get("mem_limit_mb")
                            if mem_limit and mem_limit > 0:
                                mem_pct = (metrics["mem_avg_mb"] / mem_limit) * 100
                                icon = "🔴" if mem_pct > 80 else "🟡" if mem_pct > 60 else "🟢"
                                output_lines.append(f"  {icon} Mem: {mem_pct:.1f}% ({metrics['mem_avg_mb']}Mi / {mem_limit}Mi)")
                            else:
                                output_lines.append(f"  MEM avg   : {metrics['mem_avg_mb']} MB")
                                output_lines.append(f"  MEM max   : {metrics['mem_max_mb']} MB")

                    # Workload events
                    k8s_events = _dtctl_get_k8s_events(wl_id, window)
                    if k8s_events:
                        output_lines.append("  Eventos:")
                        for ev in k8s_events[:10]:
                            ts = ev.get("timestamp", "")[:19]
                            name = ev.get("event.name", "")
                            etype = ev.get("event.type", "")
                            output_lines.append(f"    {ts} | {name} ({etype})")

    return "\n".join(output_lines)


def main() -> int:
    args = parse_args()

    try:
        if not is_running_in_venv():
            raise RuntimeError(
                'Execute via ./view_dyna_incidents "<query>" ou dentro de uma venv ativa.'
            )

        cfg = load_config()

        if args.diagnose:
            print(json.dumps(_run_diagnostics(cfg), ensure_ascii=True, indent=2))
            return 0

        if not (args.query or "").strip():
            raise ValueError('Informe --query "<texto>" ou use --diagnose')

        time_filter = _resolve_time_filter(args)

        mode = resolve_mode(cfg)

        if mode == "classic":
            items_raw = fetch_open_incidents_classic(cfg, args.query, args.limit, time_filter=time_filter)
            items_raw = enrich_problems_classic(cfg, items_raw)
            items = [to_output_item_classic(cfg, p) for p in items_raw]

            # Fallback: se classic não retornou dados e dtctl disponível, tenta via DQL/dtctl
            if not items and _dtctl_available():
                dtctl_records = _dtctl_fetch_open_incidents(args.query, args.limit, time_filter)
                if dtctl_records:
                    items = [to_output_item_dql(cfg, r) for r in dtctl_records]
        else:
            items_raw = fetch_open_incidents_dql(cfg, args.query, args.limit, time_filter=time_filter)
            items = [to_output_item_dql(cfg, r) for r in items_raw]

            # Fallback: se DQL não retornou dados e temos api_token, tenta classic
            if not items and cfg.api_token:
                items_raw = fetch_open_incidents_classic(cfg, args.query, args.limit, time_filter=time_filter)
                items_raw = enrich_problems_classic(cfg, items_raw)
                items = [to_output_item_classic(cfg, p) for p in items_raw]

        if args.as_json:
            print(json.dumps(items, ensure_ascii=True, indent=2))
        else:
            print_results(items, executive=args.executive)

        # Se nenhum incidente, não prosseguir com análise
        if not items:
            return 0

        # Davis Root Cause Analysis (ativo por padrão)
        if cfg.platform_token or cfg.api_token:
            print("\n" + "="*70)
            print("ANALISE DAVIS - CAUSA-RAIZ")
            print("="*70)
            for item in items:
                problem_id = item.get("problem_id", "")
                alarm = item.get("alarm", "")
                platform = item.get("platform", "")
                workload = item.get("workload", "")

                if problem_id:
                    print(f"\n{alarm}")

                    # 1. Tenta Classic API (mais rico)
                    classic_details = fetch_davis_analysis_classic(cfg, problem_id) if cfg.api_token else {}

                    # 2. Tenta DQL como fallback
                    davis_records: list[dict[str, Any]] = []
                    if not classic_details and cfg.platform_token:
                        display_id = item.get("alarm", "").split(" - ")[0] if " - " in item.get("alarm", "") else ""
                        davis_records = fetch_davis_causation_dql(cfg, display_id, time_filter)

                    davis_insights = format_davis_insights(davis_records, classic_details or None)
                    print(davis_insights)

                    # Recomendações / Remediação
                    recs_block = format_recommendations(
                        title=alarm, platform=platform,
                        classic_details=classic_details or None,
                    )
                    if recs_block:
                        print()
                        print(recs_block)

                    # Workload Health for Kubernetes — triggers regardless of cloud platform
                    # if we can identify K8s data (cluster, CLOUD_APPLICATION entity, containerd tech, etc.)
                    _techs = (item.get("technologies") or "").lower()
                    _entity_types = item.get("entity_types") or ""
                    _k8s_cluster_val = item.get("k8s_cluster", "")
                    is_k8s_platform = (
                        platform.lower() in ("kubernetes", "k8s", "azure/aks")
                        or "aks" in platform.lower()
                        or "eks" in platform.lower()
                        or "containerd" in _techs
                        or "kubernetes" in _techs
                        or "docker" in _techs
                        or (_k8s_cluster_val and _k8s_cluster_val != "Nao identificado")
                        or "CLOUD_APPLICATION" in str(_entity_types)
                        or "K8S" in str(_entity_types)
                    )
                    if is_k8s_platform and workload and workload != "Nao identificado":
                        print("\n[KUBERNETES - SAUDE DO WORKLOAD]")
                        k8s_cluster = item.get("k8s_cluster", "")
                        k8s_namespace = item.get("k8s_namespace", "")

                        # Enriquece com dados do classic_details se disponível
                        if classic_details:
                            if not k8s_cluster or k8s_cluster == "Nao identificado":
                                cl = classic_details.get("k8s.cluster.name", "")
                                k8s_cluster = cl[0] if isinstance(cl, list) and cl else (cl if isinstance(cl, str) else "")
                            if not k8s_namespace or k8s_namespace == "Nao identificado":
                                ns = classic_details.get("k8s.namespace.name", "")
                                k8s_namespace = ns[0] if isinstance(ns, list) and ns else (ns if isinstance(ns, str) else "")

                        # Limpa valores "Nao identificado"
                        if k8s_cluster == "Nao identificado":
                            k8s_cluster = ""
                        if k8s_namespace == "Nao identificado":
                            k8s_namespace = ""

                        # Fallback: resolve K8s metadata via entity relationships (SERVICE → PG → metadata)
                        k8s_workload_name = ""
                        if not k8s_namespace or not k8s_cluster:
                            _entity_ids = []
                            for e in (classic_details.get("affectedEntities") or []):
                                eid = e.get("entityId", "")
                                if isinstance(eid, dict):
                                    eid = eid.get("id", "")
                                if isinstance(eid, str) and eid:
                                    _entity_ids.append(eid)
                            if _entity_ids:
                                _k8s_meta = _resolve_k8s_metadata_from_entities(cfg, _entity_ids)
                                if not k8s_namespace and _k8s_meta.get("k8s_namespace"):
                                    k8s_namespace = _k8s_meta["k8s_namespace"]
                                if not k8s_cluster and _k8s_meta.get("k8s_cluster"):
                                    k8s_cluster = _k8s_meta["k8s_cluster"]
                                if _k8s_meta.get("k8s_workload_name"):
                                    k8s_workload_name = _k8s_meta["k8s_workload_name"]

                        # Fallback dtctl: resolve cluster/namespace via CLOUD_APPLICATION lookup
                        if (not k8s_cluster or not k8s_namespace) and _dtctl_available():
                            # Use first clean workload name to find CLOUD_APPLICATION
                            _wl_names_tmp = []
                            if isinstance(workload, str):
                                _wl_names_tmp = [re.sub(r"\s*\[.*?\]\s*$", "", w.strip()).strip() for w in workload.split(",")]
                            for _wn in _wl_names_tmp[:2]:
                                if not _wn:
                                    continue
                                _dtctl_wl = _dtctl_get_k8s_workload(_wn)
                                if _dtctl_wl:
                                    _k8s_names = _dtctl_resolve_k8s_names(_dtctl_wl)
                                    if not k8s_cluster and _k8s_names.get("cluster"):
                                        k8s_cluster = _k8s_names["cluster"]
                                    if not k8s_namespace and _k8s_names.get("namespace"):
                                        k8s_namespace = _k8s_names["namespace"]
                                    if not k8s_workload_name:
                                        k8s_workload_name = _dtctl_wl.get("entity.name", _wn)
                                    break

                        # Fetch cluster infra metrics uma vez
                        cluster_infra = fetch_k8s_cluster_infra_metrics(cfg, k8s_cluster, k8s_namespace, time_filter)

                        # Fallback: se DQL/Metrics API não trouxe dados, usa dtctl
                        if not cluster_infra.get("cluster_cpu_used") and _dtctl_available() and k8s_cluster:
                            cluster_infra = _dtctl_enrich_cluster_infra(k8s_cluster, cluster_infra)

                        # Parse workload list if it's a string representation of a list
                        workload_names = []
                        if isinstance(workload, str):
                            if workload.startswith('['):
                                try:
                                    workload_names = ast.literal_eval(workload)
                                except:
                                    workload_names = [workload]
                            else:
                                # Pode ser "svc1 [TYPE], svc2 [TYPE]" do classic API
                                workload_names = [w.strip() for w in workload.split(",")]
                        elif isinstance(workload, list):
                            workload_names = workload

                        # Remove sufixo [SERVICE/...] dos nomes de workload
                        clean_names = []
                        for wn in workload_names:
                            clean = re.sub(r"\s*\[.*?\]\s*$", "", wn).strip()
                            if clean:
                                clean_names.append(clean)
                        workload_names = clean_names or workload_names

                        # Fetch health for each workload
                        for wname in workload_names[:3]:  # Limita a 3 workloads
                            health_records = fetch_workload_health_dql(cfg, wname, time_filter)
                            metrics = fetch_k8s_resource_metrics(cfg, wname, k8s_cluster, k8s_namespace, time_filter, k8s_workload_name=k8s_workload_name)
                            health_info = format_workload_health(wname, health_records, metrics, cluster_infra, k8s_cluster, k8s_namespace)
                            print(health_info)

        # dtctl Deep Analysis — topology, events, dependencies
        if not args.no_dtctl and _dtctl_available():
            print("\n" + "=" * 70)
            print("DTCTL DEEP ANALYSIS — Topologia e Eventos (via DQL)")
            print("=" * 70)
            dtctl_output = dtctl_deep_analysis(items, time_filter)
            if dtctl_output.strip():
                print(dtctl_output)
            else:
                print("  (nenhum dado adicional obtido via dtctl)")
        elif not args.no_dtctl and not shutil.which("dtctl"):
            print("\n⚠ dtctl nao instalado — analise de topologia/dependencias indisponivel.")
            print("  Instale: curl -fsSL https://raw.githubusercontent.com/dynatrace-oss/dtctl/main/install.sh | sh")
            print("  Configure: dtctl auth login --context my-env --environment \"https://<tenant>.apps.dynatrace.com\"")

        return 0

    except requests.HTTPError as ex:
        status = ex.response.status_code if ex.response is not None else "?"
        body = ex.response.text if ex.response is not None else str(ex)
        print(f"Erro HTTP {status}: {_short_body(body)}", file=sys.stderr)
        return 2
    except Exception as ex:
        print(f"Erro: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
