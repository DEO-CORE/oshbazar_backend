"""Бизнес-логика расчётов: периоды, тарифы, начисления, задолженность, аванс.

Инварианты журнала (LedgerEntry):
    * `amount > 0`  — увеличивает задолженность (начисление/корректировка/возврат);
    * `amount < 0`  — уменьшает её (банковский платёж);
    * баланс пользователя = Σ amount; > 0 — задолженность, < 0 — переплата (аванс).
Списание аванса в счёт нового начисления баланс не меняет (уменьшаются и задолженность,
и аванс), поэтому в журнал не попадает — оно отражается в `Charge.credit_applied`.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from apps.trade_points.models import AccessRole, TradePoint

from .models import BillingPeriod, Charge, ChargeStatus, LedgerEntry, Tariff, period_for_date

ZERO = Decimal("0")
Q2 = Decimal("0.01")

ACCESS_ORDER = [AccessRole.OWNER, AccessRole.TENANT, AccessRole.EMPLOYEE]


def money(value) -> Decimal:
    return Decimal(value).quantize(Q2, rounding=ROUND_HALF_UP)


def current_period(today: date | None = None) -> BillingPeriod:
    return period_for_date(today or timezone.localdate())


def open_periods() -> list[BillingPeriod]:
    return list(BillingPeriod.objects.filter(is_open=True).order_by("-year", "-month"))


def tariff_for_period(period: BillingPeriod) -> Tariff | None:
    """Тариф, действующий в расчётном периоде (по дате окончания периода)."""
    target = period.starts_on
    tariff = (
        Tariff.objects.filter(is_active=True, valid_from__lte=target)
        .filter(models_q_valid_to(target))
        .order_by("-valid_from")
        .first()
    )
    if tariff is None:
        tariff = Tariff.objects.filter(is_active=True).order_by("-valid_from").first()
    return tariff


def models_q_valid_to(target: date):
    from django.db.models import Q

    return Q(valid_to__isnull=True) | Q(valid_to__gte=target)


def resolve_owner(point: TradePoint):
    """Ответственный плательщик торговой точки."""
    accesses = list(
        point.user_accesses.filter(is_active=True).select_related("user").order_by("created_at")
    )
    if not accesses:
        return None
    for role in ACCESS_ORDER:
        for access in accesses:
            if access.access_role == role:
                return access.user
    return accesses[0].user


def user_balance(user, trade_point: TradePoint | None = None) -> Decimal:
    """Σ журнала операций пользователя (или по точке — тогда без аванса)."""
    entries = LedgerEntry.objects.filter(user=user)
    if trade_point is not None:
        entries = entries.filter(trade_point=trade_point)
    return entries.aggregate(total=Sum("amount"))["total"] or ZERO


def available_credit(user) -> Decimal:
    balance = user_balance(user)
    return -balance if balance < 0 else ZERO


def write_ledger(*, user, kind, amount, trade_point=None, charge=None, payment_ref=None,
                 source=None, note="") -> LedgerEntry:
    """Запись в журнал с блокировкой строки пользователя (атомарный баланс)."""
    from django.contrib.auth import get_user_model

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        balance = user_balance(user) + Decimal(amount)
        return LedgerEntry.objects.create(
            user=user,
            trade_point=trade_point,
            charge=charge,
            payment_ref=payment_ref,
            kind=kind,
            source=source or LedgerEntry.Source.MONEY,
            amount=Decimal(amount),
            balance_after=balance,
            note=note,
        )


# ---------------------------------------------------------------------------
# Начисления
# ---------------------------------------------------------------------------
def create_charge_for_reading(reading, *, actor=None, force: bool = False) -> Charge | None:
    """Сформировать начисление по одобренному показанию (ТЗ, п. 9–11)."""
    point = reading.trade_point
    period = reading.period
    meter = reading.meter

    existing = (
        Charge.objects.filter(period=period, meter=meter)
        .exclude(status=ChargeStatus.VOID)
        .first()
    )
    if existing and not force:
        return existing

    tariff = tariff_for_period(period)
    price = tariff.price if tariff else ZERO
    consumption = Decimal(reading.consumption)
    amount = money(consumption * price)
    owner = resolve_owner(point)
    prior_credit = (
        available_credit(owner)
        if owner is not None and getattr(settings, "AUTO_APPLY_CREDIT", True)
        else ZERO
    )

    charge = Charge.objects.create(
        period=period,
        trade_point=point,
        meter=meter,
        owner=owner,
        source_reading_id=reading.id,
        previous_value=reading.previous_value,
        current_value=reading.value,
        consumption=consumption,
        tariff=tariff,
        tariff_price=price,
        amount=amount,
        due_date=period.due_date,
        status=ChargeStatus.UNPAID,
    )
    write_ledger(
        user=owner or _system_user(),
        kind=LedgerEntry.Kind.CHARGE,
        amount=amount,
        trade_point=point,
        charge=charge,
        source=LedgerEntry.Source.MANUAL,
        note=f"Начисление за {period.label}",
    )

    # Аванс пользователя автоматически покрывает новое начисление (ТЗ, п. 12 —
    # логика использования переплаты согласуется с заказчиком).
    if prior_credit > ZERO:
        allocate_amount(
            owner, min(prior_credit, amount), source=LedgerEntry.Source.CREDIT, charge=charge
        )

    _notify_charge(charge)
    return charge


def _system_user():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(is_superuser=True).order_by("id").first()


def _notify_charge(charge: Charge) -> None:
    if charge.owner is None:
        return
    from apps.notifications.services import notify

    notify(
        charge.owner,
        "charge_created",
        {
            "period": charge.period.title,
            "amount": f"{charge.amount:.2f}",
            "currency": charge.tariff.currency if charge.tariff else settings.PAYMENT_CURRENCY,
            "due_date": charge.due_date.isoformat() if charge.due_date else "—",
            "point": str(charge.trade_point),
        },
    )


@transaction.atomic
def recalculate_charge(charge: Charge, *, actor=None) -> Charge:
    """Пересчёт начисления (только если ничего не оплачено)."""
    charge = Charge.objects.select_for_update().get(pk=charge.pk)
    if charge.status == ChargeStatus.VOID:
        raise ValueError("charge_void")
    if charge.paid_amount > ZERO:
        raise ValueError("charge_already_paid")
    old_amount = charge.amount
    tariff = charge.tariff or tariff_for_period(charge.period)
    charge.tariff = tariff
    charge.tariff_price = tariff.price if tariff else ZERO
    charge.amount = money(charge.consumption * charge.tariff_price)
    charge.save(update_fields=["tariff", "tariff_price", "amount", "updated_at"])
    if charge.amount != old_amount:
        write_ledger(
            user=charge.owner or _system_user(), kind=LedgerEntry.Kind.ADJUSTMENT,
            amount=charge.amount - old_amount, trade_point=charge.trade_point,
            charge=charge, source=LedgerEntry.Source.MANUAL,
            note=f"Пересчёт начисления {charge.period.label}",
        )
    return charge


def void_charge(charge: Charge, *, actor=None, reason: str = "") -> Charge:
    """Аннулирование начисления (недоступно обычному пользователю — проверяется во view)."""
    if charge.paid_amount > ZERO:
        raise ValueError("charge_already_paid")
    if charge.status == ChargeStatus.VOID:
        return charge
    charge.status = ChargeStatus.VOID
    charge.note = f"{charge.note}\nANUL: {reason}".strip()
    charge.save(update_fields=["status", "note", "updated_at"])
    write_ledger(
        user=charge.owner or _system_user(),
        kind=LedgerEntry.Kind.ADJUSTMENT,
        amount=-charge.amount,
        trade_point=charge.trade_point,
        charge=charge,
        source=LedgerEntry.Source.MANUAL,
        note=f"Аннулирование начисления {charge.period.label}: {reason}",
    )
    return charge


# ---------------------------------------------------------------------------
# Оплата
# ---------------------------------------------------------------------------
def allocate_amount(
    user,
    amount: Decimal,
    *,
    payment=None,
    trade_point: TradePoint | None = None,
    charge: Charge | None = None,
    charge_ids: list | None = None,
    source: str = LedgerEntry.Source.MONEY,
) -> dict:
    """Распределить сумму по начислениям пользователя (старейшие первыми).

    Возвращает `{"allocations": [...], "remainder": Decimal, "balance": Decimal}`.
    Остаток money-платежа формирует аванс (баланс журнала становится отрицательным).
    """
    from django.contrib.auth import get_user_model

    from apps.payments.models import PaymentAllocation

    amount = money(amount)
    if amount <= ZERO:
        return {"allocations": [], "remainder": ZERO, "balance": user_balance(user)}

    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)

        charges = (
            Charge.objects.select_for_update()
            .filter(owner=user)
            .exclude(status=ChargeStatus.VOID)
            .exclude(status=ChargeStatus.PAID)
            .order_by("created_at")
        )
        if trade_point is not None:
            charges = charges.filter(trade_point=trade_point)
        if charge is not None:
            charges = charges.filter(pk=charge.pk)
        if charge_ids:
            charges = charges.filter(pk__in=charge_ids)

        allocations = []
        remainder = amount
        for ch in charges.iterator():
            if remainder <= ZERO:
                break
            free = ch.amount - ch.paid_amount
            if free <= ZERO:
                continue
            take = min(remainder, free)
            ch.paid_amount = ch.paid_amount + take
            if source == LedgerEntry.Source.CREDIT:
                ch.credit_applied = ch.credit_applied + take
            ch.sync_status()
            if payment is not None:
                PaymentAllocation.objects.create(payment=payment, charge=ch, amount=take)
            allocations.append({"charge_id": str(ch.id), "charge": ch, "amount": take})
            remainder -= take

        payment_ref = payment.id if payment is not None else None

        if source == LedgerEntry.Source.MONEY:
            # Вся полученная сумма уменьшает баланс: распределённая часть гасит
            # начисления, остаток образует аванс.
            write_ledger(
                user=user,
                kind=LedgerEntry.Kind.PAYMENT,
                amount=-amount,
                trade_point=trade_point,
                payment_ref=payment_ref,
                source=source,
                note="Платёж" if payment is None else f"Платёж {payment.provider}",
            )

        balance = user_balance(user)

    return {"allocations": allocations, "remainder": remainder, "balance": balance}


def refund_payment(payment, *, actor=None, reason: str = "") -> dict:
    """Возврат платежа увеличивает задолженность обратно."""
    from apps.payments.models import PaymentStatus

    with transaction.atomic():
        payment = type(payment).objects.select_for_update().get(pk=payment.pk)
        if payment.status != PaymentStatus.SUCCEEDED:
            raise ValueError("payment_not_succeeded")
        for allocation in payment.allocations.select_related("charge"):
            charge = allocation.charge
            charge.paid_amount = charge.paid_amount - allocation.amount
            charge.sync_status()
            allocation.delete()
        payment.status = PaymentStatus.REFUNDED
        payment.save(update_fields=["status", "updated_at"])
        entry = write_ledger(
            user=payment.user,
            kind=LedgerEntry.Kind.REFUND,
            amount=payment.amount,
            payment_ref=payment.id,
            source=LedgerEntry.Source.MANUAL,
            note=f"Возврат платежа {payment.provider_payment_id or payment.id}: {reason}",
        )
    return {"balance": entry.balance_after}


# ---------------------------------------------------------------------------
# Отчёты по задолженности
# ---------------------------------------------------------------------------
def _period_rows(charges_qs) -> list[dict]:
    rows = (
        charges_qs.values("period_id", "period__year", "period__month")
        .annotate(
            amount=Sum("amount"),
            paid=Sum("paid_amount"),
            max_due=Max("due_date"),
            points=Count("trade_point", distinct=True),
        )
        .order_by("-period__year", "-period__month")
    )
    result = []
    for row in rows:
        amount = row["amount"] or ZERO
        paid = row["paid"] or ZERO
        period = BillingPeriod.objects.filter(pk=row["period_id"]).first()
        remaining = amount - paid
        if remaining <= ZERO:
            status = ChargeStatus.PAID
        elif paid > ZERO:
            status = ChargeStatus.PARTIALLY_PAID
        else:
            status = ChargeStatus.UNPAID
        result.append(
            {
                "period": str(row["period_id"]),
                "label": period.label if period else "",
                "title": period.title if period else "",
                "amount": str(money(amount)),
                "paid": str(money(paid)),
                "remaining": str(money(max(remaining, ZERO))),
                "due_date": row["max_due"].isoformat() if row["max_due"] else None,
                "status": status,
            }
        )
    return result


def debt_summary(user, trade_point: TradePoint | None = None) -> dict:
    """Задолженность и аванс пользователя (ТЗ, п. 11–12)."""
    charges_qs = Charge.objects.filter(owner=user).exclude(status=ChargeStatus.VOID)
    if trade_point is not None:
        charges_qs = charges_qs.filter(trade_point=trade_point)

    charged = charges_qs.aggregate(total=Sum("amount"))["total"] or ZERO
    paid = charges_qs.aggregate(total=Sum("paid_amount"))["total"] or ZERO

    balance = user_balance(user)
    debt = balance if balance > ZERO else ZERO
    credit = -balance if balance < ZERO else ZERO

    by_period = _period_rows(charges_qs)

    by_point = []
    point_rows = (
        charges_qs.values("trade_point_id", "trade_point__code", "trade_point__name")
        .annotate(amount=Sum("amount"), paid=Sum("paid_amount"))
        .order_by("trade_point__code")
    )
    for row in point_rows:
        amount = row["amount"] or ZERO
        point_paid = row["paid"] or ZERO
        by_point.append(
            {
                "trade_point": str(row["trade_point_id"]),
                "code": row["trade_point__code"],
                "name": row["trade_point__name"],
                "amount": str(money(amount)),
                "paid": str(money(point_paid)),
                "remaining": str(money(max(amount - point_paid, ZERO))),
            }
        )

    open_periods_count = sum(1 for row in by_period if Decimal(row["remaining"]) > ZERO)
    due_dates = [row["due_date"] for row in by_period if row["due_date"] and Decimal(row["remaining"]) > ZERO]

    return {
        "currency": settings.PAYMENT_CURRENCY,
        "balance": str(money(balance)),
        "debt": str(money(debt)),
        "credit": str(money(credit)),
        "total_charged": str(money(charged)),
        "total_paid": str(money(paid)),
        "open_amount": str(money(max(charged - paid, ZERO))),
        "open_periods_count": open_periods_count,
        "next_due_date": min(due_dates) if due_dates else None,
        "by_period": by_period,
        "by_trade_point": by_point,
    }


def point_summary(point: TradePoint, user) -> dict:
    """Сводка по торговую точку: показания, начисление, задолженность."""
    from apps.readings.models import MeterReading, ReadingStatus

    readings = (
        MeterReading.objects.filter(trade_point=point)
        .exclude(status=ReadingStatus.REJECTED)
        .select_related("period", "meter", "submitted_by")
    )
    latest = readings.order_by("-period__year", "-period__month", "-submitted_at").first()
    previous = None
    if latest:
        previous = readings.filter(meter=latest.meter).filter(
            Q(period__year__lt=latest.period.year)
            | Q(period__year=latest.period.year, period__month__lt=latest.period.month)
        ).order_by(
            "-period__year", "-period__month"
        ).first()

    period = current_period()
    charge = (
        Charge.objects.filter(trade_point=point, period=period)
        .exclude(status=ChargeStatus.VOID)
        .order_by("-created_at")
        .first()
    )
    if charge is None:
        charge = (
            Charge.objects.filter(trade_point=point)
            .exclude(status=ChargeStatus.VOID)
            .filter(paid_amount__lt=F("amount"))
            .order_by("created_at")
            .first()
        )

    point_debt = Charge.objects.filter(trade_point=point).exclude(status=ChargeStatus.VOID).aggregate(
        amount=Sum("amount"), paid=Sum("paid_amount")
    )
    amount = point_debt["amount"] or ZERO
    point_paid = point_debt["paid"] or ZERO

    summary = debt_summary(user)

    from apps.trade_points.serializers import MeterSerializer, TradePointSerializer

    from .serializers import ChargeSerializer

    return {
        "trade_point": TradePointSerializer(point, context={"request": None}).data,
        "period": {"id": str(period.id), "label": period.label, "title": period.title,
                   "due_date": period.due_date.isoformat()},
        "last_reading": (
            {
                "id": str(latest.id),
                "value": str(latest.value),
                "previous_value": str(latest.previous_value),
                "consumption": str(latest.consumption),
                "period": latest.period.label,
                "submitted_at": latest.submitted_at.isoformat(),
                "status": latest.status,
            }
            if latest
            else None
        ),
        "previous_reading_value": str(previous.value) if previous else None,
        "current_charge": ChargeSerializer(charge).data if charge else None,
        "point_debt": str(max(amount - point_paid, ZERO)),
        "user_debt": summary["debt"],
        "user_credit": summary["credit"],
        "meters": MeterSerializer(point.meters.filter(is_active=True), many=True).data,
    }


def consumption_history(point: TradePoint, months: int = 12) -> list[dict]:
    """История потребления по месяцам для графика (ТЗ, п. 15)."""
    from apps.readings.models import MeterReading, ReadingStatus

    readings = (
        MeterReading.objects.filter(trade_point=point)
        .exclude(status=ReadingStatus.REJECTED)
        .select_related("period")
    )
    charges = Charge.objects.filter(trade_point=point).exclude(status=ChargeStatus.VOID).select_related("period")

    history: dict[str, dict] = {}
    for reading in readings:
        key = str(reading.period_id)
        row = history.setdefault(
            key,
            {
                "period": key,
                "label": reading.period.label,
                "title": reading.period.title,
                "previous_value": ZERO,
                "current_value": ZERO,
                "consumption": ZERO,
                "tariff": None,
                "amount": ZERO,
                "paid": ZERO,
                "status": ChargeStatus.UNPAID,
                "due_date": None,
            },
        )
        row["previous_value"] += Decimal(reading.previous_value)
        row["current_value"] += Decimal(reading.value)
        row["consumption"] += Decimal(reading.consumption)

    for charge in charges:
        period_id = str(charge.period_id)
        row = history.setdefault(
            period_id,
            {
                "period": period_id,
                "label": charge.period.label,
                "title": charge.period.title,
                "previous_value": ZERO,
                "current_value": ZERO,
                "consumption": ZERO,
                "tariff": None,
                "amount": ZERO,
                "paid": ZERO,
                "status": ChargeStatus.UNPAID,
                "due_date": None,
            },
        )
        row["amount"] += charge.amount
        row["paid"] += charge.paid_amount
        row["tariff"] = str(charge.tariff_price)
        row["due_date"] = charge.due_date.isoformat() if charge.due_date else None
        if row["paid"] >= row["amount"] and row["amount"] > ZERO:
            row["status"] = ChargeStatus.PAID
        elif row["paid"] > ZERO:
            row["status"] = ChargeStatus.PARTIALLY_PAID
        else:
            row["status"] = ChargeStatus.UNPAID

    rows = sorted(history.values(), key=lambda item: item["label"], reverse=True)[:months]
    for row in rows:
        row["previous_value"] = str(row["previous_value"])
        row["current_value"] = str(row["current_value"])
        row["consumption"] = str(row["consumption"])
        row["amount"] = str(row["amount"])
        row["paid"] = str(row["paid"])
        row["remaining"] = str(max(Decimal(row["amount"]) - Decimal(row["paid"]), ZERO))
    # Хронологический порядок для графика (старые → новые).
    return list(reversed(rows))


def close_period(period: BillingPeriod, *, actor=None) -> dict:
    """Закрытие периода: фиксация начислений (ТЗ, п. 7)."""
    from apps.readings.models import MeterReading, ReadingStatus

    created = []
    missing = []
    with transaction.atomic():
        readings = MeterReading.objects.filter(period=period, status=ReadingStatus.APPROVED)
        for reading in readings.select_related("trade_point", "meter"):
            charge = create_charge_for_reading(reading, actor=actor, force=False)
            if charge is not None:
                created.append(str(charge.id))

        points_without_charge = TradePoint.objects.filter(
            is_active=True, meters__is_active=True
        ).exclude(charges__period=period).distinct()
        missing = [f"{p.code}" for p in points_without_charge]
        period.close()

    return {
        "period": period.label,
        "closed": True,
        "charges_created": created,
        "points_without_charge": missing,
    }


def consumption_history_for_user(user, months: int = 12) -> list[dict]:
    """Агрегированная история потребления по всем точкам пользователя (график)."""
    from apps.readings.models import MeterReading, ReadingStatus
    from apps.trade_points.services import accessible_trade_points

    point_ids = accessible_trade_points(user).values("id")
    reading_rows = (
        MeterReading.objects.filter(trade_point_id__in=point_ids)
        .exclude(status=ReadingStatus.REJECTED)
        .values("period_id", "period__year", "period__month")
        .annotate(
            previous_value=Sum("previous_value"),
            current_value=Sum("value"),
        )
        .order_by("-period__year", "-period__month")[:months]
    )

    charge_rows = {
        row["period_id"]: row
        for row in Charge.objects.filter(owner=user)
        .exclude(status=ChargeStatus.VOID)
        .values("period_id")
        .annotate(
            amount=Sum("amount"),
            paid=Sum("paid_amount"),
            tariff=Max("tariff_price"),
            due_date=Max("due_date"),
        )
    }

    from .models import MONTHS_RU

    history: dict[str, dict] = {}
    for row in reading_rows:
        period_id = str(row["period_id"])
        previous = row["previous_value"] or ZERO
        current = row["current_value"] or ZERO
        history[period_id] = {
            "period": period_id,
            "label": f"{row['period__year']:04d}-{row['period__month']:02d}",
            "title": f"{MONTHS_RU.get(row['period__month'], '')} {row['period__year']}",
            "previous_value": str(money(previous)),
            "current_value": str(money(current)),
            "consumption": str(money(max(current - previous, ZERO))),
            "tariff": None,
            "amount": ZERO,
            "paid": ZERO,
            "due_date": None,
            "status": ChargeStatus.UNPAID,
        }

    for period_id, row in charge_rows.items():
        key = str(period_id)
        amount = row["amount"] or ZERO
        paid = row["paid"] or ZERO
        if paid <= ZERO:
            charge_status = ChargeStatus.UNPAID
        elif paid < amount:
            charge_status = ChargeStatus.PARTIALLY_PAID
        else:
            charge_status = ChargeStatus.PAID
        entry = history.get(key)
        if entry is None:
            period = BillingPeriod.objects.filter(pk=period_id).first()
            entry = {
                "period": key,
                "label": period.label if period else "",
                "title": period.title if period else "",
                "previous_value": "0",
                "current_value": "0",
                "consumption": "0",
                "tariff": None,
                "amount": ZERO,
                "paid": ZERO,
                "due_date": None,
                "status": ChargeStatus.UNPAID,
            }
            history[key] = entry
        entry["amount"] = amount
        entry["paid"] = paid
        entry["tariff"] = str(row["tariff"]) if row["tariff"] is not None else None
        entry["due_date"] = row["due_date"].isoformat() if row["due_date"] else None
        entry["status"] = charge_status

    rows = sorted(history.values(), key=lambda item: item["label"], reverse=True)[:months]
    for row in rows:
        amount = Decimal(row["amount"])
        paid = Decimal(row["paid"])
        row["amount"] = str(amount)
        row["paid"] = str(paid)
        row["remaining"] = str(max(amount - paid, ZERO))
        row["status"] = str(row["status"])
    return list(reversed(rows))
