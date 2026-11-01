import os
import uuid

from django.core.validators import MinValueValidator
from django.db import models
from django.contrib.auth.models import User


AGE_CERTIFICATION_CHOICES = [
    ('U', 'U'),
    ('UA 7+', 'UA 7+'),
    ('UA 13+', 'UA 13+'),
    ('UA 16+', 'UA 16+'),
    ('A', 'A'),
]


class Movie(models.Model):
    name= models.CharField(max_length=255)
    image= models.ImageField(upload_to="movies/")
    rating = models.DecimalField(max_digits=3,decimal_places=1)
    cast= models.TextField()
    description= models.TextField(blank=True,null=True) # optional
    trailer_url = models.URLField(blank=True, null=True, help_text="Optional YouTube or other trailer URL.")
    genres = models.ManyToManyField('Genre', blank=True, related_name='movies')
    languages = models.ManyToManyField('Language', blank=True, related_name='movies')
    cast_members = models.ManyToManyField('CastMember', blank=True, related_name='movies')
    age_certification = models.CharField(
        max_length=10,
        choices=AGE_CERTIFICATION_CHOICES,
        blank=True,
    )
    duration_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    release_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name

class Theater(models.Model):
    name = models.CharField(max_length=255)
    city = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        db_index=True,
        help_text="City where the theater is located. Optional so existing "
                  "theaters without a known city are not blocked.",
    )
    movie = models.ForeignKey(Movie,on_delete=models.CASCADE,related_name='theaters')
    time= models.DateTimeField(db_index=True)

    def __str__(self):
        return f'{self.name} - {self.movie.name} at {self.time}'

class Seat(models.Model):
    theater = models.ForeignKey(Theater,on_delete=models.CASCADE,related_name='seats')
    seat_number = models.CharField(max_length=10)
    is_booked=models.BooleanField(default=False)

    def __str__(self):
        return f'{self.seat_number} in {self.theater.name}'

class Booking(models.Model):
    user=models.ForeignKey(User,on_delete=models.CASCADE)
    seat=models.OneToOneField(Seat,on_delete=models.CASCADE)
    movie=models.ForeignKey(Movie,on_delete=models.CASCADE)
    theater=models.ForeignKey(Theater,on_delete=models.CASCADE)
    booked_at=models.DateTimeField(auto_now_add=True, db_index=True)
    attended = models.BooleanField(default=False, help_text='Confirm that the customer attended the show.')
    def __str__(self):
        return f'Booking by{self.user.username} for {self.seat.seat_number} at {self.theater.name}'

class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class CastMember(models.Model):
    name = models.CharField(max_length=150, unique=True)
    biography = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class MoviePoster(models.Model):
    movie = models.ForeignKey(
        Movie,
        on_delete=models.CASCADE,
        related_name='posters'
    )
    image = models.ImageField(upload_to='movies/posters/')
    alt_text = models.CharField(max_length=255, blank=True)
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_primary', '-uploaded_at']

    def __str__(self):
        return f'Poster for {self.movie.name}'

class Review(models.Model):
    movie = models.ForeignKey(
        Movie,
        on_delete=models.CASCADE,
        related_name='reviews'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='movie_reviews'
    )
    rating = models.PositiveSmallIntegerField(
        choices=[(i, str(i)) for i in range(1, 6)]
    )
    comment = models.TextField(max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['movie', 'user'],
                name='unique_review_per_user_movie'
            )
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user.username} - {self.movie.name} ({self.rating}/5)'


class ReviewReport(models.Model):
    SPAM = 'spam'
    ABUSE = 'abuse'
    HATE = 'hate'
    INAPPROPRIATE = 'inappropriate'
    OTHER = 'other'

    REASON_CHOICES = [
        (SPAM, 'Spam'),
        (ABUSE, 'Abusive or harassing content'),
        (HATE, 'Hate or discriminatory content'),
        (INAPPROPRIATE, 'Inappropriate content'),
        (OTHER, 'Other'),
    ]

    PENDING = 'pending'
    REVIEWED = 'reviewed'
    DISMISSED = 'dismissed'

    STATUS_CHOICES = [
        (PENDING, 'Pending'),
        (REVIEWED, 'Reviewed'),
        (DISMISSED, 'Dismissed'),
    ]

    review = models.ForeignKey(
        Review,
        on_delete=models.CASCADE,
        related_name='reports'
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='review_reports'
    )
    reason = models.CharField(max_length=20, choices=REASON_CHOICES)
    explanation = models.TextField(blank=True, max_length=1000)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=PENDING
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['review', 'user'],
                name='unique_report_per_user_review'
            )
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f'Report by {self.user.username} on review {self.review_id}'






class SeatReservation(models.Model):
    seat = models.OneToOneField(
        Seat,
        on_delete=models.CASCADE,
        related_name='reservations',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='seat_reservations',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['expires_at']
        indexes = [
            models.Index(fields=['seat', 'expires_at']),
            models.Index(fields=['user', 'expires_at']),
        ]

    def __str__(self):
        return (
            f'{self.user.username} reserves '
            f'{self.seat.seat_number} until {self.expires_at}'
        )

class Payment(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_PAID = 'paid'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REFUND_REQUIRED = 'refund_required'
    STATUS_REFUNDED = 'refunded'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PAID, 'Paid'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_REFUND_REQUIRED, 'Refund required'),
        (STATUS_REFUNDED, 'Refunded'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='movie_payments',
    )
    theater = models.ForeignKey(
        Theater,
        on_delete=models.PROTECT,
        related_name='payments',
    )
    seat_ids = models.JSONField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    razorpay_order_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )
    razorpay_payment_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )
    failure_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    checkout_dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Payment {self.pk} - {self.status} - {self.amount} {self.currency}'

class PaymentAttempt(models.Model):
    """Record each individual payment attempt made against a payment order."""

    STATUS_PENDING = 'pending'
    STATUS_AUTHORIZED = 'authorized'
    STATUS_CAPTURED = 'captured'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_AUTHORIZED, 'Authorized'),
        (STATUS_CAPTURED, 'Captured'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    payment = models.ForeignKey(
        Payment,
        on_delete=models.CASCADE,
        related_name='attempts',
    )
    razorpay_payment_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    failure_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (
            f'Payment attempt {self.pk} - '
            f'{self.status} - {self.amount} {self.currency}'
        )


def ticket_upload_path(instance, filename):
    """Store ticket PDFs under media/tickets/<booking_id>/."""
    return os.path.join(
        'tickets',
        str(instance.booking_id),
        filename,
    )


class Ticket(models.Model):
    """A generated PDF ticket for a confirmed, paid booking."""

    STATUS_PENDING = 'pending'
    STATUS_GENERATED = 'generated'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_GENERATED, 'Generated'),
        (STATUS_FAILED, 'Failed'),
    ]

    booking = models.OneToOneField(
        'Booking',
        on_delete=models.CASCADE,
        related_name='ticket',
        help_text='The confirmed booking this ticket belongs to.',
    )
    verification_token = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text='Secure, unpredictable token embedded in the ticket QR code.',
    )
    pdf_file = models.FileField(
        upload_to=ticket_upload_path,
        blank=True,
        null=True,
        help_text='Generated PDF ticket file.',
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    email_sent = models.BooleanField(default=False)
    email_attempts = models.PositiveSmallIntegerField(default=0)
    last_email_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Ticket for Booking {self.booking_id} ({self.status})'

    @property
    def is_downloadable(self):
        """Ticket can be downloaded only when generated and its payment is paid."""
        if self.status != self.STATUS_GENERATED or not self.pdf_file:
            return False

        from .payment_utils import get_paid_payment_for_booking
        return get_paid_payment_for_booking(self.booking) is not None

    @classmethod
    def generate_verification_token(cls):
        """Generate a cryptographically random verification token."""
        return uuid.uuid4().hex

    def verification_url(self):
        """Return the absolute URL for verifying this ticket's QR code."""
        from django.conf import settings
        from django.urls import reverse

        relative_url = reverse(
            'verify_ticket',
            args=[self.verification_token],
        )
        base_url = getattr(settings, "TICKET_VERIFICATION_BASE_URL", "").strip().rstrip("/")

        if base_url:
            return f"{base_url}{relative_url}"

        return relative_url


