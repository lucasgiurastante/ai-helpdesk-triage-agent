# Progress Log — Evolución Agentic AI · AI Help Desk

Registro de todo lo que se implementa en este repositorio, tarea por tarea del tablero Trello
"Evolución Agentic AI — AI Help Desk" (https://trello.com/b/GjoMp1dT).

## Estado inicial (línea base)

- Repo: CLI Python stdlib-only, triage determinista, adapters mock/real (ServiceNow, Jira, Entra, Okta),
  eval con 28 tickets, UI Streamlit de demo.
- Todas las tarjetas del tablero en estado **Pendiente**.
- Sin FastAPI, LLM, RAG, LangGraph, webhooks ni voz.

## Fase 00 · Línea base y contrato — COMPLETADA

### Tarea 1 — Definir límites entre sistema original y laboratorio ✅
- Archivo: `docs/LAB_BOUNDARIES.md` (tabla de fronteras, reglas, criterio de aceptación).
- `lab/` y `api/` reutilizan `services/` y `policies/` sin duplicar.

### Tarea 2 — Definir configuración central mock/live ✅
- Archivo: `config.py` (`LabConfig`, `load_config()`, `get_config()`).
- Por defecto todo mock; fuerza mock si faltan credenciales.

### Tarea 3 — Fijar versiones y dependencias opcionales ✅
- Archivo: `requirements-lab.txt` (FastAPI, uvicorn, pydantic, openai, anthropic, chromadb, langgraph, streamlit, ...).
- `.env.example` ampliado con variables `LAB_*` (comentadas).

### Tarea 4 — Alinear la documentación heredada ✅
- `docs/ARCHITECTURE.md` sección 7 (laboratorio agentic).
- `docs/LAB_BOUNDARIES.md` como referencia de fronteras.

### Tarea 5 — Definir comandos reales de ejecución ✅
- `Makefile`: targets `compile` (incluye api/lab), `api-check`, `api-run`.
- `api/main.py` ejecutable con `uvicorn api.main:app`.

### Tarea 6 — Validar y documentar la línea base real ✅
- `python3 -m compileall` OK.
- `python3 -m unittest discover` → 164 tests OK.
- `python3 eval/run_eval.py` → EVAL PASSED (todas las métricas 1.0).

## Fase 01 · API FastAPI + Pydantic — COMPLETADA

### Tarea "Crear la aplicación FastAPI" ✅
- `api/__init__.py` (guardas de importación).
- `api/main.py` (`create_app()`, app, startup log de modo).
- `api/models.py` (HealthResponse, TriageRequest, TriageResponse, ScoreBreakdown).
- `api/services.py` (TriageLabService envuelve TriageService; carga KB y policies).
- `api/routes/health.py` (GET /health).
- `api/routes/triage.py` (POST /triage).
- Verificado con TestClient: HEALTH ok mock; POST /triage → 200 (category=vpn, priority=P2).

### Pendientes Fase 01
- Definir modelos Pydantic (parcial: hecho en models.py).
- Implementar GET /health (hecho).
- Implementar POST /triage (hecho).
- Implementar contrato de respuesta del agente (hecho).
- Validar tamaño y campos de entrada (hecho: max_length en modelos + empty check).
- Añadir request_id y errores uniformes (hecho: request_id + handlers HTTP 422/500).
- Mantener el motor original como servicio (hecho).
- Separar dependencias de API (hecho: requirements-lab.txt).

## Fase 02 · LLM y salida estructurada — COMPLETADA ✅

### Entregables
- `lab/llm/base.py` — interfaz común `LLMProvider` (complete, is_available, metadata).
- `lab/llm/schema.py` — `LLMResponse`, `LLMMetadata`, `LLMUsage`, `LLM_JSON_SCHEMA`.
- `lab/llm/mock.py` — `MockLLMProvider` determinista (heurísticas por keywords).
- `lab/llm/openai_provider.py` — `OpenAIProvider` (guardas, manejo de fallos).
- `lab/llm/anthropic_provider.py` — `AnthropicProvider` (guardas, manejo de fallos).
- `lab/llm/factory.py` — `create_provider()` con fallback a mock.
- `lab/llm/validation.py` — `validate_response()` (clamp, normalización, minimización).
- `lab/llm/__init__.py` — exports.
- `tests/test_llm.py` — 5 tests (mock, validación, fallback, roundtrip JSON).

### Verificación
- Import OK, mock devuelve LLMResponse estructurada.
- Fallback a mock cuando OpenAI/Anthropic no disponibles.
- Validación clampea confidence y normaliza priority.
- Suite: 172 tests OK (5 nuevos).

## Fase 03 · RAG con Chroma y citas — COMPLETADA ✅

### Entregables (subagente A — ingesta/index)
- `lab/rag/store.py` — `VectorStore` (Chroma con guardas + fallback in-memory).
- `lab/rag/metadata.py` — `add_metadata`, `version_of`, `content_hash`.
- `lab/rag/chunking.py` — `chunk_text` (oraciones/párrafos, tamaño/overlap).
- `lab/rag/ingestion.py` — `load_kb`, `ingest_kb`, `update_document`, `remove_document`.
- `lab/rag/index.py` — `build_index`, `rebuild` (determinista, persiste).

### Entregables (subagente B — retrieval/seguridad)
- `lab/rag/retrieval.py` — `retrieve`, `RetrievedDoc` (top-k con filtros).
- `lab/rag/citations.py` — `build_citations`, `format_citations_for_prompt`, `Citation`.
- `lab/rag/abstention.py` — `should_abstain`, `abstain_response`.
- `lab/rag/safety.py` — `sanitize_document`, `is_suspicious`, `wrap_context`.

### Tests
- `tests/test_rag.py` (16) + `tests/test_rag_retrieval.py` (14) = 30 tests OK.
- Chroma no instalado → fallback in-memory utilizado.

## Fase 04 · Orquestación con LangGraph — COMPLETADA ✅

### Entregables (subagente A — estado + nodos + graph)
- `lab/orchestration/state.py` — `HelpdeskState` (TypedDict).
- `lab/orchestration/nodes.py` — node_entry, node_classify, node_retrieve, node_redact, node_policy, node_approval, node_escalate.
- `lab/orchestration/graph.py` — `build_graph()` (LangGraph con guardas + fallback secuencial), `run_pipeline()`.
- `tests/test_orchestration.py` — 4 tests (VPN, abstención, policy, invoke).

### Entregables (subagente B — ramas, límites, persistencia)
- `lab/orchestration/branches.py` — route_after_policy, route_after_retrieve, route_after_redact.
- `lab/orchestration/limits.py` — MAX_ITERATIONS=5, MAX_TOOL_CALLS=10, checks e increments.
- `lab/orchestration/persistence.py` — save_state, load_state, InMemoryCheckpointer, get_checkpointer.
- `tests/test_orchestration_branches.py` — 22 tests.

### Verificación
- run_pipeline VPN → final con respuesta LLM + citas, policy=require_approval, iterations<=5.
- run_pipeline "xyzzy..." → rag_abstained=True.
- 228 tests OK (skipped=3). Eval PASSED.
