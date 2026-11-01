"""Helpers for matching paid payments to individual bookings."""


def get_paid_payment_for_booking(booking):
    """Return the paid payment that includes this booking's seat, if any."""
    from .models import Payment

    payments = Payment.objects.filter(
        user_id=booking.user_id,
        theater_id=booking.theater_id,
        status=Payment.STATUS_PAID,
    ).order_by("-paid_at", "-created_at")

    for payment in payments:
        seat_ids = payment.seat_ids

        if not isinstance(seat_ids, list):
            continue

        try:
            normalized_seat_ids = {int(seat_id) for seat_id in seat_ids}
        except (TypeError, ValueError):
            continue

        if booking.seat_id in normalized_seat_ids:
            return payment

    return None
