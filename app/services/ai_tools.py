import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import engine, SessionLocal
from app.models.lead import Lead
from app.models.tag import Tag
from app.models.task import Task
from app.models.user import User
import json
import urllib.request
import urllib.error

# =====================================================================
# Database Inspector Tool
# =====================================================================

def get_database_schema() -> str:
    """
    Retorna o schema do banco de dados (tabelas e colunas importantes)
    útil para a IA saber as tabelas antes de executar uma busca (run_select_query).
    """
    schema = """
    Tabelas principais:
    - leads (id, nome, email, whatsapp, destinos [JSON], data_chegada, data_partida, status_venda, campos_personalizados [JSON], is_active, created_at, updated_at)
    - tags (id, nome, cor, created_at)
    - lead_tags (lead_id, tag_id)
    - tasks (id, title, description, due_date, status [pending, in_progress, completed], lead_id, assigned_to_id)
    - funnels (id, nome, descricao, is_default, is_active)
    - funnel_entries (id, funnel_id, lead_id, etapa_id [nova_oportunidade, contato_feito, em_negociacao, proposta_enviada, follow_up, fechou_venda, perda])
    - segments (id, nome, rules [JSON])
    - users (id, name, email, is_active, role [admin, agent])
    """
    return schema

def run_select_query(query: str) -> str:
    """
    Executa uma query SQL de LEITURA (SELECT) genérica no banco de dados SQLite.
    Nunca alterar dados usando essa ferramenta. Usar apenas para responder perguntas
    analíticas como "quantos leads temos?", "quantas tarefas estão em pending?".
    
    Args:
        query: A query SQL (SQLite) de leitura a ser executada.
    """
    query = query.strip()
    if not query.lower().startswith("select"):
        return json.dumps({"error": "Apenas consultas SELECT são permitidas."})
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text(query))
            rows = [dict(row._mapping) for row in result.all()]
            return json.dumps(rows, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})

def run_sql_write_query(query: str) -> str:
    """
    Executa comandos SQL de ESCRITA (INSERT, UPDATE, DELETE) no banco de dados SQLite.
    Permite modificar qualquer tabela do sistema livremente. Use com cautela!
    Retorna o número de linhas afetadas ou o erro.
    
    Args:
        query: A query SQL de escrita a ser executada.
    """
    query = query.strip()
    if query.lower().startswith("select"):
        return json.dumps({"error": "Use run_select_query para SELECT."})
    
    try:
        with engine.begin() as conn:
            result = conn.execute(text(query))
            return json.dumps({"success": True, "rows_affected": result.rowcount})
    except Exception as e:
        return json.dumps({"error": str(e)})

# =====================================================================
# Operational Tools
# =====================================================================

def update_lead_status(lead_id: int, status_venda: str = None, cancel_reason: str = None) -> str:
    """
    Atualiza o campo status_venda de um lead existente.
    
    Args:
        lead_id: O ID do lead.
        status_venda: O novo status de venda (ex: 'venda', 'em_negociacao', 'perda', 'arquivado').
        cancel_reason: Opcional, motivo caso seja perda.
    """
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return json.dumps({"error": f"Lead {lead_id} não encontrado."})
        
        if status_venda:
            lead.status_venda = status_venda
            
        if cancel_reason:
            if not lead.campos_personalizados:
                lead.campos_personalizados = {}
            lead.campos_personalizados['motivo_perda'] = cancel_reason
            
        db.commit()
        return json.dumps({"success": True, "message": f"Lead {lead_id} atualizado."})
    finally:
        db.close()

def create_task(lead_id: int, title: str, description: str, due_date: str) -> str:
    """
    Cria uma nova tarefa associada a um lead.
    
    Args:
        lead_id: O ID do lead associado a tarefa.
        title: O título da tarefa.
        description: A descrição da tarefa.
        due_date: A data/hora limite da tarefa no formato YYYY-MM-DDTHH:MM:SS.
    """
    db = SessionLocal()
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(due_date.replace("Z", ""))
        
        task = Task(
            title=title,
            description=description,
            due_date=dt,
            status="pending",
            lead_id=lead_id
        )
        db.add(task)
        db.commit()
        return json.dumps({"success": True, "message": f"Tarefa '{title}' criada para o lead {lead_id}."})
    except Exception as e:
        db.rollback()
        return json.dumps({"error": str(e)})
    finally:
        db.close()

def add_tag_to_lead(lead_id: int, tag_nome: str) -> str:
    """
    Adiciona uma tag ao lead e cria a tag caso ela não exista.
    """
    db = SessionLocal()
    try:
        tag_nome = tag_nome.strip()
        tag = db.query(Tag).filter(Tag.nome == tag_nome).first()
        if not tag:
            tag = Tag(nome=tag_nome, cor="#2B6CB0")
            db.add(tag)
            db.commit()
            db.refresh(tag)
        
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return json.dumps({"error": f"Lead {lead_id} não encontrado."})
            
        if tag not in lead.tags:
            lead.tags.append(tag)
            db.commit()
            
        return json.dumps({"success": True, "message": f"Tag '{tag_nome}' adicionada ao lead {lead_id}."})
    except Exception as e:
        db.rollback()
        return json.dumps({"error": str(e)})
    finally:
        db.close()

def create_lead(nome: str, email: str = None, whatsapp: str = None, destinos: str = None, data_chegada: str = None, data_partida: str = None, tag: str = None, status_venda: str = "em_negociacao") -> str:
    """
    Cria um novo lead rapidamente no sistema com base nos dados fornecidos.
    
    Args:
        nome: O nome do lead.
        email: Email do lead.
        whatsapp: WhatsApp do lead.
        destinos: Destinos, separados por vírgula (Ex: "Atacama, Uyuni").
        data_chegada: Data de chegada no formato YYYY-MM-DD.
        data_partida: Data de partida no formato YYYY-MM-DD.
        tag: Opcional, nome da tag principal a ser vinculada ao lead criado (Ex: "Atacama").
        status_venda: Status inicial do funil ('em_negociacao', 'venda', etc).
    """
    db = SessionLocal()
    try:
        from datetime import datetime
        d_chegada = datetime.fromisoformat(data_chegada).date() if data_chegada else None
        d_partida = datetime.fromisoformat(data_partida).date() if data_partida else None
        lista_destinos = [d.strip() for d in destinos.split(",")] if destinos else []

        lead = Lead(
            nome=nome,
            email=email,
            whatsapp=whatsapp,
            destinos=lista_destinos,
            data_chegada=d_chegada,
            data_partida=d_partida,
            campos_personalizados={},
            status_venda=status_venda
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        
        # Se uma tag foi pedida, nós reaproveitamos a lógica de adicao
        if tag:
            db.close() # close early here
            return add_tag_to_lead(lead.id, tag)
            
        return json.dumps({"success": True, "lead_id": lead.id, "message": f"Lead '{nome}' criado."})
    except Exception as e:
        db.rollback()
        return json.dumps({"error": str(e)})
    finally:
        db.close()

def get_api_endpoints() -> str:
    """
    Retorna a lista de todos os endpoints REST disponíveis no sistema baseados na doc OpenAPI.
    Isso diz à IA quais métodos HTTP em '/api/...' ela pode acessar, bem como um resumo.
    Use essa ferramenta antes de chamar call_internal_api para saber a URL e o método.
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/openapi.json", headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
        
        endpoints = []
        for path, methods in data.get("paths", {}).items():
            if not path.startswith("/api/"):
                continue
            for method, details in methods.items():
                desc = details.get("summary", "")
                endpoints.append(f"[{method.upper()}] {path} - {desc}")
                
        return json.dumps({"endpoints": endpoints})
    except Exception as e:
        return json.dumps({"error": f"Falha ao ler OpenAPI docs: {str(e)}"})

def call_internal_api(method: str, path: str, payload_json: str = None) -> str:
    """
    Faz uma requisição HTTP para a própria API do sistema.
    Isso te dá acesso a QUALQUER MÉTODO como se você fosse o frontend do sistema.
    
    Args:
        method: O método HTTP (GET, POST, PUT, DELETE).
        path: O caminho do endpoint (ex: '/api/leads/segment').
        payload_json: Opcional. Uma string contendo um JSON válido para o body da requisição (ex: '{"nome": "João"}').
    """
    url = f"http://127.0.0.1:8000{path}"
    
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    data = None
    if payload_json and payload_json.strip():
        data = payload_json.encode('utf-8')
        
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        with urllib.request.urlopen(req) as response:
            response_data = response.read().decode()
            try:
                return json.dumps({"status": response.status, "data": json.loads(response_data)})
            except:
                return json.dumps({"status": response.status, "data": response_data})
    except urllib.error.HTTPError as e:
        response_data = e.read().decode()
        try:
            return json.dumps({"error_status": e.code, "details": json.loads(response_data)})
        except:
            return json.dumps({"error_status": e.code, "details": response_data})
    except Exception as e:
        return json.dumps({"error": str(e)})

# List of tools to pass to Gemini
AVAILABLE_TOOLS = [
    get_database_schema, run_select_query, run_sql_write_query, 
    update_lead_status, create_task, create_lead, add_tag_to_lead,
    get_api_endpoints, call_internal_api
]

# Dictionary to map function names to actual functions during execution
TOOL_FUNCTIONS = {
    "get_database_schema": get_database_schema,
    "run_select_query": run_select_query,
    "run_sql_write_query": run_sql_write_query,
    "update_lead_status": update_lead_status,
    "create_task": create_task,
    "create_lead": create_lead,
    "add_tag_to_lead": add_tag_to_lead,
    "get_api_endpoints": get_api_endpoints,
    "call_internal_api": call_internal_api,
}
