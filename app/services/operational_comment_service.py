import re
from sqlalchemy.orm import Session
from typing import List, Optional
from app.repositories.operational_comment_repo import OperationalCommentRepository
from app.repositories.operational_card_repo import OperationalCardRepository
from app.repositories.operational_flow_repo import OperationalFlowRepository
from app.models.operational.notification import OperationalComment, OperationalMention
from sqlalchemy import func
from app.models.user import User


class OperationalCommentService:
    def __init__(self, db: Session):
        self.db = db
        self.comment_repo = OperationalCommentRepository(db)
        self.card_repo = OperationalCardRepository(db)
        self.flow_repo = OperationalFlowRepository(db)

    def list_comments_by_card(self, card_id: int) -> List[OperationalComment]:
        # Valida existência do card
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        return self.comment_repo.list_comments_by_card(card_id)

    def create_comment(self, card_id: int, data: dict, current_user) -> OperationalComment:
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")

        # Regra decidida: Permitir leitura e bloquear criação de novos comentários em cards arquivados.
        if card.is_archived:
            raise ValueError("Não é possível adicionar comentários a um card arquivado")

        user_id = getattr(current_user, "id", None)
        comment_data = {
            "card_id": card_id,
            "user_id": user_id,
            "content": data["content"]
        }

        comment = self.comment_repo.create_comment(comment_data)
        self.db.flush()  # Para obter o ID do comentário

        # Parse de menções @username
        mentions = self._parse_and_create_mentions(comment.id, data["content"])

        # Log de atividade
        self.flow_repo.create_activity_log({
            "card_id": card_id,
            "user_id": user_id,
            "action": "create_comment",
            "details": {
                "comment_id": comment.id,
                "mentions_count": len(mentions)
            }
        })

        self.db.commit()
        self.db.refresh(comment)
        return comment

    def list_mentions_by_comment(self, comment_id: int) -> List[OperationalMention]:
        comment = self.comment_repo.get_comment(comment_id)
        if not comment:
            raise ValueError(f"Comentário com ID {comment_id} não encontrado")
        return self.comment_repo.list_mentions_by_comment(comment_id)

    def _parse_and_create_mentions(self, comment_id: int, content: str) -> List[OperationalMention]:
        # Encontra padrões do tipo @username (letras, números, underlines)
        tokens = re.findall(r"@(\w+)", content)
        created_mentions = []
        
        # Evita duplicados no mesmo comentário
        tokens = list(set(tokens))

        for token in tokens:
            # Tenta encontrar o usuário correspondente:
            # 1. Pelo prefixo do e-mail (ex: admin@brasileirosnoatacama.com -> admin)
            # 2. Pelo nome slugificado sem espaços (ex: João Pedro -> joaopedro)
            user = self.db.query(User).filter(
                (User.email.like(f"{token}@%")) |
                (func.replace(User.nome, " ", "").ilike(token))
            ).first()

            if user:
                mention = self.comment_repo.create_mention(comment_id, user.id)
                created_mentions.append(mention)
            else:
                # Loga aviso ou registra no console da IA sobre a menção ignorada
                # (usuário não encontrado) para segurança e conformidade de negócio.
                print(f"[COMMENT MENTION WARNING] Username '@{token}' no comentário {comment_id} não corresponde a nenhum usuário ativo.")

        return created_mentions
