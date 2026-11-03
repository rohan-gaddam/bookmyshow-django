# 🎬 BookMyShow Clone — Full-Stack Cinema Booking Web Application

[![Django](https://img.shields.io/badge/Django-3.2.19-092E20?style=for-the-badge&logo=django&logoColor=white)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Neon%20Serverless-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://neon.tech/)
[![Vercel](https://img.shields.io/badge/Deployed%20on-Vercel-black?style=for-the-badge&logo=vercel&logoColor=white)](https://bookmyshow-hazel.vercel.app/)
[![Razorpay](https://img.shields.io/badge/Payments-Razorpay-blue?style=for-the-badge&logo=razorpay&logoColor=white)](https://razorpay.com/)
[![Tests](https://img.shields.io/badge/Tests-133%20Passing-brightgreen?style=for-the-badge)](https://github.com/rohan-gaddam/bookmyshow-django)

A production-ready cinema booking and theater management web application inspired by BookMyShow. Built with **Django**, **PostgreSQL (Neon Serverless)**, and deployed on **Vercel**, featuring real-time seat reservation with concurrency locks, Razorpay payment processing, automated PDF ticket generation with QR code verification, and a comprehensive business analytics dashboard.

---

## 🌐 Live Production Links

* **🚀 Public Web Application:** [https://bookmyshow-hazel.vercel.app/](https://bookmyshow-hazel.vercel.app/)
* **📊 Business Analytics Dashboard:** [https://bookmyshow-hazel.vercel.app/admin-dashboard/](https://bookmyshow-hazel.vercel.app/admin-dashboard/)
* **🔐 Django Administration Portal:** [https://bookmyshow-hazel.vercel.app/admin/](https://bookmyshow-hazel.vercel.app/admin/)
* **📁 GitHub Repository:** [https://github.com/rohan-gaddam/bookmyshow-django](https://github.com/rohan-gaddam/bookmyshow-django)

> **Note:** The Admin Dashboard (`/admin-dashboard/`) and Django Admin (`/admin/`) require staff/superuser authentication. Use the credentials provided in the project submission report.

---

## 📌 Features & Module Implementations

### 1. Movie Management & Verified Reviews (Task 1)
- **Rich Movie Metadata:** Showcases movie posters, genre tags, language tags, duration, and age certifications (U, UA 13+, UA 16+, A).
- **YouTube Trailer Integration:** Embedded video player on movie detail pages.
- **Verified Viewer Reviews:** A review access control system that strictly permits reviews only from users with a confirmed booking for that movie.
- **Smart Recommendations:** Contextual recommendations displaying similar movies based on shared genres and languages.

### 2. Smart Seat Reservation & Concurrency Control (Task 2)
- **Interactive Seat Map:** Visual seat selection grid displaying real-time availability states across different seat classes.
- **2-Minute Temporary Lock:** Selected seats are held for exactly 120 seconds with a live frontend countdown timer.
- **Concurrency Protection:** Database-level atomic transactions (`transaction.atomic()`) with row-level locking (`select_for_update()`) prevent double-booking under concurrent traffic.

### 3. Payment Processing with Razorpay (Task 3)
- **Razorpay Gateway Integration:** Seamless checkout modal for debit/credit cards, UPI, and net banking in test mode.
- **Cryptographic Verification:** Server-side HMAC-SHA256 signature verification validates payment callbacks before confirming tickets.
- **Transaction History:** Comprehensive booking and payment history accessible from the user profile (`/users/profile/`).

### 4. Admin Business Analytics Dashboard (Task 4)
- **Real-Time Revenue Cards:** Instant calculation of daily, weekly, monthly, and yearly revenue metrics.
- **Theater Capacity Analytics:** Occupancy percentages per theater and screen showtime.
- **Audience Traffic Insights:** Peak booking hours distribution and top-performing movies.
- **Streaming CSV Export:** Memory-efficient, generator-based streaming export for Revenue, Bookings, Movies, Theaters, Payments, and Users.

### 5. Movie Discovery & Dynamic Multi-Filtering (Task 5)
- **Multi-Attribute Filters:** Filter dynamically by Genre, Language, City, and Minimum Rating simultaneously.
- **Dynamic Counters:** Category pills automatically display the live count of matching movies.
- **Search & Pagination:** Instant keyword search by title and cast, combined with smooth pagination (12 titles per page) and multi-field sorting.

### 6. Automated PDF Tickets & QR Code Verification (Task 6)
- **Automated PDF Generation:** High-resolution vector PDF tickets created via ReportLab upon confirmed payment.
- **Barcode & QR Code Security:** Tickets feature a unique booking barcode and a cryptographically signed QR code.
- **Public Verification Endpoint:** Cinema gate staff can scan the QR code to verify ticket authenticity at `/verify-ticket/<token>/`.

---

## ⚡ Performance Improvements & Database Optimizations

1. **Engine-Level Aggregations (Zero Memory Bloat):**
   - Dashboard metrics (revenue, occupancy rates, hourly traffic) execute directly inside PostgreSQL using Django ORM aggregations (`Sum()`, `Count()`, `Avg()`, `TruncDate()`, `TruncHour()`).
   - Query computations execute in single database round-trips with \(O(1)\) application server RAM, avoiding heavy object instantiation.

2. **High-Volume Streaming CSV Export:**
   - Uses Python's streaming generator pattern alongside Django's `StreamingHttpResponse` and `iterator(chunk_size=2000)`.
   - Safely streams 100,000+ booking records without serverless execution timeouts or memory exhaustion.

3. **Strategic Indexing & Concurrency Constraints:**
   - Composite indexes on `Booking(user, theater, booked_at)`, `Payment(razorpay_order_id, status)`, and `SeatReservation(seat, expires_at)`.
   - Unique constraints on `SeatReservation(seat_id)` prevent duplicate simultaneous holds.
   - Unique constraints on `Ticket(booking_id)` and `Ticket(verification_token)` guarantee one unique ticket per confirmed payment.

---

## 🧪 Testing & Quality Assurance

The codebase includes an extensive automated test suite covering unit tests, integration flows, concurrency locks, and security access controls:

```bash
python manage.py test movies
```

**Results:**
```text
Found 133 test(s).
Creating test database for alias 'default'...
....................................................................................................................................
----------------------------------------------------------------------
Ran 133 tests in 28.452s

OK
```
**Status: 133 passed, 0 failures, 0 errors.**

---

## 🛠️ Tech Stack

| Component | Technology |
| :--- | :--- |
| **Backend** | Python 3.12, Django 3.2.19 |
| **Database** | PostgreSQL (Neon Serverless), SQLite (Local Dev) |
| **Frontend** | Bootstrap 5, HTML5, CSS3, JavaScript |
| **Payment Gateway** | Razorpay SDK |
| **PDF & QR Engine** | ReportLab, `qrcode` library |
| **Deployment** | Vercel Serverless Functions |

---

## 🚀 Local Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/rohan-gaddam/bookmyshow-django.git
   cd bookmyshow-django
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Apply migrations:**
   ```bash
   python manage.py migrate
   ```

5. **Run the development server:**
   ```bash
   python manage.py runserver
   ```
   Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) in your browser.

---

## 👨‍💻 Author

* **Rohan** — ElevanceSkills Full-Stack Web Development Internship
* **Repository:** [https://github.com/rohan-gaddam/bookmyshow-django](https://github.com/rohan-gaddam/bookmyshow-django)
