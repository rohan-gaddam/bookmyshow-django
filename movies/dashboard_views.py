import csv
import io
from datetime import date, datetime, time, timedelta
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.db.models.functions import ExtractHour, TruncDate, TruncMonth, TruncWeek, TruncYear
from django.http import StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone

from .models import Booking, Movie, Payment, Theater


def admin_dashboard_access(view_func):
    """Require staff access and explicit booking/payment viewing permissions."""
    @login_required(login_url="/admin/login/")
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if (
            not user.is_staff
            or not user.has_perm("movies.view_booking")
            or not user.has_perm("movies.view_payment")
        ):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return _wrapped


def _parse_date_range(request):
    """Return a validated inclusive date range, defaulting to the last 30 days."""
    today = timezone.localdate()
    default_start = today - timedelta(days=29)
    errors = []

    try:
        start_date = date.fromisoformat(request.GET.get("start_date", ""))
    except (TypeError, ValueError):
        start_date = default_start

    try:
        end_date = date.fromisoformat(request.GET.get("end_date", ""))
    except (TypeError, ValueError):
        end_date = today

    # An absent date uses the default; an invalid supplied date is reported.
    raw_start = request.GET.get("start_date")
    raw_end = request.GET.get("end_date")
    if raw_start:
        try:
            date.fromisoformat(raw_start)
        except (TypeError, ValueError):
            errors.append("The start date is invalid.")
    if raw_end:
        try:
            date.fromisoformat(raw_end)
        except (TypeError, ValueError):
            errors.append("The end date is invalid.")

    if start_date > end_date:
        errors.append("The start date must not be after the end date.")
        start_date, end_date = default_start, today

    current_tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(
        datetime.combine(start_date, time.min), current_tz
    )
    end_exclusive = timezone.make_aware(
        datetime.combine(end_date + timedelta(days=1), time.min), current_tz
    )
    return start_date, end_date, start_dt, end_exclusive, errors


def _revenue_total(start_dt, end_exclusive):
    result = Payment.objects.filter(
        status=Payment.STATUS_PAID,
        paid_at__gte=start_dt,
        paid_at__lt=end_exclusive,
    ).aggregate(total=Sum("amount"))
    return result["total"] or 0


def _period_bounds(period, today):
    if period == "day":
        start = today
        end = today + timedelta(days=1)
    elif period == "week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = today.replace(day=1)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:
        start = today.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)

    current_tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(start, time.min), current_tz),
        timezone.make_aware(datetime.combine(end, time.min), current_tz),
    )


@admin_dashboard_access
def admin_dashboard(request):
    start_date, end_date, start_dt, end_exclusive, errors = _parse_date_range(request)
    today = timezone.localdate()

    bookings = Booking.objects.filter(
        booked_at__gte=start_dt,
        booked_at__lt=end_exclusive,
    )
    paid_payments = Payment.objects.filter(
        status=Payment.STATUS_PAID,
        paid_at__gte=start_dt,
        paid_at__lt=end_exclusive,
    )

    revenue_cards = {}
    for label, period in (
        ("today", "day"),
        ("this_week", "week"),
        ("this_month", "month"),
        ("this_year", "year"),
    ):
        period_start, period_end = _period_bounds(period, today)
        revenue_cards[label] = _revenue_total(period_start, period_end)

    booking_trend = list(
        bookings.annotate(period=TruncDate("booked_at"))
        .values("period")
        .annotate(total=Count("id"))
        .order_by("period")
    )

    revenue_trend = list(
        paid_payments.annotate(period=TruncDate("paid_at"))
        .values("period")
        .annotate(revenue=Sum("amount"), transactions=Count("id"))
        .order_by("period")
    )

    occupancy = list(
        Theater.objects.filter(time__gte=start_dt, time__lt=end_exclusive)
        .annotate(
            total_seats=Count("seats", distinct=True),
            booked_seats=Count("booking", distinct=True),
        )
        .values(
            "id",
            "name",
            "movie__name",
            "time",
            "total_seats",
            "booked_seats",
        )
        .order_by("-time")[:100]
    )
    for row in occupancy:
        total = row["total_seats"] or 0
        row["occupancy_percent"] = round(
            (row["booked_seats"] * 100.0 / total), 2
        ) if total else 0

    top_movies = list(
        bookings.values("movie__name")
        .annotate(total_bookings=Count("id"))
        .order_by("-total_bookings", "movie__name")[:10]
    )

    top_theaters = list(
        bookings.values("theater__name")
        .annotate(total_bookings=Count("id"))
        .order_by("-total_bookings", "theater__name")[:10]
    )

    peak_hours = list(
        bookings.annotate(hour=ExtractHour("booked_at"))
        .values("hour")
        .annotate(total=Count("id"))
        .order_by("hour")
    )

    user_growth = list(
        __import__("django.contrib.auth", fromlist=["get_user_model"])
        .get_user_model()
        .objects.filter(date_joined__gte=start_dt, date_joined__lt=end_exclusive)
        .annotate(period=TruncDate("date_joined"))
        .values("period")
        .annotate(total=Count("id"))
        .order_by("period")
    )

    # This uses updated_at as the best available proxy for when payment
    # status was last changed. The model has no dedicated refund timestamp.
    payment_status_stats = list(
        Payment.objects.filter(
            updated_at__gte=start_dt,
            updated_at__lt=end_exclusive,
        )
        .values("status")
        .annotate(count=Count("id"), amount=Sum("amount"))
        .order_by("status")
    )

    context = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "date_errors": errors,
        "revenue_cards": revenue_cards,
        "selected_booking_count": bookings.count(),
        "selected_revenue": _revenue_total(start_dt, end_exclusive),
        "booking_trend": booking_trend,
        "revenue_trend": revenue_trend,
        "occupancy": occupancy,
        "top_movies": top_movies,
        "top_theaters": top_theaters,
        "peak_hours": peak_hours,
        "user_growth": user_growth,
        "payment_status_stats": payment_status_stats,
    }
    return render(request, "admin_dashboard.html", context)


class Echo:
    """Minimal CSV writer target used for streaming output."""
    def write(self, value):
        return value


@admin_dashboard_access
def admin_dashboard_csv(request):
    start_date, end_date, start_dt, end_exclusive, errors = _parse_date_range(request)
    if errors:
        raise PermissionDenied("Please correct the date range before exporting.")

    report = request.GET.get("report", "bookings")
    pseudo_buffer = Echo()
    writer = csv.writer(pseudo_buffer)

    def generate_rows():
        if report == "bookings":
            yield writer.writerow([
                "Booking ID", "Booked At", "Username", "Movie", "Theater",
                "Seat", "Attended",
            ])
            rows = (
                Booking.objects.filter(
                    booked_at__gte=start_dt,
                    booked_at__lt=end_exclusive,
                )
                .select_related("user", "movie", "theater", "seat")
                .order_by("booked_at")
                .iterator(chunk_size=2000)
            )
            for item in rows:
                yield writer.writerow([
                    item.id, item.booked_at, item.user.get_username(),
                    item.movie.name, item.theater.name, item.seat.seat_number,
                    item.attended,
                ])

        elif report == "revenue":
            yield writer.writerow(["Date", "Revenue", "Paid Transactions"])
            rows = (
                Payment.objects.filter(
                    status=Payment.STATUS_PAID,
                    paid_at__gte=start_dt,
                    paid_at__lt=end_exclusive,
                )
                .annotate(period=TruncDate("paid_at"))
                .values("period")
                .annotate(revenue=Sum("amount"), transactions=Count("id"))
                .order_by("period")
                .iterator()
            )
            for item in rows:
                yield writer.writerow([
                    item["period"], item["revenue"], item["transactions"],
                ])

        elif report == "movies":
            yield writer.writerow(["Movie", "Bookings"])
            rows = (
                Booking.objects.filter(
                    booked_at__gte=start_dt,
                    booked_at__lt=end_exclusive,
                )
                .values("movie__name")
                .annotate(total=Count("id"))
                .order_by("-total")
                .iterator()
            )
            for item in rows:
                yield writer.writerow([item["movie__name"], item["total"]])

        elif report == "theaters":
            yield writer.writerow(["Theater", "Bookings"])
            rows = (
                Booking.objects.filter(
                    booked_at__gte=start_dt,
                    booked_at__lt=end_exclusive,
                )
                .values("theater__name")
                .annotate(total=Count("id"))
                .order_by("-total")
                .iterator()
            )
            for item in rows:
                yield writer.writerow([item["theater__name"], item["total"]])

        elif report == "payments":
            yield writer.writerow([
                "Payment ID", "Created At", "Updated At", "Username",
                "Theater", "Amount", "Currency", "Status", "Order ID",
                "Payment ID from Gateway",
            ])
            rows = (
                Payment.objects.filter(
                    updated_at__gte=start_dt,
                    updated_at__lt=end_exclusive,
                )
                .select_related("user", "theater")
                .order_by("updated_at")
                .iterator(chunk_size=2000)
            )
            for item in rows:
                yield writer.writerow([
                    item.id, item.created_at, item.updated_at,
                    item.user.get_username(), item.theater.name,
                    item.amount, item.currency, item.status,
                    item.razorpay_order_id, item.razorpay_payment_id,
                ])

        elif report == "users":
            from django.contrib.auth import get_user_model
            User = get_user_model()
            yield writer.writerow(["User ID", "Username", "Email", "Joined At"])
            rows = (
                User.objects.filter(
                    date_joined__gte=start_dt,
                    date_joined__lt=end_exclusive,
                )
                .order_by("date_joined")
                .values_list("id", "username", "email", "date_joined")
                .iterator(chunk_size=2000)
            )
            for item in rows:
                yield writer.writerow(item)

        else:
            yield writer.writerow(["Error"])
            yield writer.writerow(["Unsupported report type"])

    response = StreamingHttpResponse(
        generate_rows(),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = (
        'attachment; filename="bookmyshow_{}_{}_to_{}.csv"'.format(
            report, start_date.isoformat(), end_date.isoformat()
        )
    )
    return response
