"""
Celery tasks for asynchronous ticket generation and email delivery.

Ticket processing is scheduled via transaction.on_commit() after a successful
payment so the booking/payment request never waits for PDF generation or email.
Email failures use bounded retries and never affect the booking/payment status.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import Booking, Payment, Ticket

logger = logging.getLogger(__name__)

# Bounded retry configuration for temporary email failures
EMAIL_MAX_RETRIES = 3
EMAIL_RETRY_DELAY_BASE = 60  # seconds; grows exponentially per retry


@shared_task(bind=True, max_retries=EMAIL_MAX_RETRIES, default_retry_delay=EMAIL_RETRY_DELAY_BASE)
def send_ticket_email(self, ticket_id):
    """
    Send the ticket PDF as an email attachment to the booking user.

    Retries on temporary email delivery failures with exponential backoff.
    Permanent failures are logged and the task stops retrying after max_retries.
    A failed email never changes the booking or payment status.
    """
    try:
        ticket = Ticket.objects.select_related('booking__user', 'booking__movie', 'booking__theater').get(id=ticket_id)
    except Ticket.DoesNotExist:
        logger.error('Ticket %s not found; cannot send email.', ticket_id)
        return {'error': 'ticket_not_found', 'ticket_id': ticket_id}

    booking = ticket.booking
    user = booking.user

    if not ticket.pdf_file:
        logger.error(
            'Ticket %s has no PDF file; cannot send email for booking %s.',
            ticket_id,
            booking.id,
        )
        ticket.status = Ticket.STATUS_FAILED
        ticket.last_email_error = 'No PDF file available.'
        ticket.save(update_fields=['status', 'last_email_error', 'updated_at'])
        return {'error': 'no_pdf_file', 'ticket_id': ticket_id}

    if not user.email:
        logger.error(
            'User %s has no email address; cannot send ticket for booking %s.',
            user.id,
            booking.id,
        )
        ticket.status = Ticket.STATUS_FAILED
        ticket.last_email_error = 'User has no email address.'
        ticket.save(update_fields=['status', 'last_email_error', 'updated_at'])
        return {'error': 'no_email', 'ticket_id': ticket_id}

    # Idempotency: skip if email was already sent successfully
    if ticket.email_sent:
        logger.info(
            'Ticket %s email already sent for booking %s; skipping.',
            ticket_id,
            booking.id,
        )
        return {'status': 'already_sent', 'ticket_id': ticket_id}

    subject = f'Your Booking Confirmation - {booking.movie.name}'
    context = {
        'user': user,
        'booking': booking,
        'movie': booking.movie,
        'theater': booking.theater,
        'seat': booking.seat,
        'ticket': ticket,
    }

    html_message = render_to_string('emails/ticket_confirmation.html', context)
    text_message = render_to_string('emails/ticket_confirmation.txt', context)

    email = EmailMultiAlternatives(
        subject=subject,
        body=text_message,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@bookmyseat.local'),
        to=[user.email],
    )
    email.attach_alternative(html_message, 'text/html')

    # Attach the PDF ticket
    ticket.pdf_file.open('rb')
    pdf_data = ticket.pdf_file.read()
    ticket.pdf_file.close()
    email.attach(
        f'ticket_booking_{booking.id}.pdf',
        pdf_data,
        'application/pdf',
    )

    try:
        email.send(fail_silently=False)
        ticket.email_sent = True
        ticket.last_email_error = ''
        ticket.save(
            update_fields=['email_sent', 'last_email_error', 'updated_at']
        )
        logger.info(
            'Ticket email sent for booking %s (ticket %s) to %s',
            booking.id,
            ticket.id,
            user.email,
        )
        return {'status': 'sent', 'ticket_id': ticket_id, 'email': user.email}
    except Exception as exc:
        ticket.email_attempts = (ticket.email_attempts or 0) + 1
        ticket.last_email_error = str(exc)[:500]
        ticket.save(
            update_fields=['email_attempts', 'last_email_error', 'updated_at']
        )
        logger.warning(
            'Email delivery failed for ticket %s (attempt %s/%s): %s',
            ticket_id,
            ticket.email_attempts,
            EMAIL_MAX_RETRIES,
            exc,
        )
        # Re-raise so Celery retries with backoff, up to max_retries
        raise self.retry(exc=exc, countdown=EMAIL_RETRY_DELAY_BASE * (2 ** (self.request.retries)))


@shared_task
def process_ticket_after_payment(payment_id):
    """
    Generate one ticket for each booking covered by a successfully paid
    payment. Safe to retry: existing tickets are reused.
    """
    try:
        payment = Payment.objects.select_related("user", "theater").get(
            id=payment_id
        )
    except Payment.DoesNotExist:
        logger.error("Payment %s not found; cannot process tickets.", payment_id)
        return {"error": "payment_not_found", "payment_id": payment_id}

    if payment.status != Payment.STATUS_PAID:
        logger.warning(
            "Payment %s is not paid; ticket generation skipped.", payment_id
        )
        return {
            "status": "skipped",
            "reason": "payment_not_paid",
            "payment_id": payment_id,
        }

    raw_seat_ids = payment.seat_ids or []
    if not isinstance(raw_seat_ids, list) or not raw_seat_ids:
        logger.error("Payment %s has no valid seat IDs.", payment_id)
        return {
            "status": "skipped",
            "reason": "invalid_seat_ids",
            "payment_id": payment_id,
        }

    try:
        seat_ids = list({int(seat_id) for seat_id in raw_seat_ids})
    except (TypeError, ValueError):
        logger.error("Payment %s contains invalid seat IDs.", payment_id)
        return {
            "status": "skipped",
            "reason": "invalid_seat_ids",
            "payment_id": payment_id,
        }

    bookings = list(
        Booking.objects.select_related("movie", "theater", "seat", "user")
        .filter(
            user_id=payment.user_id,
            theater_id=payment.theater_id,
            seat_id__in=seat_ids,
        )
        .order_by("id")
    )

    if not bookings:
        logger.warning(
            "No matching bookings found for paid payment %s.", payment_id
        )
        return {
            "status": "skipped",
            "reason": "no_matching_bookings",
            "payment_id": payment_id,
        }

    results = []
    for booking in bookings:
        # Confirm this specific booking's seat belongs to this payment.
        if booking.seat_id not in seat_ids:
            continue

        ticket, created = Ticket.objects.get_or_create(
            booking=booking,
            defaults={
                "verification_token": Ticket.generate_verification_token(),
                "status": Ticket.STATUS_PENDING,
            },
        )

        # Reuse a valid generated PDF; regenerate if missing or incomplete.
        file_exists = bool(ticket.pdf_file)
        if file_exists:
            try:
                file_exists = ticket.pdf_file.storage.exists(ticket.pdf_file.name)
            except (OSError, ValueError):
                file_exists = False

        if ticket.status != Ticket.STATUS_GENERATED or not file_exists:
            ticket.status = Ticket.STATUS_PENDING
            ticket.save(update_fields=["status", "updated_at"])
            success = _generate_ticket_pdf(ticket)
            ticket.refresh_from_db()

            if not success or not ticket.pdf_file:
                logger.error(
                    "Ticket PDF generation failed for booking %s.", booking.id
                )
                results.append({
                    "booking_id": booking.id,
                    "ticket_id": ticket.id,
                    "status": "failed",
                })
                continue

        if not ticket.email_sent:
            send_ticket_email.delay(ticket.id)

        results.append({
            "booking_id": booking.id,
            "ticket_id": ticket.id,
            "status": "generated" if created else "existing",
            "email_scheduled": not ticket.email_sent,
        })

    return {
        "status": "completed",
        "payment_id": payment_id,
        "tickets_processed": len(results),
        "results": results,
    }



def _generate_ticket_pdf(ticket):
    """Inline PDF generation (delegated to ticket_service for testability)."""
    from .ticket_service import generate_ticket_pdf
    return generate_ticket_pdf(ticket)


def schedule_ticket_processing(payment_id):
    """
    Schedule ticket processing after the current transaction commits.

    Safe to call both inside and outside an atomic block:
    - Inside transaction.atomic(): defers until commit via on_commit.
    - Outside: runs immediately (via apply_async which still queues to Celery).
    """
    def _schedule():
        process_ticket_after_payment.delay(payment_id)
        logger.debug(
            'Scheduled ticket processing for payment %s after commit.',
            payment_id,
        )

    transaction.on_commit(_schedule)
