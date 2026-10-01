"""Charge each model invocation in template package creation workflows."""

import uuid

from sqlalchemy import update

from ...core.config import app_config
from ...database.database import AsyncSessionLocal
from ...database.models import CreditTransaction, User
from ..credits_service import CreditsService

OPERATION = "template_package_ai_call"


class PackageCreationCreditError(RuntimeError):
    def __init__(self, message, status_code=402):
        super().__init__(message)
        self.status_code = status_code


class PackageCreationService:
    """Delegate model work and debit immediately before the resolved provider runs."""

    def __init__(self, service, user_id, description, *, sessions=None):
        self.service = service
        self.user_id = user_id
        self.description = description
        self.sessions = sessions if sessions is not None else AsyncSessionLocal

    def __getattr__(self, name):
        return getattr(self.service, name)

    async def _chat_completion_for_role(self, role, **kwargs):
        return await self.service._chat_completion_for_role(
            role, **kwargs, _before_request=self._charge
        )

    async def _charge(self, settings):
        if not app_config.enable_credits_system:
            return
        if (settings.get("provider") or "").strip().lower() != "landppt":
            return
        cost = CreditsService.COSTS[OPERATION]
        try:
            async with self.sessions() as session, session.begin():
                balance = await session.scalar(
                    update(User)
                    .where(User.id == self.user_id, User.credits_balance >= cost)
                    .values(credits_balance=User.credits_balance - cost)
                    .returning(User.credits_balance)
                )
                if balance is None:
                    raise PackageCreationCreditError(
                        f"积分不足，每次 AI 调用需要 {cost} 积分；后续调用已停止"
                    )
                session.add(
                    CreditTransaction(
                        user_id=self.user_id,
                        amount=-cost,
                        balance_after=balance,
                        transaction_type="consume",
                        description=f"{self.description}（AI 调用）",
                        reference_id=f"template-package-call:{uuid.uuid4().hex}",
                    )
                )
        except PackageCreationCreditError:
            raise
        except Exception as exc:
            raise PackageCreationCreditError(
                "积分扣费失败，AI 调用未发起，请稍后重试", status_code=503
            ) from exc
