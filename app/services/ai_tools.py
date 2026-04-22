import logging
import re

import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import text, create_engine
from app.database import engine, SessionLocal, IS_SQLITE
from app.config import DATABASE_URL, DATABASE_READONLY_URL
from app.models.lead import Lead
from app.models.tag import Tag
from app.models.task import Task
from app.models.user import User
import json
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# Conexão read-only para queries SELECT da IA
if IS_SQLITE:
    # SQLite: usar URI mode com ?mode=ro para read-only
    _ro_url = DATABASE_URL.replace("sqlite:///", "sqlite:///file:", 1) + "?mode=ro&uri=true"
    _read_only_engine = create_engine(_ro_url, connect_args={"check_same_thread": False})
else:
    # PostgreSQL: usar user dedicado read-only (crm_readonly) via DATABASE_READONLY_URL
    _read_only_engine = create_engine(
        DATABASE_READONLY_URL,
        pool_size=3,
        max_overflow=5,
        pool_pre_ping=True,
    )

# Variável global para armazenar o contexto do usuário atual
# Setado pelo router de AI antes de executar as tools
_current_user_api_key = None

def set_ai_user_context(api_key: str):
    """Define a API key do usuário que está interagindo com a IA."""
    global _current_user_api_key
    _current_user_api_key = api_key

def clear_ai_user_context():
    global _current_user_api_key
    _current_user_api_key = None

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
    Executa uma query SQL de LEITURA (SELECT) genérica no banco de dados.
    Nunca alterar dados usando essa ferramenta. Usar apenas para responder perguntas
    analíticas como "quantos leads temos?", "quantas tarefas estão em pending?".
    
    Args:
        query: A query SQL de leitura a ser executada.
    """
    query = query.strip().rstrip(";")
    
    # Bloquear múltiplos statements
    if ";" in query:
        return json.dumps({"error": "Apenas uma query por vez é permitida."})
    
    if not query.lower().startswith("select"):
        return json.dumps({"error": "Apenas consultas SELECT são permitidas."})
    
    # Bloquear subqueries destrutivas
    _dangerous = re.compile(r'\b(insert|update|delete|drop|alter|create|attach|pragma|truncate)\b', re.IGNORECASE)
    if _dangerous.search(query):
        return json.dumps({"error": "Query contém palavras-chave não permitidas para leitura."})
    
    try:
        with _read_only_engine.connect() as conn:
            result = conn.execute(text(query))
            rows = [dict(row._mapping) for row in result.fetchmany(500)]  # Limitar resultados
            logger.info(f"[AI SQL READ] {query[:200]} -> {len(rows)} rows")
            return json.dumps(rows, default=str)
    except Exception as e:
        logger.warning(f"[AI SQL READ ERROR] {query[:200]} -> {e}")
        return json.dumps({"error": str(e)})

# Tabelas que a IA pode modificar (whitelist)
_WRITABLE_TABLES = {"leads", "tags", "lead_tags", "tasks", "funnel_entries", "lead_history", "segments"}
# Tabelas que a IA NUNCA pode modificar
_PROTECTED_TABLES = {"users", "chat_sessions", "chat_messages"}

def run_sql_write_query(query: str) -> str:
    """
    Executa comandos SQL de ESCRITA (INSERT, UPDATE, DELETE) no banco de dados.
    Permite modificar dados das tabelas do sistema. Use com cautela!
    Retorna o número de linhas afetadas ou o erro.
    
    RESTRIÇÕES DE SEGURANÇA:
    - Comandos DDL (DROP, ALTER, TRUNCATE, CREATE) são BLOQUEADOS
    - Apenas INSERT, UPDATE e DELETE são permitidos
    - Tabelas protegidas (users, chat_sessions) não podem ser alteradas
    - Máximo de 100 linhas afetadas por operação
    
    Args:
        query: A query SQL de escrita a ser executada.
    """
    query = query.strip().rstrip(";")
    
    # Bloquear múltiplos statements
    if ";" in query:
        return json.dumps({"error": "Apenas um comando por vez é permitido."})
    
    if query.lower().startswith("select"):
        return json.dumps({"error": "Use run_select_query para SELECT."})
    
    # Bloquear comandos destrutivos/DDL via regex em qualquer posição
    _blocked_pattern = re.compile(r'\b(drop|alter|truncate|create|attach|detach|pragma|vacuum|reindex)\b', re.IGNORECASE)
    if _blocked_pattern.search(query):
        return json.dumps({"error": "Query contém comandos DDL bloqueados por segurança."})
    
    # Verificar se começa com um comando permitido
    first_word = query.split()[0].lower() if query.split() else ""
    _allowed = ["insert", "update", "delete"]
    if first_word not in _allowed:
        return json.dumps({"error": f"Comando '{first_word}' não permitido. Use apenas INSERT, UPDATE ou DELETE."})
    
    # Verificar se a tabela é protegida
    query_lower = query.lower()
    for protected in _PROTECTED_TABLES:
        if protected in query_lower:
            return json.dumps({"error": f"A tabela '{protected}' é protegida e não pode ser modificada pela IA. Use os endpoints da API."})
    
    try:
        with engine.begin() as conn:
            result = conn.execute(text(query))
            affected = result.rowcount
            
            # Limitar operações em massa
            if affected > 100:
                logger.warning(f"[AI SQL WRITE] Operação em massa: {affected} linhas afetadas. Query: {query[:200]}")
            
            logger.info(f"[AI SQL WRITE] {query[:200]} -> {affected} rows affected")
            return json.dumps({"success": True, "rows_affected": affected})
    except Exception as e:
        logger.warning(f"[AI SQL WRITE ERROR] {query[:200]} -> {e}")
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
    Retorna a lista de todos os endpoints REST disponíveis no sistema baseados na doc OpenAPI,
    incluindo um resumo dos campos necessários no corpo da requisição (JSON Payload).
    Use essa ferramenta antes de chamar call_internal_api para saber como montar a requisição perfeitamente.
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
                
                # Extract payload info if available
                payload_info = ""
                try:
                    if "requestBody" in details:
                        content = details["requestBody"].get("content", {})
                        if "application/json" in content:
                            schema = content["application/json"].get("schema", {})
                            if "$ref" in schema:
                                ref_name = schema["$ref"].split("/")[-1]
                                model_schema = data.get("components", {}).get("schemas", {}).get(ref_name, {})
                                props = list(model_schema.get("properties", {}).keys())
                                if props:
                                    payload_info = f" | Payload JSON ({ref_name}): " + ", ".join(props)
                except Exception:
                    pass
                    
                endpoints.append(f"[{method.upper()}] {path} - {desc}{payload_info}")
                
        return json.dumps({"endpoints": endpoints})
    except Exception as e:
        return json.dumps({"error": f"Falha ao ler OpenAPI docs: {str(e)}"})

def call_internal_api(method: str, path: str, payload_json: str = None) -> str:
    """
    Faz uma requisição HTTP para a própria API do sistema.
    Isso te dá acesso a QUALQUER MÉTODO como se você fosse o frontend do sistema.
    A requisição usa o contexto de segurança do usuário atualmente logado.
    
    Args:
        method: O método HTTP (GET, POST, PUT, DELETE).
        path: O caminho do endpoint (ex: '/api/leads/segment').
        payload_json: Opcional. Uma string contendo um JSON válido para o body da requisição (ex: '{"nome": "João"}').
    """
    # Sanitizar path — prevenir SSRF para hosts externos
    if path.startswith("http") or ".." in path:
        return json.dumps({"error": "Path inválido. Use apenas caminhos relativos como /api/leads."})
    
    url = f"http://127.0.0.1:8000{path}"
    
    # Usar a API key do usuário atual (setada pelo router de AI)
    api_key = _current_user_api_key
    
    # Fallback: buscar API key do admin (necessário para funcionar se contexto não foi setado)
    if not api_key:
        try:
            db = SessionLocal()
            admin_user = db.query(User).filter(User.role == "admin", User.is_active == True).first()
            if admin_user and admin_user.api_key:
                api_key = admin_user.api_key
            db.close()
        except Exception:
            pass
    
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    if api_key:
        headers['X-API-Key'] = api_key
    
    data = None
    if payload_json and payload_json.strip():
        data = payload_json.encode('utf-8')
    
    logger.info(f"[AI API CALL] {method.upper()} {path}")
        
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

# =====================================================================
# Document Generation Tools
# =====================================================================

UPLOAD_DIR = None

def _get_upload_dir():
    global UPLOAD_DIR
    if UPLOAD_DIR is None:
        import os
        UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    return UPLOAD_DIR

def generate_excel_document(filename: str, sheet_name: str, headers: str, rows: str) -> str:
    """
    Gera um arquivo Excel (.xlsx) com os dados fornecidos e retorna o link para download.
    Use esta ferramenta quando o usuário pedir um relatório, lista ou dados em formato Excel/planilha.
    
    Args:
        filename: Nome do arquivo sem extensão (ex: 'relatorio_leads'). Será adicionado .xlsx automaticamente.
        sheet_name: Nome da aba/planilha (ex: 'Leads').
        headers: Cabeçalhos das colunas separados por '|' (ex: 'Nome|Email|Status').
        rows: Linhas de dados, cada linha separada por ';;' e cada coluna por '|' (ex: 'João|joao@email.com|Ativo;;Maria|maria@email.com|Inativo').
    """
    import os
    import uuid
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_name

        # Parse headers
        header_list = [h.strip() for h in headers.split("|")]
        
        # Estilizar cabeçalhos
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill(start_color="2B6CB0", end_color="2B6CB0", fill_type="solid")
        thin_border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )

        for col_idx, header in enumerate(header_list, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal='center')
            cell.border = thin_border

        # Parse e inserir dados
        if rows and rows.strip():
            row_list = rows.split(";;")
            for row_idx, row_data in enumerate(row_list, 2):
                cols = [c.strip() for c in row_data.split("|")]
                for col_idx, value in enumerate(cols, 1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=value)
                    cell.border = thin_border

        # Auto-ajustar largura das colunas
        for col in ws.columns:
            max_length = 0
            col_letter = col[0].column_letter
            for cell in col:
                try:
                    if cell.value:
                        max_length = max(max_length, len(str(cell.value)))
                except:
                    pass
            ws.column_dimensions[col_letter].width = min(max_length + 4, 50)

        # Salvar
        safe_filename = f"{filename.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.xlsx"
        filepath = os.path.join(_get_upload_dir(), safe_filename)
        wb.save(filepath)

        # Validação pós-save: confirmar que o arquivo foi realmente salvo e é válido
        if not os.path.isfile(filepath):
            return json.dumps({"error": f"Falha ao salvar arquivo: {filepath} não existe após wb.save()"})
        
        saved_size = os.path.getsize(filepath)
        with open(filepath, "rb") as check_f:
            magic = check_f.read(4)
        
        if magic != b'PK\x03\x04':
            return json.dumps({"error": f"Arquivo salvo mas não é XLSX válido (magic bytes: {magic})"})
        
        print(f"[EXCEL_GEN] Arquivo gerado: {safe_filename} | {saved_size} bytes | path: {filepath}")

        download_url = f"/api/ai/download/{safe_filename}"
        return json.dumps({
            "success": True,
            "filename": safe_filename,
            "file_size_bytes": saved_size,
            "download_url": download_url,
            "message": f"Arquivo Excel gerado com sucesso! Link: {download_url}"
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({"error": str(e)})

def generate_pdf_document(filename: str, title: str, content: str) -> str:
    """
    Gera um arquivo PDF com o conteúdo fornecido e retorna o link para download.
    Use esta ferramenta quando o usuário pedir um documento, relatório ou contrato em PDF.
    
    Args:
        filename: Nome do arquivo sem extensão (ex: 'relatorio_mensal'). Será adicionado .pdf automaticamente.
        title: Título do documento que aparecerá no topo do PDF.
        content: Conteúdo do documento. Use '\\n' para quebras de linha. Use '## ' no início de uma linha para subtítulos. Use '- ' no início para listas.
    """
    import os
    import uuid
    try:
        from fpdf import FPDF

        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=20)
        
        # Título
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(43, 108, 176)  # Cor primária do CRM
        pdf.cell(0, 10, txt=str(title).encode('latin-1', 'replace').decode('latin-1'), border=0, ln=1, align="C")
        pdf.ln(2)
        
        # Linha decorativa
        pdf.set_draw_color(43, 108, 176)
        pdf.set_line_width(0.5)
        pdf.line(20, pdf.get_y(), 190, pdf.get_y())
        pdf.ln(6)
        
        # Data de geração
        from datetime import datetime
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(128, 128, 128)
        pdf.cell(0, 6, txt=f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}", border=0, ln=1, align="R")
        pdf.ln(5)

        # Conteúdo
        content = str(content).encode('latin-1', 'replace').decode('latin-1')
        lines = content.split("\\n") if "\\n" in content else content.split("\n")
        
        for line in lines:
            line = line.strip()
            if not line:
                pdf.ln(4)
                continue
                
            if line.startswith("## "):
                pdf.set_font("Helvetica", "B", 13)
                pdf.set_text_color(43, 108, 176)
                pdf.cell(0, 8, txt=line[3:], border=0, ln=1)
                pdf.ln(1)
            elif line.startswith("- "):
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(50, 50, 50)
                pdf.cell(5, 6, txt="-", border=0, ln=0)
                pdf.multi_cell(0, 6, txt=line[2:])
            else:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(50, 50, 50)
                pdf.multi_cell(0, 6, txt=line)

        # Rodapé
        pdf.ln(10)
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 6, txt="CRM Brasileiros no Atacama - Documento gerado automaticamente pela IA", border=0, ln=1, align="C")

        safe_filename = f"{filename.replace(' ', '_')}_{uuid.uuid4().hex[:6]}.pdf"
        filepath = os.path.join(_get_upload_dir(), safe_filename)
        pdf.output(filepath)

        download_url = f"/api/ai/download/{safe_filename}"
        return json.dumps({
            "success": True,
            "filename": safe_filename,
            "download_url": download_url,
            "message": f"PDF gerado com sucesso! Link: {download_url}"
        })
    except Exception as e:
        logger.exception("Erro ao gerar PDF")
        return json.dumps({"error": str(e)})


# List of tools to pass to Gemini
AVAILABLE_TOOLS = [
    get_database_schema, run_select_query, run_sql_write_query, 
    update_lead_status, create_task, create_lead, add_tag_to_lead,
    get_api_endpoints, call_internal_api,
    generate_excel_document, generate_pdf_document
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
    "generate_excel_document": generate_excel_document,
    "generate_pdf_document": generate_pdf_document,
}
