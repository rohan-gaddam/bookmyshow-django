from django.urls import path
from . import views
urlpatterns=[
    path('payments/webhook/', views.razorpay_webhook, name='razorpay_webhook'),
    
    path('<int:movie_id>/', views.movie_detail, name='movie_detail'),path('',views.movie_list,name='movie_list'),
    path('<int:movie_id>/theaters',views.theater_list,name='theater_list'),
    path('theater/<int:theater_id>/seats/book/',views.book_seats,name='book_seats'),
    path('theater/<int:theater_id>/seats/payment-order/', views.create_payment_order, name='create_payment_order'),
    path('theater/<int:theater_id>/seats/checkout-dismissed/', views.checkout_dismissed, name='checkout_dismissed'),
    path('theater/<int:theater_id>/seats/confirm/', views.confirm_seat_booking, name='confirm_seat_booking'),
    path('theater/<int:theater_id>/seats/availability/', views.seat_availability, name='seat_availability'),
    path('reviews/<int:review_id>/edit/',views.edit_review,name='edit_review'),
    path('reviews/<int:review_id>/report/',views.report_review,name='report_review'),
    path('bookings/<int:booking_id>/ticket/download/', views.download_ticket, name='download_ticket'),
    path('tickets/verify/<str:token>/', views.verify_ticket, name='verify_ticket'),
]


