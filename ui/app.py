"""Interview UI for the local, mock-only Help Desk triage demo."""

from __future__ import annotations

import json
import html
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import agent  # noqa: E402

st.set_page_config(
    page_title="Help Desk Triage Lab",
    page_icon="🛟",
    layout="wide",
    initial_sidebar_state="expanded",
)

EVENT_LABELS = {
    "ticket_received": "Ticket recibido",
    "triage_completed": "Triage completado",
    "identity_checked": "Identidad consultada",
    "policy_decided": "Política evaluada",
    "action_proposed": "Acción propuesta",
    "approval_created": "Aprobación solicitada",
    "escalation_raised": "Escalamiento generado",
    "action_executed": "Acción simulada",
    "action_failed": "Acción bloqueada",
    "itsm_writeback_skipped": "Write-back omitido",
    "itsm_writeback": "Write-back registrado",
    "run_completed": "Ejecución terminada",
}
DECISION_LABELS = {
    "auto_execute": "Resolución automática (mock)",
    "require_approval": "Requiere aprobación humana",
    "escalate": "Escalado a un equipo",
}
RISK_COLORS = {
    "low": "#34d399",
    "medium": "#fbbf24",
    "high": "#fb923c",
    "critical": "#fb7185",
}

st.markdown(
    """
    <style>
      .stApp { background: #08111f; }
      [data-testid="stSidebar"] { background: #0d1929; border-right: 1px solid #203047; }
      .block-container { max-width: 1440px; padding-top: 2.1rem; padding-bottom: 3rem; }
      .hero { padding: 1.35rem 1.55rem; border: 1px solid #243752; border-radius: 18px;
              background: linear-gradient(115deg, #10253a 0%, #102032 52%, #102b34 100%);
              margin: 0.2rem 0 1.1rem; }
      .hero-kicker { color: #78dbc9; font-size: .75rem; letter-spacing: .13em;
                     font-weight: 800; text-transform: uppercase; }
      .hero h1 { margin: .25rem 0 .4rem; color: #f2f7fb; font-size: 2rem; }
      .hero p { margin: 0; color: #b9c8d8; font-size: 1rem; }
      .safe-badge { display: inline-block; border-radius: 999px; border: 1px solid #247968;
                    background: #10372f; color: #9cf1d8; padding: .35rem .7rem;
                    font-size: .72rem; letter-spacing: .04em; font-weight: 800; }
      .section-label { color: #91a8bd; text-transform: uppercase; letter-spacing: .11em;
                       font-size: .72rem; font-weight: 800; margin: .4rem 0 .65rem; }
      .decision-card { border: 1px solid #293b52; border-left: 5px solid #34d399;
                       background: #101c2b; border-radius: 14px; padding: 1rem 1.1rem;
                       margin: .4rem 0 1rem; }
      .decision-title { color: #eef5fb; font-size: 1.13rem; font-weight: 750; }
      .decision-sub { color: #9fb1c5; margin-top: .3rem; }
      .reason-box { border: 1px solid #263951; border-radius: 12px; background: #0d1928;
                    padding: .75rem .9rem; margin: .4rem 0; color: #c5d1df; }
      .timeline { border-left: 2px solid #284158; margin: .35rem 0 .7rem .3rem; padding-left: 1rem; }
      .timeline-row { position: relative; margin: .65rem 0 1rem; color: #dce6f0; }
      .timeline-row:before { content: ''; position: absolute; left: -1.42rem; top: .34rem;
                             width: .48rem; height: .48rem; border-radius: 50%;
                             background: #55d6be; box-shadow: 0 0 0 4px #153343; }
      .timeline-event { font-size: .92rem; font-weight: 700; }
      .timeline-meta { color: #8196ad; font-size: .76rem; margin-top: .12rem; }
      div[data-testid="stMetric"] { background: #101c2b; border: 1px solid #25374d;
                                     border-radius: 13px; padding: .8rem 1rem; }
      div[data-testid="stMetricLabel"] { color: #9db0c4; }
      .small-note { color: #8296ab; font-size: .78rem; }
      .ticket-desc { border: 1px solid #263951; border-radius: 12px; padding: .9rem 1rem;
                     background: #0d1928; color: #d6e0eb; line-height: 1.55; }
      hr { border-color: #203047; }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_demo_tickets() -> List[dict]:
    """Load the repository's synthetic CSV tickets through the existing parser."""
    return agent._load_tickets(str(REPO_ROOT / "data" / "tickets.csv"))


def run_mock_batch(tickets: List[dict]) -> Dict[str, Any]:
    """Run the existing pipeline with mock adapters and temporary artefacts."""
    adapters = agent.build_adapters("mock", "mock", "mock", tickets)
    with tempfile.TemporaryDirectory(prefix="helpdesk-streamlit-") as output_dir:
        summary = agent.run_pipeline(
            tickets,
            adapters,
            dry_run=True,
            verbose=False,
            approve_all=False,
            outputs_dir=output_dir,
        )
        audit_path = Path(output_dir) / "audit.log"
        events = []
        if audit_path.exists():
            with audit_path.open("r", encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle if line.strip()]
        return {"summary": summary, "events": events}


def render_timeline(events: List[dict], ticket_id: str) -> None:
    relevant = [
        event
        for event in events
        if event.get("ticket_id") == ticket_id
        and event.get("event") in EVENT_LABELS
    ]
    if not relevant:
        st.caption("No hay eventos de auditoría para este ticket.")
        return
    rows = []
    for event in relevant:
        label = EVENT_LABELS[event["event"]]
        detail = event.get("decision") or event.get("category") or event.get("status")
        if event.get("event") == "identity_checked":
            detail = "usuario encontrado" if event.get("user_found") else "usuario no encontrado"
        elif event.get("event") == "policy_decided":
            detail = "{} · riesgo {}".format(event.get("decision"), event.get("risk"))
        elif event.get("event") == "itsm_writeback_skipped":
            detail = event.get("reason", "modo demo")
        stamp = str(event.get("ts", ""))
        rows.append(
            '<div class="timeline-row"><div class="timeline-event">{}</div>'
            '<div class="timeline-meta">{} · {}</div></div>'.format(
                html.escape(label), html.escape(str(detail or "Registrado")), html.escape(stamp)
            )
        )
    st.markdown('<div class="timeline">{}</div>'.format("".join(rows)), unsafe_allow_html=True)


def render_decision(result: dict) -> None:
    decision = result.get("decision", "require_approval")
    risk = result.get("risk", "medium")
    color = RISK_COLORS.get(risk, "#a8b7c8")
    st.markdown(
        '<div class="decision-card" style="border-left-color:{}">'
        '<div class="decision-title">{}</div>'
        '<div class="decision-sub">Riesgo <b style="color:{}">{}</b> '
        '· Confianza <b>{:.0%}</b></div></div>'.format(
            color,
            html.escape(DECISION_LABELS.get(decision, decision)),
            color,
            html.escape(str(risk).upper()),
            float(result.get("confidence") or 0),
        ),
        unsafe_allow_html=True,
    )


def main() -> None:
    tickets = load_demo_tickets()
    ticket_by_id = {ticket["ticket_id"]: ticket for ticket in tickets}

    with st.sidebar:
        st.markdown("### 🛟 Triage Lab")
        st.caption("Prototipo interactivo · ejecución local/mock")
        st.markdown('<span class="safe-badge">MOCK ONLY · DRY RUN</span>', unsafe_allow_html=True)
        st.markdown("---")
        st.markdown("**Qué vas a ver**")
        st.markdown(
            "1. Clasificación y prioridad\n2. Consulta de identidad\n"
            "3. Política de riesgo\n4. Acción, aprobación o escalamiento"
        )
        st.markdown("---")
        st.caption("No usa credenciales ni escribe en ServiceNow, Jira, Entra ID u Okta.")

    st.markdown(
        '<div class="hero"><div class="hero-kicker">Explainable support automation</div>'
        '<h1>AI Help Desk Triage</h1>'
        '<p>Del ticket a una decisión trazable: clasificar, validar, aplicar política y escalar con control humano.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    left, right = st.columns([1.1, 1.7], gap="large")
    with left:
        st.markdown('<div class="section-label">1 · Elegí un caso</div>', unsafe_allow_html=True)
        selected_id = st.selectbox(
            "Ticket sintético",
            options=list(ticket_by_id),
            format_func=lambda ticket_id: "{} · {}".format(
                ticket_id,
                ticket_by_id[ticket_id].get("service", "Ticket"),
            ),
            label_visibility="collapsed",
        )
        ticket = ticket_by_id[selected_id]
        st.markdown(
            '<div class="ticket-desc">{}</div>'.format(
                html.escape(ticket.get("description", ""))
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            "Solicitante: `{}` · Servicio: `{}` · Creado: `{}`".format(
                ticket.get("requester"), ticket.get("service"), ticket.get("created_at")
            )
        )
        run_clicked = st.button("▶ Ejecutar demo completa", type="primary", use_container_width=True)
        if run_clicked:
            with st.spinner("Procesando {} tickets con adaptadores mock...".format(len(tickets))):
                try:
                    st.session_state["demo_result"] = run_mock_batch(tickets)
                    st.session_state["approval_state"] = {}
                    st.session_state.pop("demo_error", None)
                except Exception as exc:  # surfaced as a concise local demo error
                    st.session_state["demo_error"] = str(exc)
                    st.session_state["demo_result"] = None
        if st.session_state.get("demo_error"):
            st.error("No se pudo ejecutar la demo: {}".format(st.session_state["demo_error"]))
        if not st.session_state.get("demo_result"):
            st.info("Ejecutá la demo para ver triage, decisiones, aprobaciones y métricas.")

    demo = st.session_state.get("demo_result")
    if not demo:
        with right:
            st.markdown('<div class="section-label">2 · Recorrido del agente</div>', unsafe_allow_html=True)
            st.write("La interfaz mostrará los pasos del pipeline y el motivo de la decisión.")
        return

    summary = demo["summary"]
    result_by_id = {item["ticket_id"]: item for item in summary.get("tickets", [])}
    result = result_by_id.get(selected_id)
    with right:
        st.markdown('<div class="section-label">2 · Resultado del ticket</div>', unsafe_allow_html=True)
        if not result:
            st.warning("No se encontró un resultado para este ticket.")
            return
        render_decision(result)
        c1, c2, c3 = st.columns(3)
        c1.metric("Prioridad", result.get("priority", "—"))
        c2.metric("Categoría", result.get("category", "—"))
        c3.metric("Impacto · urgencia", "{} · {}".format(result.get("impact", "—"), result.get("urgency", "—")))
        st.markdown("**Acción sugerida** · `{}`".format(result.get("suggested_action", "human_review")))
        if result.get("suggested_resolution"):
            st.markdown("<div class='reason-box'><b>Resolución sugerida</b><br>{}</div>".format(
                html.escape(result["suggested_resolution"])
            ), unsafe_allow_html=True)
        if result.get("reasons"):
            with st.expander("Por qué decidió esto", expanded=True):
                for reason in result["reasons"]:
                    st.markdown("- {}".format(reason))
                st.caption("Reglas: {}".format(", ".join(result.get("rule_ids") or []) or "—"))
        if result.get("decision") == "require_approval" and result.get("approval_id"):
            approval_state = st.session_state.setdefault("approval_state", {})
            state_key = result["approval_id"]
            current = approval_state.get(state_key, "Pendiente")
            st.markdown("**Aprobación simulada** · `{}` · {}".format(state_key, current))
            a1, a2 = st.columns(2)
            if a1.button("Simular aprobación", key="approve_" + state_key, use_container_width=True):
                approval_state[state_key] = "Aprobada (solo UI)"
            if a2.button("Simular rechazo", key="reject_" + state_key, use_container_width=True):
                approval_state[state_key] = "Rechazada (solo UI)"
            st.caption("Estos botones solo cambian el estado visible; no ejecutan acciones.")
        elif result.get("decision") == "escalate":
            st.warning("Escalar a `{}`. Las acciones críticas nunca se ejecutan desde esta interfaz.".format(
                result.get("escalation_queue") or "itsm-l2"
            ))

    st.markdown("---")
    batch_col, timeline_col = st.columns([1, 1.4], gap="large")
    metrics = summary.get("metrics", {})
    with batch_col:
        st.markdown('<div class="section-label">3 · Resumen del lote</div>', unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("Tickets", len(summary.get("tickets", [])))
        m2.metric("Auto-resueltos", metrics.get("auto_resolved", 0))
        m3.metric("Escalados", metrics.get("escalated", 0))
        m4, m5, m6 = st.columns(3)
        m4.metric("Aprobación", metrics.get("requires_approval", 0))
        m5.metric("Deflexión", "{:.0%}".format(metrics.get("deflection_rate", 0)))
        m6.metric("Confianza media", "{:.0%}".format(metrics.get("average_confidence", 0)))
        with st.expander("Ver métricas completas"):
            st.json(metrics)
    with timeline_col:
        st.markdown('<div class="section-label">4 · Audit trail del ticket</div>', unsafe_allow_html=True)
        render_timeline(demo.get("events", []), selected_id)
        with st.expander("Datos técnicos del resultado"):
            st.json(result)
    st.caption("Run `{}` · ejecución dry-run · artefactos temporales eliminados al terminar.".format(summary.get("run_id")))


if "demo_result" not in st.session_state:
    st.session_state["demo_result"] = None
if "approval_state" not in st.session_state:
    st.session_state["approval_state"] = {}

main()
