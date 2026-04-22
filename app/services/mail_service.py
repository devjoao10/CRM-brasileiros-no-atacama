from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr
from app.config import (
    MAIL_USERNAME, 
    MAIL_PASSWORD, 
    MAIL_FROM, 
    MAIL_PORT, 
    MAIL_SERVER, 
    MAIL_FROM_NAME,
    ENVIRONMENT,
    APP_DOMAIN,
)

# Configuração Padrão do fastapi-mail
conf = ConnectionConfig(
    MAIL_USERNAME=MAIL_USERNAME,
    MAIL_PASSWORD=MAIL_PASSWORD,
    MAIL_FROM=MAIL_FROM,
    MAIL_PORT=MAIL_PORT,
    MAIL_SERVER=MAIL_SERVER,
    MAIL_FROM_NAME=MAIL_FROM_NAME,
    MAIL_STARTTLS=True,
    MAIL_SSL_TLS=False,
    USE_CREDENTIALS=True,
    VALIDATE_CERTS=ENVIRONMENT == "production"  # True em produção, False em dev local
)

mail_handler = FastMail(conf)

async def send_verification_email(email_to: str, token: str, is_lead: bool = False):
    """
    Envia um email (Double Opt-in) contendo o link mágico com o Token JWT de verificação.
    """
    domain = APP_DOMAIN
    
    # Rota GET que vamos criar para receber o clique
    verify_url = f"{domain}/api/users/verify-click?token={token}"
    
    if is_lead:
        titulo = "✈️ Acesse seus Roteiros Especiais do Atacama"
        corpo = "Para continuarmos nosso atendimento e liberarmos seus roteiros exclusivos, clique no botão abaixo para confirmar seu email."
    else:
        titulo = "Confirme seu E-mail corporativo - Brasileiros no Atacama"
        corpo = "Para ativar sua conta de equipe e acessar o CRM, clique no botão abaixo:"

    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2>Bem-vindo(a) à Brasileiros no Atacama!</h2>
        <p>{corpo}</p>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{verify_url}" 
               style="display: inline-block; padding: 15px 30px; background-color: #25D366; color: white; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 16px;">
               Verificar Meu E-mail
            </a>
        </div>
        <p style="font-size: 12px; color: #666;">Se você não solicitou este acesso, ignore esta mensagem.</p>
    </div>
    """

    message = MessageSchema(
        subject=titulo,
        recipients=[email_to],
        body=html_content,
        subtype=MessageType.html
    )

    try:
        await mail_handler.send_message(message)
    except Exception as e:
        print(f"Erro Crítico ao enviar email para {email_to}: {str(e)}")
        # Em ambiente local sem SMTP, printa no terminal pra debug!
        print(f"URL de Confirmação: {verify_url}")
