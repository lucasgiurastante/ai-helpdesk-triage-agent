# Límites entre sistema original y laboratorio

> Fase 00 · Tarea 1 — "Definir límites entre sistema original y laboratorio".
> Criterio de aceptación: la arquitectura documenta fronteras y evita duplicar políticas.

## 1. Principio rector

El **agente original** (CLI, `agent.py`, `services/`, `policies/`, `adapters/`, `data/`, `eval/`)
es la **línea base estable y demostrable**. Se preserva intacto: no se reescribe, no se rompe,
no se duplican sus políticas.

La **evolución agentic** vive en carpetas nuevas y separadas:

- `api/` — backend HTTP del laboratorio (FastAPI). Expone el motor original como servicio.
- `lab/` — componentes nuevos del flujo agentic (LLM, RAG, orquestación, webhooks, voz).

Ningún componente de `lab/` ni `api/` reimplementa lógica que ya existe en `services/` o `policies/`.
Reutilizan importando desde el paquete original.

## 2. Tabla de fronteras

| Responsabilidad | Dónde vive | Notas |
|---|---|---|
| CLI pipeline, triage determinista, prioridad | `agent.py`, `services/triage.py` | Línea base. No tocar. |
| Políticas de riesgo, escalación, acceso | `policies/*.json`, `services/policy.py` | Fuente única de verdad. `lab/` las consume, no las copia. |
| Aprobaciones y auditoría | `services/approval.py`, `services/monitoring.py` | Reutilizar. |
| Adapters (ITSM, identidad, apps) mock/real | `adapters/` | Reutilizar vía inyección. |
| Datos y KB | `data/` | `lab/` lee `data/kb.json`; no duplica. |
| Evaluación baseline | `eval/` | Se conserva como regresión (Fase 08). |
| UI de demo (Streamlit) | `ui/app.py` | Se extiende, no se reemplaza. |
| Backend HTTP del laboratorio | `api/` (nuevo) | FastAPI. Importa el motor original como servicio. |
| Proveedores LLM | `lab/llm/` (nuevo) | Interfaz común + mock + OpenAI + Anthropic. |
| RAG (Chroma, citas) | `lab/rag/` (nuevo) | Ingesta de `data/kb.json`. |
| Orquestación (LangGraph) | `lab/orchestration/` (nuevo) | Consume `services/` y `policies/`. |
| Webhooks / integraciones mock | `lab/webhooks/` (nuevo) | Esquema + endpoint + idempotencia. |
| Voz STT/TTS | `lab/voice/` (nuevo) | Interfaces desacopladas. |
| Configuración central mock/live | `config.py` (raíz, nuevo) | Única fuente de configuración de modo. |
| Documentación de fronteras | `docs/LAB_BOUNDARIES.md` | Este archivo. |

## 3. Reglas

1. **No duplicar políticas.** `lab/` y `api/` importan `services.policy`, `policies/*.json`. Nunca copian reglas.
2. **No reescribir el motor original.** `agent.py` y `services/` se consumen como librería.
3. **Configuración única.** Todo modo (mock/live) se resuelve en `config.py`. Nada de `os.getenv` disperso.
4. **Dependencias opcionales aisladas.** FastAPI, Chroma, LangGraph, etc. viven en `requirements-lab.txt` y se importan con guardas (`try/except ImportError`).
5. **Escrituras externas desactivadas por defecto.** `HELPDESK_ALLOW_WRITES=0` sigue siendo el kill switch.
6. **Tests de línea base intactos.** `tests/` no se modifica; los nuevos tests van en `tests/lab/` (o `tests/api/`).

## 4. Criterio de aceptación (Tarea 1)

- [x] Existe `docs/LAB_BOUNDARIES.md` con la tabla de fronteras.
- [x] Documenta que `lab/` y `api/` reutilizan `services/` y `policies/` sin duplicar.
- [x] Define la regla de configuración única (`config.py`).
