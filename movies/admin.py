from django.contrib import admin
from .models import (
    Movie,
    Theater,
    Seat,
    Booking,
    Review,
    Genre,
    Language,
    CastMember,
    MoviePoster,
    ReviewReport,
)

@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'rating',
        'age_certification',
        'duration_minutes',
        'release_date',
        'cast',
    ]
    list_filter = ['genres', 'languages', 'age_certification']
    search_fields = ['name', 'cast', 'description']
    fields = [
        'name',
        'image',
        'rating',
        'cast',
        'description',
        'trailer_url',
        'genres',
        'languages',
        'cast_members',
        'age_certification',
        'duration_minutes',
        'release_date',
    ]
    filter_horizontal = ['genres', 'languages', 'cast_members']

@admin.register(Theater)
class TheaterAdmin(admin.ModelAdmin):
    list_display = ['name', 'movie', 'time']

@admin.register(Seat)
class SeatAdmin(admin.ModelAdmin):
    list_display = ['theater', 'seat_number', 'is_booked']

@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['user', 'seat', 'movie', 'theater', 'booked_at', 'attended']


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['movie', 'user', 'rating', 'created_at']
    list_filter = ['rating', 'created_at']
    search_fields = ['movie__name', 'user__username', 'comment']


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(Language)
class LanguageAdmin(admin.ModelAdmin):
    list_display = ['name']
    search_fields = ['name']


@admin.register(CastMember)
class CastMemberAdmin(admin.ModelAdmin):
    list_display = ['name', 'biography']
    search_fields = ['name', 'biography']


@admin.register(ReviewReport)
class ReviewReportAdmin(admin.ModelAdmin):
    list_display = ['review', 'user', 'reason', 'status', 'created_at']
    list_filter = ['status', 'reason', 'created_at']
    search_fields = ['review__movie__name', 'user__username', 'explanation']
    list_select_related = ['review', 'user']

@admin.register(MoviePoster)
class MoviePosterAdmin(admin.ModelAdmin):
    list_display = ['movie', 'alt_text', 'is_primary', 'uploaded_at']
    list_filter = ['is_primary']
    search_fields = ['movie__name', 'alt_text']
    list_select_related = ['movie']




