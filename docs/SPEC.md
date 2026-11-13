# SPEC.md — AI Help Desk Triage & Remediation Agent

**Contrato técnico interno del equipo de 3 agentes.** Owner: Director agent (`[D]`).
Fecha: 2026-11-12. Destinatarios: Implementador A (`[A]`), Implementador B (`[B]`).

> Este documento es el **único canal de comunicación** entre los dos implementadores.
> Nadie crea archivos fuera de su lista (§9). Ante ambigüedad no resuelta acá: **no decidir por
> su cuenta** → preguntar al director.

---

## 1. Objetivo, no-objetivos y stack

### 1.1 Objetivo

Repo público de GitHub que demuestra, con código ejecutable, cómo se diseña un agente de triage
de Help Desk **seguro**: clasifica tickets, prioriza, recupera KB, valida identidad, evalúa
riesgo, decide si ejecuta solo o pide aprobación humana, escala, y deja audit trail completo
con métricas y evaluación continua.

Criterio de éxito: **técnico y honesto**. Cada decisión del agente es explicable y reproducible.
Si una feature no se puede explicar con un ejemplo concreto, no entra al repo.

### 1.2 No-objetivos (NO se implementan)

| No-objetivo | Motivo |
|---|---|
| UI web / Streamlit / dashboard HTML | La demo es CLI. UI agrega superficie sin valor probatorio. |
| LLM real (OpenAI/Gemini/Groq/Claude API) | La clasificación es determinista y heurística: "explainable" > "smart". El README documenta cómo se enchufaría un LLM. |
| Persistencia real (DB, Redis) | Archivos JSON/CSV/JSONL, reemplazables por Postgres en prod. |
| Envío real de email / Slack / Teams | La notificación de aprobación es un archivo + stdout. |
| Escrituras a sistemas productivos | Los conectores reales exigen credenciales explícitas + kill-switch `HELPDESK_ALLOW_WRITES=1`. |
| Multi-tenancy, RBAC del repo, CI/CD, K8s | Fuera de alcance. |

### 1.3 Stack (hard constraints)

- **Python 3.9+** (target mínimo 3.9; verificado en la máquina con 3.15).
- **Núcleo = stdlib ONLY.** `agent.py`, `services/`, `policies/`, `data/`, `eval/` corren sin `pip install`.
- **`requests` NO está instalado** en esta máquina ⇒ los conectores reales hacen `import requests`
  **lazy, dentro del método que lo necesita** (§3.1).
- **Sin `pytest` instalado** ⇒ los tests usan `unittest.TestCase` (compatibles con pytest si se
  instala). Comando canónico: `python3 -m unittest discover -s tests -t . -v`.
- **Sin `python-dotenv`** ⇒ loader `.env` propio, stdlib, en `adapters/base.py` (§3.1).
- GitHub Actions: NO en esta entrega (queda como next-step en el README).

### 1.4 Comandos canónicos (desde el repo root)

```bash
python3 agent.py --demo                                    # demo 5 min, dry-run, narrado
python3 agent.py --input data/tickets.csv --dry-run --verbose
python3 agent.py --input data/tickets.csv --verbose         # escribe al ITSM mock
python3 -m unittest discover -s tests -t . -v                # suite completa, 0 installs
python3 eval/run_eval.py                                     # eval harness + report
make help
```

Las rutas se resuelven **relativas al root del repo** (`Path(__file__).resolve().parent`), no al CWD.

---

## 2. Enumeraciones canónicas (CONTRATO — nadie las cambia)

Fuente de verdad para `kb.json`, `policies/*.json`, `eval/labeled_tickets.json` y el código.

**2.1 Categorías** — `access` (permisos/roles de cuenta existente), `account_locked` (cuenta
bloqueada), `password` (olvidada/vencida/restablecer), `network` (conectividad/WiFi/LAN/DNS),
`vpn` (acceso remoto), `software` (instalación/licencia), `hardware` (equipo/periférico),
`email` (cliente de correo/adjuntos/firma), `security` (phishing/malware/fraude),
`unknown` (sin match confiable).

**Regla de desambiguación:** el spec original usaba `access` y `access_request` separadas →
se **unifican en `access`** y se agrega el campo `intent`: `"trouble"` (algo roto) | `"request"`
(algo que se pide). `intent` = `"request"` si hay verbos de solicitud (`solicito`, `necesito`,
`quiero`, `acceder a`, `permiso`, `alta`, `habilitar`); `"trouble"` si hayverbs de falla
(`no puedo`, `falla`, `error`, `dice`, `bloqueado`). Con ambos, gana `"trouble"`.

**2.2 Prioridades** — `P1` (más urgente) … `P4`. **2.3 Riesgo** — `low`, `medium`, `high`, `critical`.
**2.4 Decisiones** — `auto_execute` | `require_approval` | `escalate`.

**2.5 Acciones canónicas**

| action | Riesgo base | Aprobador por defecto |
|---|---|---|
| `send_kb_article` | low | — |
| `password_reset_link` | low | — (sólo con identidad verificada) |
| `unlock_account` | medium | `support_lead` |
| `grant_role` | high | `manager` |
| `grant_folder_access` | high | `manager` |
| `request_access_approval` | high | `manager` |
| `assign_software_package` | medium | `support_lead` |
| `restart_service` | medium | `support_lead` |
| `escalate_network_team` | medium | `on_call` |
| `escalate_security_team` | critical | `security` |
| `disable_user` | critical | `security` |
| `human_review` | medium | `support_lead` |
| `no_action` | low | — |

**2.6 Roles aprobadores** — `support_lead` | `manager` | `security` | `on_call`.
**2.7 Colas de escalamiento** — `on-call` | `security` | `network-team` | `itsm-l2`.
**2.8 Impacto / Urgencia** — `impact`: `individual`|`team`|`department`|`organization`;
`urgency`: `low`|`normal`|`high`|`critical`.
**2.9 Estados de aprobación** — `pending` → `approved`|`rejected`|`expired`|`superseded`;
desde `approved` → `executed`|`failed`.

---

## 3. Contratos de API

Estilo: identificadores y docstrings en **inglés**; comentarios/README/docs en ES con excerpts EN.
Todo retorno es `dict` con claves **exactas**. Ninguna función del núcleo lanza por datos de
negocio: devuelve dict con `status`/`error`.

### 3.1 `adapters/base.py` — ABCs, errores, helpers (dueño `[B]`)

```python
class AdapterError(Exception): ...
class AdapterConfigError(AdapterError): ...    # credenciales/config faltante
class AdapterNotFoundError(AdapterError): ...  # recurso no encontrado
class VerificationRequired(AdapterError): ...  # identidad no verificada

def require_requests() -> object
    """Import LAZY de `requests`. Lanza AdapterConfigError con mensaje claro:
       "Optional dependency 'requests' is required for <ConnectorName>. Install it with: pip install requests" """

def http_request(method, url, *, auth=None, headers=None, params=None, json_body=None,
                 timeout=(5, 15), retries=3, backoff_base=0.5,
                 retry_statuses=(429, 500, 502, 503, 504)) -> dict
    """Request con reintentos exponenciales (backoff_base * 2**intento + jitter determinista).
    Reintenta sólo en retry_statuses, requests.exceptions.Timeout y ConnectionError.
    NO reintenta 4xx salvo 429. Devuelve el JSON parseado; si el body no es JSON devuelve
    {"raw": "<texto>", "status_code": <int>}."""

def load_dotenv(path: str = ".env", override: bool = False) -> Dict[str, str]
    """Parser .env stdlib: ignora vacíos y comentarios (#); soporta KEY=VALUE, comillas simples/
    dobles y `export KEY=VALUE`. NO sobreescribe variables existentes salvo override=True."""

def utc_now_iso() -> str
    """datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')"""
```

**3.1.1 `ITSMAdapter` (ABC)**

```python
class ITSMAdapter(abc.ABC):
    name: str  # "mock" | "servicenow" | "jira"
    def get_ticket(self, ticket_id: str) -> dict: ...
    def add_comment(self, ticket_id: str, comment: str) -> dict: ...
    def assign(self, ticket_id: str, queue: str) -> dict: ...
    def set_priority(self, ticket_id: str, priority: str) -> dict: ...
    def resolve(self, ticket_id: str, resolution: str) -> dict: ...
    def list_tickets(self, limit: int = 50, offset: int = 0, query: Optional[str] = None) -> List[dict]: ...
```

Shapes (idénticos en mock y reales):

```python
# get_ticket / list_tickets -> ticket
{"ticket_id": "TCK-001", "title": "...", "description": "<texto original>",
 "requester": "juan.perez", "service": "SAP Fiori", "priority": "P3", "status": "new",
 "created_at": "2026-11-10T09:15:00Z", "source": "mock|servicenow|jira", "raw": { ... }}

# add_comment / assign / set_priority / resolve
{"ticket_id": "TCK-001", "status": "comment_added|assigned|priority_updated|resolved",
 "external_id": "<id API o None en mock>", "timestamp": "<isoZ>",
 "comment": "<texto>" | "queue": "<cola>" | "priority": "P2" | "resolution": "<texto>"}
```

**Reglas comunes de los conectores reales (obligatorias):**
- Timeouts explícitos `timeout=(5, 15)` (connect, read). Reintentos: 3, backoff exponencial
  base 0.5 s, sólo en 429/5xx y errores de conexión.
- `write_enabled() -> bool`: `False` si `HELPDESK_ALLOW_WRITES != "1"`. Los métodos de escritura
  (`add_comment`, `assign`, `set_priority`, `resolve`, `reset_password`, `unlock_account`,
  `grant_role`) **no llaman a la API** y devuelven
  `{"status": "skipped", "reason": "writes_disabled", ...}`.
- Nunca loguean credenciales; `Authorization`, `password`, `access_token`, `client_secret` se
  sanean a `"***"` en todo lo que se persiste.

**3.1.2 `IdentityAdapter` (ABC)**

```python
class IdentityAdapter(abc.ABC):
    name: str  # "mock" | "entra" | "okta"
    def get_user(self, username: str) -> dict: ...
    def get_groups(self, username: str) -> List[str]: ...
    def is_active(self, username: str) -> bool: ...
    def request_access(self, username: str, resource: str,
                       justification: Optional[str] = None) -> dict: ...
    def reset_password(self, username: str, *, verified: bool = False,
                       verification_method: Optional[str] = None) -> dict: ...
    def unlock_account(self, username: str, *, verified: bool = False) -> dict: ...
    def get_manager(self, username: str) -> Optional[str]: ...
```

`get_user` devuelve:
`{"username","email","display_name","department","active","locked","locked_by_admin","groups",
"manager","mfa_enrolled","password_last_set_days","identity_verified","source","raw"}`.

**Reglas de seguridad (obligatorias — son el corazón del guardrail):**
1. `reset_password(..., verified=False)` → si `verified` es False, o el usuario no tiene MFA
   enrolado, o no está activo ⇒ lanza `VerificationRequired` ("Password reset requires verified
   identity with MFA"). **Nunca** ejecuta el reset en ese caso.
2. `unlock_account(..., verified=False)` → si `verified` es False ⇒ `VerificationRequired`. Si
   `locked_by_admin` es True ⇒ `VerificationRequired` ("locked by administrator; requires security approval").
3. `request_access` sólo acepta recursos del allow-list de `policies/access_policies.json`; si no
   está ⇒ `AdapterConfigError` con la lista de recursos válidos.
4. `get_user` de usuario inexistente ⇒ `AdapterNotFoundError`.
5. Todo método mutante devuelve `{"status": "success"|"skipped"|"failed", "action_id", "user",
   "timestamp"}` y es **idempotente**: `action_id = sha256(user|action|resource)[:12]`.

**3.1.3 `AppAdapter` (ABC)**

```python
class AppAdapter(abc.ABC):
    name: str      # "mock" | "sap_fiori" | "vpn" | "shared_drive"
    service: str   # "SAP Fiori" | "VPN" | "Shared Drive"
    def check_status(self, app: str) -> dict: ...
    def execute_action(self, action: dict) -> dict: ...
    def list_resources(self) -> List[dict]: ...
```

`execute_action(action)` recibe `{"action","target","resource","user","params"}` y devuelve
`{"action_id","action","status": "succeeded|simulated|skipped|failed","detail","executed_at","dry_run"}`.
`check_status` devuelve `{"app","status": "operational|degraded|down","checked_at","details","incident_ref"}`.
`list_resources` devuelve `{"resource","type","sensitive","owner"}`.

### 3.2 `adapters/` — implementaciones (dueño `[B]`)

**3.2.1 `adapters/__init__.py` — factory (API pública que consume `agent.py`)**

```python
ITSM_KINDS = ("mock", "servicenow", "jira")
IDENTITY_KINDS = ("mock", "entra", "okta")
APP_KINDS = ("mock", "sap_fiori", "vpn", "shared_drive")

def build_itsm_adapter(kind: Optional[str] = None, **kwargs) -> ITSMAdapter
def build_identity_adapter(kind: Optional[str] = None, **kwargs) -> IdentityAdapter
def build_app_adapter(kind: Optional[str] = None, **kwargs) -> AppAdapter
```
- `kind=None` ⇒ lee env `HELPDESK_ITSM_ADAPTER` / `HELPDESK_IDENTITY_ADAPTER` / `HELPDESK_APP_ADAPTER`, default `"mock"`.
- `kind` inválido ⇒ `AdapterConfigError` ("Unknown ITSM adapter 'x'. Valid options: mock, servicenow, jira").
- `kind` real sin credenciales ⇒ `AdapterConfigError` **al construir** (fail-fast, no en el primer request).
- `kwargs` opcionales: `data_path`, `users_path`, `groups_path`, `base_url`, `timeout`.

**3.2.2 `adapters/itsm.py`**

```python
class MockITSM(ITSMAdapter):
    def __init__(self, data_path: str = "data/tickets.csv") -> None
class ServiceNowITSM(ITSMAdapter):
    def __init__(self, instance=None, username=None, password=None, access_token=None,
                 table: str = "incident", assignment_group: str = "Service Desk") -> None
class JiraITSM(ITSMAdapter):
    def __init__(self, url=None, email=None, api_token=None, project_key=None,
                 issue_type: str = "Service Request", resolve_transition_id: str = "31") -> None
```

**`MockITSM`** — lee `data/tickets.csv` (NO tickets hardcodeados: corregir el spec original).
Estado en memoria: `_tickets`, `_comments`, `_priority`, `_queue`, `_resolved`.
`get_ticket` de id inexistente ⇒ `AdapterNotFoundError`. `list_tickets` devuelve el CSV completo
con los overrides de memoria aplicados.

**`ServiceNowITSM`** (API real) — Base `https://{SERVICENOW_INSTANCE}.service-now.com`;
Auth Basic `(username, password)` **o** `Authorization: Bearer {SERVICENOW_ACCESS_TOKEN}` (el token
tiene prioridad); sin ambos ⇒ `AdapterConfigError` en `__init__`; Headers `Accept`/`Content-Type: application/json`.

| método | endpoint |
|---|---|
| `get_ticket` | `GET /api/now/table/{table}?number={id}&sysparm_limit=1` |
| `list_tickets` | `GET /api/now/table/{table}?sysparm_limit={limit}&sysparm_offset={offset}` |
| `add_comment` | `POST /api/now/table/sys_journal_field` body `{"sys_id","article","element_id":"comments"}` |
| `assign` | `PATCH /api/now/table/{table}/{sys_id}` body `{"assignment_group":"<queue>"}` |
| `set_priority` | `PATCH ...` body `{"priority":"<priority>"}` |
| `resolve` | `PATCH ...` body `{"close_code":"Solved","close_notes":"<res>","state":"6","resolved_at":"<isoZ>"}` |

`sys_id` se resuelve con `get_ticket` y se cachea por `ticket_id`; inexistente ⇒ `AdapterNotFoundError`.

**`JiraITSM`** (API real) — Base `{JIRA_URL}` (sin `/` final); Auth Basic `(JIRA_EMAIL, JIRA_API_TOKEN)`
— **API token, nunca contraseña**; sin ambos ⇒ `AdapterConfigError`.

| método | endpoint |
|---|---|
| `get_ticket` | `GET /rest/api/3/issue/{id}?fields=summary,description,priority,status,reporter,created,labels` |
| `list_tickets` | `GET /rest/api/3/search?jql=project={JIRA_PROJECT_KEY}&maxResults={limit}&startAt={offset}` |
| `add_comment` | `POST /rest/api/3/issue/{id}/comment` body ADF `{"body":{"type":"doc","version":1,"content":[{"type":"paragraph","content":[{"type":"text","text":"<comment>"}]}]}}` |
| `assign` | `PUT /rest/api/3/issue/{id}/assignee` body `{"accountId":"<queue>"}` |
| `set_priority` | `PUT /rest/api/3/issue/{id}` body `{"fields":{"priority":{"name":"<priority>"}}}` |
| `resolve` | `POST /rest/api/3/issue/{id}/transitions` body `{"transition":{"id":"<JIRA_RESOLVE_TRANSITION_ID>"}}` |

Mapper: `fields.priority.name`→priority; `fields.reporter.emailAddress`→requester (recortar prefijo
`upn:`); `fields.labels`→service; `fields.description` (ADF)→texto plano (concatenar `content[].content[].text`).

**3.2.3 `adapters/identity.py`**

```python
class MockIdentity(IdentityAdapter):
    def __init__(self, users_path: str = "data/users.json", groups_path: str = "data/groups.json") -> None
class EntraIDIdentity(IdentityAdapter):
    def __init__(self, tenant_id=None, client_id=None, client_secret=None, access_token=None,
                 base_url: str = "https://graph.microsoft.com/v1.0") -> None
class OktaIdentity(IdentityAdapter):
    def __init__(self, org=None, api_token=None, base_url=None) -> None
```

**`MockIdentity`** — fuente de verdad: `data/users.json` + `data/groups.json`.

**`EntraIDIdentity`** (Microsoft Graph) — Base `https://graph.microsoft.com/v1.0` (override
`GRAPH_BASE_URL`); Auth `Authorization: Bearer {ENTRA_ACCESS_TOKEN}`. Si hay `tenant_id`+
`client_id`+`client_secret` y no hay token, **documentar** el flujo OAuth2 client-credentials
(`POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope
`https://graph.microsoft.com/.default`) como paso manual (no se implementa el token fetch: no hay
`requests` garantizado ni secretos en el repo). Sin token ⇒ `AdapterConfigError` con instrucciones.

| método | endpoint |
|---|---|
| `get_user` | `GET /users/{username}` → `accountEnabled`→active, `mail`→email, `displayName`→display_name, `department`→department, `manager`→manager, `signInMethods`/`passwordPolicies`→mfa_enrolled |
| `get_groups` | `GET /users/{username}/memberOf` → lista de ids de grupo |
| `is_active` | `bool(get_user()["active"])` |
| `get_manager` | `GET /users/{username}/manager` → userPrincipalName o `None` |
| `reset_password` | `PATCH /users/{username}` body `{"passwordProfile":{"forceChangePasswordNextSignIn":true,"password":"<generado>"}}` (requiere `Directory.ReadWrite.All`; documentar la alternativa Self-Service Password Reset) |
| `unlock_account` | `PATCH /users/{username}` body `{"accountEnabled":true}` — **Graph no expone unlock de lockout de credenciales**: si la tenant no lo soporta, devolver `{"status":"failed","reason":"not_supported_by_graph"}` — **no simular éxito** |

**`OktaIdentity`** (Okta Management API) — Base `https://{OKTA_ORG}.okta.com/api/v1` (o
`OKTA_BASE_URL`); Auth `Authorization: SSWS {OKTA_API_TOKEN}`.

| método | endpoint |
|---|---|
| `get_user` | `GET /users/{username}` → `status` ("ACTIVE"/"DEPROVISIONED"/"LOCKED_OUT")→active/locked, `profile.*` |
| `get_groups` | `GET /users/{username}/groups` → `profile.name` |
| `is_active` | `get_user()["status"] == "ACTIVE"` |
| `get_manager` | `GET /users/{username}/managers` → primer login o `None` |
| `reset_password` | `POST /users/{username}/lifecycle/resetPassword` body `{"sendEmail":true}` |
| `unlock_account` | `POST /users/{username}/lifecycle/unlock` body `{}` |
| `request_access` | Okta no tiene "access request" nativo ⇒ devuelve `{"status":"pending_approval","request_id":"<uuid>"}` **sin escribir**; el alta real post-aprobación es `POST /users/{username}/groups/{group_id}` |

**3.2.4 `adapters/apps.py`**

```python
class MockApps(AppAdapter):
    def __init__(self, service: str = "SAP Fiori", status: str = "operational") -> None
class MockSAPFioriApp(MockApps): ...    # service="SAP Fiori"
class MockVPNApp(MockApps): ...          # service="VPN"
class MockSharedDriveApp(MockApps): ... # service="Shared Drive"
```
`MockApps.list_resources()` devuelve recursos del `service` con `{"resource","type","sensitive","owner"}`.
`execute_action` devuelve `status:"simulated"` si `write_enabled()` es False (default), `"succeeded"` sólo en modo escritura explícito.

### 3.3 `services/triage.py` (dueño `[A]`)

```python
class TriageService:
    def __init__(self, kb: List[dict], priority_matrix: Optional[dict] = None,
                 category_overrides: Optional[dict] = None) -> None
    def normalize(self, text: str) -> str
    def tokenize(self, text: str) -> List[str]
    def match_article(self, text: str, service: Optional[str] = None) -> dict
    def score(self, article: dict, matched_strong: List[str], matched_weak: List[str],
              text_tokens: List[str], service: Optional[str] = None) -> dict
    def classify(self, ticket: dict) -> dict
    def extract_entities(self, ticket: dict, articles: List[dict]) -> dict
    def impact_urgency(self, ticket: dict, category: str, text: str) -> Tuple[str, str]
```

**3.3.1 Normalización y tokenización (deterministas)**
`text.lower()` → quitar acentos (`unicodedata.normalize("NFKD", s)`, filtrar `Mn`) → reemplazar
`[^a-z0-9]+` por espacio → colapsar espacios → `strip()`. `tokenize`: split por espacios +
**singularización ligera**: quitar sufijo `"es"` si `len>4`, luego `"s"` si `len>3`; se aplica del
lado del texto y del lado del keyword ⇒ matching insensible a plurales ES/EN.

**3.3.2 Matching contra la KB** — cada artículo tiene `keywords` (peso 1) y `strong_keywords` (peso 2).
Un keyword matchea si su secuencia de tokens singularizados aparece **contigua** en los tokens del texto.
Desempate, en orden: (1) mayor `score`; (2) mayor cantidad de `strong_keywords` matcheados; (3) `article["id"]` menor (lexicográfico).

**3.3.3 Fórmula de confianza (CONTRATO — reemplaza la del spec original)**

El spec original usaba `matched / len(keywords)`: castiga artículos con muchos keywords
(un artículo de 8 keywords con 2 matches da 0.25) y no mide la fuerza del match. **Se reemplaza** por:

```
weighted_matched = 2*len(matched_strong) + 1*len(matched_weak)
weighted_total   = 2*len(strong_keywords) + 1*len(keywords)
coverage     = weighted_matched / weighted_total                 # 0..1
evidence     = min(1.0, weighted_matched / 3.0)                  # 0..1 (satura en 3)
specificity = 1.0 if matched_strong else 0.6                     # 0.6..1.0
service_bonus = 0.05 if article.service == ticket.service else 0.0
confidence = round(min(1.0, 0.50*coverage + 0.30*evidence + 0.20*specificity + service_bonus), 2)
```

**Umbrales (contrato; van también en `policies/risk_matrix.json`):**

| confidence | Interpretación | Efecto |
|---|---|---|
| `>= 0.70` | alta | habilita auto-ejecución si el riesgo lo permite |
| `0.45 – 0.69` | media | **nunca** auto-ejecuta ⇒ `require_approval` (o `escalate` si hay regla) |
| `< 0.45` | baja / sin match | categoría `unknown`, acción `human_review`, `require_approval` rol `support_lead`, reason "low classification confidence" |

`score()` devuelve el desglose (esto es lo que se muestra en README y demo — la confianza es explicable):
`{"confidence","coverage","evidence","specificity","service_bonus","weighted_matched","weighted_total"}`.

**3.3.4 Salida de `classify(ticket)`**
`{"ticket_id","requester","service","normalized_text","intent","category","priority","impact",
"urgency","confidence","kb_id","matched_keywords","matched_strong_keywords","score_breakdown",
"suggested_action","suggested_resolution","base_risk","entities","alternatives"}` donde
`entities = {"user","email","app","resource","mentioned_users"}` y
`alternatives = [{"kb_id","category","confidence"}]` (2º y 3º match, para explicabilidad).

**3.3.5 Prioridad: matriz impacto × urgencia** (en `policies/risk_matrix.json` → `priority_matrix`)

| impact \ urgency | low | normal | high | critical |
|---|---|---|---|---|
| individual | P4 | P3 | P2 | P1 |
| team | P4 | P3 | P2 | P1 |
| department | P3 | P2 | P1 | P1 |
| organization | P2 | P1 | P1 | P1 |

**Prioridad final = la más urgente entre (matriz) y (`priority_hint` de la KB).** Así un hint P4 no
degrada un incidente de impacto departmental. `category_overrides` ajusta por categoría
(obligatorio: `security` → `P1`, riesgo `critical`, acción `escalate_security_team`).

Reglas de **impacto**: `organization` si el texto tiene scope global (`toda la empresa`,
`todos los usuarios`, `sitio completo`); `department` si tiene scope de área (`todo el piso`,
`todo el departamento`, `todo el equipo`); `team` si el usuario es VIP, o hay ≥3 usuarios
mencionados, o el 2º ticket del mismo `service` en el lote; `individual` por defecto.

Reglas de **urgencia**: `critical` (`urgente`, `crítico`, `bloquea la producción`, `caída total`);
`high` (`no puedo trabajar`, `me bloquea`, `deadline`, `fin de mes`); `low` (`cuando puedas`,
`sin apuro`, `para cuando tengas tiempo`); `normal` por defecto.

**3.3.6 Extracción de entidades (determinista)**
`user` = `ticket["requester"]` (fuente de verdad) + `mentioned_users` = regex
`\b([a-z]+(?:[._][a-z]+)+)\b` sobre el texto normalizado (el filtro contra `users.json` lo hace
`agent.py`). `email` = regex `[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}` sobre el texto original.
`app` = `ticket["service"]` si está en la lista conocida (`SAP Fiori`, `Shared Drive`, `Network`,
`Software`, `Security`, `VPN`, `Email`, `Hardware`, `Account`), si no el `article["service"]`.
`resource` = `article["resource"]` o match de `article["resource_patterns"]` contra el texto.

### 3.4 `services/policy.py` (dueño `[A]`)

```python
class PolicyEngine:
    def __init__(self, risk_matrix: dict, access_policies: dict, escalation_rules: List[dict]) -> None
    def risk_for_action(self, action: str, base_risk: str) -> str
    def evaluate(self, triage: dict, user: dict, request: Optional[dict] = None) -> dict
    def matched_escalations(self, ctx: dict) -> List[dict]
    def decide(self, risk: str, confidence: float, user: dict, ctx: dict) -> dict
```

**3.4.0 Contexto (`ctx`) que se arma antes de evaluar** — `evaluate()` lo construye a partir de
`triage` + `user` + `request` (el `request` es la política del recurso solicitado, si el ticket
pide un recurso; si no, `resource` es `None` y `resource.sensitive` es `False`):
`{"priority","category","intent","impact","urgency","risk","base_risk","confidence","matched_count",
"action","effective_action","service","kb_id","resource","resource.sensitive","user.vip",
"user.active","user.locked","user.mfa_enrolled","user.identity_verified"}`.

**3.4.1 Precedencia del riesgo (CONTRATO)**
(1) `risk_matrix.action_risk[action]` — máxima prioridad; (2) `risk_matrix.category_overrides[category].risk`;
(3) `access_policies[resource].risk`; (4) `kb.base_risk`; (5) default `medium`.
El riesgo **nunca baja** por contexto (una acción `high` no degrada a `low`); el contexto sólo lo **sube**.

**3.4.2 Factores de suba (`risk_factors` en `risk_matrix.json`)** — `+1 nivel` = low→medium→high→critical, satura en critical. Cada factor aplicado **debe** aparecer en `reasons` y `rule_ids`.

| id | Condición | Motivo |
|---|---|---|
| `RISK-001` | `user.vip == true` | solicitante VIP ⇒ revisión humana |
| `RISK-002` | `user.active == false` | solicitante inactivo ⇒ revisar antes de tocar identidad |
| `RISK-003` | recurso con `"sensitive": true` | recurso sensible |
| `RISK-004` | `confidence < 0.45` | confianza de clasificación baja |
| `RISK-005` | `user.mfa_enrolled == false` | sin MFA ⇒ acción de identidad sin verificar |
| `RISK-006` | `action in [grant_role, grant_folder_access, disable_user]` | mínimo privilegio con impacto alto |

**3.4.3 Matriz de decisión (CONTRATO — reemplaza el `requires_approval` del spec original)**

El spec original devolvía `True` para medium/high/critical ⇒ no quedaba **ningún** camino
automático salvo riesgo bajo, y derivaba el riesgo de la categoría en vez de la acción. Regla fina:

```
1. Si alguna regla de escalamiento matchea con efecto "escalate"  -> decision = "escalate"
2. Else si riesgo efectivo == "critical"                          -> decision = "escalate"
3. Else si riesgo efectivo == "high"                              -> decision = "require_approval"
4. Else si riesgo efectivo == "medium"                            -> decision = "require_approval"
5. Else (riesgo "low"):
     5a. si confidence >= 0.70 y no hay flags (vip / user inactivo /
         confidence baja / recurso sensible / sin MFA)            -> decision = "auto_execute"
     5b. en cualquier otro caso                                   -> decision = "require_approval"
```

**Efecto práctico:** el camino automático existe, pero **sólo** para riesgo `low` con confianza
alta y sin flags. Riesgo `medium` **siempre** requiere aprobación de `support_lead` (no de
security — el spec original asignaba "security" a todo lo que no era high: bug corregido).
`critical` **nunca** se auto-ejecuta y escala a la cola `security`.
`requires_approval` (booleano) queda como campo **derivado**: `True` si `decision != "auto_execute"`.
La fuente de verdad es `decision`.

**3.4.4 Salida de `evaluate()`**
`{"decision","requires_approval","risk","base_risk","risk_factors","effective_action",
"approver_role","escalation_queue","confidence","timeout_minutes","reasons","rule_ids","policy_version"}`.
`reasons` es **obligatorio y no vacío** — es lo que ve el aprobador y lo que va al log.
`approver_role` es `None` si `auto_execute`. Ejemplo:
`decision="require_approval"`, `risk="high"`, `approver_role="manager"`,
`reasons=["Acción grant_folder_access (riesgo base medium) elevada a high por RISK-003 (recurso sensible)",
"La política de riesgo high exige aprobación del manager"]`, `rule_ids=["RISK-003","RISK-MATRIX-HIGH"]`.

**3.4.5 Motor de reglas de escalamiento (schema estructurado — NO eval de strings)**

`policies/escalation_rules.json` es una **lista** de reglas:
`{"id","name","priority","when","effect":{"decision":"escalate|require_approval","queue",
"approver_role","reason"},"reasons":[...]}`. `when` acepta
`{"all":[...]} | {"any":[...]} | {"not":{...}} | {"field","op","value"}`.
**Campos consultables (el `ctx` que recibe el motor):** `priority`, `category`, `intent`, `impact`,
`urgency`, `risk`, `base_risk`, `confidence`, `matched_count`, `action`, `effective_action`,
`service`, `kb_id`, `resource`, `resource.sensitive`, `user.vip`, `user.active`, `user.locked`,
`user.mfa_enrolled`, `user.identity_verified`.
**Operadores permitidos:** `eq`, `ne`, `in`, `not_in`, `gt`, `gte`, `lt`, `lte`.
**Prohibido** `eval()`/`exec()` o parsear strings de condición. Si una regla no se puede expresar
con este schema, **no se implementa**.

Reglas obligatorias (ids fijos):

| id | when | effect |
|---|---|---|
| `ESC-001` | `priority == P1` | escalate → `on-call`, approver `on_call` |
| `ESC-002` | `category == security` | escalate → `security`, approver `security` |
| `ESC-003` | `confidence < 0.70` | require_approval, approver `support_lead` |
| `ESC-004` | `user.vip == true` | require_approval, approver `support_lead` |
| `ESC-005` | `risk in [high, critical]` | require_approval (no cambia la cola) |
| `ESC-006` | `category == network and impact in [department, organization]` | escalate → `network-team` |
| `ESC-007` | `category == vpn and confidence < 0.50` | require_approval, approver `support_lead` |

Si varias reglas matchean, aplica la de **mayor `priority`** (número mayor = más fuerte); empate ⇒
gana `escalate` sobre `require_approval`. Todas las matcheadas se listan en `rule_ids`.

### 3.5 `services/approval.py` (dueño `[A]`)

```python
APPROVAL_STATUSES = ("pending","approved","rejected","expired","superseded","executed","failed")

class ApprovalQueue:
    def __init__(self, path: str, policy: dict) -> None
    def load(self) -> List[dict]
    def save(self) -> None
    def create(self, request: dict) -> dict
    def get(self, approval_id: str) -> Optional[dict]
    def pending(self, ticket_id: Optional[str] = None) -> List[dict]
    def approve(self, approval_id: str, approver: str, note: str = "") -> dict
    def reject(self, approval_id: str, approver: str, note: str = "") -> dict
    def expire_due(self, now: Optional[str] = None) -> List[dict]
    def supersede(self, approval_id: str, reason: str) -> dict
    def stats(self) -> dict

def apply_approved_action(approval: dict, executor: Callable[[dict], dict]) -> dict
```

**Reglas duras:**
1. `create()` **nunca** ejecuta nada; crea el registro con `status="pending"`.
2. `apply_approved_action()` **sólo** invoca `executor` si `approval["status"] == "approved"`.
   `pending` ⇒ `{"executed": False, "reason": "approval_required"}` y **no** llama al executor;
   `rejected`/`expired`/`superseded` ⇒ `{"executed": False, "reason": <status>}`.
3. **Jamás** se ejecuta una acción `high`/`critical` sin aprobación: guarda extra — si
   `approval["risk"] in ("high","critical")` y `approval.get("decided_by")` está vacío ⇒
   `{"executed": False, "reason": "no_human_decision"}`.
4. `approval_id` **determinista e idempotente**: `APR-{ticket_id}-{sha256(f"{ticket_id}|{action}|{resource}")[:8].upper()}`
   (el spec original usaba `APR-{ticket_id}` y colisionaba con aprobaciones múltiples del mismo ticket).
5. `idempotency_key = sha256(f"{ticket_id}|{action}|{resource}|{target}")[:16]`. Si ya existe una
   `pending` con la misma key ⇒ `create()` la devuelve sin duplicar, con `"duplicate": True`.
6. `supersede()`: al crear una aprobación nueva para el mismo ticket **y misma acción**, la
   `pending` anterior pasa a `superseded` con `superseded_by = <nuevo id>`.
7. `expire_due()`: marca `expired` las `pending` con `expires_at < now`;
   `expires_at = created_at + timeout_minutes` (support_lead 240, manager 480, security 60, on_call 60).
8. `approve()`/`reject()` exigen `approver` no vacío y **verifican el rol**: debe coincidir con
   `approval["approver_role"]`; si no ⇒ `{"status":"error","error":"approver_role_mismatch","expected_role":...}`.
   `decision_note` es obligatorio en `reject()`.
9. Shape del registro (persiste en `outputs/approvals.json`, array JSON indent 2):
`{"approval_id","idempotency_key","run_id","ticket_id","requester","action","resource","params",
"risk","decision","approver_role","approver","status","reason","reasons","confidence","created_at",
"expires_at","decided_at","decided_by","decision_note","superseded_by","action_id","duplicate"}`.

### 3.6 `services/monitoring.py` (dueño `[A]`)

```python
class AuditLogger:
    def __init__(self, path: str, run_id: str) -> None
    def log(self, event: str, ticket_id: Optional[str] = None, **fields) -> dict
    def close(self) -> None

class MetricsCollector:
    def __init__(self) -> None
    def record_triage(self, ticket_id, category, priority, confidence, latency_ms, risk, decision) -> None
    def record_action(self, ticket_id, action, status, latency_ms, estimated_minutes, risk) -> None
    def record_approval(self, ticket_id, outcome, role) -> None
    def record_escalation(self, ticket_id, queue, reason) -> None
    def record_error(self, ticket_id, subsystem, message) -> None
    def snapshot(self) -> dict
    def write_markdown(self, path: str) -> None
    def write_json(self, path: str) -> None
```

**Eventos de auditoría (enum fijo):** `run_started`, `ticket_received`, `triage_completed`,
`identity_checked`, `policy_decided`, `action_proposed`, `approval_created`, `approval_resolved`,
`action_executed`, `action_failed`, `escalation_raised`, `itsm_writeback`, `integration_error`,
`run_completed`. Cada línea de `outputs/audit.log` (JSONL) tiene:
`{"ts","run_id","event","ticket_id","actor"}` (`actor` = `agent`|`human`|`system`) + campos del
evento (categoría, prioridad, confidence, risk, decision, approver_role, action, reasons, rule_ids, latency_ms).

**Métricas (claves exactas del snapshot):**

| clave | def. |
|---|---|
| `tickets_processed` | tickets leídos |
| `auto_resolved` | `decision == auto_execute` y acción OK |
| `requires_approval` | `decision == require_approval` |
| `escalated` | `decision == escalate` |
| `unknown_category` | `category == unknown` |
| `human_overrides` | aprobaciones rechazadas o modificadas por humano |
| `approvals_approved` / `approvals_rejected` / `approvals_expired` / `approvals_superseded` | contadores de aprobación |
| `average_confidence` | media sobre tickets con categoría conocida |
| `average_confidence_all` | media sobre todos (incluye `unknown`) |
| `average_triage_latency_ms` / `average_resolution_latency_ms` | latencias medias |
| `mttr_minutes` | media de `estimated_minutes` de los tickets auto-resueltos (proxy de MTTR) |
| `deflection_rate` | `auto_resolved / tickets_processed` |
| `fcr_rate` | `auto_resolved / tickets_processed` (en este pipeline FCR == deflexión; se documenta) |
| `estimated_minutes_saved` | `sum(max(0, estimated_minutes - 1))` sobre auto-resueltos |
| `action_failure_count` / `integration_error_count` | fallos |
| `escalation_reasons` | `{cola: count}` |
| `category_distribution` / `priority_distribution` | `{x: count}` |
| `cost_estimate_usd` | `0.0` siempre (no hay LLM); con LLM: `(tokens_in/1e6*in_price + tokens_out/1e6*out_price)` |
| `run_id` | id de corrida |

`write_markdown(path)` → `outputs/metrics.md` (tabla, formato del spec original **más** las métricas
nuevas); `write_json(path)` → `outputs/metrics.json`.

### 3.7 `agent.py` — orquestador (dueño `[A]`)

```python
def build_adapters(itsm_kind=None, identity_kind=None, app_kind=None) -> dict
    # -> {"itsm": ITSMAdapter, "identity": IdentityAdapter, "apps": AppAdapter}
    # Delega en adapters.build_*_adapter. Import LAZY dentro de la función para que
    # `import agent` nunca falle si falta el paquete adapters.

def run_pipeline(tickets: List[dict], adapters: dict, *, dry_run: bool = True,
                 verbose: bool = False, approve_all: bool = False,
                 run_id: Optional[str] = None) -> dict
def main(argv: Optional[List[str]] = None) -> int
```

**Flujo por ticket (10 pasos, cada uno emite evento de auditoría):**
1. `ticket_received` — normaliza el ticket, detecta campos faltantes (requester/description).
2. `triage_completed` — `TriageService.classify()`.
3. `identity_checked` — `identity.get_user(requester)`; inexistente o inactivo ⇒ se registra y el
   ticket va a `human_review` con reason "requester not found / inactive" (nunca acción automática).
4. `policy_decided` — `PolicyEngine.evaluate(triage, user)`.
5. `action_proposed` — se arma el payload (`action`, `target`, `resource`, `params`).
6. `decision == "require_approval"` ⇒ `approval_created` (crea registro, **no ejecuta**).
7. `decision == "escalate"` ⇒ `escalation_raised` + `approval_created` (rol de escalamiento); la
   acción **no** se ejecuta en el mismo paso.
8. `decision == "auto_execute"` ⇒ `action_executed`/`action_failed` vía `apps.execute_action()` +
   identity adapter si la acción lo requiere. **Nunca** se ejecuta si `risk in ("high","critical")`.
9. `itsm_writeback` — `add_comment` + `set_priority` + `assign`; `resolve` sólo si la acción fue
   auto-ejecutada con éxito. Todo respeta `write_enabled()`.
10. `run_completed` — escribe `outputs/triaged_tickets.json`, `outputs/triaged_tickets.csv`,
    `outputs/actions.json`, `outputs/approvals.json`, `outputs/metrics.json`, `outputs/metrics.md`.

**`approve_all=True`** — tras crear las aprobaciones del lote, el agente las aprueba con
`approver="<rol>_auto"` y ejecuta la acción. El log marca `actor:"human"` y `simulated_approval:true`
para no mentir. Sólo para demo/test; el flag queda registrado en `run_started`.

**CLI exacta:**
`python3 agent.py [--input PATH] [--adapter {mock,servicenow,jira}]
[--identity-adapter {mock,entra,okta}] [--app-adapter {mock,sap_fiori,vpn,shared_drive}]
[--dry-run] [--verbose] [--demo] [--approve-all] [--outputs-dir DIR] [--run-id ID]`
- `--input` default `data/tickets.csv`. `--dry-run` ⇒ sin escrituras. `--demo` ⇒ equivale a
  `--input data/tickets.csv --dry-run --verbose` **más** narración por ticket y tabla final de
  métricas; implica `dry_run=True`. `--verbose` ⇒ imprime por ticket: categoría, prioridad,
  confidence + desglose, KB match, riesgo, decisión, razón, acción y estado.
- Exit codes: `0` run OK (incluso con errores por ticket: se aíslan y el lote continúa); `1` error
  fatal de config (credenciales faltantes, input inexistente); `2` eval bajo umbrales (sólo eval).

**Idempotencia del run:** `run_id = RUN-{YYYYMMDDTHHMMSSZ}-{hash4}` (o el de `--run-id`).
`action_id` determinista por `ticket_id + action + target`: si ya existe una acción con el mismo
`action_id` en `outputs/actions.json`, se marca `status:"duplicate_skipped"` (no se duplica el efecto).

### 3.8 `eval/run_eval.py` (dueño `[B]`)

```python
def load_labeled(path: str = "eval/labeled_tickets.json") -> List[dict]
def run_evaluation(labeled: List[dict], adapters: dict, **kwargs) -> dict
def write_reports(report: dict, md_path: str, json_path: str) -> None
def main(argv: Optional[List[str]] = None) -> int
```

Pipeline del eval: **sólo** triage + identity + policy (sin ejecutar acciones, sin write-back,
sin red). Usa `MockIdentity` y el `TriageService` + `PolicyEngine` reales. Determinista.

**Métricas del eval:** `category_accuracy`, `priority_accuracy`, `action_accuracy` (recall de la
acción correcta), `decision_accuracy`, `auto_resolution_rate`, `escalation_rate`, `approval_rate`,
`avg_confidence`, `category_confusion` (`{real: {predicted: n}}`), `priority_confusion`,
`failures` (`[{ticket_id, expected, actual, description}]`).

**Umbrales de aceptación (hard gate; exit code 1 si no se cumplen):**
`category_accuracy >= 0.85` · `priority_accuracy >= 0.80` · `action_accuracy >= 0.75` ·
`decision_accuracy >= 0.80`.

**Salidas:** `outputs/eval_report.md` (tabla legible + confusion matrices + failures) y
`outputs/eval_report.json`.
**CLI:** `--input`, `--out-md`, `--out-json`, `--min-category-accuracy`, `--min-priority-accuracy`,
`--min-action-accuracy`, `--min-decision-accuracy`, `--json-only`, `--verbose`.

---

## 4. Esquemas de datos exactos

JSON con `indent=2`, `ensure_ascii=False`, UTF-8, newline final.

**4.1 `data/tickets.csv` (`[A]`)** — header **exacto**: `ticket_id,requester,description,service,created_at`.
`ticket_id` `TCK-001`..`TCK-012`; `requester` = username de `data/users.json`; `description` texto
natural ES/EN; `service` ∈ {`SAP Fiori`,`Shared Drive`,`Network`,`Software`,`Security`,`VPN`,
`Email`,`Hardware`,`Account`}; `created_at` ISO-8601 UTC con `Z`.
**Mínimo 12 tickets**: los 5 del spec original (`TCK-001..005`) + 7 que cubran los bordes obligatorios:
VIP, usuario **inactivo**, categoría `unknown` (texto sin señal), confianza baja (texto ambiguo),
`security`, `vpn`, `hardware`, `software`.
**La columna `vip` del spec original se elimina**: la verdad del VIP es `data/users.json` (§8 #12).

**4.2 `data/users.json` (`[A]`)** — objeto keyed por username; cada value:
`{"active","locked","locked_by_admin","manager","groups","vip","mfa_enrolled","identity_verified",
"password_last_set_days","email","display_name","department"}`.
Mínimo **8 usuarios** cubriendo: 1 VIP, 1 inactivo (`active:false`), 1 bloqueado (`locked:true`),
1 con `locked_by_admin:true`, 1 sin MFA, 1 con `password_last_set_days > 90`, 2 managers.
`identity_verified` simula la verificación out-of-band (en prod: OTP por SMS/email); el agente
**nunca** confía en el texto del ticket para esto.

**4.3 `data/groups.json` (`[A]`)** — `{group: {"display_name","members":[],"sensitive","owner","requires_approval"}}`.
Mínimo 6 grupos, ≥2 con `"sensitive": true`. Es la fuente de `allowed_groups` de `access_policies.json`.

**4.4 `data/kb.json` (`[A]`)** — ≥12 artículos, **IDs fijos**. Cada artículo:
```json
{"id":"KB001","title":"...","category":"account_locked","intent":"trouble","service":"SAP Fiori",
 "priority_hint":"P3","action":"password_reset_link","base_risk":"low",
 "keywords":["sap","fiori","login"],"strong_keywords":["bloqueado","locked out"],
 "resolution":"...","resolution_en":"...","runbook":["...","...","..."],
 "estimated_minutes":12,"resource":null,"resource_patterns":[]}
```
A puede añadir `title`, `resolution`, `runbook`, `estimated_minutes`, `resource`, `resource_patterns`;
**no puede cambiar** `id`, `category`, `intent`, `priority_hint`, `action`, `base_risk` ni los keywords.

| id | category | intent | service | P | action | base_risk | strong_keywords | keywords |
|---|---|---|---|---|---|---|---|---|
| KB001 | account_locked | trouble | SAP Fiori | P3 | password_reset_link | low | bloqueado, cuenta bloqueada, locked, locked out, bloqueo de cuenta | sap, fiori, login, entrar, acceso, credencial, contrasena, password |
| KB002 | password | trouble | Account | P3 | password_reset_link | low | olvide, olvido, olvidada, forgot, vencida, caducada, expired, restablecer | contrasena, password, login, acceso, sap |
| KB003 | access | request | Shared Drive | P3 | request_access_approval | high | solicitar acceso, permiso a, acceso a la carpeta, request access | carpeta, folder, permisos, permiso, finanzas, finance, sharepoint, drive |
| KB004 | access | request | Shared Drive | P3 | grant_folder_access | high | carpeta compartida, shared drive, onedrive, google drive, permiso de escritura | carpeta, permiso, escritura, lectura, drive, unidad |
| KB005 | access | trouble | SAP Fiori | P3 | grant_role | high | sap, fiori, rol, role, transaccion, t-code, sapgui, abap | acceso, error, permiso, transaction |
| KB006 | network | trouble | Network | P1 | escalate_network_team | medium | sin internet, no hay internet, caido, caida, outage, dns, sin conexion, red caida | internet, red, network, wifi, piso, conexion, problema, planta |
| KB007 | vpn | trouble | VPN | P2 | restart_service | medium | vpn, anyconnect, noclient, tunel, tunnel, no conecta | conexion, remoto, remote, acceso, oficina |
| KB008 | software | request | Software | P4 | assign_software_package | medium | instalar, instalacion, power bi, licencia, license, no tiene instalado | software, aplicacion, programa, excel, teams, office |
| KB009 | hardware | trouble | Hardware | P4 | send_kb_article | low | no enciende, pantalla negra, teclado roto, bateria, no prende | laptop, notebook, pc, monitor, impresora, printer, teclado, mouse, pantalla, telefono |
| KB010 | email | trouble | Email | P3 | send_kb_article | low | outlook, thunderbird, firma, regla de correo, adjuntar archivo, adjuntos no | correo, email, mail, bandeja, adjunto, calendario, reunion, invite |
| KB011 | security | trouble | Security | P1 | escalate_security_team | critical | phishing, malware, virus, fraude, scam, robo de contrasena, hackeo, correo sospechoso | seguridad, correo, email, sospechoso, urgente, hack |
| KB012 | access | trouble | Account | P3 | disable_user | critical | cuenta inactiva, offboarding, desactivar, disable, baja de usuario | inactivo, desactivar, acceso, usuario, cuenta, no usa |

Distribución de categorías en la KB: `account_locked` 1 (KB001) · `password` 1 (KB002) ·
`access` 4 (KB003, KB004 request + KB005, KB012 trouble) · `network` 1 · `vpn` 1 ·
`software` 1 · `hardware` 1 · `email` 1 · `security` 1. Coincide con la composición del eval
set (§4.9): los tickets etiquetados deben cubrir las 9 categorías reales + `unknown`.

Los overlaps deliberados (KB001↔KB002, KB003↔KB004, KB005↔KB001) son intencionales: ejercitan
el desempate por `strong_keywords` (§3.3.2). El eval set **no** debe contener texto que matchee
por igual a dos artículos.

**4.5 `policies/access_policies.json` (`[A]`)** — keyed por recurso:
`{"description","risk","action","approver_role","requires_approval","sensitive","allowed_groups","allowed_roles"}`.
Mínimo 6 recursos: `sap-fiori-basic` (low), `finance-folder` (high, sensitive), `shared-drive-team`
(medium), `vpn-access` (low), `admin-access` (critical, sensitive), `software-license-office` (medium).
Cada recurso con `sensitive` y `requires_approval` **consistentes** con su riesgo (low ⇒ `requires_approval: false`).

**4.6 `policies/risk_matrix.json` (`[A]`)** — un archivo, 5 secciones:

```json
{"version":"1.0",
 "thresholds":{"auto_execute_min_confidence":0.70,"review_min_confidence":0.45},
 "levels":{
   "low":{"decision":"auto_execute","auto_execute_allowed":true,"approver_role":null,"timeout_minutes":60},
   "medium":{"decision":"require_approval","auto_execute_allowed":false,"approver_role":"support_lead","timeout_minutes":240},
   "high":{"decision":"require_approval","auto_execute_allowed":false,"approver_role":"manager","timeout_minutes":480},
   "critical":{"decision":"escalate","auto_execute_allowed":false,"approver_role":"security","timeout_minutes":60}},
 "action_risk":{"send_kb_article":"low","password_reset_link":"low","unlock_account":"medium",
   "grant_role":"high","grant_folder_access":"high","request_access_approval":"high",
   "assign_software_package":"medium","restart_service":"medium","escalate_network_team":"medium",
   "escalate_security_team":"critical","disable_user":"critical","human_review":"medium","no_action":"low"},
 "category_overrides":{"security":{"priority":"P1","risk":"critical","action":"escalate_security_team"}},
 "priority_matrix":{
   "individual":{"low":"P4","normal":"P3","high":"P2","critical":"P1"},
   "team":{"low":"P4","normal":"P3","high":"P2","critical":"P1"},
   "department":{"low":"P3","normal":"P2","high":"P1","critical":"P1"},
   "organization":{"low":"P2","normal":"P1","high":"P1","critical":"P1"}},
 "risk_factors":[
   {"id":"RISK-001","when":{"field":"user.vip","op":"eq","value":true},"delta":1,"reason":"El solicitante es un usuario VIP: requiere revisión humana"},
   {"id":"RISK-002","when":{"field":"user.active","op":"eq","value":false},"delta":1,"reason":"El solicitante no está activo: revisar antes de tocar la identidad"},
   {"id":"RISK-003","when":{"field":"resource.sensitive","op":"eq","value":true},"delta":1,"reason":"El recurso solicitado es sensible"},
   {"id":"RISK-004","when":{"field":"confidence","op":"lt","value":0.45},"delta":1,"reason":"Confianza de clasificación baja"},
   {"id":"RISK-005","when":{"field":"user.mfa_enrolled","op":"eq","value":false},"delta":1,"reason":"El solicitante no tiene MFA registrado"},
   {"id":"RISK-006","when":{"field":"action","op":"in","value":["grant_role","grant_folder_access","disable_user"]},"delta":1,"reason":"Acción de mínimo privilegio con impacto alto"}]}
```

El motor de `risk_factors` usa **el mismo schema de condiciones** que las reglas de escalamiento
(§3.4.5): un solo DSL para todo el motor de reglas.

**4.7 `policies/escalation_rules.json` (`[A]`)** — lista con los ids fijos `ESC-001..ESC-007` de
§3.4.5, con el schema exacto documentado ahí. Cada regla: `id`, `name`, `priority` (10, 20, 30…),
`when`, `effect` (`decision`, `queue`, `approver_role`, `reason`), `reasons` (textos ES).

**4.8 `outputs/*` (generados en runtime)**

| Archivo | Formato | Contenido |
|---|---|---|
| `outputs/triaged_tickets.json` | Array JSON | un objeto por ticket con todas las claves de §3.3.4 + `decision`, `risk`, `status`, `run_id` |
| `outputs/triaged_tickets.csv` | CSV | columnas fijas: `ticket_id,requester,service,category,intent,priority,impact,urgency,confidence,risk,decision,action,status,kb_id,approval_id` |
| `outputs/actions.json` | Array JSON | `{action_id,ticket_id,action,target,resource,risk,status,dry_run,created_at,executed_at,result}` |
| `outputs/approvals.json` | Array JSON | shape §3.5.9 |
| `outputs/audit.log` | JSONL | un evento por línea |
| `outputs/metrics.json` | Objeto JSON | snapshot de métricas |
| `outputs/metrics.md` | Markdown | reporte legible (tabla) |
| `outputs/eval_report.md` / `.json` | MD / JSON | reporte del eval harness |

**4.9 `eval/labeled_tickets.json` (`[B]`)**
```json
{"version":"1.0","generated_at":"2026-11-12T00:00:00Z","tickets":[
 {"ticket_id":"EVAL-001","description":"No puedo loguearme a Fiori, la cuenta está bloqueada",
  "requester":"juan.perez","service":"SAP Fiori",
  "expected":{"category":"account_locked","intent":"trouble","priority":"P3","risk":"low",
              "action":"password_reset_link","decision":"auto_execute"},
  "notes":"Coincide con KB001"}]}
```
Composición mínima (**27 tickets**): account_locked 3 · password 3 · access 4 (2 request + 2 trouble)
· network 3 · vpn 2 · software 3 · hardware 2 · email 2 · security 3 · unknown 2.
Los textos son **paráfrasis**, no copias literales de `data/tickets.csv` (evita overfitting).
`expected.risk`/`expected.decision` deben ser consistentes con §3.4.1–3.4.3 y `risk_matrix.json`.
Incluir ≥1 caso `require_approval` por confianza baja y ≥1 `unknown`.

**4.10 `eval/metrics.py` (`[B]`)** — funciones puras sin I/O:
`accuracy(pairs) -> float`, `confusion_matrix(expected: List[str], actual: List[str]) -> Dict[str, Dict[str,int]]`,
`summarize(results: List[dict]) -> dict`.

---

## 5. Contrato del eval harness

1. El eval **no** toca la red, **no** ejecuta acciones, **no** escribe en ITSM.
2. Determinista: mismos archivos ⇒ mismo reporte (salvo el timestamp del reporte).
3. Comparación por **igualdad exacta de string** en `category`, `intent`, `priority`, `action`, `decision`.
4. `outputs/eval_report.md` incluye: tabla de métricas globales, confusion matrix de categoría,
   confusion matrix de prioridad y la lista de `failures` con el detalle esperado vs obtenido
   (es la sección "continuous improvement" del README).
5. **Gate**: si una métrica queda bajo el umbral, imprime
   `EVAL FAILED: <métrica> <valor> < <umbral>` y devuelve exit code **1**. `make eval` propaga ese código.

---

## 6. Criterios de aceptación (Definition of Done)

Todos los comandos desde el repo root; todos deben salir limpio (exit 0).

### 6.1 Checks ejecutables

```bash
# 1. Sintaxis de todo el código
python3 -m compileall -q agent.py adapters services eval tests

# 2. Suite completa, CERO installs
python3 -m unittest discover -s tests -t . -v

# 3. Demo (dry-run, narrado)
python3 agent.py --demo

# 4. Pipeline dry-run verbose
python3 agent.py --input data/tickets.csv --dry-run --verbose

# 5. Pipeline con write-back al ITSM mock
python3 agent.py --input data/tickets.csv --verbose

# 6. Eval harness (PASS + exit 0)
python3 eval/run_eval.py

# 7. Artefactos
test -f outputs/triaged_tickets.json && test -f outputs/triaged_tickets.csv \
 && test -f outputs/actions.json && test -f outputs/approvals.json \
 && test -f outputs/audit.log && test -f outputs/metrics.json \
 && test -f outputs/metrics.md && test -f outputs/eval_report.md \
 && test -f outputs/eval_report.json && echo "outputs OK"

# 8. NINGÚN import de requests a nivel de módulo en adapters/
! grep -rnE "^[[:space:]]*(import requests|from requests)" adapters/ && echo "LAZY IMPORT OK"

# 9. Python 3.9: sin match statements ni unions en anotaciones
! grep -rnE "^[[:space:]]*match .+:" --include="*.py" . && echo "NO MATCH OK"
! grep -rnE ":[[:space:]]*[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\|[[:space:]]*[A-Za-z_]" --include="*.py" . && echo "NO PEP604 OK"

# 10. Sin utcnow() deprecado
! grep -rn "utcnow()" --include="*.py" . && echo "NO UTCNOW OK"

# 11. Makefile
make help && make test && make eval
```

### 6.2 Archivos que deben existir

```
README.md  Makefile  requirements.txt  .env.example  .gitignore  agent.py
adapters/{__init__,base,itsm,identity,apps}.py
services/{__init__,common,triage,policy,approval,monitoring}.py
policies/{access_policies,risk_matrix,escalation_rules}.json
data/{tickets.csv,users.json,groups.json,kb.json}
eval/{__init__,run_eval,metrics}.py  eval/labeled_tickets.json
tests/{test_triage,test_policy,test_approval,test_monitoring,test_agent_pipeline,
       test_adapters,test_eval,test_packaging}.py
docs/{SPEC.md,ARCHITECTURE.md,INTERVIEW_PLAYBOOK.md}  outputs/.gitkeep
```

### 6.3 Checklist funcional

- [ ] `python3 agent.py --demo` corre sin `pip install` y produce los 7 artefactos de `outputs/`.
- [ ] El demo muestra ≥1 ticket auto-ejecutado, ≥1 que requiere aprobación y ≥1 escalado.
- [ ] `python3 eval/run_eval.py` pasa los 4 umbrales (§3.8) y escribe `eval_report.md` + `.json`.
- [ ] Ninguna acción `high`/`critical` aparece con `status:"succeeded"` sin `approval_id` aprobado
      previo (verificable en `outputs/actions.json` y `outputs/audit.log`).
- [ ] `outputs/audit.log` tiene ≥10 eventos por ticket.
- [ ] `outputs/metrics.md` contiene todas las claves de §3.6.
- [ ] Los conectores reales se pueden **importar** sin `requests` instalado y sin credenciales
      (sólo al *construir* con `kind="servicenow"` lanzan `AdapterConfigError`).
- [ ] `MockITSM` lee `data/tickets.csv` (no hay tickets hardcodeados).
- [ ] Todos los timestamps son ISO-8601 UTC con sufijo `Z`.
- [ ] No hay `print` de credenciales ni secretos en logs ni en `outputs/`.
- [ ] README contiene el disclaimer literal de mocks (§7.1), diagrama Mermaid, guardrails y la
      sección "de mock a producción".
- [ ] `docs/INTERVIEW_PLAYBOOK.md` contiene los 8 sub-bloques de §7.2.
- [ ] `docs/ARCHITECTURE.md` contiene diagrama Mermaid, decisiones de diseño, guardrails y mock→real.

---

## 7. Entregables de entrevista (dueño `[B]`)

### 7.1 `README.md` — secciones obligatorias

1. Título + una línea ES y EN. 2. **Disclaimer literal** (obligatorio, sin modificar):
   `ITSM and identity integrations are mocked for demo purposes.` /
   `The adapter pattern allows replacing mock connectors with ServiceNow, Jira Service Management, Microsoft Entra ID or Okta APIs.`
   + nota ES: las implementaciones reales existen en `adapters/` y requieren credenciales explícitas;
   el kill-switch `HELPDESK_ALLOW_WRITES` impide escrituras no deseadas.
3. **Arquitectura**: un bloque ```mermaid``` (graph TD) con Ticket → Triage → Identity → Policy
   Engine → (Approval Queue → Human) / (Action Executor) → ITSM Write-back → Audit + Metrics;
   los adapters como subgrafos con las variantes mock/real.
4. **Quickstart**: los comandos literales de §1.4. 5. **Cómo funciona**: flujo de 10 pasos + tabla
   de decisiones de riesgo. 6. **Confidence explicable**: la fórmula de §3.3.3 con un ejemplo
   numérico real de la corrida. 7. **Guardrails**: 8 bullets — whitelist de acciones, ticket =
   input no confiable, verificación de identidad out-of-band, mínimo privilegio, idempotencia,
   kill-switch de escrituras, auditoría JSONL, escalamiento por umbrales.
8. **Métricas y eval**: qué mide, cómo correrlo, umbrales y resultado actual.
9. **De mock a producción**: 5 pasos concretos (config, secretos, colas, observabilidad, evaluación continua).
10. **Estructura del repo** (árbol con archivos clave). 11. **Limitaciones** (honestidad: sin LLM,
    sin UI, sin multi-tenant, mocks). 12. **Licencia MIT + contacto**.

### 7.2 `docs/INTERVIEW_PLAYBOOK.md` — ES con excerpts EN

1. **Pitch de 60 segundos** (EN literal).
2. **Guion de demo de 5 minutos**: tabla con minuto / lo que digo (EN literal) / comando literal /
   qué hay que ver en pantalla. Mínimo 6 pasos: `python3 agent.py --demo` → triage con confidence →
   approval queue → `cat outputs/approvals.json` → `python3 eval/run_eval.py` → `cat outputs/metrics.md`.
   Incluir **qué decir si preguntan por qué no hay LLM**.
3. **Q&A anticipada (10 preguntas) con respuestas en inglés** — las 10 de §5 de la guía de
   preparación + 2 del repo (guardrails, eval). Respuestas de 100–150 palabras.
4. **5 historias STAR** (ES y EN): (1) incidente crítico, (2) proceso repetitivo automatizado,
   (3) automatización con IA, (4) usuario difícil/VIP, (5) algo que salió mal y qué aprendí.
   Situación/Tarea/Acción/Resultado y un número cada una.
5. **Preguntas para el cliente** — las de §8 de la guía + las técnicas del repo.
6. **"How I built this"** en 3 capas (problema, diseño, validación con comandos), en EN.
7. **30/60/90 días** por fases (EN + ES).
8. **Cheat sheet "¿esto es real?"** — 6 objeciones previsibles y cómo responderlas con honestidad.

### 7.3 `docs/ARCHITECTURE.md`

1. Diagrama Mermaid (contenedor + secuencia del flujo de un ticket). 2. Decisiones de diseño
   (ADR-lite, 8 entradas): patrón Adapter, clasificación determinista, reglas estructuradas,
   HITL por nivel de riesgo, JSONL como audit trail, idempotencia por `action_id`, kill-switch de
   escrituras, kill de LLM. 3. **Guardrails** en tabla amenaza → mitigación → dónde se implementa.
4. **De mock a real**: tabla mock → real (ServiceNow/Jira/Entra/Okta) y qué falta para producción
   (secret manager, cola, RBAC del agente, reintentos, circuit breaker, rate limits).
5. **Performance y escalabilidad**: O(n) por ticket, stateless, dónde entra un LLM.
6. **Limitaciones conocidas** (honestidad técnica).

---

## 8. Ambigüedades del spec original resueltas por el director

| # | Ambigüedad | Resolución (obligatoria) |
|---|---|---|
| 1 | `requires_approval()` devuelve `True` para medium/high/critical ⇒ sin camino automático salvo riesgo bajo; el riesgo se derivaba de la categoría, no de la acción | §3.4.3: riesgo **por acción** (`action_risk`), umbrales de confianza explícitos, y sólo `low` + confianza ≥0.70 + sin flags ⇒ `auto_execute`. `requires_approval` queda derivado, no fuente de verdad |
| 2 | `datetime.utcnow()` deprecado (3.12) + `isoformat()+"Z"` con microsegundos | `datetime.now(timezone.utc)`, microsegundos recortados, `+00:00`→`Z`; helper único `utc_now_iso()`; prohibido `utcnow()` (check §6.1 #10) |
| 3 | Confianza = `matched/len(keywords)`: demasiado severa y sin señal de fuerza del match | §3.3.3: `0.50*coverage + 0.30*evidence + 0.20*specificity + service_bonus`, con desglose devuelto. Umbrales 0.70 / 0.45 |
| 4 | `confidence` sin umbral claro en ningún lado | §3.3.3 + `risk_matrix.thresholds` (0.70 auto / 0.45 revisión); `ESC-003` usa 0.70 |
| 5 | `access` y `access_request` se solapan; `account_locked` aparece como KB y como acción | §2.1: una sola categoría `access` + `intent` (`trouble`/`request`); `account_locked` queda como categoría |
| 6 | `create_approval()` asigna `approver = "manager" if risk=="high" else "security"` ⇒ medium cae en "security" (bug) | §3.4.3 + `risk_matrix.levels[*].approver_role`: medium→`support_lead`, high→`manager`, critical→`security` |
| 7 | `approval_id = f"APR-{ticket_id}"` colisiona con aprobaciones múltiples del mismo ticket | §3.5.4: `APR-{ticket_id}-{hash8}` determinista + `idempotency_key` + `supersede` |
| 8 | `escalation_rules.json` usa strings `"priority == 'P1'"` sin parser definido | §3.4.5: schema estructurado (`field`/`op`/`value` + `all`/`any`/`not`), **prohibido** `eval`/`exec` |
| 9 | El código contaba `escalated` dos veces (P1 y security) sin distinguir, y no separaba `require_approval` de `escalate` | §3.4.3: `decision` ∈ 3 valores; `escalated` cuenta sólo `escalate`; `escalation_reasons` desglosa por cola |
| 10 | `MockITSM.get_ticket` devuelve un ticket hardcodeado | §3.2.2: `MockITSM` lee `data/tickets.csv` con estado en memoria |
| 11 | La estructura pide `outputs/triaged_tickets.csv` pero el código escribe `.json` | §4.8: se generan **ambos**; el `.json` es el canónico |
| 12 | El CSV de tickets trae columna `vip` que duplica `users.json` | §4.1: columna eliminada; la verdad es `users.json` |
| 13 | No hay reglas de identidad: los conectores reales no tenían seguridad | §3.1.2: `verified` + MFA + `locked_by_admin` + allow-list de recursos + `VerificationRequired` |
| 14 | No hay idempotencia ni control de reintentos en los conectores | §3.1.1: timeouts, backoff, `action_id` determinista, `write_enabled()` |
| 15 | No hay timeout ni expiración de aprobaciones | §3.5.7: `expires_at` + `expire_due()` + `timeout_minutes` por rol |
| 16 | No hay `impact`/`urgency` (sólo `priority_hint` del KB) | §3.3.5: matriz impacto×urgencia + hint, se toma la más urgente |
| 17 | No hay extracción de entidades | §3.3.6: user, email, app, resource, mentioned_users |
| 18 | Riesgo de prompt injection: nada decía que el texto del ticket fuera no confiable | §7.1 guardrails + §3.3: el texto **sólo** alimenta matching y extracción; ninguna acción puede derivarse de una instrucción contenida en el texto; `agent.py` §3.7 no interpola campos del ticket en comandos ni políticas |
| 19 | `requirements.txt` no existía; el demo dependía de instalar cosas | §1.3: núcleo sólo stdlib; `requirements.txt` **sin dependencias obligatorias** (opcionales comentadas) |
| 20 | El README del spec original era ES puro para una entrevista en inglés | §7.1: README ES con títulos bilingües + disclaimer EN literal; playbook con excerpts EN |
| 21 | El spec listaba `data/groups.json` pero el código no lo usaba | §4.3: `groups.json` es la fuente de `allowed_groups` de `access_policies.json` |
| 22 | No se definía qué pasa con usuario inexistente o inactivo | §3.7 paso 3: inexistente/inactivo ⇒ `human_review` con reason explícita, nunca acción automática |
| 23 | Métrica "costo por ticket" sin LLM | §3.6: `cost_estimate_usd = 0.0` + fórmula documentada para cuando haya LLM |
| 24 | "Estimated time saved: 45 minutes" hardcodeado en el ejemplo | §3.6: `estimated_minutes_saved` se calcula desde `estimated_minutes` de la KB menos 1 min de manejo del agente |

---

## 9. Mapping de archivos por implementador

**Regla dura:** cada implementador crea **sólo** los archivos de su lista. Si necesita uno que no
está en su lista, **no lo crea**: avisa al director y espera. Está prohibido modificar archivos del
otro implementador (ni "parchar un typo"): si detectás un bug en su código, lo reportás.

### 9.1 Implementador A — Núcleo funcional (runtime)

```
agent.py
services/__init__.py  services/common.py  services/triage.py  services/policy.py
services/approval.py  services/monitoring.py
policies/access_policies.json  policies/risk_matrix.json  policies/escalation_rules.json
data/tickets.csv  data/users.json  data/groups.json  data/kb.json
tests/test_triage.py  tests/test_policy.py  tests/test_approval.py
tests/test_monitoring.py  tests/test_agent_pipeline.py
```

**Frontera con B:** A importa de `adapters` **sólo** `build_itsm_adapter`, `build_identity_adapter`,
`build_app_adapter` (§3.2.1). A **no** importa clases concretas (`ServiceNowITSM`, etc.) ni crea
archivos en `adapters/`, `eval/`, `docs/` ni en la raíz.
**Cómo A desarrolla sin B (obligatorio):** los tests de A definen **fakes locales** en el propio
archivo de test (`FakeITSM`, `FakeIdentity`, `FakeApps`) para poder correr
`python3 -m unittest discover -s tests -t . -v` sin el paquete `adapters`. El test end-to-end de
integración va en `tests/test_agent_pipeline.py` y se marca
`@unittest.skipUnless(ADAPTERS_AVAILABLE, ...)` si `adapters` aún no existe; cuando B entregue, se
quita el skip y se verifica.

### 9.2 Implementador B — Adaptadores, evaluación, docs y packaging

```
adapters/__init__.py  adapters/base.py  adapters/itsm.py  adapters/identity.py  adapters/apps.py
eval/__init__.py  eval/metrics.py  eval/run_eval.py  eval/labeled_tickets.json
tests/test_adapters.py  tests/test_eval.py  tests/test_packaging.py
README.md  Makefile  requirements.txt  .env.example  .gitignore  outputs/.gitkeep
docs/ARCHITECTURE.md  docs/INTERVIEW_PLAYBOOK.md
```

**Frontera con A:** B **no** modifica `services/`, `policies/`, `data/` ni `agent.py`. B lee
`data/kb.json` y `policies/risk_matrix.json` como **entradas de sólo lectura** (para escribir el
eval set y los tests), no los edita. B no crea archivos en `services/`, `policies/` ni `data/`.

### 9.3 Director — `[D]`

```
docs/SPEC.md   (ya escrito — este documento)
```
El director verifica la integración final (checks §6.1) una vez que A y B entreguen, y arbitra
cualquier conflicto de contrato.

### 9.4 Dependencias (para ordenar el ataque)

| Archivo | Depende de | ¿Puede empezar antes? |
|---|---|---|
| `services/*` | stdlib + `data/` + `policies/` | Sí (con fakes) |
| `adapters/*` | stdlib (+ `requests` lazy) | Sí |
| `data/*`, `policies/*` | nada | Sí |
| `eval/*` | `services/*` | Sí; el harness se valida al final |
| `agent.py` | `adapters/*` + `services/*` | No — se cierra último |
| `docs/*`, `README.md` | comportamiento final | Sí (en paralelo) |

---

## 10. Reglas transversales (obligatorias para los dos)

1. **Idempotencia**: toda acción tiene `action_id` determinista; reprocesar no duplica efectos.
2. **Auditoría**: todo evento relevante va a `outputs/audit.log` (JSONL). No hay acciones sin log.
3. **Explicabilidad**: toda decisión de política trae `reasons` no vacío y `rule_ids`.
4. **Sin acciones destructivas**: los conectores reales no escriben sin credenciales **y** sin
   `HELPDESK_ALLOW_WRITES=1`.
5. **Sin secretos en el repo**: `.env` en `.gitignore`; `.env.example` sólo placeholders.
6. **Timestamps**: ISO-8601 UTC con `Z`, sin microsegundos.
7. **Python 3.9**: sin `match`, sin `X | Y` ni `dict[str,int]` en anotaciones (usar `typing`).
8. **Sin dependencias obligatorias**: `requirements.txt` no instala nada por defecto.
9. **Idioma**: código en inglés; `README` y `docs/` de entrevista en ES con excerpts EN.
10. **Si algo no está en este SPEC, no lo inventes**: preguntá al director.

---

— Director agent, 2026-11-12
