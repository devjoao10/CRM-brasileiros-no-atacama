import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
# Security
# Chave secreta fixa para não derrubar o login a cada restart do Uvicorn
SECRET_KEY = os.getenv("SECRET_KEY", "bna_secret_dev_key_2026_xyz_001_fixed")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours

# Database
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./crm_atacama.db")

# API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
API_PREFIX = "/api"
PROJECT_NAME = "CRM Brasileiros no Atacama"
VERSION = "1.0.0"
DESCRIPTION = """
CRM interno da Brasileiros no Atacama.

## Autenticação
- **Frontend**: JWT via cookie ou header `Authorization: Bearer <token>`
- **Integrações (N8N)**: Header `X-API-Key: <sua-api-key>`

## Recursos disponíveis
- **Auth**: Login, geração de API Key, dados do usuário
- **Users**: CRUD completo de usuários (admin)
"""
