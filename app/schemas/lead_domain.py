"""
Contratos de entrada e saida de `POST /api/v2/lead-domain/handoff`.

Fronteira de validacao do endpoint chamado pelo Conversas (WP BIA-V2, Fase 6).
Tudo que passa daqui e considerado bem formado pelo router.
"""
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HandoffRequest(BaseModel):
    """Corpo de `POST /api/v2/lead-domain/handoff`.

    `extra="forbid"` de proposito: o contrato HTTP com o Conversas tem
    exatamente estes tres campos. Ignorar um campo desconhecido faria o
    remetente acreditar que ele foi honrado (por exemplo mandar um `reason`
    que ninguem grava, ou errar `target_user` no lugar de `target_user_id` e
    receber 200). Recusar e ruidoso e acionavel; ignorar e silencioso.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        ...,
        description=(
            "Chave de idempotencia do handoff, UUID (RFC 4122). Repetir o mesmo "
            "event_id com os mesmos parametros devolve o mesmo resultado sem "
            "efeito duplicado."
        ),
    )
    lead_id: int = Field(..., gt=0, description="ID do lead que muda de responsavel.")
    target_user_id: int = Field(
        ..., gt=0, description="ID do usuario que passa a ser responsavel pelo lead."
    )

    @field_validator("event_id")
    @classmethod
    def _canonicalizar_event_id(cls, valor: str) -> str:
        """Exige UUID de verdade e devolve a forma CANONICA (minuscula, com hifens).

        Sem canonicalizar, `"A0EEBC99-..."` (maiusculo) e a mesma string sem
        hifen sao UUIDs iguais-no-valor e diferentes-na-string: o indice UNIQUE
        e a re-consulta de idempotencia comparam STRING, entao a colisao real
        escapa e o handoff e aplicado duas vezes. Mesma regra, e pelo mesmo
        motivo, que `validar_event_id` aplica do lado do Conversas — reescrita
        aqui porque os dois pacotes se chamam `app` e nao podem se importar.

        NAO existe normalizacao de vazio para ausente aqui, e isso e
        deliberado: `""`, `"   "` ou qualquer lixo levantam, e o FastAPI
        devolve 422. Converter um `event_id` informado-mas-vazio em "ausente"
        reabriria a colisao de chave de idempotencia que o plano da V2 fechou
        explicitamente.
        """
        try:
            return str(uuid.UUID(valor))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("event_id deve ser um UUID valido (RFC 4122)") from exc


class HandoffResponse(BaseModel):
    """Resultado do handoff. `replayed` distingue aplicacao de repeticao."""

    lead_id: int = Field(..., description="ID do lead afetado.")
    target_user_id: int = Field(..., description="ID do responsavel resultante.")
    event_id: str = Field(..., description="Chave de idempotencia, na forma canonica.")
    replayed: bool = Field(
        ...,
        description=(
            "False na primeira aplicacao deste event_id; True quando ele ja "
            "tinha sido processado com os mesmos parametros."
        ),
    )
