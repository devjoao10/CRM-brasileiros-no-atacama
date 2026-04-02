import json
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
import google.generativeai as genai
from google.generativeai import protos as genai_protos
from google.protobuf.struct_pb2 import Struct

from app.config import GEMINI_API_KEY
from app.auth import get_current_user
from app.models.user import User
from app.services.ai_tools import AVAILABLE_TOOLS, TOOL_FUNCTIONS

router = APIRouter(prefix="/api/ai", tags=["Assistente IA"])

# Inicializa as credenciais da Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

class ChatMessage(BaseModel):
    role: str
    content: str
    
class ChatRequest(BaseModel):
    messages: List[ChatMessage]

SYSTEM_PROMPT = """
Você é a inteligência artificial do sistema "CRM Brasileiros no Atacama" promovida a modo DEUS (God Mode).
Seu papel é ajudar o usuário administrando leads, viagens, tarefas, equipes, etc.

COMO COMPLETAR SUAS TAREFAS:
Você tem duas abordagens principais para alterar ou consultar o sistema:
1. **Agentic API (PREFERENCIAL)**: Você já tem mapeado em sua memória (veja abaixo) todas as rotas /api/ disponíveis. Use a ferramenta `call_internal_api` para simular requisições HTTP para essas rotas. Use preferencialmente as rotas oficias (ex: `POST /api/leads`) porque elas aplicam validações de campo, gatilham automações N8N e garantem a integridade dos dados!
2. **God Mode SQL**: Se a rota não existir, não funcionar, ou for uma alteração em massa complexa, você tem as ferramentas `run_select_query` e `run_sql_write_query` para modificar **qualquer** tabela diretamente no banco SQLite (`leads`, `tags`, `users`, etc). Use `get_database_schema` se não souber os campos.
3. Outros helpers fáceis: `create_lead`, `add_tag_to_lead`.
"""

@router.post("/chat", summary="Conversar com a IA do sistema")
def ai_chat(
    request: ChatRequest,
    req: Request,
    current_user: User = Depends(get_current_user)
):
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY não configurada no servidor.")
    
    # Injetat dinamicamente a arquitetura de rotas da aplicação dentro do CÉREBRO (prompt perpétuo) da IA
    openapi_schema = req.app.openapi()
    endpoints_info = []
    for path, methods in openapi_schema.get("paths", {}).items():
        if path.startswith("/api/"):
            for method, info in methods.items():
                endpoints_info.append(f"[{method.upper()}] {path} - {info.get('summary', '')}")
                
    dynamic_system_prompt = SYSTEM_PROMPT + "\n\n### LISTA DE ENDPOINTS (APIs LOCAIS) QUE VOCÊ PODE CHAMAR VIA 'call_internal_api':\n" + "\n".join(endpoints_info)
    
    # Prepara o modelo com as ferramentas
    try:
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            tools=AVAILABLE_TOOLS,
            system_instruction=dynamic_system_prompt
        )
        
        # Constrói o histórico da conversa limpo e validado para a API do Gemini
        history = []
        last_message = request.messages[-1].content
        
        if len(request.messages) > 1:
            for msg in request.messages[:-1]:
                role = "user" if msg.role == "user" else "model"
                
                # Previne a quebra da API do Gemini por envio de roles consecutivos iguais
                if history and history[-1]["role"] == role:
                    history[-1]["parts"][0]["text"] += f"\n\n{msg.content}"
                else:
                    history.append({"role": role, "parts": [{"text": msg.content}]})
                    
        # Segurança: O Gemini NÃO aceita que o 'history' termine com 'user' se a próxima ação (chat.send_message) vier do 'user'
        if history and history[-1]["role"] == "user":
            last_popped = history.pop()
            last_message = last_popped["parts"][0]["text"] + f"\n\n{last_message}"
        
        # Inicia o chat protegido
        chat = model.start_chat(history=history)
        
        # Envia a mensagem principal
        response = chat.send_message(last_message)
        
        # Loop the function calling if necessary
        while True:
            function_call = None
            if response.candidates and response.candidates[0].content.parts:
                for part in response.candidates[0].content.parts:
                    if part.function_call:
                        function_call = part.function_call
                        break
            
            if not function_call:
                break
                
            func_name = function_call.name
            
            # Extract arguments
            args = {}
            if hasattr(function_call, 'args'):
                for key, val in function_call.args.items():
                    args[key] = val
            
            # Execute local function
            if func_name in TOOL_FUNCTIONS:
                func = TOOL_FUNCTIONS[func_name]
                try:
                    result = func(**args)
                except Exception as e:
                    result = json.dumps({"error": str(e)})
            else:
                result = json.dumps({"error": f"Função {func_name} não encontrada."})
            
            # Send result back to Gemini
            # Converte o dict de resposta para o objeto Struct esperado pelo protobuf
            response_struct = Struct()
            response_struct.update({"result": result})
            
            function_response = genai_protos.Part(
                function_response=genai_protos.FunctionResponse(
                    name=func_name,
                    response=response_struct
                )
            )
            response = chat.send_message(function_response)
            
        # Obter texto final com segurança (se o texto não existir pode ser que a IA apenas enviou chamadas seguidas, mas geralmente envia texto final)
        final_text = ""
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if part.text:
                    final_text += part.text
        
        return {
            "role": "model",
            "content": final_text
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Erro interno da IA: {str(e)}")
