import uuid

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import ApiKeyDep, IdempotencyKeyDep, SessionDep
from app.api.rate_limit import RateLimitDep
from app.schemas.payment import PaymentAccepted, PaymentCreate, PaymentResponse
from app.services import payments as payment_service

router = APIRouter(prefix="/api/v1/payments", tags=["payments"], dependencies=[ApiKeyDep])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PaymentAccepted,
    dependencies=[RateLimitDep],
)
async def create_payment(
    request: Request,
    payload: PaymentCreate,
    idempotency_key: IdempotencyKeyDep,
    session: SessionDep,
) -> PaymentAccepted:
    routing_key = request.app.state.settings.rabbitmq.payments_routing_key
    async with session.begin():
        payment, _ = await payment_service.create_payment(
            session,
            idempotency_key=idempotency_key,
            data=payload,
            routing_key=routing_key,
        )
    return PaymentAccepted.model_validate(payment)


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(payment_id: uuid.UUID, session: SessionDep) -> PaymentResponse:
    payment = await payment_service.get_payment(session, payment_id)
    if payment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Payment {payment_id} not found",
        )
    return PaymentResponse.model_validate(payment)
