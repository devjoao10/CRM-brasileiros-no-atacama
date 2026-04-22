from typing import Optional
from datetime import datetime, date, time, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.lead import Lead
from app.models.pipeline import Funnel, FunnelEntry
from app.models.task import Task
from app.models.user import User
from app.auth import get_current_user

router = APIRouter(prefix="/api/analytics", tags=["Analytics"])

@router.get("/dashboard")
async def get_dashboard_analytics(
    start_date: Optional[date] = Query(None, description="Data inicial do período (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="Data final do período (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Retorna métricas chave e os dados pro gráfico agrupados por data.
    Por padrão retorna os últimos 30 dias se nenhuma data for passada.
    """
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    if start_date > end_date:
        raise HTTPException(status_code=400, detail="start_date não pode ser maior que end_date.")

    # Convertendo para datetime cobrindo da 00:00 até 23:59:59
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)

    # KPIs via SQL (sem carregar todos os leads em memória)
    total_leads = db.query(func.count(Lead.id)).filter(
        Lead.created_at >= start_dt, Lead.created_at <= end_dt
    ).scalar()

    vendas_fechadas = db.query(func.count(Lead.id)).filter(
        Lead.created_at >= start_dt, Lead.created_at <= end_dt,
        Lead.status_venda == "venda"
    ).scalar()

    conversao = 0
    if total_leads > 0:
        conversao = round((vendas_fechadas / total_leads) * 100, 1)

    # Tarefas pendentes/atrasadas
    hoje_end = datetime.combine(date.today(), time.max)
    tarefas_query = db.query(func.count(Task.id)).filter(
        Task.status.in_(["pendente", "em_andamento"]),
        Task.data_vencimento <= hoje_end
    )
    if current_user.role != "admin":
        tarefas_query = tarefas_query.filter(Task.user_id == current_user.id)
    tarefas_pendentes = tarefas_query.scalar()

    # Gráfico diário via SQL GROUP BY
    daily_leads = db.query(
        func.date(Lead.created_at).label("dia"),
        func.count(Lead.id).label("total")
    ).filter(
        Lead.created_at >= start_dt, Lead.created_at <= end_dt
    ).group_by(func.date(Lead.created_at)).all()

    daily_vendas = db.query(
        func.date(Lead.created_at).label("dia"),
        func.count(Lead.id).label("total")
    ).filter(
        Lead.created_at >= start_dt, Lead.created_at <= end_dt,
        Lead.status_venda == "venda"
    ).group_by(func.date(Lead.created_at)).all()

    # Montar dicionário do gráfico
    leads_by_day = {str(row.dia): row.total for row in daily_leads}
    vendas_by_day = {str(row.dia): row.total for row in daily_vendas}

    chart_dict = {}
    curr_date = start_date
    while curr_date <= end_date:
        d_str = curr_date.strftime("%Y-%m-%d")
        chart_dict[d_str] = {
            "leads": leads_by_day.get(d_str, 0),
            "vendas": vendas_by_day.get(d_str, 0),
        }
        curr_date += timedelta(days=1)

    chart_labels = sorted(list(chart_dict.keys()))
    chart_leads = [chart_dict[lbl]["leads"] for lbl in chart_labels]
    chart_vendas = [chart_dict[lbl]["vendas"] for lbl in chart_labels]

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "kpis": {
            "total_leads": total_leads,
            "vendas_fechadas": vendas_fechadas,
            "taxa_conversao": f"{conversao}%",
            "tarefas_pendentes": tarefas_pendentes
        },
        "chart": {
            "labels": chart_labels,
            "leads": chart_leads,
            "vendas": chart_vendas
        }
    }


@router.get("/reports")
async def get_detailed_reports(
    start_date: Optional[date] = Query(None, description="Data inicial do período (YYYY-MM-DD)"),
    end_date: Optional[date] = Query(None, description="Data final do período (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    
    all_leads = db.query(Lead).filter(Lead.created_at >= start_dt, Lead.created_at <= end_dt).all()
    
    total = len(all_leads)
    em_negociacao = sum(1 for l in all_leads if l.status_venda == 'em_negociacao')
    vendas = sum(1 for l in all_leads if l.status_venda == 'venda')
    perdas = sum(1 for l in all_leads if l.status_venda == 'perda')
    
    destinos_count = {}
    origem_count = {}
    tags_count = {}
    etapas_count = {}
    status_by_day = {}
    
    # Initialize days for chart
    curr_date = start_date
    while curr_date <= end_date:
        d_str = curr_date.strftime("%Y-%m-%d")
        status_by_day[d_str] = {"vendas": 0, "perdas": 0, "em_negociacao": 0}
        curr_date += timedelta(days=1)
    
    for l in all_leads:
        # Time series
        d_str = l.created_at.strftime("%Y-%m-%d")
        if d_str in status_by_day:
            if l.status_venda in status_by_day[d_str]:
                status_by_day[d_str][l.status_venda] += 1
                
        # Variables
        if l.destinos and isinstance(l.destinos, list):
            for d in l.destinos:
                if d:
                    destinos_count[d] = destinos_count.get(d, 0) + 1
        
        if l.campos_personalizados and isinstance(l.campos_personalizados, dict):
            origem = l.campos_personalizados.get("origem") or l.campos_personalizados.get("Origem") or "Outros/Manual"
            origem_count[origem] = origem_count.get(origem, 0) + 1

        for t in l.tags:
            tags_count[t.nome] = tags_count.get(t.nome, 0) + 1
            
        for fe in l.funnel_entries:
            funnel_name = fe.funnel.nome if fe.funnel else "Desconhecido"
            etapa_name = fe.etapa_id
            if fe.funnel and fe.funnel.etapas:
                for stage in fe.funnel.etapas:
                    if stage.get("id") == fe.etapa_id:
                        etapa_name = stage.get("nome", fe.etapa_id)
                        break
            en = f"{funnel_name} - {etapa_name}"
            etapas_count[en] = etapas_count.get(en, 0) + 1

    # Order keys by value descending
    dest_sorted = {k: v for k, v in sorted(destinos_count.items(), key=lambda item: item[1], reverse=True)}
    orig_sorted = {k: v for k, v in sorted(origem_count.items(), key=lambda item: item[1], reverse=True)}
    tags_sorted = {k: v for k, v in sorted(tags_count.items(), key=lambda item: item[1], reverse=True)}
    etapas_sorted = {k: v for k, v in sorted(etapas_count.items(), key=lambda item: item[1], reverse=True)}

    return {
        "kpis": {
            "total": total,
            "em_negociacao": em_negociacao,
            "vendas": vendas,
            "perdas": perdas
        },
        "breakdown": {
            "destinos": dest_sorted,
            "origems": orig_sorted,
            "tags": tags_sorted,
            "etapas": etapas_sorted
        },
        "chart": {
            "labels": sorted(list(status_by_day.keys())),
            "vendas": [status_by_day[lbl]["vendas"] for lbl in sorted(list(status_by_day.keys()))],
            "perdas": [status_by_day[lbl]["perdas"] for lbl in sorted(list(status_by_day.keys()))],
            "em_negociacao": [status_by_day[lbl]["em_negociacao"] for lbl in sorted(list(status_by_day.keys()))]
        }
    }
