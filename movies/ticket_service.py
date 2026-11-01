"""
Ticket generation service.

Generates professional PDF cinema tickets with QR codes for verified bookings.
Designed to be used from Celery background tasks, not directly from request/response
views, so the booking/payment flow never waits for PDF generation or email delivery.
"""

import io
import os
import logging

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch, mm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from django.conf import settings
from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from .models import Booking, Payment, Ticket
from .payment_utils import get_paid_payment_for_booking

logger = logging.getLogger(__name__)

# Ticket dimensions (slightly smaller than a US Letter for a ticket look)
TICKET_WIDTH = 3.0 * inch
TICKET_HEIGHT = 7.0 * inch

# Accent color matching the BookMySeat branding
PRIMARY_COLOR = colors.HexColor('#1a1a2e')
ACCENT_COLOR = colors.HexColor('#16213e')
LIGHT_BG = colors.HexColor('#f8f9fa')
BORDER_COLOR = colors.HexColor('#e2e8f0')


def _draw_ticket_border(c, width, height):
    """Draw the outer ticket border with a decorative inner line."""
    c.setStrokeColor(PRIMARY_COLOR)
    c.setLineWidth(2)
    c.rect(10, 10, width - 20, height - 20, stroke=1, fill=0)
    c.setStrokeColor(ACCENT_COLOR)
    c.setLineWidth(1)
    c.rect(16, 16, width - 32, height - 32, stroke=1, fill=0)


def _draw_header(c, width, height):
    """Draw the ticket header with the cinema branding."""
    c.setFillColor(PRIMARY_COLOR)
    c.rect(16, height - 50, width - 32, 40, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont('Helvetica-Bold', 16)
    c.drawCentredString(width / 2.0, height - 32, 'BOOKMYSEAT')
    c.setFont('Helvetica', 9)
    c.drawCentredString(width / 2.0, height - 44, 'Cinema Ticket')


def _draw_title_section(c, width, y, movie_name):
    """Draw the movie title section."""
    c.setFillColor(PRIMARY_COLOR)
    c.setFont('Helvetica-Bold', 14)
    c.drawString(26, y, movie_name[:40])
    c.setFillColor(colors.HexColor('#64748b'))
    c.setFont('Helvetica', 9)
    c.drawString(26, y - 14, 'Movie')


def _draw_detail_row(c, label, value, x, y, font_size=9):
    """Draw a label/value detail row."""
    c.setFillColor(colors.HexColor('#64748b'))
    c.setFont('Helvetica-Bold', font_size)
    c.drawString(x, y, label)
    c.setFillColor(colors.HexColor('#1e293b'))
    c.setFont('Helvetica', font_size)
    c.drawString(x + 70, y, str(value))


def _generate_qr_code(verification_url):
    """Generate a QR code image and return it as an ImageReader."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(verification_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color='black', back_color='white')
    buffer = io.BytesIO()
    qr_img.save(buffer, format='PNG')
    buffer.seek(0)
    return ImageReader(buffer)


def generate_ticket_pdf(ticket):
    """
    Generate a professional PDF ticket for the given Ticket instance.

    Saves the PDF to ticket.pdf_file and updates ticket.status /
    ticket.generated_at. Returns True on success, False on failure.
    """
    booking = ticket.booking
    movie = booking.movie
    theater = booking.theater
    seat = booking.seat
    payment = get_paid_payment_for_booking(booking)

    if payment is None:
        logger.error(
            "No matching paid payment found for booking %s; ticket generation stopped.",
            booking.id,
        )
        return False

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(TICKET_WIDTH, TICKET_HEIGHT))
    width, height = TICKET_WIDTH, TICKET_HEIGHT

    try:
        _draw_ticket_border(c, width, height)
        _draw_header(c, width, height)

        y = height - 70

        # Movie title
        _draw_title_section(c, width, y, movie.name)
        y -= 34

        # Detail rows
        _draw_detail_row(c, 'Theater', theater.name, 26, y)
        y -= 16
        _draw_detail_row(c, 'Show', theater.time.strftime('%A, %d %b %Y'), 26, y)
        y -= 16
        _draw_detail_row(c, 'Time', theater.time.strftime('%I:%M %p'), 26, y)
        y -= 16
        _draw_detail_row(c, 'Seat', seat.seat_number, 26, y)
        y -= 16
        _draw_detail_row(c, 'Booking ID', str(booking.id), 26, y)
        y -= 16
        if payment:
            _draw_detail_row(c, 'Payment Ref', payment.razorpay_payment_id or payment.razorpay_order_id or 'â€”', 26, y)
            y -= 16
        _draw_detail_row(c, 'Price', f"{payment.currency} {payment.amount}" if payment else 'â€”', 26, y)
        y -= 22

        # Divider
        c.setStrokeColor(BORDER_COLOR)
        c.setLineWidth(0.5)
        c.line(26, y, width - 26, y)
        y -= 18

        # Ticket validity notice
        c.setFillColor(colors.HexColor('#dc2626'))
        c.setFont('Helvetica-Bold', 8)
        c.drawString(26, y, 'Valid only for the confirmed booking above.')
        y -= 12
        c.setFillColor(colors.HexColor('#64748b'))
        c.setFont('Helvetica', 8)
        c.drawString(26, y, 'Please present this ticket at the theater entrance.')
        y -= 26

        # QR code section
        c.setFillColor(PRIMARY_COLOR)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(26, y, 'Verify Ticket')
        y -= 14

        verification_url = ticket.verification_url()
        qr_reader = _generate_qr_code(verification_url)
        qr_size = 110
        qr_x = (width - qr_size) / 2.0
        c.drawImage(qr_reader, qr_x, y - qr_size, width=qr_size, height=qr_size)
        y -= qr_size + 14

        c.setFillColor(colors.HexColor('#64748b'))
        c.setFont('Helvetica', 8)
        c.drawCentredString(width / 2.0, y, 'Scan to verify this ticket')
        y -= 10
        c.drawCentredString(width / 2.0, y, verification_url)

        c.showPage()
        c.save()

        buffer.seek(0)
        pdf_bytes = buffer.read()
        buffer.close()

        # Store PDF bytes directly in database blob first (serverless-safe persistence)
        ticket.pdf_blob = pdf_bytes
        ticket.status = Ticket.STATUS_GENERATED
        ticket.generated_at = timezone.now()

        # Attempt to save to local FileField if filesystem is writable (local dev only)
        try:
            pdf_filename = f'ticket_booking_{booking.id}.pdf'
            ticket.pdf_file.save(pdf_filename, ContentFile(pdf_bytes), save=False)
        except Exception as file_exc:
            logger.warning(
                'Could not save ticket PDF to filesystem for booking %s (using pdf_blob): %s',
                booking.id,
                file_exc,
            )
            ticket.pdf_file = None

        ticket.save()

        logger.info(
            'Ticket PDF generated for booking %s (ticket %s)',
            booking.id,
            ticket.id,
        )
        return True

    except Exception as exc:
        logger.exception(
            'Failed to generate ticket PDF for booking %s: %s',
            booking.id,
            exc,
        )
        ticket.status = Ticket.STATUS_FAILED
        ticket.save(update_fields=['status', 'updated_at'])
        return False


