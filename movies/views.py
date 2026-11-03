from django.views.decorators.csrf import csrf_exempt
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
import json
import logging
import os
import hmac
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation
from django.utils.crypto import constant_time_compare
import razorpay
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
import re
import time
from urllib.parse import parse_qs, urlsplit

from django.contrib import messages
from django.db.models import Avg, Count, F, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, Http404, JsonResponse, HttpResponse
from django.utils.encoding import escape_uri_path
from .models import Movie, Theater, Seat, Booking, Genre, Language, Review, ReviewReport, SeatReservation, Payment, PaymentAttempt, Ticket
from .payment_utils import get_paid_payment_for_booking
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from . import tasks
from .ticket_service import generate_ticket_pdf

logger = logging.getLogger(__name__)


MOVIES_PER_PAGE = 12

VALID_SORTS = ('popularity', 'newest', 'rating', 'price')


YOUTUBE_VIDEO_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]{11}$')
YOUTUBE_HOSTS = {
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
    'youtu.be',
    'www.youtu.be',
    'youtube-nocookie.com',
    'www.youtube-nocookie.com',
}
YOUTUBE_EMBED_URL = 'https://www.youtube-nocookie.com/embed/{video_id}'
YOUTUBE_WATCH_URL = 'https://www.youtube.com/watch?v={video_id}'


def extract_youtube_video_id(url):
    if not isinstance(url, str):
        return None

    url = url.strip()
    if not url or any(character.isspace() for character in url):
        return None

    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = (parsed.hostname or '').lower()
    except ValueError:
        return None

    if parsed.scheme.lower() not in {'http', 'https'}:
        return None
    if parsed.username or parsed.password:
        return None
    if port not in {None, 80, 443}:
        return None
    if host not in YOUTUBE_HOSTS:
        return None

    path = parsed.path
    if host in {'youtu.be', 'www.youtu.be'}:
        candidate = path[1:] if path.startswith('/') else None
        if candidate and candidate.endswith('/'):
            candidate = candidate[:-1]
        if not candidate or '/' in candidate:
            return None
        return candidate if YOUTUBE_VIDEO_ID_PATTERN.match(candidate) else None

    if path in {'/watch', '/watch/'}:
        video_ids = parse_qs(parsed.query, keep_blank_values=True).get('v', [])
        if len(video_ids) != 1:
            return None
        candidate = video_ids[0]
        return candidate if YOUTUBE_VIDEO_ID_PATTERN.match(candidate) else None

    for prefix in ('/embed/', '/shorts/'):
        if path.startswith(prefix):
            candidate = path[len(prefix):]
            if candidate.endswith('/'):
                candidate = candidate[:-1]
            if not candidate or '/' in candidate:
                return None
            return (
                candidate
                if YOUTUBE_VIDEO_ID_PATTERN.match(candidate)
                else None
            )

    return None


def get_youtube_embed_url(url):
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None
    return YOUTUBE_EMBED_URL.format(video_id=video_id)


def get_youtube_watch_url(url):
    video_id = extract_youtube_video_id(url)
    if not video_id:
        return None
    return YOUTUBE_WATCH_URL.format(video_id=video_id)


def get_additional_posters(movie):
    main_image_name = movie.image.name if movie.image else ''
    seen_image_names = {main_image_name} if main_image_name else set()
    additional_posters = []

    for poster in movie.posters.all():
        image = poster.image
        if not image or not image.name:
            continue
        if image.name in seen_image_names:
            continue
        seen_image_names.add(image.name)
        if not image.storage.exists(image.name):
            continue
        additional_posters.append(poster)

    return additional_posters


def _parse_int_param(value):
    """Safely parse an integer query parameter. Returns None for invalid input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_date_param(value):
    """Safely parse a YYYY-MM-DD date query parameter. Returns None for invalid input."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except ValueError:
        return None


def _parse_decimal_param(value):
    """Safely parse a decimal query parameter. Returns None for invalid input."""
    if not value:
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _filter_movies(queryset, request):
    """Apply all movie discovery filters using the Django ORM.

    Filters are combined, invalid values are ignored safely, and
    distinct() avoids duplicate rows from joined relationships.
    """
    search_query = request.GET.get('search', '').strip()
    if search_query:
        queryset = queryset.filter(name__icontains=search_query)

    genre_id = _parse_int_param(request.GET.get('genre'))
    if genre_id:
        queryset = queryset.filter(genres__id=genre_id)

    language_id = _parse_int_param(request.GET.get('language'))
    if language_id:
        queryset = queryset.filter(languages__id=language_id)

    city = request.GET.get('city', '').strip()
    if city:
        queryset = queryset.filter(theaters__city__iexact=city)

    theater_id = _parse_int_param(request.GET.get('theater'))
    if theater_id:
        queryset = queryset.filter(theaters__id=theater_id)

    release_date = _parse_date_param(request.GET.get('release_date'))
    if release_date:
        queryset = queryset.filter(release_date=release_date)

    min_rating = _parse_decimal_param(request.GET.get('min_rating'))
    if min_rating is not None:
        queryset = queryset.filter(rating__gte=min_rating)

    show_date = _parse_date_param(request.GET.get('show_date'))
    if show_date:
        # Theater.time is a DateTimeField; match show timings on a date.
        queryset = queryset.filter(theaters__time__date=show_date)

    return queryset.distinct(), search_query


def _sort_movies(queryset, sort):
    """Order the movie queryset by the requested sort key."""
    if sort == 'popularity':
        # Popularity is derived from booking records.
        return queryset.annotate(
            booking_count=Count('booking', distinct=True)
        ).order_by('-booking_count', '-rating', 'name')
    if sort == 'newest':
        return queryset.order_by('-release_date', 'name')
    if sort == 'rating':
        return queryset.order_by('-rating', 'name')
    if sort == 'price':
        # Ticket price is a single global setting
        # (settings.RAZORPAY_TICKET_PRICE), so every movie has the same
        # price and price alone cannot reorder movies. A per-movie price
        # field would require changing payment amounts, which is out of
        # scope. Apply a deterministic, useful tie-break order instead.
        return queryset.order_by('-rating', 'name')
    return queryset.order_by('name')


def _get_recommended_movies(request, limit=6, exclude_movie=None):
    """Recommend movies from booking history and recently viewed movies.

    Preferences are inferred from the authenticated user's attended
    bookings and from recently viewed movie ids stored in the session.
    Falls back to popular, highly rated movies for users with no history.
    Never returns the movie currently being viewed, and never returns
    duplicates. No sensitive data is stored in the session.
    """
    preference_ids = set()
    if request.user.is_authenticated:
        preference_ids.update(
            Booking.objects.filter(
                user=request.user, attended=True
            ).values_list('movie_id', flat=True)
        )

    recently_viewed = request.session.get('recently_viewed', [])
    if not isinstance(recently_viewed, list):
        recently_viewed = []
    preference_ids.update(
        mid for mid in recently_viewed
        if _parse_int_param(mid) is not None
    )

    if exclude_movie is not None:
        preference_ids.discard(exclude_movie.id)

    if preference_ids:
        preferences = Movie.objects.filter(id__in=preference_ids)
        genre_ids = set(
            preferences.values_list('genres__id', flat=True).distinct()
        )
        language_ids = set(
            preferences.values_list('languages__id', flat=True).distinct()
        )
        if genre_ids or language_ids:
            queryset = (
                Movie.objects.exclude(id__in=preference_ids)
            )
            if exclude_movie is not None:
                queryset = queryset.exclude(id=exclude_movie.id)
            queryset = (
                queryset.filter(
                    Q(genres__id__in=genre_ids)
                    | Q(languages__id__in=language_ids)
                )
                .distinct()
                .annotate(
                    relevance=Count(
                        'genres',
                        filter=Q(genres__id__in=genre_ids),
                        distinct=True,
                    )
                    + Count(
                        'languages',
                        filter=Q(languages__id__in=language_ids),
                        distinct=True,
                    ),
                    booking_count=Count('booking', distinct=True),
                )
                .order_by('-relevance', '-booking_count', '-rating', 'name')[:limit]
            )
            return list(queryset)

    # Fallback for new users or when no history-based match exists:
    # popular, highly rated movies they have not booked or viewed yet.
    queryset = Movie.objects.all()
    if exclude_movie is not None:
        queryset = queryset.exclude(id=exclude_movie.id)
    if preference_ids:
        queryset = queryset.exclude(id__in=preference_ids)
    return list(
        queryset.annotate(
            booking_count=Count('booking', distinct=True)
        ).order_by('-booking_count', '-rating', 'name')[:limit]
    )


def movie_list(request):
    base_queryset = Movie.objects.all()
    movies, search_query = _filter_movies(base_queryset, request)

    sort = request.GET.get('sort', '').strip()
    if sort not in VALID_SORTS:
        sort = ''
    movies = _sort_movies(movies, sort)
    movies = movies.prefetch_related('genres', 'languages')

    paginator = Paginator(movies, MOVIES_PER_PAGE)

    # Total matching-movie count reflecting every active search/filter param.
    total_count = paginator.count
    page_number = request.GET.get('page')
    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    # Preserve existing Trending Movies and Recent Releases behavior:
    # both sections reflect the current search query only.
    trending_query = Movie.objects.all()
    recent_query = Movie.objects.filter(release_date__isnull=False)
    if search_query:
        trending_query = trending_query.filter(name__icontains=search_query)
        recent_query = recent_query.filter(name__icontains=search_query)
    trending_movies = trending_query.order_by('-rating', 'name')[:6]
    recent_releases = recent_query.order_by('-release_date', 'name')[:6]

    # Dropdown values populated from the database.
    genres = Genre.objects.only('id', 'name')
    languages = Language.objects.only('id', 'name')
    theaters = Theater.objects.only('id', 'name', 'city')
    cities = (
        Theater.objects.exclude(city__isnull=True)
        .exclude(city='')
        .values_list('city', flat=True)
        .distinct()
        .order_by('city')
    )

    # Query string without the page param, so pagination keeps filters.
    get_params = request.GET.copy()
    get_params.pop('page', None)
    querystring = get_params.urlencode()

    has_filters = bool(
        request.GET.get('genre')
        or request.GET.get('language')
        or request.GET.get('city')
        or request.GET.get('theater')
        or request.GET.get('release_date')
        or request.GET.get('min_rating')
        or request.GET.get('show_date')
        or sort
    )

    recommended_movies = _get_recommended_movies(request)

    return render(
        request,
        'movies/movie_list.html',
        {
            'movies': page_obj,
            'page_obj': page_obj,
            'paginator': paginator,
            'total_count': total_count,
            'search_query': search_query,
            'sort': sort,
            'querystring': querystring,
            'has_filters': has_filters,
            'trending_movies': trending_movies,
            'recent_releases': recent_releases,
            'genres': genres,
            'languages': languages,
            'theaters': theaters,
            'cities': cities,
            'recommended_movies': recommended_movies,
        },
    )
def theater_list(request,movie_id):
    movie = get_object_or_404(Movie,id=movie_id)
    theater=Theater.objects.filter(movie=movie)
    return render(request,'movies/theater_list.html',{'movie':movie,'theaters':theater})



@login_required(login_url='/login/')
def book_seats(request, theater_id):
    theaters = get_object_or_404(Theater, id=theater_id)

    def render_seat_selection(error=None):
        now = timezone.now()
        try:
            SeatReservation.objects.filter(
                seat__theater=theaters, expires_at__lte=now
            ).delete()
        except OperationalError:
            pass

        seats = list(
            Seat.objects.filter(theater=theaters).select_related('theater')
        )
        seats.sort(
            key=lambda seat: (
                ''.join(c for c in seat.seat_number if not c.isdigit()).lower(),
                int(''.join(c for c in seat.seat_number if c.isdigit()) or 0),
                seat.seat_number,
            )
        )

        active_reservations = {
            reservation.seat_id: reservation
            for reservation in SeatReservation.objects.filter(
                seat__theater=theaters,
                expires_at__gt=now,
            ).select_related('user')
        }

        for seat in seats:
            seat.seat_reservation = active_reservations.get(seat.id)

        user_active_reservations = [
            res for res in active_reservations.values()
            if request.user.is_authenticated and res.user_id == request.user.id
        ]
        has_user_reservations = len(user_active_reservations) > 0

        earliest_expiry = None
        remaining_time_str = "02:00"
        if has_user_reservations:
            earliest_expiry = min(res.expires_at for res in user_active_reservations)
            rem = max(0, int((earliest_expiry - now).total_seconds()))
            remaining_time_str = f"{rem // 60:02d}:{rem % 60:02d}"

        return render(
            request,
            'movies/seat_selection.html',
            {
                'theaters': theaters,
                'seats': seats,
                'error': error,
                'seat_reservations': active_reservations,

                'current_time': now,
                'user_reservations': has_user_reservations,
                'reservation_expires_at': earliest_expiry,
                'remaining_time_str': remaining_time_str,
            },
        )

    if request.method != 'POST':
        return render_seat_selection()

    raw_ids = request.POST.getlist('seats')
    if not raw_ids:
        return render_seat_selection(
            "Please select at least one available seat."
        )

    try:
        selected_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return render_seat_selection(
            "Invalid seat selection. Please select your seats again."
        )

    if len(selected_ids) != len(set(selected_ids)):
        return render_seat_selection(
            "Duplicate seats were submitted. Please select again."
        )

    now = timezone.now()
    expires_at = now + timezone.timedelta(minutes=2)

    try:
        for attempt in range(5):
            try:
                with transaction.atomic():
                    # Expired records must be removed before a new reservation
                    # can use the seat's unique reservation slot.
                    SeatReservation.objects.filter(expires_at__lte=now).delete()

                    seats = list(
                        Seat.objects.filter(
                            theater=theaters,
                            id__in=selected_ids,
                        ).order_by('id')
                    )

                    if len(seats) != len(selected_ids):
                        raise ValueError(
                            "One or more selected seats do not belong to this theater."
                        )

                    unavailable = [
                        seat.seat_number for seat in seats if seat.is_booked
                    ]
                    if unavailable:
                        raise ValueError(
                            "These seats are already booked: "
                            + ", ".join(unavailable)
                            + ". Please select again."
                        )

                    active_other_reservations = SeatReservation.objects.filter(
                        seat_id__in=selected_ids,
                        expires_at__gt=now,
                    ).exclude(user=request.user).select_related('seat')

                    reserved_by_others = list(
                        active_other_reservations.values_list(
                            'seat__seat_number', flat=True
                        )
                    )
                    if reserved_by_others:
                        raise ValueError(
                            "These seats are temporarily reserved by another user: "
                            + ", ".join(reserved_by_others)
                            + ". Please choose different seats."
                        )

                    # Keep this user's selection synchronized: release seats they
                    # previously held for this theater but have now deselected.
                    SeatReservation.objects.filter(
                        user=request.user,
                        seat__theater=theaters,
                        expires_at__gt=now,
                    ).exclude(seat_id__in=selected_ids).delete()

                    for seat in seats:
                        reservation, created = SeatReservation.objects.get_or_create(
                            seat=seat,
                            defaults={
                                'user': request.user,
                                'expires_at': expires_at,
                            },
                        )
                        if not created:
                            if reservation.user_id != request.user.id:
                                if reservation.expires_at > now:
                                    raise ValueError(
                                        "Seat {} was just reserved by another user. "
                                        "Please select again.".format(seat.seat_number)
                                    )
                                # Reclaiming an expired reservation starts a new period.
                                reservation.user = request.user
                                reservation.expires_at = expires_at
                                reservation.save(update_fields=['user', 'expires_at'])
                            # If this user already owns an active reservation,
                            # preserve its original expiry instead of extending it.

                break
            except OperationalError as exc:
                if 'locked' not in str(exc).lower() or attempt == 4:
                    return render_seat_selection(
                        'The reservation system is busy. Please refresh availability '
                        'and try again.'
                    )
                time.sleep(0.05 * (attempt + 1))
    except ValueError as exc:
        return render_seat_selection(str(exc))
    except IntegrityError:
        return render_seat_selection(
            "A reservation conflict occurred. Availability has been refreshed. "
            "Please review your selection and try again."
        )

    messages.success(
        request,
        "Your seats are temporarily reserved for 2 minutes. "
        "Booking is not confirmed until the confirmation step is completed."
    )
    return render_seat_selection()

@login_required(login_url='/login/')
def create_payment_order(request, theater_id):
    """Create a Razorpay order for seats actively reserved by this user."""
    if request.method != 'POST':
        return JsonResponse(
            {'error': 'Only POST requests are supported.'},
            status=405,
        )

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return JsonResponse(
            {'error': 'Online payments are not configured yet.'},
            status=503,
        )

    theater = get_object_or_404(Theater, id=theater_id)
    raw_ids = request.POST.getlist('seats')

    if not raw_ids:
        return JsonResponse({'error': 'Select at least one reserved seat.'}, status=400)

    try:
        selected_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Invalid seat selection.'}, status=400)

    if len(selected_ids) != len(set(selected_ids)):
        return JsonResponse({'error': 'Duplicate seats were submitted.'}, status=400)

    try:
        unit_price = Decimal(str(settings.RAZORPAY_TICKET_PRICE))
        if not unit_price.is_finite() or unit_price <= 0:
            raise ValueError
        amount = unit_price * len(selected_ids)
        amount_minor = int(amount * 100)
        if amount * 100 != amount_minor:
            raise ValueError
    except (ValueError, TypeError, ArithmeticError):
        return JsonResponse({'error': 'Ticket pricing is misconfigured.'}, status=500)

    now = timezone.now()
    original_reservation_expiries = {}
    payment_expires_at = None

    try:
        with transaction.atomic():
            reservations = list(
                SeatReservation.objects.filter(
                    user=request.user,
                    seat__theater=theater,
                    seat_id__in=selected_ids,
                    expires_at__gt=now,
                ).select_related('seat').order_by('seat_id')
            )

            if len(reservations) != len(selected_ids):
                return JsonResponse(
                    {'error': 'A reservation has expired or is no longer yours. Select seats again.'},
                    status=409,
                )

            if any(reservation.seat.is_booked for reservation in reservations):
                return JsonResponse(
                    {'error': 'A selected seat is already booked.'},
                    status=409,
                )

            # Recheck that every submitted seat belongs to this theater.
            if any(reservation.seat.theater_id != theater.id for reservation in reservations):
                return JsonResponse({'error': 'Invalid theater seat selection.'}, status=400)

            # Extend only after the user starts payment; normal selection stays at 2 minutes.
            original_reservation_expiries = {
                reservation.pk: reservation.expires_at
                for reservation in reservations
            }
            payment_expires_at = now + timezone.timedelta(minutes=5)
            for reservation in reservations:
                reservation.expires_at = payment_expires_at
                reservation.save(update_fields=['expires_at'])

            payment = Payment.objects.create(
                user=request.user,
                theater=theater,
                seat_ids=sorted(selected_ids),
                amount=amount,
                currency='INR',
                status=Payment.STATUS_PENDING,
            )
            PaymentAttempt.objects.create(
                payment=payment,
                amount=amount,
                currency='INR',
                status=PaymentAttempt.STATUS_PENDING,
            )

        # Do not hold a database transaction open during the network request.
        client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
        order = client.order.create(data={
            'amount': amount_minor,
            'currency': 'INR',
            'receipt': 'movie-payment-{}'.format(payment.pk),
            'notes': {
                'payment_record_id': str(payment.pk),
                'user_id': str(request.user.pk),
                'theater_id': str(theater.pk),
            },
        })

        order_id = order.get('id')
        if not order_id:
            raise RuntimeError('Razorpay did not return an order ID.')

        payment.razorpay_order_id = order_id
        payment.save(update_fields=['razorpay_order_id', 'updated_at'])

        return JsonResponse({
            'payment_record_id': payment.pk,
            'order_id': order_id,
            'amount': amount_minor,
            'currency': 'INR',
            'key_id': settings.RAZORPAY_KEY_ID,
            'ticket_count': len(selected_ids),
            'reservation_expires_at': payment_expires_at.isoformat(),
        })

    except Exception:
        # If order creation failed, restore each reservation's original expiry.
        if payment_expires_at is not None:
            for reservation_id, original_expiry in original_reservation_expiries.items():
                SeatReservation.objects.filter(
                    pk=reservation_id,
                    user=request.user,
                    expires_at=payment_expires_at,
                ).update(expires_at=original_expiry)

        # Never return provider exception details or credentials to the browser.
        if 'payment' in locals():
            Payment.objects.filter(
                pk=payment.pk,
                status=Payment.STATUS_PENDING,
                razorpay_order_id__isnull=True,
            ).update(
                status=Payment.STATUS_FAILED,
                failure_reason='Payment order creation failed.',
            )
            PaymentAttempt.objects.filter(
                payment=payment,
                status=PaymentAttempt.STATUS_PENDING,
                razorpay_payment_id__isnull=True,
            ).update(
                status=PaymentAttempt.STATUS_FAILED,
                failure_reason='Payment order creation failed.',
            )
        return JsonResponse(
            {'error': 'Could not start payment. Check your reservation and try again.'},
            status=502,
        )

@login_required(login_url='/login/')
def checkout_dismissed(request, theater_id):
    """Record checkout dismissal without cancelling or changing payment state."""
    if request.method != 'POST':
        return JsonResponse(
            {'error': 'Only POST requests are supported.'},
            status=405,
        )

    order_id = request.POST.get('razorpay_order_id', '').strip()
    if not order_id:
        return JsonResponse({'error': 'Payment order ID is required.'}, status=400)

    payment = Payment.objects.filter(
        razorpay_order_id=order_id,
        user=request.user,
        theater_id=theater_id,
    ).first()

    if payment is None:
        return JsonResponse({'error': 'Payment record not found.'}, status=404)

    # Never downgrade paid, refund-required, refunded, failed, or cancelled
    # payments because a browser checkout window was dismissed.
    if payment.status != Payment.STATUS_PENDING:
        return JsonResponse({
            'success': True,
            'recorded': False,
            'status': payment.status,
            'message': 'Payment status was left unchanged.',
        })

    now = timezone.now()
    Payment.objects.filter(
        pk=payment.pk,
        status=Payment.STATUS_PENDING,
        checkout_dismissed_at__isnull=True,
    ).update(checkout_dismissed_at=now, updated_at=now)

    return JsonResponse({
        'success': True,
        'recorded': True,
        'status': Payment.STATUS_PENDING,
        'retryable': True,
        'message': 'Checkout dismissal recorded; payment remains retryable.',
    })


@login_required(login_url='/login/')
def confirm_seat_booking(request, theater_id):
    """Verify a Razorpay payment and atomically fulfil its seat bookings."""
    if request.method != 'POST':
        return JsonResponse(
            {'error': 'Only POST requests are supported.'},
            status=405,
        )

    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return JsonResponse(
            {'error': 'Online payments are not configured.'},
            status=503,
        )

    order_id = request.POST.get('razorpay_order_id', '').strip()
    payment_id = request.POST.get('razorpay_payment_id', '').strip()
    signature = request.POST.get('razorpay_signature', '').strip()

    if not order_id or not payment_id or not signature:
        return JsonResponse(
            {'error': 'Required payment verification fields are missing.'},
            status=400,
        )

    payment = Payment.objects.filter(
        razorpay_order_id=order_id,
        user=request.user,
        theater_id=theater_id,
    ).first()

    if payment is None:
        return JsonResponse(
            {'error': 'Payment record not found.'},
            status=404,
        )

    if payment.status == Payment.STATUS_PAID:
        if payment.razorpay_payment_id == payment_id:
            return JsonResponse({
                'success': True,
                'message': 'This payment has already been processed.',
            })
        return JsonResponse(
            {'error': 'This order has already been processed.'},
            status=409,
        )

    if payment.status != Payment.STATUS_PENDING:
        return JsonResponse(
            {'error': 'This payment is not awaiting verification.'},
            status=409,
        )

    client = razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )

    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })

        provider_payment = client.payment.fetch(payment_id)

        if (
            provider_payment.get('id') != payment_id
            or provider_payment.get('order_id') != order_id
            or provider_payment.get('status') != 'captured'
            or provider_payment.get('amount') != int(payment.amount * 100)
            or provider_payment.get('currency') != payment.currency
        ):
            return JsonResponse(
                {'error': 'Payment details could not be verified.'},
                status=400,
            )

    except Exception:
        return JsonResponse(
            {'error': 'Payment verification failed. Please contact support if money was deducted.'},
            status=400,
        )

    now = timezone.now()
    seat_ids = payment.seat_ids

    if (
        not isinstance(seat_ids, list)
        or not seat_ids
        or any(type(seat_id) is not int for seat_id in seat_ids)
        or len(seat_ids) != len(set(seat_ids))
    ):
        return JsonResponse(
            {'error': 'The stored seat selection is invalid.'},
            status=400,
        )

    try:
        with transaction.atomic():
            current_payment = Payment.objects.get(pk=payment.pk)

            if current_payment.status == Payment.STATUS_PAID:
                if current_payment.razorpay_payment_id == payment_id:
                    return JsonResponse({
                        'success': True,
                        'message': 'This payment has already been processed.',
                    })
                return JsonResponse(
                    {'error': 'This order has already been processed.'},
                    status=409,
                )

            if current_payment.status != Payment.STATUS_PENDING:
                return JsonResponse(
                    {'error': 'This payment is no longer pending.'},
                    status=409,
                )

            # Persist the verified provider payment attempt atomically with booking.
            existing_attempt = PaymentAttempt.objects.filter(
                razorpay_payment_id=payment_id
            ).first()

            if (
                existing_attempt is not None
                and existing_attempt.payment_id != current_payment.pk
            ):
                return JsonResponse(
                    {'error': 'Payment identifier conflict.'},
                    status=409,
                )

            if existing_attempt is None:
                attempt = PaymentAttempt.objects.filter(
                    payment=current_payment,
                    razorpay_payment_id__isnull=True,
                ).order_by('-created_at').first()
                if attempt is None:
                    attempt = PaymentAttempt(payment=current_payment)
            else:
                attempt = existing_attempt

            attempt.razorpay_payment_id = payment_id
            attempt.amount = current_payment.amount
            attempt.currency = current_payment.currency
            attempt.status = PaymentAttempt.STATUS_CAPTURED
            attempt.failure_reason = ''
            attempt.save()

            reservations = list(
                SeatReservation.objects.filter(
                    user=current_payment.user,
                    seat_id__in=seat_ids,
                    seat__theater_id=current_payment.theater_id,
                    expires_at__gt=now,
                ).select_related('seat').order_by('seat_id')
            )

            seats = list(
                Seat.objects.filter(
                    id__in=seat_ids,
                    theater_id=current_payment.theater_id,
                ).order_by('id')
            )

            reservation_ids = {reservation.seat_id for reservation in reservations}
            actual_seat_ids = {seat.id for seat in seats}

            if (
                reservation_ids != set(seat_ids)
                or actual_seat_ids != set(seat_ids)
                or any(seat.is_booked for seat in seats)
            ):
                current_payment.status = Payment.STATUS_REFUND_REQUIRED
                current_payment.razorpay_payment_id = payment_id
                current_payment.paid_at = now
                current_payment.failure_reason = (
                    'Payment captured, but reservations expired or seats became unavailable. '
                    'Refund or manual reconciliation required.'
                )
                current_payment.save(update_fields=[
                    'status', 'razorpay_payment_id', 'paid_at',
                    'failure_reason', 'updated_at',
                ])
                return JsonResponse(
                    {
                        'success': False,
                        'refund_required': True,
                        'message': 'Payment received, but booking could not be completed. Please contact support.',
                    },
                    status=409,
                )

            if current_payment.razorpay_payment_id not in (None, '', payment_id):
                return JsonResponse(
                    {'error': 'Payment identifier conflict.'},
                    status=409,
                )

            for seat in seats:
                Booking.objects.create(
                    user=current_payment.user,
                    seat=seat,
                    movie=current_payment.theater.movie,
                    theater=current_payment.theater,
                )
                seat.is_booked = True
                seat.save(update_fields=['is_booked'])

            SeatReservation.objects.filter(
                user=current_payment.user,
                seat_id__in=seat_ids,
            ).delete()

            current_payment.status = Payment.STATUS_PAID
            current_payment.razorpay_payment_id = payment_id
            current_payment.paid_at = now
            current_payment.failure_reason = ''
            current_payment.save(update_fields=[
                'status', 'razorpay_payment_id', 'paid_at',
                'failure_reason', 'updated_at',
            ])

    except IntegrityError:
        # The transaction rolls back any partial booking. A captured payment
        # must be reconciled rather than silently reported as a failed charge.
        try:
            Payment.objects.filter(
                pk=payment.pk,
                status=Payment.STATUS_PENDING,
            ).update(
                status=Payment.STATUS_REFUND_REQUIRED,
                razorpay_payment_id=payment_id,
                paid_at=timezone.now(),
                failure_reason='Payment captured, but a booking integrity conflict requires reconciliation.',
            )
        except Exception:
            pass

        return JsonResponse(
            {
                'success': False,
                'refund_required': True,
                'message': 'Payment received but booking needs reconciliation. Please contact support.',
            },
            status=409,
        )

    except Payment.DoesNotExist:
        return JsonResponse(
            {'error': 'Payment record not found.'},
            status=404,
        )

    # Generate ticket synchronously after the transaction commits,
    # so the booking/payment response is not delayed by background work.
    # Celery is not configured in production, so we call the task function
    # directly rather than via .delay().
    try:
        tasks.process_ticket_after_payment(current_payment.id)
    except Exception:
        # Ticket generation failure must not invalidate a successful payment.
        logger.exception(
            'Ticket generation failed for payment %s.', current_payment.id
        )

    return JsonResponse({
        'success': True,
        'message': 'Payment verified and booking confirmed.',
        'ticket_count': len(seat_ids),
    })

def seat_availability(request, theater_id):
    if request.method != 'GET':
        return JsonResponse(
            {'error': 'Only GET requests are supported.'},
            status=405,
        )

    theater = get_object_or_404(Theater, id=theater_id)
    now = timezone.now()

    # Remove expired reservations so seats return to the available state.
    try:
        SeatReservation.objects.filter(
            seat__theater=theater,
            expires_at__lte=now,
        ).delete()
    except OperationalError:
        pass

    seats = Seat.objects.filter(theater=theater).order_by('id')
    active_reservations = {
        reservation.seat_id: reservation
        for reservation in SeatReservation.objects.filter(
            seat__theater=theater,
            expires_at__gt=now,
        )
    }

    seat_data = []
    for seat in seats:
        reservation = active_reservations.get(seat.id)

        if seat.is_booked:
            status = 'booked'
        elif reservation:
            status = 'reserved'
        else:
            status = 'available'

        seat_data.append({
            'id': seat.id,
            'is_booked': seat.is_booked,
            'status': status,
            'expires_at': (
                reservation.expires_at.isoformat()
                if reservation else None
            ),
            'is_reserved_by_current_user': (
                bool(
                    request.user.is_authenticated
                    and reservation
                    and reservation.user_id == request.user.id
                )
            ),
        })

    return JsonResponse({
        'theater_id': theater.id,
        'server_time': now.isoformat(),
        'seats': seat_data,
    })

def movie_detail(request, movie_id):
    movie = get_object_or_404(
        Movie.objects.prefetch_related('posters'),
        id=movie_id
    )

    # Track recently viewed movie ids in the session. Only ids are stored;
    # no sensitive user data is kept in the session.
    recently_viewed = request.session.get('recently_viewed', [])
    if not isinstance(recently_viewed, list):
        recently_viewed = []
    recently_viewed = [mid for mid in recently_viewed if mid != movie.id]
    recently_viewed.insert(0, movie.id)
    request.session['recently_viewed'] = recently_viewed[:20]
    request.session.modified = True

    review_error = None

    if request.method == 'POST':
        if not request.user.is_authenticated:
            return redirect('/login/')

        has_booking = Booking.objects.filter(
            user=request.user,
            movie=movie,
            attended=True
        ).exists()

        if not has_booking:
            review_error = "You must book and attend this movie before submitting a review."
        elif movie.reviews.filter(user=request.user).exists():
            review_error = "You have already reviewed this movie."
        else:
            rating_value = request.POST.get('rating', '').strip()
            comment = request.POST.get('comment', '').strip()

            if rating_value not in {'1', '2', '3', '4', '5'}:
                review_error = "Please select a valid rating from 1 to 5."
            elif not comment:
                review_error = "Please enter a review comment."
            elif len(comment) > 2000:
                review_error = "Your review must not exceed 2000 characters."
            else:
                try:
                    with transaction.atomic():
                        Review.objects.create(
                            movie=movie,
                            user=request.user,
                            rating=int(rating_value),
                            comment=comment
                        )
                    return redirect('movie_detail', movie_id=movie.id)
                except IntegrityError:
                    review_error = "You have already reviewed this movie."

    reviews = list(movie.reviews.select_related('user').all())
    if reviews:
        booked_user_ids = set(
            Booking.objects.filter(
                movie=movie,
                attended=True,
                user_id__in=[review.user_id for review in reviews]
            ).values_list('user_id', flat=True)
        )
    else:
        booked_user_ids = set()

    for review in reviews:
        review.booking_verified = review.user_id in booked_user_ids

    average_review_rating = movie.reviews.aggregate(
        average=Avg('rating')
    )['average']

    related_movies = Movie.objects.exclude(
        id=movie.id
    ).annotate(
        shared_genres=Count(
            'genres',
            filter=Q(genres__in=movie.genres.all()),
            distinct=True,
        ),
        shared_languages=Count(
            'languages',
            filter=Q(languages__in=movie.languages.all()),
            distinct=True,
        ),
    ).annotate(
        relevance=F('shared_genres') + F('shared_languages')
    ).filter(
        shared_genres__gt=0,
        shared_languages__gt=0,
    ).order_by(
        '-relevance', '-rating', 'name'
    )

    recommended_movies = list(related_movies[:3])

    additional_posters = get_additional_posters(movie)
    trailer_embed_url = get_youtube_embed_url(movie.trailer_url)
    trailer_watch_url = get_youtube_watch_url(movie.trailer_url)

    can_review = False
    already_reviewed = False

    if request.user.is_authenticated:
        can_review = Booking.objects.filter(
            user=request.user,
            movie=movie,
            attended=True
        ).exists()

        already_reviewed = movie.reviews.filter(
            user=request.user
        ).exists()

    return render(request, 'movies/movie_detail.html', {
        'movie': movie,
        'reviews': reviews,
        'can_review': can_review,
        'already_reviewed': already_reviewed,
        'review_error': review_error,
        'average_review_rating': average_review_rating,
        'recommended_movies': recommended_movies,
        'additional_posters': additional_posters,
        'trailer_embed_url': trailer_embed_url,
        'trailer_watch_url': trailer_watch_url,
    })


@login_required(login_url='/login/')
def edit_review(request, review_id):
    review = get_object_or_404(
        Review.objects.select_related('movie'),
        id=review_id,
        user=request.user,
    )
    errors = []
    selected_rating = str(review.rating)
    comment_value = review.comment

    if request.method == 'POST':
        selected_rating = request.POST.get('rating', '').strip()
        comment_value = request.POST.get('comment', '').strip()

        if selected_rating not in {'1', '2', '3', '4', '5'}:
            errors.append("Please select a valid rating from 1 to 5.")
        if not comment_value:
            errors.append("Please enter a review comment.")
        elif len(comment_value) > 2000:
            errors.append("Your review must not exceed 2000 characters.")

        if not errors:
            review.rating = int(selected_rating)
            review.comment = comment_value
            review.save(update_fields=['rating', 'comment'])
            messages.success(request, "Your review has been updated.")
            return redirect('movie_detail', movie_id=review.movie_id)

    return render(request, 'movies/review_edit.html', {
        'review': review,
        'errors': errors,
        'selected_rating': selected_rating,
        'comment_value': comment_value,
    })


@login_required(login_url='/login/')
def report_review(request, review_id):
    review = get_object_or_404(
        Review.objects.select_related('movie', 'user'),
        id=review_id,
    )
    reason_choices = ReviewReport.REASON_CHOICES
    valid_reasons = {value for value, _ in reason_choices}
    errors = []
    selected_reason = ''
    explanation = ''

    if request.method == 'POST':
        selected_reason = request.POST.get('reason', '').strip()
        explanation = request.POST.get('explanation', '').strip()

        if selected_reason not in valid_reasons:
            errors.append(
                "Please select a valid reason for reporting this review."
            )
        if len(explanation) > 1000:
            errors.append(
                "Your explanation must not exceed 1000 characters."
            )

        if not errors:
            if ReviewReport.objects.filter(
                review=review,
                user=request.user,
            ).exists():
                messages.info(
                    request,
                    "You have already reported this review."
                )
                return redirect('movie_detail', movie_id=review.movie_id)

            try:
                ReviewReport.objects.create(
                    review=review,
                    user=request.user,
                    reason=selected_reason,
                    explanation=explanation,
                )
            except IntegrityError:
                messages.info(
                    request,
                    "You have already reported this review."
                )
            else:
                messages.success(
                    request,
                    "Thank you. Your report has been submitted for moderation."
                )
            return redirect('movie_detail', movie_id=review.movie_id)

    return render(request, 'movies/review_report.html', {
        'review': review,
        'reason_choices': reason_choices,
        'selected_reason': selected_reason,
        'explanation': explanation,
        'errors': errors,
    })


@csrf_exempt
def razorpay_webhook(request):
    """
    Process authenticated Razorpay payment events.
    The webhook signature is verified against the exact raw request body.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    webhook_secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    if not webhook_secret:
        return JsonResponse({"error": "Webhook is not configured"}, status=503)

    received_signature = request.headers.get("X-Razorpay-Signature", "")
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        request.body,
        hashlib.sha256,
    ).hexdigest()

    if not received_signature or not hmac.compare_digest(
        expected_signature, received_signature
    ):
        return JsonResponse({"error": "Invalid webhook signature"}, status=400)

    try:
        event_data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    event_type = event_data.get("event")
    entity = (
        event_data.get("payload", {})
        .get("payment", {})
        .get("entity", {})
    )

    if event_type not in ("payment.captured", "payment.failed"):
        return JsonResponse({"status": "ignored"}, status=200)

    order_id = entity.get("order_id")
    payment_id = entity.get("id")
    provider_amount = entity.get("amount")
    provider_currency = entity.get("currency")

    if not order_id or not payment_id or provider_amount is None or not provider_currency:
        return JsonResponse({"error": "Incomplete payment event"}, status=400)

    try:
        payment = Payment.objects.get(razorpay_order_id=order_id)
    except Payment.DoesNotExist:
        return JsonResponse({"error": "Unknown payment order"}, status=404)

    if (
        int(provider_amount) != int(payment.amount * 100)
        or str(provider_currency).upper() != str(payment.currency).upper()
    ):
        return JsonResponse({"error": "Payment amount or currency mismatch"}, status=400)

    if event_type == "payment.captured" and entity.get("status") != "captured":
        return JsonResponse({"error": "Payment is not captured"}, status=400)

    # Record each provider payment ID once. Repeated webhook deliveries update
    # the same attempt instead of creating duplicate transaction records.
    def record_attempt(attempt_status, failure_reason=""):
        with transaction.atomic():
            existing = PaymentAttempt.objects.filter(
                razorpay_payment_id=payment_id
            ).first()

            if existing and existing.payment_id != payment.pk:
                raise ValueError("Payment ID is already linked to another order")

            if existing:
                attempt = PaymentAttempt.objects.select_for_update().get(pk=existing.pk)
            else:
                attempt = (
                    PaymentAttempt.objects.select_for_update()
                    .filter(payment=payment, razorpay_payment_id__isnull=True)
                    .order_by("-created_at")
                    .first()
                )

                if attempt is None:
                    attempt = PaymentAttempt(payment=payment)

                attempt.razorpay_payment_id = payment_id

            # Never downgrade a captured attempt if an older failure event arrives late.
            if attempt.status != "captured" or attempt_status == "captured":
                attempt.status = attempt_status
                attempt.failure_reason = failure_reason[:500]

            attempt.amount = Decimal(str(provider_amount)) / Decimal("100")
            attempt.currency = str(provider_currency).upper()
            attempt.save()

    if event_type == "payment.failed":
        error_details = entity.get("error_description") or entity.get("error_reason") or "Payment failed"
        try:
            record_attempt("failed", str(error_details))
        except ValueError:
            return JsonResponse({"error": "Payment ID conflict"}, status=409)

        # Keep the parent order pending while its reservation is still valid,
        # so the customer can retry. Expired reservations remain subject to
        # the application's existing reservation-expiry checks.
        return JsonResponse({"status": "failure_recorded"}, status=200)

    # Reuse the existing confirmation path, which verifies the provider payment
    # server-side and performs the actual booking. The checkout signature below
    # is generated server-side only after the webhook signature has been verified.
    checkout_secret = getattr(settings, "RAZORPAY_KEY_SECRET", "")
    if not checkout_secret:
        return JsonResponse({"error": "Payment verification is not configured"}, status=503)

    checkout_signature = hmac.new(
        checkout_secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    try:
        from django.test import RequestFactory

        internal_request = RequestFactory().post(
            "/movies/theater/{}/seats/confirm/".format(payment.theater_id),
            data={
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": checkout_signature,
            },
        )
        internal_request.user = payment.user

        confirmation_response = confirm_seat_booking(
            internal_request, payment.theater_id
        )

        # Razorpay has confirmed capture even if booking fulfillment reports a
        # conflict (for example, a seat was taken). Record the captured attempt
        # so the transaction is visible for reconciliation/refund handling.
        record_attempt("captured", "")

        if confirmation_response.status_code >= 500:
            return JsonResponse({"error": "Booking processing failed; retry webhook"}, status=500)

        return JsonResponse(
            {
                "status": "processed",
                "booking_processing_status": confirmation_response.status_code,
            },
            status=200,
        )

    except ValueError:
        return JsonResponse({"error": "Payment ID conflict"}, status=409)
    except Exception:
        # Do not expose internal errors or secrets to the payment provider.
        return JsonResponse({"error": "Webhook processing failed; retry delivery"}, status=500)


@login_required(login_url='/login/')
def download_ticket(request, booking_id):
    """
    Download the PDF ticket for an authenticated user's own confirmed booking.

    Only the booking owner may download. Failed/unverified bookings have no
    downloadable ticket. If generation is pending, returns a 202 status.
    """
    booking = get_object_or_404(Booking, id=booking_id)

    # Authorization: only the booking owner may download
    if booking.user_id != request.user.id:
        return JsonResponse(
            {'error': 'You do not have permission to download this ticket.'},
            status=403,
        )

    # Only paid bookings can have downloadable tickets
    payment = get_paid_payment_for_booking(booking)
    if not payment:
        return JsonResponse(
            {'error': 'No valid ticket available for this booking.'},
            status=404,
        )

    ticket = Ticket.objects.filter(booking=booking).first()
    if not ticket:
        return JsonResponse(
            {'error': 'Ticket has not been generated yet.'},
            status=404,
        )

    if ticket.status == Ticket.STATUS_PENDING:
        return JsonResponse(
            {
                'error': 'Ticket generation is in progress. Please try again shortly.',
                'status': 'pending',
            },
            status=202,
        )

    has_valid_blob = bool(ticket.pdf_blob) and len(ticket.pdf_blob) > 0
    has_valid_file = bool(ticket.pdf_file)

    if ticket.status == Ticket.STATUS_FAILED or (not has_valid_blob and not has_valid_file):
        return JsonResponse(
            {'error': 'Ticket generation failed. Please contact support.'},
            status=404,
        )

    # Serve the PDF file from pdf_blob (serverless-safe) or pdf_file (local dev)
    try:
        if has_valid_blob:
            response = HttpResponse(
                ticket.pdf_blob,
                content_type='application/pdf',
            )
            response['Content-Disposition'] = (
                f'attachment; filename="{escape_uri_path(f"ticket_booking_{booking.id}.pdf")}"'
            )
            return response
        if has_valid_file:
            ticket.pdf_file.open('rb')
            response = FileResponse(
                ticket.pdf_file,
                content_type='application/pdf',
                as_attachment=True,
                filename=escape_uri_path(f'ticket_booking_{booking.id}.pdf'),
            )
            ticket.pdf_file.close()
            return response
        raise FileNotFoundError('No PDF file or blob available')
    except (FileNotFoundError, OSError, ValueError):
        return JsonResponse(
            {'error': 'Ticket file not found.'},
            status=404,
        )


def verify_ticket(request, token):
    """
    Public ticket verification endpoint for QR code validation.

    Validates the ticket verification token and reports whether the ticket
    is valid without exposing sensitive user or payment information.
    """
    try:
        ticket = Ticket.objects.select_related('booking__movie', 'booking__theater', 'booking__seat').get(
            verification_token=token
        )
    except Ticket.DoesNotExist:
        return JsonResponse(
            {'valid': False, 'error': 'Invalid ticket reference.'},
            status=404,
        )

    booking = ticket.booking
    payment = get_paid_payment_for_booking(booking)

    has_valid_data = (bool(ticket.pdf_blob) and len(ticket.pdf_blob) > 0) or bool(ticket.pdf_file)
    is_valid = (
        ticket.status == Ticket.STATUS_GENERATED
        and has_valid_data
        and payment is not None
    )

    return JsonResponse({
        'valid': is_valid,
        'movie': booking.movie.name,
        'theater': booking.theater.name,
        'show_time': booking.theater.time.isoformat(),
        'seat': booking.seat.seat_number,
        'booking_id': booking.id,
        'ticket_status': ticket.status,
    })


def serve_media(request, file_path):
    """
    Serve media files (movie posters) reliably on both local dev and Vercel serverless.

    This view resolves files across multiple search directories (settings.MEDIA_ROOT,
    bundled public/media, static media, and ephemeral /tmp/media).
    If a specific file is missing, it provides a graceful fallback to a default
    valid movie poster with HTTP 200 and Cache-Control headers so that images
    never break on the frontend or during page refreshes.
    """
    import tempfile

    clean_path = (file_path or '').strip('/')
    extensions = ('', '.jpg', '.jpeg', '.png', '.webp', '.gif')

    search_dirs = [
        settings.MEDIA_ROOT,
        os.path.join(settings.BASE_DIR, 'bookmyseat', 'static', 'media'),
        os.path.join(settings.BASE_DIR, 'public', 'media'),
        os.path.join(settings.BASE_DIR, 'staticfiles', 'media'),
        os.path.join(tempfile.gettempdir(), 'media'),
    ]

    candidates = []
    for ext in extensions:
        candidates.append(f"{clean_path}{ext}")
    if '.' in clean_path:
        base_no_ext = clean_path.rsplit('.', 1)[0]
        for ext in extensions:
            cand = f"{base_no_ext}{ext}"
            if cand not in candidates:
                candidates.append(cand)

    active_path = None
    for s_dir in search_dirs:
        if not s_dir or not os.path.exists(s_dir):
            continue
        for rel in candidates:
            cand_path = os.path.join(s_dir, rel)
            if os.path.isfile(cand_path) and os.path.getsize(cand_path) > 100:
                active_path = cand_path
                break
        if active_path:
            break

    # Case-insensitive search on Linux/Vercel
    if not active_path and '/' in clean_path:
        sub_folder, target_name = clean_path.rsplit('/', 1)
        target_name_lower = target_name.lower()
        target_base_lower = target_name_lower.rsplit('.', 1)[0] if '.' in target_name_lower else target_name_lower

        for s_dir in search_dirs:
            if not s_dir or not os.path.exists(s_dir):
                continue
            folder_path = os.path.join(s_dir, sub_folder)
            if os.path.isdir(folder_path):
                try:
                    for actual_file in os.listdir(folder_path):
                        actual_file_lower = actual_file.lower()
                        actual_base_lower = actual_file_lower.rsplit('.', 1)[0] if '.' in actual_file_lower else actual_file_lower
                        if actual_file_lower == target_name_lower or actual_base_lower == target_base_lower:
                            cand_path = os.path.join(folder_path, actual_file)
                            if os.path.isfile(cand_path) and os.path.getsize(cand_path) > 100:
                                active_path = cand_path
                                break
                except Exception:
                    pass
            if active_path:
                break

    # Fallback to default poster if requested file could not be found anywhere
    if not active_path:
        for s_dir in search_dirs:
            if not s_dir or not os.path.exists(s_dir):
                continue
            default_poster = os.path.join(s_dir, 'movies', '635217f73e372771013edb4c-the-avengers-poster-marvel-movie-canvas1.jpg')
            if os.path.isfile(default_poster) and os.path.getsize(default_poster) > 100:
                active_path = default_poster
                break

    if active_path and os.path.isfile(active_path):
        with open(active_path, 'rb') as f:
            content = f.read()

        ext = os.path.splitext(active_path)[1].lower()
        content_type_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.webp': 'image/webp',
            '.gif': 'image/gif',
        }
        content_type = content_type_map.get(ext, 'image/jpeg')

        response = HttpResponse(content, content_type=content_type)
        response['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
        return response

    raise Http404("Media file not found")


@login_required(login_url='/login/')
def regenerate_ticket(request, booking_id):
    """
    Generate or regenerate a ticket PDF for the authenticated user's own paid booking.

    Idempotent: safe to call repeatedly. Only the booking owner may trigger
    regeneration. Returns HTTP 200 on success, HTTP 404 if the booking or
    its paid payment does not exist, HTTP 403 for unauthorized access.
    Supports both browser navigation (GET with redirect) and API calls (POST/JSON).
    """
    booking = get_object_or_404(Booking, id=booking_id)

    # Authorization: only the booking owner may regenerate
    if booking.user_id != request.user.id:
        if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
            messages.error(request, 'You do not have permission to generate this ticket.')
            return redirect('profile')
        return JsonResponse(
            {'error': 'You do not have permission to regenerate this ticket.'},
            status=403,
        )

    # Only paid bookings can have tickets regenerated
    payment = get_paid_payment_for_booking(booking)
    if not payment:
        if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
            messages.error(request, 'No verified payment found for this booking.')
            return redirect('profile')
        return JsonResponse(
            {'error': 'No valid ticket available for this booking.'},
            status=404,
        )

    # Get or create the ticket record
    ticket, _ = Ticket.objects.get_or_create(
        booking=booking,
        defaults={
            'verification_token': Ticket.generate_verification_token(),
            'status': Ticket.STATUS_PENDING,
        },
    )

    # Reset status and regenerate
    ticket.status = Ticket.STATUS_PENDING
    ticket.pdf_blob = None
    ticket.pdf_file = None
    ticket.email_sent = False
    ticket.email_attempts = 0
    ticket.last_email_error = ''
    ticket.save()

    # Generate PDF synchronously
    try:
        success = generate_ticket_pdf(ticket)
        ticket.refresh_from_db()

        if success and (ticket.pdf_blob or ticket.pdf_file):
            if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
                messages.success(request, 'Ticket generated successfully!')
                return redirect('profile')
            return JsonResponse({
                'success': True,
                'message': 'Ticket regenerated successfully.',
                'ticket_id': ticket.id,
                'download_url': reverse('download_ticket', args=[booking.id]),
            })
        else:
            if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
                messages.error(request, 'Ticket generation failed. Please contact support.')
                return redirect('profile')
            return JsonResponse({
                'success': False,
                'error': 'Ticket generation failed. Please contact support.',
            }, status=500)
    except Exception as exc:
        logger.exception(
            'Ticket regeneration failed for booking %s: %s', booking.id, exc
        )
        if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
            messages.error(request, 'Ticket generation failed. Please contact support.')
            return redirect('profile')
        return JsonResponse({
            'success': False,
            'error': 'Ticket generation failed. Please contact support.',
        }, status=500)
